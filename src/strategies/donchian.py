"""Donchian breakout strategy gated by the Kaufman Efficiency Ratio.

Entry: the bid closes outside the prior ``channel``-period high/low band
(Donchian channel) → trade the breakout in that direction. Exit: no fixed
take-profit (``level_win = 0``) — the position rides the move and is closed by
the shared ATR trailing stop (``close_strategy = follower``), the protective
stop, or the end-of-day force close.

Quality gates applied **before** any entry, in order:

1. **Spread gate** — ``spread / bid`` must stay under ``max_spread_ratio``;
   a breakout cannot outrun a wide spread.
2. **Regime gate (the market-selection filter)** — the Kaufman Efficiency
   Ratio over ``efficiency_period`` candles must reach ``min_efficiency``.
   ER ≈ 1 means a clean directional path, ER ≈ 0 sideways chop. Backtests on
   synthetic curves show this gate turns the ranging regimes from bleeding
   (≈ −0.4 €/trade, 18 trades/epic/day of spread churn) to flat-or-positive
   (≈ 3 trades/epic/day) while leaving trending-regime profits intact —
   see ``docs/strategies/donchian-er.md`` for the full sweep.

The strategy emits both BUY and SELL signals; the live pipeline currently
opens BUY only (``evaluate_open_gates`` rejects SELL), so short breakouts are
ignored until short support lands in ``TradingService``.

Documented in ``docs/strategies/donchian-er.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import (
    TradingLevels,
    TradingSignal,
    atr,
    efficiency_ratio,
    linear_regression,
    position_in_range,
)
from src.feed.price_buffer import EpicBuffer
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class DonchianER(BaseStrategy):
    """N-period channel breakout, traded only when the market is trending."""

    name = "donchian_er"

    channel: int = 20  # Donchian lookback (prior candles forming the band)
    stop_atr_k: float = 2.5  # protective stop distance, in ATR multiples
    atr_period: int = 14
    efficiency_period: int = 30  # ER lookback window
    min_efficiency: float = 0.60  # regime gate threshold (0 disables)
    max_spread_ratio: float = 0.0015

    @property
    def warmup(self) -> int:
        return max(self.channel, self.efficiency_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> DonchianER:
        return cls(
            channel=settings.strategy_donchian_channel,
            stop_atr_k=settings.strategy_donchian_stop_atr_k,
            atr_period=settings.strategy_atr_period,
            efficiency_period=settings.strategy_efficiency_period,
            min_efficiency=settings.strategy_min_efficiency,
            max_spread_ratio=settings.strategy_max_spread_ratio,
        )

    def evaluate(self, epic: str, buf: EpicBuffer) -> TradingSignal | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None
        last = candles[-1]
        bid = last.bid_close
        offer = last.offer_close
        spread = last.spread
        if bid <= 0:
            return None

        # Gate 1 — spread: a breakout edge dies under a wide spread.
        if spread / bid > self.max_spread_ratio:
            return None

        # Gate 2 — regime: only arm the breakout on efficiently trending paths.
        er = efficiency_ratio(buf.mid_closes, self.efficiency_period)
        if er < self.min_efficiency:
            return None

        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        # Donchian band from the candles *before* the current one.
        prior = candles[-self.channel - 1 : -1]
        band_high = max(c.bid_high for c in prior)
        band_low = min(c.bid_low for c in prior)

        if bid > band_high:
            direction = "BUY"
        elif bid < band_low:
            direction = "SELL"
        else:
            return None

        stop_distance = self.stop_atr_k * atr_value
        high = max(c.bid_high for c in candles)
        low = min(c.bid_low for c in candles)
        bids = buf.bid_closes

        if direction == "BUY":
            # No fixed target (level_win=0): the ATR follower rides the trend.
            levels = TradingLevels(
                bid=bid,
                offer=offer,
                spread=spread,
                high=high,
                low=low,
                scope=high - low,
                average=sum(bids) / len(bids),
                level_follower=bid - stop_distance,
                level_win=0.0,
                level_zero=offer,
                level_loose=bid - stop_distance,
                level_security=bid - stop_distance,
                stop_distance=stop_distance,
            )
        else:
            # Mirrored levels for a short — provisional until the trading
            # service supports SELL orders (gates reject SELL today).
            levels = TradingLevels(
                bid=bid,
                offer=offer,
                spread=spread,
                high=high,
                low=low,
                scope=high - low,
                average=sum(bids) / len(bids),
                level_follower=offer + stop_distance,
                level_win=0.0,
                level_zero=bid - spread,
                level_loose=offer + stop_distance,
                level_security=offer + stop_distance,
                stop_distance=stop_distance,
            )

        # Regression over the channel window: informative only (logging/UI).
        reg = linear_regression(bids[-self.channel :])
        logger.debug(
            "Donchian %s on %s: bid=%.5f band=[%.5f, %.5f] ER=%.2f ATR=%.4f",
            direction,
            epic,
            bid,
            band_low,
            band_high,
            er,
            atr_value,
        )
        return TradingSignal(
            epic=epic,
            score=er,  # the regime quality is the natural "score" here
            direction=direction,
            regression=reg,
            sma_fast=0.0,
            sma_slow=0.0,
            roc=0.0,
            spread=spread,
            avg_spread=sum(buf.spreads) / len(buf),
            position_in_range=position_in_range(bid, high, low),
            levels=levels,
        )
