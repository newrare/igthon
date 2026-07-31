"""Hourly-extreme initial stop — the plain low (BUY) / high (SELL) of the last hour.

The simplest structural placement there is: the stop goes **exactly at the lowest
level printed over the last hour of recording** for a BUY (the highest one for a
SELL). No quantile, no weighting, no ATR shaping — if the market has not gone
below that level in an hour, going below it now says the reason for the trade is
gone.

Unlike :class:`~src.stops.stop_support.StopSupport`, which deliberately ignores a
lone freak wick (recency-weighted low *quantile*), this policy takes the raw
extreme: the wick **is** the level, so the stop is never inside a range the market
has already visited. That costs more risk per unit on a spiky window — sizing is
*not* risk-based (``open_position`` deals ``minDealSize × quantity_multiplier``
whatever the distance), so a wider stop is a proportionally larger euro loss — and
buys a much lower chance of being shaken out by a repeat of a move that already
happened.

Why a raw extreme needs a noise-aware floor
-------------------------------------------
The hourly extreme is only a *structural* level when the curve actually has
structure. On a flat, choppy hour the price wanders up and down inside a band and
the hour's low is simply wherever the noise last happened to poke — so an entry
taken anywhere near the bottom of that band gets a stop a couple of points away
and is closed almost instantly by the very oscillation that produced the level.
Observed on ``IX.D.DOW.IFE.IP`` (2026-07-31 09:37): hourly low 7.0 points under
the bid while a single candle averaged 10.6 points of range — stop hit 21 seconds
after the open.

A spread- or ATR-based floor cannot catch that case: the spread says nothing about
the hour, and ATR grows with a *clean* trend just as much as with chop, so it
cannot tell the two apart. The floor is therefore derived from the **global state
of the curve** over the same window, from two complementary measures (see
:func:`noise_floor_distance`):

- :func:`~src.core.indicators.band_noise` — the detrended amplitude of the wander
  (the band's thickness), which is what a stop has to sit outside of;
- :func:`~src.core.indicators.efficiency_ratio` — how *directional* the path is
  (``1`` = clean ramp, ``0`` = pure noise), which says how much of that band is
  meaningful structure and how much is churn.

The floor multiplier is interpolated between ``noise_trend_k`` (clean trend: the
extreme is real structure, stay tight) and ``noise_chop_k`` (pure noise: the
extreme is meaningless, stand well outside the band), so two almost-identical
windows never get wildly different risk.

Buffer, floor and cap
---------------------
``buffer_atr_k × ATR`` is placed beyond the extreme (``0`` by default — the stop
sits *on* the level). The distance is then floored at ``max(noise floor,
min_stop_atr_k × ATR, min_stop_spread_k × spread)`` — the noise floor governs the
choppy-band case above, while the ATR and spread terms remain as absolute
back-stops for a market so quiet that the band itself collapses (a stop inside the
bid/offer churn is rejected by the broker anyway). It is optionally capped at
``max_stop_atr_k × ATR`` (``0`` = no cap, the default: the point of the policy is
to honour the hourly extreme wherever it is). The floor always wins over the cap.

BUY and SELL are symmetric: the extreme is read on the side the stop is triggered
on — the bid lows for a BUY, the offer highs for a SELL — so the policy works with
the two-sided entry strategies.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr, band_noise, efficiency_ratio
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops.base import StopDistance


def window_extreme(candles: list[Candle], *, direction: str) -> float | None:
    """Lowest bid low (BUY) or highest offer high (SELL) of ``candles``.

    The extreme is read on the side the stop is triggered on, so the returned
    level is directly comparable with the stop reference (the live bid for a BUY,
    the live offer for a SELL).

    Args:
        candles: The window's candles, oldest first (order is irrelevant here).
        direction: ``"BUY"`` or ``"SELL"``.

    Returns:
        The extreme level, or ``None`` when ``candles`` is empty.
    """
    if not candles:
        return None
    if direction == "SELL":
        return max(candle.offer_high for candle in candles)
    return min(candle.bid_low for candle in candles)


def noise_floor_distance(
    candles: list[Candle], *, trend_k: float, chop_k: float
) -> float:
    """Minimum stop distance implied by the global state of the curve.

    Two measures of the window, both read on the mid closes so the result is
    side-agnostic (the same band has to be cleared by a long and a short):

    - ``band_noise`` — the detrended standard deviation, i.e. the thickness of the
      band the price oscillates in, in price points;
    - ``efficiency_ratio`` — ``|net move| / path travelled``, ``1`` for a clean
      ramp and ``0`` for pure chop.

    The multiplier applied to the band is interpolated linearly on the *chop*
    (``1 − ER``)::

        k = trend_k + (chop_k − trend_k) × (1 − ER)

    A clean trend therefore keeps the tight ``trend_k`` band (its hourly extreme is
    genuine structure worth honouring), while a directionless hour gets the wide
    ``chop_k`` band — the only distance that survives an oscillation whose own
    amplitude produced the extreme in the first place.

    Args:
        candles: The window's candles, oldest first.
        trend_k: Band multiplier when the path is perfectly directional (ER = 1).
        chop_k: Band multiplier when the path is pure noise (ER = 0).

    Returns:
        The floor in price points, or ``0.0`` when the window is too short to
        measure (fewer than three candles) or perfectly flat.
    """
    mids = [candle.mid_close for candle in candles]
    if len(mids) < 3:
        return 0.0
    # ``efficiency_ratio`` consumes ``period + 1`` values, so the period is the
    # number of steps in the window, not its length.
    chop = 1.0 - efficiency_ratio(mids, len(mids) - 1)
    return (trend_k + (chop_k - trend_k) * chop) * band_noise(mids)


@dataclass
class StopHourLow(StopDistance):
    """Initial stop at the last hour's lowest low (BUY) / highest high (SELL)."""

    name = "stop_hourlow"

    atr_period: int = 14
    lookback: int = 60  # window, in candles ≈ the last hour on 1-min data
    buffer_atr_k: float = 0.0  # cushion beyond the extreme; 0 = stop on the level

    # Noise floor — the curve-state term (see ``noise_floor_distance``). Measured
    # over the same window as the extreme, so the floor and the level it protects
    # describe the same hour.
    noise_lookback: int = 0  # 0 = reuse ``lookback``
    noise_trend_k: float = 0.5  # × band, clean trend (ER = 1) — extreme is real
    noise_chop_k: float = 2.0  # × band, pure noise (ER = 0) — stand outside it

    # Absolute back-stops, for a market whose band has collapsed.
    min_stop_atr_k: float = 0.5  # distance floor (× ATR) — never inside a flat hour
    min_stop_spread_k: float = 2.0  # distance floor (× spread) — never inside noise
    max_stop_atr_k: float = 0.0  # distance cap (× ATR); 0 = no cap (extreme as-is)

    @classmethod
    def from_settings(cls, settings) -> StopHourLow:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``. Tune by editing
        # the constants; select the policy via STOP_STRATEGY in .env.
        return cls()

    def initial_stop(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> float:
        candles = list(buf.candles)
        atr_value = atr(candles, self.atr_period)
        last = buf.last
        spread = last.spread if last else 0.0
        sign = -1 if direction == "SELL" else 1
        # The stop is triggered on the close-out side: the bid for a BUY
        # (``entry_level`` already is the live bid), the offer for a SELL.
        if direction == "SELL":
            reference = last.offer_close if last else entry_level
        else:
            reference = entry_level

        window = candles[-self.lookback :]
        extreme = window_extreme(window, direction=direction)
        if extreme is None:
            # No history yet (warmup should prevent this): fall back on the floor.
            distance = 0.0
        else:
            # Negative when the extreme sits on the wrong side of the reference —
            # the floor below takes over in that case.
            distance = sign * (reference - extreme) + self.buffer_atr_k * atr_value

        # The floor that actually matters on this policy: a raw extreme inside a
        # choppy band is not a level, so the stop must clear the band itself. The
        # ATR and spread terms stay as absolute back-stops underneath it.
        noise_window = candles[-(self.noise_lookback or self.lookback) :]
        min_distance = max(
            noise_floor_distance(
                noise_window,
                trend_k=self.noise_trend_k,
                chop_k=self.noise_chop_k,
            ),
            self.min_stop_atr_k * atr_value,
            self.min_stop_spread_k * spread,
        )
        distance = max(distance, min_distance)
        if self.max_stop_atr_k > 0:
            # The floor always wins over the cap, so a misconfigured cap can never
            # tighten the stop below ``min_distance``.
            distance = min(distance, max(self.max_stop_atr_k * atr_value, min_distance))
        return reference - sign * distance
