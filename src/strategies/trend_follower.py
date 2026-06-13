"""Trend-follower strategy — the project's original live strategy.

Thin adapter exposing :func:`src.services.compute.compute_signal` (composite
score: linear regression + R² + SMA crossover + ROC, long-only) through the
pluggable :class:`~src.strategies.base.BaseStrategy` interface. The signal
mathematics are unchanged and stay in ``compute.py``; this class only carries
the parameters and the settings mapping that used to live in the scheduler and
the simulator.

Documented in ``docs/strategies/trend-follower.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.services.compute import TradingSignal, compute_signal
from src.services.price_buffer import EpicBuffer
from src.strategies.base import BaseStrategy


@dataclass
class TrendFollower(BaseStrategy):
    """Composite-score trend confirmation (regression + SMA + ROC), BUY-only."""

    name = "trend_follower"

    regression_period: int = 20
    sma_fast_period: int = 5
    sma_slow_period: int = 20
    roc_period: int = 10
    min_r2: float = 0.70
    min_score: float = 0.75
    max_spread_ratio: float = 0.0015
    stop_multiplier: float = 2.5
    target_multiplier: float = 4.0
    tactic: str = "spread"

    @property
    def warmup(self) -> int:
        return self.sma_slow_period

    @classmethod
    def from_settings(cls, settings) -> TrendFollower:
        """Same settings mapping the scheduler and simulator used historically."""
        return cls(
            regression_period=settings.strategy_lookback_points,
            sma_fast_period=settings.strategy_sma_fast,
            sma_slow_period=settings.strategy_sma_slow,
            roc_period=settings.strategy_roc_period,
            min_r2=settings.strategy_min_r2,
            min_score=settings.strategy_min_score,
            max_spread_ratio=settings.strategy_max_spread_ratio,
            stop_multiplier=settings.strategy_stop_multiplier,
            target_multiplier=settings.strategy_target_multiplier,
            tactic=settings.strategy_tactic,
        )

    def evaluate(self, epic: str, buf: EpicBuffer) -> TradingSignal | None:
        return compute_signal(
            epic,
            buf,
            regression_period=self.regression_period,
            sma_fast_period=self.sma_fast_period,
            sma_slow_period=self.sma_slow_period,
            roc_period=self.roc_period,
            min_r2=self.min_r2,
            min_score=self.min_score,
            max_spread_ratio=self.max_spread_ratio,
            follower_mult=self.stop_multiplier,
            win_mult=self.target_multiplier,
            loose_mult=self.stop_multiplier * 3,
            security_mult=self.stop_multiplier * 2,
            tactic=self.tactic,
        )
