"""ATR chandelier trailing-stop close profile (the reference exit).

This profile reproduces the project's existing exit behaviour, now expressed as
a self-contained :class:`~src.exit.base.CloseProfile` independent of any entry
strategy:

- **Initial stop** (``initial_plan``): a protective stop ``stop_atr_k × ATR``
  below the entry. This is what drives risk-based sizing and the stop attached
  to the IG order — it is chosen here, not by the entry strategy.
- **No fixed take-profit** by default (``target_level = 0``): the position
  rides the move and exits via the trailing stop or the end-of-day force-close.
- **Trailing** (``evaluate``): an ATR chandelier stop that only ever ratchets
  up, trailing ``k × ATR`` below the running high. ``k`` may differ before and
  after break-even (``atr_k_pre`` / ``atr_k_post``).

The actual maths lives in the shared pure helpers ``decide_close_reason`` and
``compute_trailing_stop``. They are imported transitionally from
``src.services.trading``; the execution-layer extraction (plan phase 3) will
relocate them into this domain and flip the dependency so ``trading`` imports
from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.base import (
    ACTION_CLOSE,
    ACTION_HOLD,
    ACTION_UPDATE_STOP,
    CloseDecision,
    CloseProfile,
    OpenPlan,
)
from src.services.compute import atr
from src.services.price_buffer import EpicBuffer

# Transitional import (see module docstring): these pure functions move into
# the exit domain in the execution-layer phase.
from src.services.trading import compute_trailing_stop, decide_close_reason


@dataclass
class AtrTrailingExit(CloseProfile):
    """ATR-based protective stop that trails the position upward."""

    name = "atr_trailing"

    atr_period: int = 14
    stop_atr_k: float = 2.5  # initial protective stop distance, in ATR multiples
    atr_k_pre: float = 2.5  # trailing distance (ATR multiples) before break-even
    atr_k_post: float = 1.5  # trailing distance after break-even
    trailing_step_ratio: float = 0.3  # min advance (× ATR) before re-pushing stop

    @classmethod
    def from_settings(cls, settings) -> AtrTrailingExit:
        return cls(
            atr_period=settings.strategy_atr_period,
            stop_atr_k=settings.strategy_donchian_stop_atr_k,
            atr_k_pre=settings.strategy_atr_k_pre,
            atr_k_post=settings.strategy_atr_k_post,
            trailing_step_ratio=settings.strategy_trailing_step_ratio,
        )

    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Place the initial stop ``stop_atr_k × ATR`` away from the entry."""
        last = buf.last
        atr_value = atr(list(buf.candles), self.atr_period)
        distance = self.stop_atr_k * atr_value
        if direction == "SELL":
            offer = last.offer_close if last else entry_level
            return OpenPlan(
                stop_level=offer + distance,
                level_zero=entry_level - (last.spread if last else 0.0),
                target_level=0.0,
                profile=self.name,
            )
        # BUY (and default)
        offer = last.offer_close if last else entry_level
        return OpenPlan(
            stop_level=entry_level - distance,
            level_zero=offer,
            target_level=0.0,
            profile=self.name,
        )

    def evaluate(
        self, position, current_bid: float, buf: EpicBuffer, *, is_close_hour: bool
    ) -> CloseDecision:
        """Close on target/stop/end-of-day, otherwise ratchet the trailing stop."""
        reason = decide_close_reason(
            current_bid,
            level_win=float(position.level_win or 0),
            level_loose=float(position.level_loose or 0),
            is_close_hour=is_close_hour,
        )
        if reason is not None:
            return CloseDecision(action=ACTION_CLOSE, reason=reason)

        level_open = float(position.level_open or 0)
        if current_bid > level_open and buf.last is not None:
            new_stop = compute_trailing_stop(
                current_bid,
                atr_value=atr(list(buf.candles), self.atr_period),
                spread=buf.last.spread,
                level_zero=float(position.level_zero or 0),
                level_follower=float(position.level_follower or 0),
                euro_per_point=float(position.euro_per_point or 0),
                euro_stop=abs(float(position.euro_stop or 0)),
                config=self,
            )
            if new_stop is not None:
                return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)

        return CloseDecision(action=ACTION_HOLD)
