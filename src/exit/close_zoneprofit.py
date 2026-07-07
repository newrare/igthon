"""The close profile — composes a stop-distance policy with three zone updaters.

``CloseZoneProfit`` is the project's single close profile. It owns nothing
about *where* the initial stop is placed nor *how* it moves; it **composes** those
decoupled responsibilities and wires them to the persisted position:

- at open, it delegates the initial protective stop to a
  :class:`~src.stops.base.StopDistance` (selected by ``STOP_STRATEGY``;
  defaults to the recency-weighted support distance), and freezes the break-even
  (``level_zero``) and margin (``level_zero + noise_margin``) references;
- on every tick, it classifies the live bid into one of three zones
  (see :mod:`src.exit.zones`) and delegates to the matching
  :class:`~src.exit.zones.base.StopUpdater`:
    * :class:`~src.exit.zones.underwater.UnderwaterStop` — at/under break-even;
    * :class:`~src.exit.zones.breakeven_band.BreakevenBandStop` — noise band;
    * :class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop` — real profit.

The close-only concerns it keeps for itself are the two hard triggers: the
end-of-day force close and the software backstop aligned with the live stop.

This composition preserves the previous ``close_zoneprofit`` behaviour exactly:
the support-anchored initial stop, and the profit-gated ATR chandelier trailing
that only engages once the bid clears the margin level (zones 1 and 2 hold the
stop, zone 3 ratchets it up in steps).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.indicators import atr
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
from src.exit.zones.trailing_ratchet import TrailingRatchetStop
from src.exit.zones.underwater import UnderwaterStop
from src.feed.price_buffer import EpicBuffer
from src.stops import StopDistance, get_stop_distance
from src.stops.stop_support import StopSupport


@dataclass
class CloseZoneProfit(CloseProfile):
    """Composes a stop-distance policy with the three per-zone stop updaters."""

    name = "close_zoneprofit"

    atr_period: int = 14
    noise_k: float = 0.5  # noise margin = max(noise_k × ATR, 2 × spread)

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

    def _noise_margin(self, atr_value: float, spread: float) -> float:
        """Noise margin (see :func:`~src.exit.base.noise_margin`)."""
        return noise_margin(self.noise_k, atr_value, spread)

    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Delegate the initial stop to the distance policy; freeze the references.

        ``level_zero`` (break-even) and ``level_margin`` (break-even + noise
        margin) are computed once here and persisted, so the dead band the stop
        must clear is fixed for the position's whole life and never drifts as ATR
        later breathes.
        """
        last = buf.last
        atr_value = atr(list(buf.candles), self.atr_period)
        spread = last.spread if last else 0.0
        stop_level = self.stop_distance.initial_stop(
            entry_level=entry_level, direction=direction, buf=buf
        )
        if direction == "SELL":
            level_zero = entry_level - spread
        else:
            level_zero = last.offer_close if last else entry_level
        return OpenPlan(
            stop_level=stop_level,
            level_zero=level_zero,
            target_level=0.0,
            level_margin=level_zero + self._noise_margin(atr_value, spread),
            profile=self.name,
        )

    def evaluate(
        self, position, current_bid: float, buf: EpicBuffer, *, is_close_hour: bool
    ) -> CloseDecision:
        """End-of-day / backstop first, then classify the zone and delegate."""
        if is_close_hour:
            return CloseDecision(action=ACTION_CLOSE, reason="end_of_day")

        # Software backstop aligned with the current real stop (the follower): the
        # broker fills the pushed stop, this only guarantees a close if that ever
        # fails. The stop is never lowered, so this is also the initial stop. It
        # runs BEFORE the ATR warm-up guard below — otherwise a restart with fewer
        # than ``atr_period`` candles (``atr`` returns 0) would disable the only
        # software close for ~atr_period minutes while the follower is live. (#9)
        level_follower = float(position.level_follower or 0)
        if level_follower > 0 and current_bid <= level_follower:
            return CloseDecision(action=ACTION_CLOSE, reason="stop")

        last = buf.last
        if last is None:
            return CloseDecision(action=ACTION_HOLD)
        atr_value = atr(list(buf.candles), self.atr_period)
        if atr_value <= 0:
            return CloseDecision(action=ACTION_HOLD)

        level_open = float(position.level_open or 0)
        level_zero = float(position.level_zero or 0)
        spread = last.spread

        # Margin level frozen at open (break-even + noise margin). Fall back to a
        # per-tick computation for positions opened before it was persisted.
        level_margin = float(getattr(position, "level_margin", 0) or 0)
        if level_margin <= 0:
            level_margin = level_zero + self._noise_margin(atr_value, spread)

        ctx = StopContext(
            current_bid=current_bid,
            level_open=level_open,
            level_zero=level_zero,
            level_margin=level_margin,
            level_follower=level_follower,
            atr_value=atr_value,
            spread=spread,
            euro_per_point=float(position.euro_per_point or 0),
            buf=buf,
        )

        zone = classify_zone(current_bid, level_zero, level_margin)
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
