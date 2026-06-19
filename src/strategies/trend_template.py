"""Hourly trend-template selector — rank every market, open the best one.

Unlike the per-epic strategies, this one is meant to be driven **once per hour**
by the scheduler's ``trend_select`` job, which scores every tradable epic and
opens the single highest-ranked one. This module owns the *per-epic* half of
that decision: given one epic's buffer it answers "how close is this curve to a
clean, reachable up-trend with a tight spread?", returning a signal whose
``score`` (in [0, 1]) is the cross-epic ranking key.

The criteria are **soft scoring components, not pass/fail gates**: the goal is
to open one epic every hour, so the strategy never rejects a market on a single
criterion — it ranks them and lets the scheduler open the best available. The
only reasons :meth:`evaluate` returns ``None`` are *structural*: too little data
to compute the curve, or a non-positive ATR (no way to size a stop).

Scoring — each component is normalized to [0, 1] (higher = closer to ideal) and
combined with **R²-dominant** weights so the trend shape drives the ranking and
spread/reachability act as tie-breakers:

1. **Shape (dominant).** A linear regression over the last ``regression_period``
   bid closes. The raw ``r_squared`` (closeness to a straight line, 1.0 =
   perfect) is the shape score *when* the slope is positive and the fit clears
   ``min_r2`` — i.e. it is a genuine up-trend; otherwise the shape score is 0 so
   a flat or falling market only ranks on the secondary criteria.

2. **Spread tightness.** ``1 - (spread / bid) / max_spread_ratio`` clamped to
   [0, 1]: 1 at zero spread, 0 once the spread reaches the configured ceiling.

3. **Reachability.** The win target is a price rise of ``spread × (1 +
   win_ratio)`` from the entry bid. The fraction of that distance the fitted
   slope is projected to cover over ``projection_horizon`` candles (≈ one hour
   on 1-minute data), clamped to [0, 1] (1 = fully reachable within the hour).

**Levels.** Take-profit ``level_win = bid + spread + win_ratio × spread``.
Protective stop: the **support of the last hour** — the lowest bid low over
``stop_lookback`` candles (≈ 60 min) — minus a small ``stop_buffer_atr_k`` ATR
cushion. The stop sits at the genuine support however far it is (no ATR distance
cap: capping pulled it back up too close to the entry and caused noise
stop-outs). When that support sits at/above the entry (price at fresh lows), the
stop falls back to an ATR-sized distance just below the bid so a valid stop
always exists — the strategy must be able to open *some* epic every hour, and
the ``euro_loss_max`` open gate bounds the resulting downside.
``level_follower``/``level_loose``/``level_security`` are pinned to that stop so
the shared trailing logic only ratchets it up.

The strategy is BUY-only and sets a real ``level_win``; positions exit on the
fast take-profit, the trailing ATR stop (a stop-out below entry is the "loser"
that drives the next hour's martingale size), or the end-of-day force close.

``hourly_selection = True`` tells the scheduler to skip the per-epic auto-open
loop and drive opens through the ``trend_select`` job instead.

Documented in ``docs/strategies/trend-template.md``.
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


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def projected_reachable(slope: float, distance: float, horizon: int) -> bool:
    """Whether a fitted up-slope reaches ``distance`` within ``horizon`` candles.

    The regression slope is the average price gain per candle; extrapolated over
    ``horizon`` candles it must cover the target ``distance`` (entry → take-profit).

    Args:
        slope: Price gain per candle from the linear regression (points/candle).
        distance: Price distance from entry to the take-profit, in points.
        horizon: Number of candles ahead to project (≈ minutes on 1-minute data).

    Returns:
        True when ``slope × horizon >= distance`` (and the slope is positive).
    """
    if slope <= 0 or horizon <= 0:
        return False
    return slope * horizon >= distance


@dataclass
class TrendTemplate(BaseStrategy):
    """Rank epics by closeness to a theoretical up-trend; open the best hourly."""

    name = "trend_template"
    hourly_selection = True

    regression_period: int = 30  # candles for the R² fit
    min_r2: float = 0.80  # min R² to count as a clean up-trend
    win_ratio: float = 2.0  # take-profit in net spread multiples
    projection_horizon: int = 60  # candles (~1h) to reach the target
    stop_lookback: int = 60  # support window — the last hour (candles)
    stop_buffer_atr_k: float = 0.5  # ATR cushion below the detected support
    atr_period: int = 14
    max_spread_ratio: float = 0.0015

    # Composite-score weights — R²-dominant: the trend shape drives the ranking,
    # spread tightness and reachability are secondary tie-breakers. They sum to
    # 1.0 so the resulting ``score`` stays in [0, 1].
    weight_shape: float = 0.60
    weight_spread: float = 0.25
    weight_reach: float = 0.15

    @property
    def warmup(self) -> int:
        return (
            max(
                self.regression_period,
                self.stop_lookback,
                self.atr_period,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> TrendTemplate:
        return cls(
            regression_period=settings.strategy_trend_template_regression_period,
            min_r2=settings.strategy_trend_template_min_r2,
            win_ratio=settings.strategy_trend_template_win_ratio,
            projection_horizon=settings.strategy_trend_template_projection_horizon,
            stop_lookback=settings.strategy_trend_template_stop_lookback,
            stop_buffer_atr_k=settings.strategy_trend_template_stop_buffer_atr_k,
            atr_period=settings.strategy_atr_period,
            max_spread_ratio=settings.strategy_max_spread_ratio,
        )

    def evaluate(self, epic: str, buf: EpicBuffer) -> TradingSignal | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough data to score the curve
        last = candles[-1]
        bid = last.bid_close
        offer = last.offer_close
        spread = last.spread
        if bid <= 0 or spread <= 0:
            return None

        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None  # cannot size a protective stop without volatility

        bids = buf.bid_closes
        reg = linear_regression(bids[-self.regression_period :])

        # Take-profit distance: from the entry bid up to ``level_win``.
        target_distance = spread * (1 + self.win_ratio)

        # --- Soft scoring components (NOT gates), each normalized to [0, 1] ---
        # The criteria rank markets rather than reject them: the scheduler opens
        # the single best-scored epic each hour, whatever its absolute score.
        #
        # Shape (dominant): raw R² when the line genuinely rises (positive slope
        # clearing ``min_r2``); 0 otherwise so a flat/falling market only ranks
        # on the secondary criteria.
        shape = reg.r_squared if reg.slope > 0 and reg.r_squared >= self.min_r2 else 0.0
        # Spread tightness: 1 at zero spread, 0 at/above the configured ceiling.
        spread_quality = _clamp01(1.0 - (spread / bid) / self.max_spread_ratio)
        # Reachability: fraction of the target the fitted slope covers over the
        # projection horizon, capped at 1 (= fully reachable within the hour).
        reach = (
            _clamp01(reg.slope * self.projection_horizon / target_distance)
            if reg.slope > 0 and target_distance > 0
            else 0.0
        )
        score = (
            self.weight_shape * shape
            + self.weight_spread * spread_quality
            + self.weight_reach * reach
        )

        # Protective stop — the support of the last hour: the lowest bid low over
        # ``stop_lookback`` candles (≈ 60 min on 1-minute data), with a small ATR
        # cushion just below it so a wick back to that low does not stop us out.
        # There is deliberately NO ATR distance cap: capping pulled the stop back
        # up close to the entry whenever the real support sat further away, which
        # made it too tight and prone to noise stop-outs. The stop sits at the
        # genuine support, however far that is — the ``euro_loss_max`` gate in
        # ``open_position`` is what bounds the resulting downside.
        support = min(c.bid_low for c in candles[-self.stop_lookback :])
        stop_level = support - self.stop_buffer_atr_k * atr_value
        if stop_level >= bid:
            # Degenerate: the support sits at/above the entry (price at fresh
            # lows). Fall back to an ATR-sized stop just below the bid so a valid
            # stop always exists — the strategy must be able to open every hour.
            stop_level = bid - max(self.stop_buffer_atr_k, 1.0) * atr_value
        stop_distance = bid - stop_level

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

        logger.debug(
            "TrendTemplate %s: score=%.3f (R²=%.3f slope=%.6f spread_q=%.2f "
            "reach=%.2f) target=%.5f stop=%.5f",
            epic,
            score,
            reg.r_squared,
            reg.slope,
            spread_quality,
            reach,
            level_win,
            stop_level,
        )
        return TradingSignal(
            epic=epic,
            score=score,  # ranking key: highest composite score wins the hour
            direction="BUY",
            regression=reg,
            sma_fast=0.0,
            sma_slow=0.0,
            roc=rate_of_change(bids, self.regression_period),
            spread=spread,
            avg_spread=sum(buf.spreads) / len(buf),
            position_in_range=position_in_range(bid, high, low),
            levels=levels,
        )
