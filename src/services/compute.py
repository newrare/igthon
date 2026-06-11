"""Technical analysis and indicator calculations — ported from Compute.php.

Provides mathematical tools for trading decisions:
- Linear regression (slope + R²)
- Simple Moving Average (SMA)
- Rate of Change (ROC)
- Spread analysis
- Composite trading score
- Strategy level calculations
"""

import logging
import math
from dataclasses import dataclass

from src.services.price_buffer import Candle, EpicBuffer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RegressionResult:
    """Result of a linear regression."""

    slope: float
    intercept: float
    r_squared: float


@dataclass(slots=True)
class TradingLevels:
    """Computed trading levels for position management."""

    bid: float
    offer: float
    spread: float
    high: float
    low: float
    scope: float
    average: float
    level_follower: float
    level_win: float
    level_zero: float
    level_loose: float
    level_security: float
    stop_distance: float


@dataclass(slots=True)
class TradingSignal:
    """Composite signal with all indicators for trading decisions."""

    epic: str
    score: float
    direction: str  # "BUY" or "SELL" or "NEUTRAL"
    regression: RegressionResult
    sma_fast: float
    sma_slow: float
    roc: float
    spread: float
    avg_spread: float
    position_in_range: float
    levels: TradingLevels


def linear_regression(values: list[float]) -> RegressionResult:
    """Compute linear regression (slope, intercept, R²) on a list of values.

    Uses the least squares method.

    Args:
        values: Ordered list of numeric values (oldest first).

    Returns:
        RegressionResult with slope, intercept, and R².
    """
    n = len(values)
    if n < 2:
        return RegressionResult(slope=0.0, intercept=0.0, r_squared=0.0)

    # x = 0, 1, 2, ..., n-1
    sum_x = n * (n - 1) / 2
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6
    sum_y = sum(values)
    sum_xy = sum(i * v for i, v in enumerate(values))

    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        return RegressionResult(slope=0.0, intercept=values[0], r_squared=0.0)

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n

    # R² calculation
    mean_y = sum_y / n
    ss_tot = sum((v - mean_y) ** 2 for v in values)
    ss_res = sum((v - (slope * i + intercept)) ** 2 for i, v in enumerate(values))

    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return RegressionResult(slope=slope, intercept=intercept, r_squared=r_squared)


def sma(values: list[float], period: int) -> float:
    """Simple Moving Average over the last N values.

    Args:
        values: Ordered list of numeric values.
        period: Number of values to average.

    Returns:
        The average of the last `period` values, or 0 if insufficient data.
    """
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def rate_of_change(values: list[float], period: int) -> float:
    """Rate of Change (ROC): percentage change over N periods.

    ROC = (current - n_periods_ago) / n_periods_ago * 100

    Args:
        values: Ordered list of numeric values.
        period: Lookback period.

    Returns:
        ROC percentage, or 0 if insufficient data.
    """
    if len(values) <= period or values[-period - 1] == 0:
        return 0.0
    return (values[-1] - values[-period - 1]) / values[-period - 1] * 100


def atr(candles: list[Candle], period: int = 14) -> float:
    """Average True Range over the last ``period`` candles (bid OHLC).

    The True Range of a candle captures its real movement, including overnight
    gaps relative to the previous close:

        TR_i = max(high - low, |high - prev_close|, |low - prev_close|)

    ATR is the simple mean of the True Ranges over ``period``. It is expressed
    in price points and is the natural measure of "market noise" used to size a
    trailing stop so it sits beyond normal oscillation.

    Args:
        candles: Ordered candles (oldest first); needs at least ``period`` + 1.
        period: Number of true-range values to average.

    Returns:
        ATR in price points, or 0.0 if there is insufficient data.
    """
    if period < 1 or len(candles) < period + 1:
        return 0.0

    true_ranges: list[float] = []
    for prev, current in zip(candles[-period - 1 :], candles[-period:]):
        prev_close = prev.bid_close
        tr = max(
            current.bid_high - current.bid_low,
            abs(current.bid_high - prev_close),
            abs(current.bid_low - prev_close),
        )
        true_ranges.append(tr)

    return sum(true_ranges) / period


def position_in_range(current: float, high: float, low: float) -> float:
    """Calculate where the current price sits in the day range (0-100%).

    Args:
        current: Current bid price.
        high: Day high.
        low: Day low.

    Returns:
        Percentage position (0 = at low, 100 = at high).
    """
    if high == low:
        return 50.0
    return (current - low) / (high - low) * 100


