"""The close profile — composes a stop-distance policy with three zone updaters.

``CloseZoneProfit`` is the project's single close profile. It owns nothing
about *where* the initial stop is placed nor *how* it moves; it **composes** those
decoupled responsibilities and wires them to the persisted position:

- at open, it delegates the initial protective stop to a
  :class:`~src.stops.base.StopDistance` (selected by ``STOP_STRATEGY``;
  defaults to the recency-weighted support distance), and freezes the break-even
  (``level_zero``) and margin (break-even + one noise margin **towards profit**)
  references;
- on every tick, it classifies the live close-out price into one of three zones
  (see :mod:`src.exit.zones`) using break-even and the *profit trigger*
  (a second symmetric band past the margin), and delegates to the matching
  :class:`~src.exit.zones.base.StopUpdater`:
    * :class:`~src.exit.zones.underwater.UnderwaterStop` — short of break-even;
    * :class:`~src.exit.zones.breakeven_band.BreakevenBandStop` — break-even up to
      the profit trigger (across the margin line);
    * :class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop` — past the
      profit trigger (real profit).

The close-only concerns it keeps for itself are the two hard triggers: the
end-of-day force close and the software backstop aligned with the live stop.

**BUY and SELL share this one profile.** Direction enters in exactly three
places — the side of break-even the margin is frozen on, the close-out price the
zones are judged against (bid for a long, offer for a short), and the ``sign``
threaded into :class:`~src.exit.zones.base.StopContext` — so every zone updater,
and every ``CLOSE_ZONE*`` selector, applies unchanged to a short. Shorts used to
bypass all of this through a separate profile that had no zones at all, which is
why a short's stop never moved inside the margin band.

This composition preserves the previous ``close_zoneprofit`` behaviour exactly:
the support-anchored initial stop, and the profit-gated ATR chandelier trailing
that only engages once price clears the profit trigger (zones 1 and 2 hold or
lock the stop short of the margin, zone 3 ratchets it in steps beyond it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.core.indicators import adverse_tick_noise, atr
from src.exit.base import (
    ACTION_CLOSE,
    ACTION_HOLD,
    ACTION_UPDATE_STOP,
    CloseDecision,
    CloseProfile,
    OpenPlan,
    noise_margin,
)
from src.exit.zones import (
    ZONEMARGE_UPDATERS,
    ZONEPROFIT_UPDATERS,
    ZONESTART_UPDATERS,
    build_zone_updater,
)
from src.exit.zones.base import (
    StopContext,
    StopUpdater,
    StopZone,
    classify_zone,
)
from src.exit.zones.breakeven_band import BreakevenBandStop
from src.exit.zones.smartgroup import GroupMember, SmartGroupStop
from src.exit.zones.trailing_ratchet import TrailingRatchetStop
from src.exit.zones.underwater import UnderwaterStop
from src.feed.price_buffer import EpicBuffer
from src.stops import StopDistance, get_stop_distance
from src.stops.stop_support import StopSupport


def _sign(direction: str | None) -> float:
    """``+1`` when profit is up (BUY), ``−1`` when profit is down (SELL)."""
    return -1.0 if direction == "SELL" else 1.0


def _close_out_price(current_bid: float, spread: float, sign: float) -> float:
    """The price a close would be filled at, from the monitor's live bid.

    A BUY is closed by selling at the **bid**; a SELL is closed by buying at the
    **offer**, one spread higher. Every level the zones reason about (break-even,
    margin, follower) is in these same terms, so the comparison is like-for-like on
    both sides — using the bid for a short would read the position as one spread
    more profitable than it is, and the software backstop would fire late.
    """
    return current_bid + spread if sign < 0 else current_bid


def _opened_at(position) -> datetime | None:
    """Reconstruct the position's open instant (UTC) from its persisted columns.

    ``time_open`` is stored as a naive time-of-day captured from
    ``datetime.now(UTC)`` at open (see ``src/execution/trading.py``), so it is
    combined with ``date`` and stamped UTC to match the buffer's UTC-aware candle
    timestamps. Returns ``None`` when either column is absent (e.g. a bare test
    stub), which callers treat as "do not slice".
    """
    day = getattr(position, "date", None)
    opened = getattr(position, "time_open", None)
    if day is None or opened is None:
        return None
    return datetime.combine(day, opened, tzinfo=UTC)


def _buffer_since(buf: EpicBuffer, opened_at: datetime | None) -> EpicBuffer:
    """Return a view of ``buf`` holding only candles from ``opened_at`` onward.

    The zone updaters read price *levels* (swing lows, rising streaks, recent
    range) from the buffer and judge them against references frozen at open
    (``level_zero`` / ``level_margin``). The live ``EpicBuffer`` is a rolling
    window fed continuously by the market feed, so it also holds candles recorded
    **before this position opened** — an intraday rally that happened to clear
    where the margin was later frozen would arm a break-even lock retroactively,
    even though nothing since the open ever approached it (observed live). Bounding
    the buffer to the open removes that pre-entry history for every updater at once.

    ``opened_at`` of ``None`` (columns absent) returns the buffer unchanged.
    """
    if opened_at is None:
        return buf
    sliced = EpicBuffer(epic=buf.epic, max_candles=buf.max_candles)
    for candle in buf.candles:
        if candle.timestamp >= opened_at:
            sliced.add(candle)
    return sliced


@dataclass
class CloseZoneProfit(CloseProfile):
    """Composes a stop-distance policy with the three per-zone stop updaters."""

    name = "close_zoneprofit"

    atr_period: int = 14
    noise_k: float = 1.5  # noise margin = noise_k × ATR (spread is not a factor)

    # Initial-stop placement (swappable via STOP_STRATEGY). Defaults to the
    # support distance so ``CloseZoneProfit()`` keeps its historical
    # behaviour when built directly (tests, simulator helpers).
    stop_distance: StopDistance = field(default_factory=StopSupport)

    # The three per-zone stop updaters, composed on each tick. Each is selected
    # independently from ``.env`` (see ``from_settings``); the defaults keep the
    # historical behaviour when the profile is built directly (tests, helpers).
    underwater: StopUpdater = field(default_factory=UnderwaterStop)
    breakeven_band: StopUpdater = field(default_factory=BreakevenBandStop)
    trailing: StopUpdater = field(default_factory=TrailingRatchetStop)

    @classmethod
    def from_settings(cls, settings) -> CloseZoneProfit:
        # The close profile is a constant-shaped composer: the initial stop
        # distance is selected by STOP_STRATEGY, and each of the three zones by
        # its own CLOSE_ZONESTART / CLOSE_ZONEMARGE / CLOSE_ZONEPROFIT selector.
        distance_name = getattr(settings, "stop_strategy", "stop_support")
        return cls(
            stop_distance=get_stop_distance(distance_name, settings),
            underwater=build_zone_updater(
                ZONESTART_UPDATERS, settings.close_zonestart, settings
            ),
            breakeven_band=build_zone_updater(
                ZONEMARGE_UPDATERS, settings.close_zonemarge, settings
            ),
            trailing=build_zone_updater(
                ZONEPROFIT_UPDATERS, settings.close_zoneprofit, settings
            ),
        )

    def _noise_margin(self, atr_value: float) -> float:
        """Noise margin (see :func:`~src.exit.base.noise_margin`)."""
        return noise_margin(self.noise_k, atr_value)

    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Delegate the initial stop to the distance policy; freeze the references.

        ``level_zero`` (break-even) and ``level_margin`` (break-even + one noise
        margin **towards profit**) are computed once here and persisted, so the
        dead band the stop must clear is fixed for the position's whole life and
        never drifts as ATR later breathes.

        Break-even is always expressed in **close-out terms** — the price at which
        closing returns exactly the entry cost. ``entry_level`` is the live bid, so
        a BUY (filled at the offer, closed on the bid) breaks even at the entry
        offer, while a SELL (filled at the bid, closed on the offer) breaks even at
        that same bid.
        """
        last = buf.last
        atr_value = atr(list(buf.candles), self.atr_period)
        stop_level = self.stop_distance.initial_stop(
            entry_level=entry_level, direction=direction, buf=buf
        )
        sign = _sign(direction)
        if direction == "SELL":
            level_zero = entry_level
        else:
            level_zero = last.offer_close if last else entry_level
        return OpenPlan(
            stop_level=stop_level,
            level_zero=level_zero,
            target_level=0.0,
            level_margin=level_zero + sign * self._noise_margin(atr_value),
            profile=self.name,
        )

    def _references(self, position, buf: EpicBuffer, atr_value: float):
        """The open-frozen references this position is managed against.

        Returns ``(sign, level_zero, level_margin, level_profit)``. The margin falls
        back to a per-tick computation for rows opened before it was persisted, and
        the profit trigger is derived (``2 × margin − zero``) rather than stored —
        that formula mirrors itself, so it lands one further noise margin *towards
        profit* on either side.
        """
        sign = _sign(getattr(position, "direction", "BUY"))
        level_zero = float(position.level_zero or 0)
        level_margin = float(getattr(position, "level_margin", 0) or 0)
        if level_margin <= 0:
            level_margin = level_zero + sign * self._noise_margin(atr_value)
        return (
            sign,
            level_zero,
            level_margin,
            level_margin + (level_margin - level_zero),
        )

    def current_zone(
        self, position, current_bid: float, buf: EpicBuffer
    ) -> StopZone | None:
        """Classify the live price into a :class:`StopZone` for this position.

        Uses the same open-frozen references and profit-trigger derivation as
        :meth:`evaluate` so the manual stop-raise "hold" sees exactly the zones
        the automatic management does. Returns ``None`` when there is no candle to
        read a spread/ATR fallback from.
        """
        last = buf.last if buf is not None else None
        if last is None:
            return None
        sign, level_zero, _margin, level_profit = self._references(
            position, buf, atr(list(buf.candles), self.atr_period)
        )
        price = _close_out_price(current_bid, last.spread, sign)
        return classify_zone(price, level_zero, level_profit, sign)

    @property
    def is_group_aware(self) -> bool:
        """True when the zone-1 updater manages the book as a whole (smartgroup)."""
        return isinstance(self.underwater, SmartGroupStop)

    def group_member(
        self, position, current_bid: float, buf: EpicBuffer
    ) -> GroupMember | None:
        """Build the group pre-pass scalars for this position, or ``None``.

        Reads the same live measures (ATR, spread, adverse-tick noise) that
        :meth:`evaluate` uses, so the group planner and the per-tick management
        agree on the numbers — including the close-out price and the sign, so longs
        and shorts share one budget. Returns ``None`` when the profile is not
        group-aware or there is no candle to read a spread/ATR from.
        """
        if not self.is_group_aware:
            return None
        last = buf.last if buf is not None else None
        if last is None:
            return None
        smart: SmartGroupStop = self.underwater  # type: ignore[assignment]
        sign = _sign(getattr(position, "direction", "BUY"))
        spread = float(last.spread or 0)
        # The adverse direction is a down-move for a long and an up-move for a
        # short: measure the band on the sign-normalised close-out series.
        closes = buf.offer_closes if sign < 0 else buf.bid_closes
        noise = adverse_tick_noise(
            [sign * c for c in closes],
            smart.params.noise_window,
            smart.params.noise_std_k,
        )
        return GroupMember(
            position_id=int(position.id),
            level_open=float(position.level_open or 0),
            level_zero=float(position.level_zero or 0),
            level_follower=float(position.level_follower or 0),
            euro_per_point=float(position.euro_per_point or 0),
            current_price=_close_out_price(current_bid, spread, sign),
            atr_value=atr(list(buf.candles), self.atr_period),
            spread=spread,
            min_stop_distance=float(getattr(position, "min_stop_distance", 0) or 0),
            noise=noise,
            sign=sign,
        )

    def plan_group(self, members: list[GroupMember]) -> dict[int, float]:
        """Delegate the whole-book tightening plan to the smartgroup updater."""
        if not self.is_group_aware:
            return {}
        smart: SmartGroupStop = self.underwater  # type: ignore[assignment]
        return smart.plan(members)

    def evaluate(
        self,
        position,
        current_bid: float,
        buf: EpicBuffer,
        *,
        is_close_hour: bool,
        group_tighten: float | None = None,
    ) -> CloseDecision:
        """End-of-day / backstop first, then classify the zone and delegate.

        ``group_tighten`` carries the pre-resolved stop level from the group
        pre-pass (``smartgroup`` only); it is threaded into the per-tick
        :class:`~src.exit.zones.base.StopContext` and left ``None`` otherwise.
        """
        if is_close_hour:
            return CloseDecision(action=ACTION_CLOSE, reason="end_of_day")

        last = buf.last
        if last is None:
            return CloseDecision(action=ACTION_HOLD)
        spread = last.spread
        direction = getattr(position, "direction", "BUY")
        sign = _sign(direction)
        # The price a close would be filled at: the bid for a long, the offer for a
        # short (see ``_close_out_price``). Every level below is in these terms.
        price = _close_out_price(current_bid, spread, sign)

        # Software backstop aligned with the current real stop (the follower): the
        # broker fills the pushed stop, this only guarantees a close if that ever
        # fails. The stop is never loosened, so this is also the initial stop. It
        # runs BEFORE the ATR warm-up guard below — otherwise a restart with fewer
        # than ``atr_period`` candles (``atr`` returns 0) would disable the only
        # software close for ~atr_period minutes while the follower is live. (#9)
        level_follower = float(position.level_follower or 0)
        if level_follower > 0 and sign * (price - level_follower) <= 0:
            return CloseDecision(action=ACTION_CLOSE, reason="stop")

        atr_value = atr(list(buf.candles), self.atr_period)
        if atr_value <= 0:
            return CloseDecision(action=ACTION_HOLD)

        level_open = float(position.level_open or 0)
        # Break-even, the margin frozen at open, and the derived profit trigger —
        # one further noise margin past the margin line, i.e. a second symmetric
        # band stacked on the first. The trigger is the boundary price must clear to
        # enter the profit-trailing zone; short of it (but past break-even) the
        # margin-zone updater keeps parking the stop on a support inside
        # break-even→margin.
        sign, level_zero, level_margin, level_profit = self._references(
            position, buf, atr_value
        )

        # Bound the buffer the updaters scan to candles recorded from the open
        # onward. ATR above intentionally keeps the full rolling history (a
        # volatility estimate wants pre-open data and must stay warm right after
        # open), but the stop-tightening gates read price levels against references
        # frozen at open, so pre-entry candles must not count (see
        # ``_buffer_since``).
        ctx = StopContext(
            current_price=price,
            level_open=level_open,
            level_zero=level_zero,
            level_margin=level_margin,
            level_follower=level_follower,
            atr_value=atr_value,
            spread=spread,
            euro_per_point=float(position.euro_per_point or 0),
            buf=_buffer_since(buf, _opened_at(position)),
            direction=direction,
            min_stop_distance=float(getattr(position, "min_stop_distance", 0) or 0),
            group_tighten=group_tighten,
        )

        zone = classify_zone(price, level_zero, level_profit, sign)
        if zone is StopZone.PROFIT:
            updater = self.trailing
        elif zone is StopZone.BREAKEVEN_BAND:
            updater = self.breakeven_band
        else:
            updater = self.underwater

        new_stop = updater.propose(ctx)
        if new_stop is None:
            return CloseDecision(action=ACTION_HOLD)
        return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)
