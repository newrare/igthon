"""High-frequency momentum scalper — trade volume, grab a spread-multiple, run.

The thesis is the opposite of the trend followers: do not wait for a big move.
Open **many** short-lived trades and, the moment the move is worth clearly more
than the spread paid to get in, take the profit. The spread is the enemy, so it
is also the unit everything is measured in.

Mechanics:

1. **Entry** — only on fresh upward momentum, on two horizons:
   - *recent*: the rate of change over ``momentum_period`` candles must reach
     ``min_roc`` (the move is already going our way);
   - *very recent*: the last ``confirm_period`` closes must each be up, so we
     buy a live up-tick rather than a stalling one (the "last minutes" filter).

2. **Take-profit** — a fixed target ``win_ratio`` spreads of *net* profit above
   break-even: ``level_win = bid + spread + win_ratio × spread``. The first
   ``spread`` covers the round-trip cost (a BUY fills at the offer and exits at
   the bid), the ``win_ratio × spread`` on top is the gain we grab immediately.
   ``decide_close_reason`` fires the ``win`` close as soon as the bid reaches it.

3. **Smart stop** — the lowest bid low over the last ``stop_lookback`` candles
   (≈ the past hour on 1-minute data) is the level the market has defended; the
   protective stop sits one ``stop_buffer_atr_k`` ATR below it. To stop a far
   support from wrecking the reward/risk of a spread-sized target, the stop
   distance is capped at ``max_stop_atr_k`` ATR.

Quality gate applied **before** the momentum check:

- **Spread gate** — ``spread / bid`` must stay under ``max_spread_ratio``. A
  scalp's edge is a couple of spreads wide; a wide spread eats it whole.

The strategy is BUY-only (the live pipeline opens BUY only). It sets a real
``level_win`` (unlike the breakout followers), so the position normally exits on
the fast take-profit; the support stop and the end-of-day force close are the
fallbacks. ``level_follower`` is pinned to the support stop so the shared
``follower`` trailing logic only ever ratchets it up.

Documented in ``docs/strategies/momentum-scalper.md``.
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
class MomentumScalper(BaseStrategy):
    """Buy fresh up-ticks, take a spread-multiple profit, stop under support."""

    name = "momentum_scalper"

    momentum_period: int = 5  # recent-trend ROC window (candles)
    min_roc: float = 0.02  # min ROC over the window, in percent
    confirm_period: int = 2  # very-recent rising-closes confirmation
    win_ratio: float = 1.5  # take-profit in *net* spread multiples
    stop_lookback: int = 60  # support-detection window (≈ last hour)
    stop_buffer_atr_k: float = 0.5  # ATR buffer below the detected support
    max_stop_atr_k: float = 3.0  # cap on the stop distance, in ATR multiples
    atr_period: int = 14
    max_spread_ratio: float = 0.0015

    @property
    def warmup(self) -> int:
        return (
            max(
                self.momentum_period,
                self.confirm_period,
                self.stop_lookback,
                self.atr_period,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> MomentumScalper:
        return cls(
            momentum_period=settings.strategy_scalper_momentum_period,
            min_roc=settings.strategy_scalper_min_roc,
            confirm_period=settings.strategy_scalper_confirm_period,
            win_ratio=settings.strategy_scalper_win_ratio,
            stop_lookback=settings.strategy_scalper_stop_lookback,
            stop_buffer_atr_k=settings.strategy_scalper_stop_buffer_atr_k,
            max_stop_atr_k=settings.strategy_scalper_max_stop_atr_k,
            atr_period=settings.strategy_atr_period,
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
        if bid <= 0 or spread <= 0:
            return None

        # Gate 1 — spread: the scalp's whole edge is a couple of spreads wide.
        if spread / bid > self.max_spread_ratio:
            return None

        bids = buf.bid_closes

        # Gate 2 — recent momentum: the move must already be running our way.
        roc = rate_of_change(bids, self.momentum_period)
        if roc < self.min_roc:
            return None

        # Gate 3 — very-recent confirmation: only buy a live up-tick, so every
        # one of the last ``confirm_period`` closes must be higher than the one
        # before it (rejects a stalling or rolling-over move).
        recent = bids[-self.confirm_period - 1 :]
        if any(b <= a for a, b in zip(recent, recent[1:])):
            return None

        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        # Smart stop — support detection: the lowest bid low over the last
        # ``stop_lookback`` candles is the level the market has defended; sit
        # the protective stop one ATR buffer below it.
        support = min(c.bid_low for c in candles[-self.stop_lookback :])
        stop_level = support - self.stop_buffer_atr_k * atr_value

        # Cap the distance: a support far below price would ruin the
        # reward/risk of a spread-sized target, so never risk more than
        # ``max_stop_atr_k`` ATR.
        max_distance = self.max_stop_atr_k * atr_value
        if bid - stop_level > max_distance:
            stop_level = bid - max_distance

        # A degenerate flat market can leave the stop at/above the bid.
        if stop_level >= bid:
            return None
        stop_distance = bid - stop_level

        # Fixed take-profit: net ``win_ratio`` spreads above break-even.
        level_win = bid + spread + self.win_ratio * spread

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

        reg = linear_regression(bids[-self.momentum_period :])
        logger.debug(
            "Scalper BUY on %s: bid=%.5f ROC=%.3f%% target=%.5f stop=%.5f (-%.5f)",
            epic,
            bid,
            roc,
            level_win,
            stop_level,
            stop_distance,
        )
        return TradingSignal(
            epic=epic,
            score=roc,  # momentum strength is the natural score here
            direction="BUY",
            regression=reg,
            sma_fast=0.0,
            sma_slow=0.0,
            roc=roc,
            spread=spread,
            avg_spread=sum(buf.spreads) / len(buf),
            position_in_range=position_in_range(bid, high, low),
            levels=levels,
        )
