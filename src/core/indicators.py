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

from src.feed.price_buffer import Candle

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
    level_margin: float = 0.0


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


def adverse_tick_noise(
    bid_closes: list[float], window: int = 20, std_k: float = 2.0
) -> float:
    """Typical *adverse* (downward) tick-to-tick amplitude of the bid.

    Sizes a long's trailing stop off the noise that can actually hit it. Only
    downward moves are counted — for a long, upward jitter never triggers the
    stop, so a symmetric measure (mean ``|Δbid|``) would be inflated by the
    trend's upward drift and misstate the real pull-back risk. Each step
    contributes ``max(0, bid[i-1] - bid[i])``; up-ticks contribute ``0``.

    Returns ``mean(down) + std_k × std(down)``: the centre of the down-move
    distribution plus a volatility band, so the result sits *beyond* a normal
    down-tick rather than at its average. This is the natural per-tick floor for
    the trailing distance, complementing the (candle-based) ATR which does not
    capture bid jitter.

    Args:
        bid_closes: Ordered bid closes (oldest first).
        window: Number of most-recent steps to measure over.
        std_k: Multiplier on the down-move standard deviation.

    Returns:
        The adverse tick-noise amplitude in price points, or 0.0 when there are
        fewer than two bids.
    """
    if window < 1 or len(bid_closes) < 2:
        return 0.0

    recent = bid_closes[-(window + 1) :]
    downs = [max(0.0, prev - cur) for prev, cur in zip(recent, recent[1:])]
    if not downs:
        return 0.0

    mean = sum(downs) / len(downs)
    variance = sum((d - mean) ** 2 for d in downs) / len(downs)
    return mean + std_k * math.sqrt(variance)


def trend_pct(values: list[float], period: int) -> tuple[float, float]:
    """Size and cleanliness of the trend over the last ``period`` values.

    The raw least-squares slope is per-step and in price units, so it is not
    comparable between a forex pair and a commodity. This normalises it into the
    **total percentage move implied by the fit across the window**
    (``slope × window_length / last_price × 100``), which is directly comparable
    across every epic in the universe — the form a cross-epic entry needs.

    Args:
        values: Ordered values, oldest first (bid closes).
        period: Lookback window; the whole list is used when shorter.

    Returns:
        ``(pct, r_squared)`` — the signed implied move in percent and the fit's
        R² (its linearity, 0-1). ``(0.0, 0.0)`` when there is nothing to fit or
        the last value is non-positive.
    """
    if period < 2 or len(values) < 2:
        return 0.0, 0.0
    window = values[-period:]
    price = window[-1]
    if price <= 0:
        return 0.0, 0.0
    reg = linear_regression(window)
    return reg.slope * len(window) / price * 100, reg.r_squared


def channel_position(candles: list[Candle], period: int) -> tuple[float, float, float]:
    """Locate the last bid inside the high/low channel of the last ``period`` candles.

    Args:
        candles: Ordered candles, oldest first; the whole list is used when
            shorter than ``period``.
        period: Channel lookback.

    Returns:
        ``(position, high, low)`` where ``position`` is 0 at the channel low, 1
        at its high, and 0.5 for a degenerate (flat) channel.
    """
    if period < 1 or not candles:
        return 0.5, 0.0, 0.0
    window = candles[-period:]
    high = max(c.bid_high for c in window)
    low = min(c.bid_low for c in window)
    span = high - low
    if span <= 0:
        return 0.5, high, low
    return (window[-1].bid_close - low) / span, high, low


def efficiency_ratio(values: list[float], period: int) -> float:
    """Kaufman Efficiency Ratio over the last ``period`` values (0-1).

    ER = |net move| / sum(|step move|): close to 1 when the path is a clean
    directional trend, close to 0 when it is choppy/sideways noise. It is the
    natural "is this market trending?" gate for a breakout strategy.

    Args:
        values: Ordered list of numeric values (oldest first).
        period: Lookback window (needs ``period + 1`` values).

    Returns:
        ER in [0, 1], or 0.0 when there is insufficient data or no movement.
    """
    if len(values) < period + 1 or period < 1:
        return 0.0
    window = values[-period - 1 :]
    net = abs(window[-1] - window[0])
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    return net / path if path > 0 else 0.0