def compute_levels(
    bid: float,
    offer: float,
    high: float,
    low: float,
    bids: list[float],
    *,
    follower_mult: float = 3.0,
    win_mult: float = 3.0,
    loose_mult: float = 8.0,
    security_mult: float = 5.0,
    tactic: str = "spread",
) -> TradingLevels:
    """Compute trading levels from current market data.

    Ported from Compute.php toolTactic logic.

    Args:
        bid: Current bid price.
        offer: Current offer price.
        high: Day high.
        low: Day low.
        bids: List of historical bid values.
        follower_mult: Multiplier for trailing stop distance.
        win_mult: Multiplier for take-profit distance.
        loose_mult: Multiplier for stop-loss distance.
        security_mult: Multiplier for security stop distance.
        tactic: Level calculation mode ("spread", "percentage", or "point").

    Returns:
        Computed TradingLevels.
    """
    spread = offer - bid
    scope = high - low
    pct_unit = scope / 100 if scope > 0 else spread
    average = sum(bids) / len(bids) if bids else bid

    # Calculate base unit depending on tactic
    if tactic == "spread":
        base = spread
    elif tactic == "percentage":
        base = pct_unit
    else:  # "point"
        base = 1.0

    follower_dist = follower_mult * base
    win_dist = win_mult * base
    loose_dist = loose_mult * base
    security_dist = security_mult * base

    return TradingLevels(
        bid=bid,
        offer=offer,
        spread=spread,
        high=high,
        low=low,
        scope=scope,
        average=average,
        level_follower=bid - follower_dist,
        level_win=bid + spread + win_dist,
        level_zero=bid + spread,
        level_loose=bid - loose_dist,
        level_security=bid - loose_dist - security_dist,
        stop_distance=math.ceil(spread + loose_dist + security_dist),
    )


def compute_signal(
    epic: str,
    buf: EpicBuffer,
    *,
    regression_period: int = 20,
    sma_fast_period: int = 5,
    sma_slow_period: int = 20,
    roc_period: int = 10,
    min_r2: float = 0.70,
    min_score: float = 0.75,
    max_spread_ratio: float = 0.0015,
    follower_mult: float = 3.0,
    win_mult: float = 3.0,
    loose_mult: float = 8.0,
    security_mult: float = 5.0,
    tactic: str = "spread",
) -> TradingSignal | None:
    """Compute a composite trading signal for an epic.

    Combines linear regression, SMA crossover, ROC, and spread analysis
    into a single score used to decide whether to open a position.

    Args:
        epic: Market identifier.
        buf: EpicBuffer with candle data.
        regression_period: Number of candles for regression.
        sma_fast_period: Fast SMA period.
        sma_slow_period: Slow SMA period.
        roc_period: ROC lookback period.
        min_r2: Minimum R² to consider trend valid.
        min_score: Minimum composite score to generate a BUY signal.
        max_spread_ratio: Maximum acceptable spread/bid ratio.
        follower_mult: Multiplier for trailing stop.
        win_mult: Multiplier for take-profit.
        loose_mult: Multiplier for stop-loss.
        security_mult: Multiplier for security stop.
        tactic: Level calculation mode.

    Returns:
        TradingSignal or None if insufficient data.
    """
    if len(buf) < sma_slow_period:
        logger.debug("Not enough data for %s (%d candles)", epic, len(buf))
        return None

    bids = buf.bid_closes
    last_candle = buf.last
    if last_candle is None:
        return None

    bid = last_candle.bid_close
    offer = last_candle.offer_close
    spread = last_candle.spread

    # High/low from buffer
    high = max(c.bid_high for c in buf.candles)
    low = min(c.bid_low for c in buf.candles)

    # 1. Linear regression
    reg_values = bids[-regression_period:]
    reg = linear_regression(reg_values)

    # 2. SMA
    sma_f = sma(bids, sma_fast_period)
    sma_s = sma(bids, sma_slow_period)

    # 3. ROC
    roc_val = rate_of_change(bids, roc_period)

    # 4. Spread ratio
    avg_spread = sum(buf.spreads) / len(buf) if len(buf) > 0 else spread
    spread_ratio = spread / bid if bid > 0 else 1.0

    # 5. Position in range
    pos_range = position_in_range(bid, high, low)

    # Composite score (weights)
    w_slope = 0.30
    w_r2 = 0.25
    w_roc = 0.25
    w_sma = 0.20

    # Normalize slope: positive = good, cap at 1.0
    scope = high - low if high != low else 1.0
    slope_norm = min(max(reg.slope * regression_period / scope, -1.0), 1.0)

    # SMA signal: 1 if golden cross, 0 otherwise
    sma_signal = 1.0 if sma_f > sma_s else 0.0

    # ROC normalized: cap at ±1.0
    roc_norm = min(max(roc_val / 1.0, -1.0), 1.0)

    score = (
        w_slope * max(slope_norm, 0)
        + w_r2 * reg.r_squared
        + w_roc * max(roc_norm, 0)
        + w_sma * sma_signal
    )

    # Direction
    if (
        score >= min_score
        and reg.r_squared >= min_r2
        and spread_ratio <= max_spread_ratio
    ):
        direction = "BUY"
    elif slope_norm < -0.3 and reg.r_squared >= min_r2:
        direction = "SELL"
    else:
        direction = "NEUTRAL"

    # Compute levels
    levels = compute_levels(
        bid=bid,
        offer=offer,
        high=high,
        low=low,
        bids=bids,
        follower_mult=follower_mult,
        win_mult=win_mult,
        loose_mult=loose_mult,
        security_mult=security_mult,
        tactic=tactic,
    )

    return TradingSignal(
        epic=epic,
        score=score,
        direction=direction,
        regression=reg,
        sma_fast=sma_f,
        sma_slow=sma_s,
        roc=roc_val,
        spread=spread,
        avg_spread=avg_spread,
        position_in_range=pos_range,
        levels=levels,
    )
