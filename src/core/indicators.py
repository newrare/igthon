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
    return atr_from_series(
        [c.bid_high for c in candles],
        [c.bid_low for c in candles],
        [c.bid_close for c in candles],
        period,
    )


def atr_from_series(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float:
    """Average True Range computed on already-extracted OHLC series.

    Same maths as :func:`atr`, which delegates here; this form exists because the
    curve-reading layer measures amplitude on a *chosen* price side (mid, bid or
    offer) and so cannot pass bid-only candles (see :mod:`src.core.reading`).

    Args:
        highs: Ordered highs (oldest first).
        lows: Ordered lows, same length as ``highs``.
        closes: Ordered closes, same length as ``highs``.
        period: Number of true-range values to average.

    Returns:
        ATR in price points, or 0.0 if there is insufficient data.
    """
    n = min(len(highs), len(lows), len(closes))
    if period < 1 or n < period + 1:
        return 0.0

    true_ranges: list[float] = []
    for i in range(n - period, n):
        prev_close = closes[i - 1]
        true_ranges.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )

    return sum(true_ranges) / period


def adverse_tick_noise(
    bid_closes: list[float], window: int = 20, std_k: float = 2.0
) -> float:
    """Typical *adverse* (downward) close-to-close amplitude of the bid.

    Despite the historical name, the "tick" here is one **1-minute candle close**,
    not a market tick: the application holds no sub-minute data at all (see
    ``docs/DATAFLOW.md`` §1). So this measures step-to-step *closing* moves, and it
    complements ATR by looking at a different thing — ATR spans each bar's full
    high-low range, this one only at what one bar's close gives back versus the
    previous. Feeding it genuinely sub-minute prices would collapse the measured
    amplitude and, with it, every trailing distance floored on it.

    Sizes a long's trailing stop off the noise that can actually hit it. Only
    downward moves are counted — for a long, upward jitter never triggers the
    stop, so a symmetric measure (mean ``|Δbid|``) would be inflated by the
    trend's upward drift and misstate the real pull-back risk. Each step
    contributes ``max(0, bid[i-1] - bid[i])``; up-moves contribute ``0``.

    Returns ``mean(down) + std_k × std(down)``: the centre of the down-move
    distribution plus a volatility band, so the result sits *beyond* a normal
    adverse step rather than at its average — the natural per-candle floor for the
    trailing distance.

    Args:
        bid_closes: Ordered bid closes (oldest first), one per candle.
        window: Number of most-recent steps to measure over.
        std_k: Multiplier on the down-move standard deviation.

    Returns:
        The adverse noise amplitude in price points, or 0.0 when there are
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


def retracement_depth(closes: list[float], window: int = 30) -> float:
    """Deepest pull-back the series gave back from a running peak over ``window``.

    Where :func:`adverse_tick_noise` measures what **one** step gives back, this
    measures what a *sequence* of them gives back: the maximum drawdown from the
    running maximum inside the window. The two answer different questions and a
    trailing stop needs both — a retracement of six candles losing 1.5 points each
    is a 9-point pull-back that the single-step measure reports as 1.5. A stop
    floored only on the step measure is therefore parked inside the market's normal
    swing amplitude and gets taken out by an ordinary retracement, while the trade
    is still running (observed live on CS.D.EURCAD.CFD.IP, 2026-08-04).
    ``atr`` does not cover it either: it spans each bar's own high-low range, not
    the multi-bar excursion.

    The window bounds the measure's memory: a freak pull-back inflates it for
    ``window`` steps and then rolls out on its own.

    The series must be **sign-normalised** so that rising always means going into
    profit (see :attr:`~src.exit.zones.base.StopContext.favourable_closes`); the
    result is then the give-back a stop of that side has to survive, whichever side
    the position is on.

    Args:
        closes: Ordered close-out prices (oldest first), one per candle.
        window: Number of most-recent steps to measure over.

    Returns:
        The deepest give-back in price points (never negative), or ``0.0`` when
        there are fewer than two values or the series only ever rose.
    """
    if window < 1 or len(closes) < 2:
        return 0.0

    recent = closes[-(window + 1) :]
    peak = recent[0]
    deepest = 0.0
    for value in recent[1:]:
        peak = max(peak, value)
        deepest = max(deepest, peak - value)
    return deepest


def band_noise(values: list[float]) -> float:
    """Amplitude of the curve's wander around its own trend line.

    Fits a least-squares line to ``values`` and returns the standard deviation of
    the residuals. Being **detrended**, it answers "how thick is the band the
    price oscillates in?" independently of how far the curve has travelled: a
    clean ramp scores near ``0`` however steep it is, while a flat market chopping
    inside a range scores about a quarter of that range's height.

    This is the complement of :func:`atr` (per-candle movement) and
    :func:`adverse_tick_noise` (tick-to-tick jitter): both grow with a strong
    trend, so neither can tell a directional market from a noisy one. Pair it with
    :func:`efficiency_ratio` to get the full picture — amplitude *and*
    directionality — of the global curve state.

    Args:
        values: Ordered price series, oldest first (mid closes of the window).

    Returns:
        The residual standard deviation in price points, or ``0.0`` when there are
        fewer than three values (a line through two points has no residual).
    """
    n = len(values)
    if n < 3:
        return 0.0
    reg = linear_regression(values)
    # OLS residuals are zero-mean by construction, so the variance is simply the
    # mean of their squares.
    residuals = (v - (reg.slope * i + reg.intercept) for i, v in enumerate(values))
    return math.sqrt(sum(residual**2 for residual in residuals) / n)


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


def sma(values: list[float], period: int) -> float:
    """Simple Moving Average of the last ``period`` values.

    Args:
        values: Ordered list of numeric values (oldest first).
        period: Averaging window.

    Returns:
        The mean of the last ``period`` values, or 0.0 when there are fewer than
        ``period`` values (an average computed on a short window would silently
        compare two different lookbacks in a crossover test).
    """
    if period < 1 or len(values) < period:
        return 0.0
    window = values[-period:]
    return sum(window) / period


def ema(values: list[float], period: int) -> float:
    """Exponential Moving Average of ``values`` with the standard ``2/(n+1)`` factor.

    Seeded with the SMA of the first ``period`` values, then rolled forward over
    the rest of the series — so the result depends on the whole history passed in,
    not only on the last ``period`` values. Compared with :func:`sma` it reacts
    faster to the latest candles, which is what makes an EMA pair the usual
    crossover detector for a *turn* rather than for an established trend.

    Args:
        values: Ordered list of numeric values (oldest first).
        period: EMA period.

    Returns:
        The EMA of the last value, or 0.0 when there are fewer than ``period``
        values.
    """
    if period < 1 or len(values) < period:
        return 0.0
    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current += alpha * (value - current)
    return current


def rsi(values: list[float], period: int = 14) -> float:
    """Wilder's Relative Strength Index over ``period`` steps (0-100).

    Ratio of average gain to average loss, smoothed the way Wilder defined it
    (each new step keeps ``(period-1)/period`` of the running average). Unlike a
    slope or a ROC it is **bounded**, so the same thresholds read the same on every
    epic: conventionally above 70 the market is stretched upward, below 30
    downward, and 50 is balance.

    It answers a question no trend measure does — *how much of the recent movement
    was in one direction, relative to the movement against it* — which is why it is
    the natural "is this move exhausted?" complement to
    :func:`efficiency_ratio` (path directionality) and :func:`trend_pct` (size).

    Args:
        values: Ordered list of numeric values (oldest first).
        period: Smoothing period; needs ``period + 1`` values.

    Returns:
        RSI in [0, 100]. Returns the neutral 50.0 when there is insufficient data,
        and 100.0 when the window never moved down.
    """
    if period < 1 or len(values) < period + 1:
        return 50.0

    deltas = [b - a for a, b in zip(values, values[1:])]
    gains = [max(0.0, d) for d in deltas]
    losses = [max(0.0, -d) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1 + avg_gain / avg_loss)


def slope_angle(slope: float, steps: int, price_range: float) -> float:
    """Regression slope expressed as the **angle a human reads on the chart**.

    A raw slope is in price units per candle, so it is unusable as a "how steep is
    it?" figure: 0.3 is vertical on a forex pair and flat on an index. Normalising
    it into a percentage (:func:`trend_pct`) fixes the cross-epic comparison but
    still says nothing about steepness *relative to what this market does* — a
    0.5 % ramp is a wall on a quiet pair and a ripple on a volatile one.

    This normalises against the window's own vertical extent instead: the angle of
    the fit line drawn on a chart whose width is the window and whose height is
    ``price_range``, i.e. exactly the picture on the dashboard. ``45°`` means the
    fit climbs the full window range across the full window; ``0°`` is flat;
    the sign follows the slope.

    Args:
        slope: Least-squares slope, in price units per step.
        steps: Number of steps the window spans (``len(window)``).
        price_range: Vertical extent of the window (its high − low).

    Returns:
        The angle in degrees, in ``(-90, 90)``; 0.0 for a degenerate window.
    """
    if steps < 1 or price_range <= 0:
        return 0.0
    return math.degrees(math.atan(slope * steps / price_range))
