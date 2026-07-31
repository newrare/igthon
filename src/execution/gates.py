"""Pure pre-open gates for the execution domain.

These helpers are exit-agnostic and entry-agnostic: they answer "is the bot
allowed to open right now?" (:func:`evaluate_open_gates`) and "did this closed
position just get taken out by the stop it was opened with?"
(:func:`should_revert_after_stop_loss`). They carry no I/O and are shared by the
live trading service and the simulator, which is why they live in the execution
domain rather than the trading service.

The gates here are correctness checks only (market hours, signal direction,
duplicate-epic suppression, same-day re-open policy) — the daily P&L / win-rate
/ max-position circuit-breakers were removed.
"""

from __future__ import annotations

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
