"""ATR trailing-stop variant that only ratchets freely once in profit.

:class:`AtrTrailingPositiveExit` reuses :class:`~src.exit.atr_trailing.AtrTrailingExit`
for the initial stop placement, but splits the per-tick management into two
regimes around break-even:

- **Secured regime** — the prospective chandelier stop already sits at or above
  the entry (``bid - margin >= entry``, i.e. the stop would close the position at
  zero euro or better). Here the profile behaves exactly like ``atr_trailing``:
  the stop trails ``k x ATR`` below price and only ever ratchets up.
- **Underwater regime** — the prospective stop is still below the entry. Instead
  of blindly holding the initial stop, the profile reads the trend SINCE THE
  POSITION OPENED (a linear regression of the bids) and steers the stop by the
  slope, normalised to ATR-per-candle:

  =============== ====================================================
  slope regime    action
  =============== ====================================================
  flat            keep the initial stop (room without extra risk)
  bullish         trail the stop up like the secured regime
  bearish soft    lower the stop one notch (bounded) — the dip may revert
  bearish steep   tighten the stop up to cut the loss, keeping a noise gap
  =============== ====================================================

The downward move (bearish soft) is bounded: the stop can never sink below
``initial_stop - max_extra_k x ATR``, so the loss stays capped. The upward moves
(bullish / bearish steep) only ever increase the stop. All the actual close
filling is delegated to the broker (the pushed stop level), exactly like the
reference profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.core.indicators import atr, linear_regression
from src.exit.atr_trailing import AtrTrailingExit
from src.exit.base import (
    ACTION_CLOSE,
    ACTION_HOLD,
    ACTION_UPDATE_STOP,
    CloseDecision,
)
from src.exit.trailing import clamp_trailing_distance, compute_trailing_stop
from src.feed.price_buffer import EpicBuffer


def _opened_at(position) -> datetime | None:
    """Best-effort open timestamp, working for both live and simulated positions.

    The simulated trade carries an ``opened_at`` datetime; the live ORM position
    carries ``date`` + ``time_open``. Returns ``None`` when neither is available
    (the caller then falls back to the whole buffer).
    """
    dt = getattr(position, "opened_at", None)
    if isinstance(dt, datetime):
        return dt
    day = getattr(position, "date", None)
    open_time = getattr(position, "time_open", None)
    if day is not None and open_time is not None:
        return datetime.combine(day, open_time)
    return None


@dataclass
class AtrTrailingPositiveExit(AtrTrailingExit):
    """ATR trailing stop that only ratchets freely once the trade is secured."""

    name = "atr_trailing_positive"

    trend_period: int = 30  # max candles since open used for the regression
    trend_min_period: int = 5  # min candles since open before trusting the slope
    flat_slope_k: float = 0.015  # |slope|/ATR below this is treated as flat
    steep_slope_k: float = 0.05  # slope/ATR below -this is a steep down-trend
    down_step_ratio: float = 0.3  # notch the stop is lowered by (x ATR)
    max_extra_k: float = 1.0  # max ATR the stop may sink below the initial stop
    noise_k: float = 0.5  # steep-bear tight gap kept below price (x ATR)

    @classmethod
    def from_settings(cls, settings) -> AtrTrailingPositiveExit:
        # Parameters are constants of this class (the field defaults above plus
        # those inherited from AtrTrailingExit), so the profile builds from those
        # and ignores ``settings``. Tune by editing the constants; select the
        # profile at runtime from the dashboard.
        return cls()

    # initial_plan is inherited unchanged from AtrTrailingExit.

    def evaluate(
        self, position, current_bid: float, buf: EpicBuffer, *, is_close_hour: bool
    ) -> CloseDecision:
        """Two-regime exit: free ratchet when secured, trend-steered when not."""
        if is_close_hour:
            return CloseDecision(action=ACTION_CLOSE, reason="end_of_day")

        last = buf.last
        if last is None:
            return CloseDecision(action=ACTION_HOLD)
        atr_value = atr(list(buf.candles), self.atr_period)
        if atr_value <= 0:
            return CloseDecision(action=ACTION_HOLD)

        level_open = float(position.level_open or 0)
        level_follower = float(position.level_follower or 0)
        initial_stop = float(position.level_loose or level_follower)
        spread = last.spread
        euro_per_point = float(position.euro_per_point or 0)
        euro_stop = abs(float(position.euro_stop or 0))

        # Software backstop aligned with the *current* real stop (the follower),
        # never the stale initial level: the broker fills the pushed stop, this
        # only guarantees a close should that ever fail.
        if level_follower > 0 and current_bid <= level_follower:
            return CloseDecision(action=ACTION_CLOSE, reason="stop")

        # Is the position "secured"? The prospective chandelier stop sitting at
        # or above the entry means "bid - margin >= entry" → a positive close.
        distance = clamp_trailing_distance(
            self.atr_k_pre * atr_value,
            spread=spread,
            euro_per_point=euro_per_point,
            euro_stop=euro_stop,
        )
        secured = (current_bid - distance) >= level_open

        if secured:
            return self._ratchet_up(
                current_bid, atr_value, spread, position, level_follower
            )

        # Underwater: steer the stop by the trend since the position opened.
        slope = self._trend_slope(position, buf)
        if slope is None:
            return CloseDecision(action=ACTION_HOLD)  # too soon → keep initial
        slope_atr = slope / atr_value  # ATR per candle, scale-free

        if slope_atr >= self.flat_slope_k:
            # Bullish: recovery underway → trail up like the secured regime.
            return self._ratchet_up(
                current_bid, atr_value, spread, position, level_follower
            )
        if slope_atr > -self.flat_slope_k:
            return CloseDecision(action=ACTION_HOLD)  # flat → keep the stop
        if slope_atr > -self.steep_slope_k:
            # Soft down-trend: give the trade room, lower the stop one notch,
            # bounded so the loss can never exceed the planned risk + cushion.
            floor = initial_stop - self.max_extra_k * atr_value
            new_stop = max(level_follower - self.down_step_ratio * atr_value, floor)
            if new_stop < level_follower:
                return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)
            return CloseDecision(action=ACTION_HOLD)

        # Steep down-trend: stop the bleeding — pull the stop up toward price,
        # keeping a noise gap so bid/offer oscillation alone cannot trigger it.
        gap = max(self.noise_k * atr_value, spread * 2.0)
        new_stop = current_bid - gap
        if new_stop > level_follower:
            return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)
        return CloseDecision(action=ACTION_HOLD)

    def _ratchet_up(
        self,
        current_bid: float,
        atr_value: float,
        spread: float,
        position,
        level_follower: float,
    ) -> CloseDecision:
        """Standard ATR chandelier ratchet (shared by the secured/bullish paths)."""
        new_stop = compute_trailing_stop(
            current_bid,
            atr_value=atr_value,
            spread=spread,
            level_zero=float(position.level_zero or 0),
            level_follower=level_follower,
            euro_per_point=float(position.euro_per_point or 0),
            euro_stop=abs(float(position.euro_stop or 0)),
            config=self,
        )
        if new_stop is not None:
            return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)
        return CloseDecision(action=ACTION_HOLD)

    def _trend_slope(self, position, buf: EpicBuffer) -> float | None:
        """Slope of a linear fit on the bids since the position opened.

        Capped at ``trend_period`` candles and requiring at least
        ``trend_min_period`` since open; returns ``None`` when there is not yet
        enough history (the caller then keeps the initial stop).
        """
        candles = list(buf.candles)
        opened_at = _opened_at(position)
        if opened_at is not None:
            open_naive = opened_at.replace(tzinfo=None)
            since = [
                c for c in candles if c.timestamp.replace(tzinfo=None) >= open_naive
            ]
        else:
            since = candles
        if len(since) < self.trend_min_period:
            return None
        window = since[-self.trend_period :]
        return linear_regression([c.bid_close for c in window]).slope
