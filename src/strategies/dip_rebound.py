"""Dip-rebound — buy the pullback inside a rising market, ride the bounce.

The thesis: a market that is *globally trending up* but has *just suffered a
significant drop* tends to resume its climb. Rather than chase fresh highs (the
momentum scalper's job) this strategy waits for the dip and opens the moment the
price turns back up, capturing the rebound from a better entry.

Mechanics:

1. **Spread gate.** ``spread / bid`` must stay under ``max_spread_ratio`` — the
   rebound's edge is eaten by a wide spread, exactly as for the scalper.

2. **Global up-trend.** A linear regression over the last ``trend_period``
   candles must have a *positive* slope and an ``r_squared`` of at least
   ``min_trend_r2``. The R² threshold is deliberately looser than a pure
   trend-follower's: a pullback dents the fit, so demanding a near-perfect line
   would reject the very setups this strategy exists to trade.

3. **Significant recent drop.** Within the last ``pullback_lookback`` candles the
   market printed a swing high (``recent_high`` = highest bid close). The recent
   dip bottom (``swing_low`` = lowest bid low over ``stop_lookback`` candles) must
   sit at least ``min_pullback_atr_k`` ATR below that high — a drop large enough
   to be a real pullback, not noise. The current bid must still be **below** the
   recent high, so there is room left to rebound (we are not buying the top).

4. **Rebound underway.** Each of the last ``rebound_period`` bid closes must be
   higher than the one before it — the bounce off the dip has actually started,
   so we buy a live up-tick rather than a knife still falling.

Levels:

- **Stop** — one ``stop_buffer_atr_k`` ATR below the dip bottom (``swing_low``):
  the level whose break would invalidate the rebound thesis. ``level_follower`` /
  ``level_loose`` / ``level_security`` are pinned to it so the shared trailing
  logic only ratchets it up.
- **Take-profit** — risk-based: ``level_win = bid + win_ratio × stop_distance``,
  where ``stop_distance = bid − stop_level``. A rebound has a natural floor (the
  dip low) so a reward/risk target is the honest way to size the win; the
  position otherwise exits via the trailing stop or the end-of-day force close.

The strategy is BUY-only (the live pipeline opens BUY only) and per-epic
(immediate-open path — ``hourly_selection`` stays False). Its ``score`` is the
pullback depth in ATR, so a deeper-but-recovering dip ranks above a shallow one.

Documented in ``docs/strategies/dip-rebound.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import (
    TradingLevels,
    TradingSignal,
    atr,
    linear_regression,
    position_in_range,
    rate_of_change,
)
from src.feed.price_buffer import EpicBuffer
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class DipRebound(BaseStrategy):
    """Buy a significant dip inside a rising market, the moment it turns up."""

    name = "dip_rebound"

    trend_period: int = 60  # candles for the global up-trend regression
    min_trend_r2: float = 0.55  # min R² for a genuine (if dented) up-trend
    pullback_lookback: int = 30  # window for the recent swing high (candles)
    min_pullback_atr_k: float = 1.5  # dip depth below the high, in ATR multiples
    rebound_period: int = 2  # trailing rising closes confirming the bounce
    win_ratio: float = 2.0  # take-profit in reward/risk multiples
    stop_lookback: int = 10  # window for the dip bottom — the stop anchor
    stop_buffer_atr_k: float = 0.5  # ATR cushion below the dip bottom
    atr_period: int = 14
    max_spread_ratio: float = 0.0015

    @property
    def warmup(self) -> int:
        return (
            max(
                self.trend_period,
                self.pullback_lookback,
                self.stop_lookback,
                self.rebound_period,
                self.atr_period,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> DipRebound:
        return cls(
            trend_period=settings.strategy_dip_rebound_trend_period,
            min_trend_r2=settings.strategy_dip_rebound_min_trend_r2,
            pullback_lookback=settings.strategy_dip_rebound_pullback_lookback,
            min_pullback_atr_k=settings.strategy_dip_rebound_min_pullback_atr_k,
            rebound_period=settings.strategy_dip_rebound_rebound_period,
            win_ratio=settings.strategy_dip_rebound_win_ratio,
            stop_lookback=settings.strategy_dip_rebound_stop_lookback,
            stop_buffer_atr_k=settings.strategy_dip_rebound_stop_buffer_atr_k,
            atr_period=settings.strategy_atr_period,
            max_spread_ratio=settings.strategy_max_spread_ratio,
        )

    def evaluate(self, epic: str, buf: EpicBuffer) -> TradingSignal | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough data to judge trend + pullback
        last = candles[-1]
        bid = last.bid_close
        offer = last.offer_close
        spread = last.spread
        if bid <= 0 or spread <= 0:
            return None

        # Gate 1 — spread: the rebound's edge is eaten by a wide spread.
        if spread / bid > self.max_spread_ratio:
            return None

        bids = buf.bid_closes

        # Gate 2 — global up-trend: positive slope, decent (if dented) R². The R²
        # bar is looser than a trend-follower's because the very pullback we want
        # to trade reduces the fit of a straight line.
        reg = linear_regression(bids[-self.trend_period :])
        if reg.slope <= 0 or reg.r_squared < self.min_trend_r2:
            return None

        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        # Gate 3 — significant recent drop: the dip bottom must sit a real
        # distance below the recent swing high, and the bid must still be below
        # that high so a rebound has room to run.
        recent_high = max(c.bid_close for c in candles[-self.pullback_lookback :])
        swing_low = min(c.bid_low for c in candles[-self.stop_lookback :])
        pullback_depth = recent_high - swing_low
        if pullback_depth < self.min_pullback_atr_k * atr_value:
            return None
        if bid >= recent_high:
            return None  # already fully recovered — the rebound is over

        # Gate 4 — rebound underway: only buy once the bounce off the dip has
        # started, i.e. each of the last ``rebound_period`` closes is rising.
        recent = bids[-self.rebound_period - 1 :]
        if any(b <= a for a, b in zip(recent, recent[1:])):
            return None

        # Stop — one ATR cushion below the dip bottom; breaking it invalidates
        # the rebound thesis. Pinned levels keep the trailing logic ratchet-only.
        stop_level = swing_low - self.stop_buffer_atr_k * atr_value
        if stop_level >= bid:
            return None  # degenerate: dip bottom at/above the entry
        stop_distance = bid - stop_level

        # Take-profit — risk-based: ``win_ratio`` times the distance to the stop.
        level_win = bid + self.win_ratio * stop_distance

        high = max(c.bid_high for c in candles)
        low = min(c.bid_low for c in candles)

        levels = TradingLevels(
            bid=bid,
            offer=offer,
            spread=spread,
            high=high,
            low=low,
            scope=high - low,
            average=sum(bids) / len(bids),
            level_follower=stop_level,
            level_win=level_win,
            level_zero=offer,
            level_loose=stop_level,
            level_security=stop_level,
            stop_distance=stop_distance,
        )

        # Pullback depth in ATR is the natural "how big a rebound" ranking score.
        depth_atr = pullback_depth / atr_value
        logger.debug(
            "DipRebound BUY %s: bid=%.5f depth=%.2fATR win=%.5f stop=%.5f (-%.5f)",
            epic,
            bid,
            depth_atr,
            level_win,
            stop_level,
            stop_distance,
        )
        return TradingSignal(
            epic=epic,
            score=depth_atr,
            direction="BUY",
            regression=reg,
            sma_fast=0.0,
            sma_slow=0.0,
            roc=rate_of_change(bids, self.rebound_period),
            spread=spread,
            avg_spread=sum(buf.spreads) / len(buf),
            position_in_range=position_in_range(bid, high, low),
            levels=levels,
        )
