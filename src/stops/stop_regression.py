"""Regression-channel, trend-and-noise-aware initial stop.

Places the initial protective stop a **noise band** below the entry, where the
band is derived from two independent, per-epic measurements taken over the
``lookback`` window:

Noise (the band width) — residual volatility
---------------------------------------------
Fit a least-squares line to the last ``lookback`` bid closes and take the
standard deviation of the **residuals** around that line (see
:func:`residual_sigma`). This is the dispersion that remains *once the trend is
removed*, so it isolates genuine noise from directional movement. It is a
cleaner noise measure than ATR for stop placement: ATR mechanically inflates in
a strong trend (each bar's true range is large even when the path is clean),
which would push the stop out for the wrong reason. Two epics with an identical
trend slope but different residual dispersion get **different** stops — which is
the whole point.

Trend (the band multiplier) — efficiency ratio
-----------------------------------------------
The band is widened when the move is **choppy** and tightened when it is a clean
trend, via the Kaufman efficiency ratio ``ER`` (net move / summed absolute
moves, in [0, 1]): the distance is ``sigma_k × σ_resid × (1 + chop_beta ×
(1 − ER))``. A clean trend (``ER → 1``) leaves the band at ``sigma_k × σ``; pure
chop (``ER → 0``) widens it by ``(1 + chop_beta)``, because a directionless
market revisits its recent levels and a tight stop there is noise-food.

Floor
-----
The distance is floored at ``max(min_stop_spread_k × spread, min_stop_atr_k ×
ATR)`` so it is never placed inside the bid/offer churn nor tighter than a
minimal volatility gap when the regression band collapses (a near-flat window).

Only the BUY stop is band-derived (the live pipeline is long-only); a SELL falls
back to a flat ``stop_atr_k × ATR`` above the offer.

The parameters (``sigma_k = 2.5``, ``chop_beta = 1.0``) were chosen on a
risk-normalised (R-multiple) backtest over three recorded weeks: at equal width
the residual-sigma band beat the ATR-flat and support-anchored policies on every
week, and the choppiness widening lifted the win rate without the degenerate
"never-stopped" over-widening that a larger multiple would bring.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr, band_noise, efficiency_ratio
from src.feed.price_buffer import EpicBuffer
from src.stops.base import StopDistance


def residual_sigma(values: list[float]) -> float:
    """Std of the residuals of ``values`` around their least-squares line.

    The population standard deviation of ``value[i] − (slope·i + intercept)``:
    the price dispersion that remains once the linear trend is removed, i.e. a
    trend-independent measure of noise. Returns ``0.0`` for fewer than three
    values (a line through two points has no residual).

    This policy's own name for :func:`~src.core.indicators.band_noise`, which is
    the shared implementation — ``stop_hourlow`` derives its minimum stop distance
    from the same measure.

    Args:
        values: Ordered numeric series, oldest first.

    Returns:
        The residual standard deviation (``>= 0``).
    """
    return band_noise(values)


@dataclass
class StopRegression(StopDistance):
    """Initial stop a choppiness-scaled residual-noise band below the entry (BUY)."""

    name = "stop_regression"

    atr_period: int = 14
    lookback: int = 60  # regression + ER window (candles ≈ last hour on 1-min data)
    sigma_k: float = 2.5  # base band width, in residual-sigma multiples
    chop_beta: float = 1.0  # extra width at full chop: ×(1 + chop_beta) when ER → 0
    min_stop_atr_k: float = 1.0  # distance floor (× ATR) — never inside a flat window
    min_stop_spread_k: float = 2.0  # distance floor (× spread) — never inside noise
    stop_atr_k: float = 2.5  # flat distance used for the SELL fallback

    @classmethod
    def from_settings(cls, settings) -> StopRegression:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``. Tune by editing
        # the constants; select the policy at runtime via STOP_STRATEGY in .env.
        return cls()

    def initial_stop(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> float:
        candles = list(buf.candles)
        atr_value = atr(candles, self.atr_period)
        last = buf.last
        spread = last.spread if last else 0.0

        if direction == "SELL":
            # Long-only live pipeline; keep a flat ATR stop above the offer.
            offer = last.offer_close if last else entry_level
            return offer + self.stop_atr_k * atr_value

        closes = [candle.bid_close for candle in candles[-self.lookback :]]
        sigma = residual_sigma(closes)
        # ER over the same window; needs period + 1 values, cap the period at
        # what is available so a short warmup window still yields a value.
        er_period = min(self.lookback, max(len(closes) - 1, 1))
        er = efficiency_ratio(closes, er_period)

        # Choppiness widening: clean trend (ER→1) → sigma_k × σ; full chop
        # (ER→0) → sigma_k × σ × (1 + chop_beta).
        distance = self.sigma_k * sigma * (1.0 + self.chop_beta * (1.0 - er))
        # Floor: never inside the spread churn, never tighter than a minimal ATR
        # gap when the regression band collapses on a near-flat window.
        floor = max(self.min_stop_spread_k * spread, self.min_stop_atr_k * atr_value)
        distance = max(distance, floor)
        return entry_level - distance
