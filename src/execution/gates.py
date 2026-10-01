"""Pure pre-open gates for the execution domain.

These helpers are exit-agnostic and entry-agnostic: they answer "is the bot
allowed to open right now?" (:func:`evaluate_open_gates`), "did this closed
position just get taken out by the stop it was opened with?"
(:func:`should_revert_after_stop_loss`) and "does the curve it walked to get
there actually look like a reversal?" (:func:`curve_supports_revert`). They carry
no I/O and are shared by the live trading service and the simulator, which is why
they live in the execution domain rather than the trading service.

The gates here are correctness checks (market hours, signal direction,
duplicate-epic suppression, same-day re-open policy), one cost check, the
spread budget (:func:`spread_fits_stop`: the entry spread must not eat too large
a share of the stop distance), and one concentration check, the same-bet gate
(:func:`same_bet_free`: never hold the same exposure twice under two epics) —
the daily P&L / win-rate / max-position circuit-breakers were removed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

from src.core.indicators import efficiency_ratio
from src.core.similarity import bet_redundancy, shape_signature
from src.feed.price_buffer import Candle

#: ``reason_close`` values that mean "the protective stop took the position
#: out", i.e. the price came to the stop rather than the bot choosing to leave:
#:
#: * ``stop`` — the software backstop aligned with the live follower
#:   (:meth:`~src.exit.close_zoneprofit.CloseZoneProfit.evaluate`);
#: * ``loose`` — the same hit on the legacy close path
#:   (:func:`~src.exit.trailing.decide_close_reason`);
#: * ``closed_externally`` — the broker-side stop resting at IG fired and the
#:   sync job reconciled the vanished position. This is the *usual* case: the IG
#:   order sits one spread plus a noise cushion beyond the software follower, so
#:   it is what actually fills on a real move against the trade.
#:
#: Deliberately excluded: ``win`` / ``manual`` / ``end_of_day`` /
#: ``market_closed`` (the bot or the user chose to close), ``never_opened`` and
#: ``not_found_in_ig`` (no real trade / no trustworthy exit level).
STOP_LOSS_CLOSE_REASONS = frozenset({"stop", "loose", "closed_externally"})

#: ``reason_open`` stamped on a position opened by the recovery-revert rule
#: (``ALLOW_RECOVERY_REVERT``). It marks the origin on the dashboard *and* caps
#: the chain: a revert that is itself stopped out is never reverted again (see
#: :func:`should_revert_after_stop_loss`), so a choppy market cannot ping-pong
#: the account through an endless BUY/SELL/BUY sequence.
RECOVERY_REVERT_REASON_OPEN = "recovery_revert"

#: Curve-legitimacy thresholds for the recovery revert
#: (:func:`curve_supports_revert`). Tuned **here**, not in ``.env``: like every
#: other strategy parameter, only the on/off selector (``ALLOW_RECOVERY_REVERT``)
#: lives in the environment.
#:
#: The filter is deliberately **permissive**: the revert is the default answer to a
#: stop-out and only the blatant "nothing happened" curves are dropped. In
#: particular, **nothing here looks at how long the position was held**: a market
#: can sit flat for twenty minutes and then break in one candle, which is a prime
#: revert — the same stop-out reached by a hundred tiny steps is not. What
#: separates them is *where the move is concentrated*, not when it happened.
#:
#: * ``REVERT_MIN_IMPULSE_RATIO`` — the biggest single-candle move against the
#:   trade, as a fraction of the risk, must reach this. This is the whole flatness
#:   test: a real break puts a visible chunk of the stop distance into one candle,
#:   whereas a flat range that eventually rubs against the stop, or a slow leak
#:   down to it, never produces one. Raising it demands a more violent break.
#: * ``REVERT_MIN_EFFICIENCY_K`` — how much straighter than a coin-flip walk the
#:   path since open must be. A random walk of ``n`` steps has an expected
#:   efficiency ratio around ``1/sqrt(n)``, so the bar is ``k/sqrt(n)``: it scales
#:   with the number of candles instead of punishing long paths for being long. It
#:   is the second flatness test, aimed at violent chop — a market whose candles
#:   are big enough to pass the impulse test but which went nowhere.
#: * ``REVERT_MAX_EFFICIENCY_BAR`` — cap on that bar, so a two-candle break is not
#:   asked to be a perfectly straight line.
#: * ``REVERT_MAX_FAVOURABLE_RATIO`` — how far the trade may have gone the *right*
#:   way (as a fraction of the risk) before turning. A trade that ran a full risk
#:   in profit and then came all the way back through its opening stop was in an
#:   oscillation, not wrong about the direction.
#: * ``REVERT_MIN_BREAK_RATIO`` — how far past the stop (same fraction of the
#:   risk) price must still be when the revert is decided. This is what rejects a
#:   stop that was *grazed*: a wick takes the stop out and price is already back
#:   on the other side of it, so there is no move to follow.
#: * ``REVERT_MIN_CURVE_CANDLES`` — fewest candles the shape tests need (one step
#:   is one candle). Fewer is accepted only when the position genuinely died within
#:   that many minutes (a break too fast to have been recorded); otherwise the feed
#:   was silent and the curve is simply unknown, which stays a refusal.
REVERT_MIN_IMPULSE_RATIO = 0.2
REVERT_MIN_EFFICIENCY_K = 1.2
REVERT_MAX_EFFICIENCY_BAR = 0.8
REVERT_MAX_FAVOURABLE_RATIO = 1.0
REVERT_MIN_BREAK_RATIO = 0.1
REVERT_MIN_CURVE_CANDLES = 2

#: Largest share of the stop distance the entry spread may take
#: (:func:`spread_fits_stop`). The distance is the worst case — from the fill to
#: the broker stop (:func:`worst_case_stop_distance`), i.e. the loss actually paid
#: when the trade is stopped out. Measured on the live book (2026-06 → 2026-09,
#: ~960 trades, spread over that same fill → initial broker stop distance): below
#: 10 % the expectancy is flat (−0.01 R), 10–20 % costs −0.11 R, and **20–35 %
#: loses −0.43 R with a third of winners** (−0.83 R since August), the confidence
#: interval clear of zero. Such a trade starts a fifth of the way to its stop and
#: its stop sits inside the bid/offer churn. Tuned **here**, not in ``.env``.
MAX_SPREAD_STOP_RATIO = 0.20

#: Same-bet gate (:func:`same_bet_free`): a candidate whose curve, signed by the
#: two trade directions, correlates above :data:`MAX_SAME_BET_REDUNDANCY` with a
#: position already held is the same exposure taken twice. Measured on open_fade
#: (W24–W39): cocoa NY and cocoa London were opened the same day on the same side
#: on 19 of 26 shared days — four of the five best trades were such pairs, i.e.
#: the cocoa risk was doubled, not diversified. Same windows and threshold as the
#: open_five ranking filter: ~1 h of candles, 20 shared timestamps to trust a
#: correlation, and a 0.80 veto (below the ~0.95 of two listings of one market, so
#: near-twins are caught too). Tuned **here**, not in ``.env``.
SAME_BET_WINDOW = 60
SAME_BET_MIN_OVERLAP = 20
MAX_SAME_BET_REDUNDANCY = 0.80


def reverse_direction(direction: str | None) -> str:
    """The opposite trade side: ``BUY`` for a short, ``SELL`` for anything else.

    Anything other than ``"SELL"`` is treated as a long, mirroring the rest of
    the codebase (``Position.direction`` defaults to ``BUY``).
    """
    return "BUY" if direction == "SELL" else "SELL"


def original_stop_level(position) -> float:
    """The protective stop placed at **open**, in price units (0 when unknown).

    The stop trajectory (``Position.stop_history``) is seeded at open with the
    software follower and the broker level actually posted at IG, then appended
    to on every ratchet — so its first point *is* the stop the position was
    opened with, whatever happened afterwards. Rows opened before the trajectory
    existed (adopted/legacy) fall back to the live follower, but only while it
    never ratcheted (``stop_update`` is 0); once it moved, the original level is
    genuinely unrecoverable and 0 is returned so callers stay conservative.
    """
    history = getattr(position, "stop_history", None) or []
    if history and isinstance(history[0], dict):
        level = history[0].get("level") or history[0].get("broker")
        if level:
            return float(level)
    if not (getattr(position, "stop_update", 0) or 0):
        return float(getattr(position, "level_follower", 0) or 0)
    return 0.0


def should_revert_after_stop_loss(
    *,
    direction: str | None,
    reason_close: str | None,
    reason_open: str | None,
    euro: float,
    level_close: float,
    original_stop: float,
    stop_ratcheted: bool,
) -> tuple[bool, str]:
    """Did this closed position hit the stop it was opened with, at a loss?

    The recovery-revert rule (``ALLOW_RECOVERY_REVERT``) opens an immediate
    position on the **opposite** side when it did: the market walked straight
    through the level the trade was built on, so the working assumption is that
    it has turned, and the bot follows the turn instead of waiting for a fresh
    signal on a market it just misread.

    All five conditions must hold:

    1. the position is not itself a recovery revert (single hop — see
       :data:`RECOVERY_REVERT_REASON_OPEN`);
    2. it closed on a stop hit (:data:`STOP_LOSS_CLOSE_REASONS`);
    3. the realized P&L is a loss — a stop that fires in profit is a *secured
       winner*, not a reversal to chase;
    4. the stop placed at open is known (:func:`original_stop_level`);
    5. that stop is the one that fired. When it never ratcheted this follows
       from (2) — the only stop the position ever had is its original one. When
       it *did* ratchet, the close level must still have reached the original
       level (a gap straight through the ratcheted stop), otherwise the trade was
       stopped out on a *raised* stop, which is the trailing logic doing its job
       and not a reversal signal.

    Returns:
        (revert, reason) — reason explains the first failed condition, or
        ``"OK"``. Mirrors :func:`evaluate_open_gates` so callers log it the same
        way.
    """
    if reason_open == RECOVERY_REVERT_REASON_OPEN:
        return False, "Position is itself a recovery revert (no second hop)"

    if reason_close not in STOP_LOSS_CLOSE_REASONS:
        return False, f"Close reason {reason_close!r} is not a stop hit"

    if euro >= 0:
        return False, f"Position closed at {euro:+.2f}€ — not a loss"

    if original_stop <= 0:
        return False, "Stop placed at open is unknown"

    if stop_ratcheted:
        sign = -1.0 if direction == "SELL" else 1.0
        if level_close <= 0 or sign * (level_close - original_stop) > 0:
            return False, "Stop had ratcheted past its original level"

    return True, "OK"


def position_opened_at(position) -> datetime | None:
    """Reconstruct a position's open instant (UTC) from its persisted columns.

    ``time_open`` is stored as a naive time-of-day captured from
    ``datetime.now(UTC)`` at open (see ``src/execution/trading.py``), so it is
    combined with ``date`` and stamped UTC to line up with the buffer's UTC-aware
    candle timestamps. Returns ``None`` when either column is missing (adopted or
    stubbed rows), which callers read as "the curve cannot be located".
    """
    day = getattr(position, "date", None)
    opened = getattr(position, "time_open", None)
    if day is None or opened is None:
        return None
    return datetime.combine(day, opened, tzinfo=UTC)


def curve_supports_revert(
    *,
    direction: str | None,
    level_open: float,
    original_stop: float,
    prices: list[float],
    current_price: float,
    minutes_held: float,
    min_impulse_ratio: float = REVERT_MIN_IMPULSE_RATIO,
    min_efficiency_k: float = REVERT_MIN_EFFICIENCY_K,
    max_efficiency_bar: float = REVERT_MAX_EFFICIENCY_BAR,
    max_favourable_ratio: float = REVERT_MAX_FAVOURABLE_RATIO,
    min_break_ratio: float = REVERT_MIN_BREAK_RATIO,
    min_curve_candles: int = REVERT_MIN_CURVE_CANDLES,
) -> tuple[bool, str]:
    """Did the market really move through the stop, or was the curve flat?

    :func:`should_revert_after_stop_loss` only establishes **which stop** fired.
    That is not enough to justify taking the opposite side: the same "loss on the
    original stop" bookkeeping covers a market that broke through the level and a
    market that never went anywhere. Reverting into the second one buys a spread in
    the direction of nothing.

    The bar is set low on purpose — a stop-out normally *does* revert, and this
    only removes the blatant cases. Concretely, it asks the curve since the open
    one main question: **is the adverse move concentrated somewhere?** A break puts
    a visible chunk of the stop distance into a single candle, whether that candle
    comes after two minutes or after an hour of nothing — a flat range rubbing
    against the stop, or a slow leak down to it, never does. Holding time is
    therefore not a criterion anywhere in this rule.

    Every threshold is expressed as a fraction of the trade's own risk
    (``|level_open - original_stop|``) or as a ratio, so it means the same thing on
    a forex pair and on an index — nothing here is in absolute points.

    Four ways the curve disqualifies itself:

    1. **grazed stop** — price is no longer past the stop by ``min_break_ratio`` of
       the risk when the revert is decided. The stop was clipped by a wick and the
       market is already back on the other side of it;
    2. **no impulse** — the biggest single-candle move against the trade is under
       ``min_impulse_ratio`` of the risk. The stop distance was covered in dribs and
       drabs: a flat or slowly-leaking market, with nothing to ride on the other
       side;
    3. **violent chop** — the path since open is no straighter than
       ``min_efficiency_k`` × a random walk of the same length (see
       :data:`REVERT_MIN_EFFICIENCY_K`). Candles big enough to pass (2) can still
       add up to a market that went nowhere;
    4. **swing, not a wrong call** — the trade ran more than
       ``max_favourable_ratio`` of its risk *in profit* before coming back through
       the stop. The direction was right at least once, so the stop-out is the tail
       of an oscillation.

    Args:
        direction: the closed position's side (``BUY`` / ``SELL``).
        level_open: its opening level.
        original_stop: the stop it was opened with (:func:`original_stop_level`).
        prices: close-out prices of the candles **since the open**, oldest first
            (bid for a long, bid + spread for a short — the terms the stop levels
            are in). ``level_open`` is a *fill* price, so the favourable excursion
            measured against it is one spread short of the truth, i.e. slightly in
            favour of reverting; every other test compares like with like.
        current_price: the close-out price right now, i.e. where the market is at
            the moment the revert would be placed.
        minutes_held: wall-clock minutes between the open and the close. Used
            **only** to tell a position that died before the first candle was
            recorded from one whose candles are missing — never as a filter.
        min_impulse_ratio: biggest adverse candle required (2).
        min_efficiency_k: straightness multiple (3).
        max_efficiency_bar: cap on the straightness bar for short paths (3).
        max_favourable_ratio: favourable-excursion ceiling (4).
        min_break_ratio: minimum penetration past the stop (1).
        min_curve_candles: fewest candles the shape tests need.

    Returns:
        (supports, reason) — ``reason`` names the shape that disqualified the
        curve, or ``"OK"``. Mirrors the other gates so callers log it the same way.
    """
    sign = -1.0 if direction == "SELL" else 1.0
    risk = abs(level_open - original_stop)
    if risk <= 0:
        return False, "Risk span at open is unknown (cannot judge the curve)"

    if current_price <= 0:
        return False, "No live price to confirm the break"

    # Penetration is measured against the stop in the ADVERSE direction: for a
    # long, price must still be below it; for a short, still above it.
    penetration = -sign * (current_price - original_stop)
    if penetration < min_break_ratio * risk:
        return False, (
            f"Price only {penetration / risk:+.0%} of the risk past the stop "
            f"(min {min_break_ratio:.0%}) — stop grazed, not broken"
        )

    if len(prices) < min_curve_candles:
        # Too few candles to have a shape at all. Accept only when the position
        # really did die before they could be recorded — a stop-out that fast is
        # a break by definition. Held longer than that, the candles are missing
        # rather than absent (feed gap), and an unknown curve is refused.
        if minutes_held <= min_curve_candles:
            return True, f"OK (stopped out within {minutes_held:.0f} min of the open)"
        return False, (
            f"Only {len(prices)} candle(s) since open for {minutes_held:.0f} min "
            "held — curve unknown"
        )

    # The walk starts at the opening level, so that level is the first point of the
    # path: a position whose very first candle crashed through the stop must show
    # its impulse. ``level_open`` is a fill price, so for a long that first step
    # carries one spread of entry cost — negligible next to a stop distance, and
    # what it biases is acceptance, never a refusal.
    path = [level_open, *prices]
    adverse_steps = [-sign * (nxt - prev) for prev, nxt in zip(path, path[1:])]
    impulse = max(adverse_steps)
    if impulse < min_impulse_ratio * risk:
        return False, (
            f"Biggest adverse candle only {impulse / risk:+.0%} of the risk "
            f"(min {min_impulse_ratio:.0%}) over {len(adverse_steps)} candle(s) — "
            "flat curve leaking to the stop, not a break"
        )

    steps = len(path) - 1
    efficiency = efficiency_ratio(path, steps)
    bar = min(max_efficiency_bar, min_efficiency_k / math.sqrt(steps))
    if efficiency < bar:
        return False, (
            f"Path efficiency {efficiency:.2f} < {bar:.2f} over {steps} candle(s) "
            "— the market chopped its way to the stop and went nowhere"
        )

    favourable = max(sign * (price - level_open) for price in prices)
    if favourable > max_favourable_ratio * risk:
        return False, (
            f"Went {favourable / risk:.0%} of the risk in favour first "
            f"(max {max_favourable_ratio:.0%}) — oscillation, not a wrong side"
        )

    return True, "OK"


def worst_case_stop_distance(
    direction: str | None, *, bid: float, offer: float, broker_stop: float
) -> float:
    """Price distance from the fill to the broker stop: the loss if stopped out.

    A BUY fills at the offer and is stopped out on the bid; a SELL fills at the
    bid and is stopped out on the offer. The broker stop level *is* the close-out
    price in both cases, so the realized loss per unit is the gap between the fill
    and that level — one spread more than a bid-to-stop distance for a long, and
    including whatever cushion separates the broker stop from the software stop.
    """
    fill = bid if direction == "SELL" else offer
    return abs(fill - broker_stop)


def spread_fits_stop(
    spread: float, stop_distance: float, max_ratio: float | None = None
) -> tuple[bool, str]:
    """Refuse an open whose entry spread eats too much of its stop distance.

    ``stop_distance`` is the worst-case fill → broker-stop distance
    (:func:`worst_case_stop_distance`). ``max_ratio`` defaults to
    :data:`MAX_SPREAD_STOP_RATIO`, read at call time so a backtest can compare a
    run with the gate relaxed. A non-positive distance means the stop sits on the
    wrong side of the fill — refused, there is no risk budget to speak of.

    Returns:
        (allowed, reason) — reason carries the measured ratio on a refusal.
    """
    limit = MAX_SPREAD_STOP_RATIO if max_ratio is None else max_ratio
    if stop_distance <= 0:
        return False, "Spread budget exceeded (no stop distance)"
    ratio = max(spread, 0.0) / stop_distance
    if ratio >= limit:
        return False, (
            f"Spread budget exceeded (spread {ratio:.0%} of the stop distance, "
            f"max {limit:.0%})"
        )
    return True, "OK"


def same_bet_free(
    *,
    epic: str,
    direction: str,
    candles: Sequence[Candle],
    book: Sequence[tuple[str, str, Sequence[Candle]]],
    window: int = SAME_BET_WINDOW,
    min_overlap: int = SAME_BET_MIN_OVERLAP,
    max_redundancy: float | None = None,
) -> tuple[bool, str]:
    """Refuse an open that is the same bet as a position already held.

    ``book`` lists the open positions as ``(epic, direction, candles)``. The
    candidate and every held curve are reduced to a shape signature over the last
    ``window`` candles (:func:`~src.core.similarity.shape_signature`) and compared
    with :func:`~src.core.similarity.bet_redundancy` — the return correlation
    signed by the two directions — so two listings of one market taken the same
    way (cocoa NY + cocoa London long) and a mirrored pair (EUR/USD long +
    USD/CHF short) are both refused, while a hedge (negative redundancy) is not.

    Applies to every entry strategy, unlike ``filter_ranked`` (which only
    deduplicates the candidates of one ranking pass, never against the book). A
    position on the candidate's own epic is skipped: the duplicate-epic gate
    already owns that case. A curve that cannot be judged (too short, flat, too
    little timestamp overlap) never refuses — a data gap must not block opens.
    ``max_redundancy`` defaults to :data:`MAX_SAME_BET_REDUNDANCY`, read at call
    time so a backtest can compare a run with the gate relaxed.

    Returns:
        (allowed, reason) — reason names the held twin and the redundancy.
    """
    limit = MAX_SAME_BET_REDUNDANCY if max_redundancy is None else max_redundancy
    candidate = shape_signature(epic, list(candles), window)
    if candidate is None:
        return True, "OK"
    for held_epic, held_direction, held_candles in book:
        if held_epic == epic:
            continue
        held = shape_signature(held_epic, list(held_candles), window)
        if held is None:
            continue
        redundancy = bet_redundancy(
            candidate, held, direction, held_direction, min_overlap=min_overlap
        )
        if redundancy is not None and redundancy > limit:
            return False, (
                f"Same bet as an open position ({held_direction} {held_epic}, "
                f"redundancy {redundancy:.2f} > {limit:.2f})"
            )
    return True, "OK"


def evaluate_open_gates(
    *,
    epic: str,
    direction: str,
    in_trading_hours: bool,
    epic_already_open: bool,
    closes_soon: bool = False,
    allow_short: bool = False,
    epic_traded_today: bool = False,
) -> tuple[bool, str]:
    """Pure pre-open rule evaluation shared by live trading and the simulator.

    The caller gathers the live state and this function applies the rules to it.

    ``closes_soon`` is True when the epic's own market closes within the pre-open
    buffer: opening then would only pay the spread before the per-epic close rule
    force-closes the trade. Defaults to False so callers with no per-epic close
    time (e.g. the simulator) are unaffected.

    ``allow_short`` lifts the long-only restriction: automatic entry strategies
    are long-only (default ``False``), while a manual dashboard open passes
    ``True`` to permit a SELL. Flip the default here to let SELL through
    everywhere once shorts are validated on the automatic path.

    ``epic_traded_today`` is True when the epic already had an opening today (in
    either direction, still open or already closed) **and** the global
    ``ALLOW_SAME_DAY_REOPEN`` policy forbids re-using it. Callers that allow
    same-day re-opens — or that have no notion of a day (some tests) — leave it
    False. It is deliberately direction-agnostic: one opening per epic per day
    covers BUY and SELL alike.

    Returns:
        (allowed, reason) — reason explains the first failed gate.
    """
    if not in_trading_hours:
        return False, "Outside trading hours"

    if direction not in ("BUY", "SELL"):
        return False, f"Unknown direction {direction}"

    if direction == "SELL" and not allow_short:
        return False, f"Signal direction is {direction}"

    if epic_already_open:
        return False, f"Epic {epic} already open"

    if epic_traded_today:
        return False, f"Epic {epic} already traded today"

    if closes_soon:
        return False, f"Market {epic} closes soon"

    return True, "OK"
