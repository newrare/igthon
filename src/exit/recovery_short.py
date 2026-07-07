"""Short exit for the loss-recovery feature — a mirrored ``trailing_ratchet``.

The loss-recovery feature (:mod:`src.execution.recovery`) opens a SELL when a
long stops out on the "trend-reversal at open" pattern. That short is not managed
by the long-only :class:`~src.exit.close_zoneprofit.CloseZoneProfit`; it gets this
dedicated profile, which mirrors the profit-zone
:class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop` for a short:

- the protective stop sits **above** the price (at the offer, the buy-to-close
  cost) and only ratchets **down**, tracking ``k × ATR`` above the running low;
- it is a **momentum-gated ATR chandelier**: only tighten when the last two
  recorded bids are both *falling* (a lone down-spike is ignored);
- an **anti-band guard** keeps the stop from parking at/above the frozen margin
  level (just below break-even), so it engages only once the short is in real
  profit — exactly the long updater's behaviour, reflected.

Two hard triggers are kept by the profile itself, like ``CloseZoneProfit``: the
end-of-day force close and a software backstop that closes when the offer reaches
the current stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
from src.exit.trailing import compute_trailing_stop_short
from src.feed.price_buffer import EpicBuffer
from src.stops import StopDistance
from src.stops.stop_support import StopSupport


@dataclass
class RecoveryShortProfile(CloseProfile):
    """Mirrored ``trailing_ratchet`` exit for a recovery SELL position."""

    name = "recovery_short"

    atr_period: int = 14
    noise_k: float = 0.5  # noise margin = max(noise_k × ATR, 2 × spread)

    # Initial short stop placement. StopSupport returns ``offer + stop_atr_k×ATR``
    # (above the entry) for a SELL — the mirror of its support-anchored BUY stop.
    stop_distance: StopDistance = field(default_factory=StopSupport)

    # Dedicated trailing width (× ATR), kept equal pre/post break-even (same as
    # the long ratchet: tightening after break-even cuts a runner short).
    atr_k_pre: float = 2.5
    atr_k_post: float = 2.5
    trailing_step_ratio: float = 0.3  # min advance (× ATR) before re-pushing stop

    # Adverse-noise floor on the trailing distance (see the long ratchet). For a
    # short the adverse move is an *upward* jitter of the offer, so the band is
    # measured on the negated bid series.
    noise_window: int = 20
    noise_std_k: float = 2.0
    noise_mult: float = 2.0

    @classmethod
    def from_settings(cls, settings) -> RecoveryShortProfile:
        # Constants live on the class (like every stop updater); build from them.
        return cls()

    def _noise_margin(self, atr_value: float, spread: float) -> float:
        """Noise margin below break-even (see :func:`~src.exit.base.noise_margin`)."""
        return noise_margin(self.noise_k, atr_value, spread)

    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Choose the initial short stop (above entry) and freeze the references.

        ``entry_level`` is the sell (bid) price. Break-even in buy-to-close terms
        is that same level (``level_zero``); the margin is one noise band *below*
        it (a short profits as the price falls).
        """
        if direction != "SELL":
            raise ValueError("RecoveryShortProfile manages SELL positions only")
        last = buf.last
        atr_value = atr(list(buf.candles), self.atr_period)
        spread = last.spread if last else 0.0
        stop_level = self.stop_distance.initial_stop(
            entry_level=entry_level, direction="SELL", buf=buf
        )
        level_zero = entry_level
        return OpenPlan(
            stop_level=stop_level,
            level_zero=level_zero,
            target_level=0.0,
            level_margin=level_zero - self._noise_margin(atr_value, spread),
            profile=self.name,
        )

    @staticmethod
    def _last_two_bids_falling(buf: EpicBuffer) -> bool:
        """True when the last two recorded bid moves are both downward.

        Requires ``bid[-3] > bid[-2] > bid[-1]``; a single down-spike yields only
        one down-step and fails this check — the mirror of the long ratchet's
        momentum gate.
        """
        closes = buf.bid_closes
        if len(closes) < 3:
            return False
        return closes[-3] > closes[-2] > closes[-1]

    def evaluate(
        self, position, current_bid: float, buf: EpicBuffer, *, is_close_hour: bool
    ) -> CloseDecision:
        """End-of-day / backstop first, then the momentum-gated short ratchet."""
        if is_close_hour:
            return CloseDecision(action=ACTION_CLOSE, reason="end_of_day")

        last = buf.last
        if last is None:
            return CloseDecision(action=ACTION_HOLD)

        # Buy-to-close cost is the offer, not the bid the monitor hands us.
        offer = last.offer_close
        level_zero = float(position.level_zero or 0)
        level_follower = float(position.level_follower or 0)

        # Software backstop aligned with the current short stop (above price): the
        # broker fills the pushed stop; this only guarantees a close if that fails.
        # The stop never rises, so this also enforces the initial stop. It runs
        # BEFORE the ATR warm-up guard below — otherwise a restart with fewer than
        # ``atr_period`` candles (``atr`` returns 0) would disable the only
        # software close for ~atr_period minutes while the stop is live. (#9)
        if level_follower > 0 and offer >= level_follower:
            return CloseDecision(action=ACTION_CLOSE, reason="stop")

        atr_value = atr(list(buf.candles), self.atr_period)
        if atr_value <= 0:
            return CloseDecision(action=ACTION_HOLD)

        spread = last.spread

        # Margin level frozen at open (break-even − noise margin). Fall back to a
        # per-tick computation for rows opened before it was persisted.
        level_margin = float(getattr(position, "level_margin", 0) or 0)
        if level_margin <= 0:
            level_margin = level_zero - self._noise_margin(atr_value, spread)

        # Momentum confirmation: only ratchet when the last two bids are both
        # falling. A lone down-spike is ignored (mirror of the long ratchet).
        if not self._last_two_bids_falling(buf):
            return CloseDecision(action=ACTION_HOLD)

        # Adverse noise for a short is upward offer jitter → measure the band on
        # the negated bid series. euro_stop=0 disables the initial-risk ceiling:
        # this only ever trails once the short is in real profit (anti-band guard),
        # so it protects acquired gain rather than the risk accepted at open.
        noise_floor = self.noise_mult * adverse_tick_noise(
            [-b for b in buf.bid_closes], self.noise_window, self.noise_std_k
        )
        new_stop = compute_trailing_stop_short(
            offer,
            atr_value=atr_value,
            spread=spread,
            level_zero=level_zero,
            level_follower=level_follower,
            euro_per_point=float(position.euro_per_point or 0),
            euro_stop=0.0,
            config=self,
            noise_floor=noise_floor,
        )
        if new_stop is None:
            return CloseDecision(action=ACTION_HOLD)

        # Never park the stop at/above the margin level (the dead band just below
        # break-even): a stop there would be triggered by noise for ~zero profit.
        # The mirror of the long ratchet's ``new_stop <= level_margin`` guard.
        if new_stop >= level_margin:
            return CloseDecision(action=ACTION_HOLD)

        return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)
