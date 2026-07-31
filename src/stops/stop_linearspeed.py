"""Speed-adaptive initial stop — tight when the move accelerates, far when it crawls.

The bet is on **continuation**: a market that has travelled fast and straight in
the trade's direction over the last few minutes is unlikely to reverse in the very
next ticks, so the risk of an adverse spike is low and the stop can sit just
outside the noise, right behind the entry. A market that has barely progressed
gives no such protection: the entry could be anywhere inside a range, so the stop
must sit behind **real structure** — the last hour's support (BUY) or resistance
(SELL) — even though that costs more risk per unit.

Speed — the regime measure
--------------------------
The window's speed is the **net regression move over the last ``speed_lookback``
candles, expressed in ATR units and signed by the trade direction** (see
:func:`directional_speed`): fit a least-squares line to the mid closes, take
``slope × (n − 1)`` (the trend's total travel across the window, immune to a
single freak tick, unlike a close-to-close delta) and divide by ATR so the number
is comparable across epics. It is positive when the market moves *with* the trade
(up for a BUY, down for a SELL) and negative when it moves against it. A choppy
window scores near zero on its own: the regression of an oscillation is flat, so
no separate choppiness term is needed.

Blending the two regimes
------------------------
``speed`` is mapped to a blend factor ``t = clamp01((speed − slow_speed) /
(fast_speed − slow_speed))`` and the distance is interpolated between the two
placements::

    distance = t × (noise_atr_k × ATR)      # t = 1 → fast: noise margin only
             + (1 − t) × structure_distance # t = 0 → slow: behind real structure

Interpolating rather than switching on a threshold removes the cliff where two
almost-identical windows would get wildly different risk.

Structure (the slow leg)
------------------------
Reuses the robust level of :mod:`~src.stops.stop_support`: a recency-weighted low
quantile of the last ``structure_lookback`` bid lows for a BUY (see
:func:`~src.stops.stop_support.weighted_support`), mirrored on the offer highs for
a SELL (see :func:`weighted_resistance`), plus a ``structure_buffer_atr_k × ATR``
cushion beyond it. A lone wick is outvoted by the mass of the distribution and
recent candles weigh more than hour-old ones, so the stop sits behind the level
the market *actually defends now*. That leg has its own floor,
``slow_min_atr_k × ATR`` (the reference ATR stop), because an *adverse* window
puts the structure on the wrong side of the entry — a market falling into a BUY
has its hourly lows above the current bid — and the worst regime must not end up
with the tightest stop.

Floor / cap
-----------
The blended distance is floored at ``max(min_stop_spread_k × spread,
min_stop_atr_k × ATR)`` — never inside the bid/offer churn, never inside a
minimal volatility gap when ATR collapses — and capped at ``max_stop_atr_k × ATR``
(``0`` = no cap) so a far structure cannot risk the whole hourly range. The floor
always wins over the cap.

BUY and SELL are fully symmetric: everything above is mirrored around the side the
stop is triggered on (the bid for a BUY, the offer for a SELL), so this policy is
usable with the two-sided entry strategies instead of falling back to a flat ATR
distance on shorts.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr, linear_regression
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops.base import StopDistance
from src.stops.stop_support import weighted_support


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def directional_speed(values: list[float], *, atr_value: float, sign: int) -> float:
    """Net regression travel over ``values``, in ATR units, signed by direction.

    The least-squares slope times the window span (``n − 1``) is the total move
    the *trend* accounts for over the window — a spike in the middle barely moves
    it, unlike a raw last-minus-first delta. Dividing by ATR makes the figure
    comparable across epics: ``2.0`` means "the last window travelled two ATR in
    the trade's direction".

    Args:
        values: Ordered price series, oldest first (mid closes of the window).
        atr_value: Current ATR, used as the normalising unit. ``<= 0`` yields
            ``0.0`` (a flat/unknown-volatility window is treated as slow).
        sign: ``+1`` for a BUY, ``-1`` for a SELL — flips the sign so a positive
            result always means "moving with the trade".

    Returns:
        The signed speed in ATR units; ``0.0`` when it cannot be measured.
    """
    if atr_value <= 0 or len(values) < 2:
        return 0.0
    reg = linear_regression(values)
    net_move = reg.slope * (len(values) - 1)
    return sign * net_move / atr_value


def weighted_resistance(
    highs: list[float],
    *,
    percentile: float = 0.10,
    recency_half_life: float = 30.0,
) -> float:
    """Recency-weighted high quantile of an offer-high series — a robust resistance.

    The exact mirror of :func:`~src.stops.stop_support.weighted_support` on the
    upper side: negating the series turns "the ``percentile`` quantile from the
    bottom of the lows" into "the ``percentile`` quantile from the top of the
    highs", so the same weighting and the same robustness properties apply to a
    short's stop.

    Args:
        highs: Candle offer highs, oldest first (at least one value).
        percentile: Target quantile in [0, 1]; lower → nearer the top of the high
            distribution (wider stop).
        recency_half_life: Half-life of the recency weighting, in candles.

    Returns:
        The weighted-quantile high.

    Raises:
        ValueError: when ``highs`` is empty.
    """
    if not highs:
        raise ValueError("weighted_resistance requires at least one high")
    return -weighted_support(
        [-high for high in highs],
        percentile=percentile,
        recency_half_life=recency_half_life,
    )


@dataclass
class StopLinearSpeed(StopDistance):
    """Initial stop interpolated between a noise margin (fast) and structure (slow)."""

    name = "stop_linearspeed"

    atr_period: int = 14

    # Regime measurement: the last 10 candles ≈ the last 10 minutes on 1-min data.
    speed_lookback: int = 10
    slow_speed: float = 0.5  # ≤ this many ATR travelled → full structure stop
    fast_speed: float = 2.0  # ≥ this many ATR travelled → full noise stop

    # Fast leg: keep only a noise margin behind the entry and trust the momentum.
    noise_atr_k: float = 1.0

    # Slow leg: the last hour's defended level, same robust estimator as
    # ``stop_support`` (weighted low/high quantile + an ATR cushion beyond it).
    structure_lookback: int = 60  # candles ≈ last hour on 1-min data
    structure_percentile: float = 0.20
    structure_recency_half_life: float = 30.0  # in candles
    structure_buffer_atr_k: float = 0.5
    slow_min_atr_k: float = 2.5  # slow-leg floor (× ATR) — see _structure_distance

    # Distance floor / cap, applied to the blended distance.
    min_stop_atr_k: float = 0.75  # never inside a minimal volatility gap
    min_stop_spread_k: float = 2.0  # never inside the bid/offer churn
    max_stop_atr_k: float = 4.0  # cap (× ATR); 0 = no cap

    @classmethod
    def from_settings(cls, settings) -> StopLinearSpeed:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``. Tune by editing
        # the constants; select the policy at runtime via STOP_STRATEGY in .env.
        return cls()

    def _structure_distance(
        self,
        candles: list[Candle],
        *,
        reference: float,
        direction: str,
        atr_value: float,
    ) -> float:
        """Distance from ``reference`` to just beyond the last hour's structure.

        Floored at ``slow_min_atr_k × ATR`` — the reference ATR stop — because the
        raw structure distance is meaningless when the level sits on the *wrong*
        side of the reference, which is exactly what an adverse window produces (a
        market falling into a BUY entry has its hourly lows *above* the current
        bid). Without that floor the worst possible regime would earn the tightest
        possible stop; with it, "no usable structure" degrades to the reference
        distance instead.
        """
        window = candles[-self.structure_lookback :]
        raw = self.slow_min_atr_k * atr_value
        if window and direction == "SELL":
            level = weighted_resistance(
                [candle.offer_high for candle in window],
                percentile=self.structure_percentile,
                recency_half_life=self.structure_recency_half_life,
            )
            raw = (level + self.structure_buffer_atr_k * atr_value) - reference
        elif window:
            level = weighted_support(
                [candle.bid_low for candle in window],
                percentile=self.structure_percentile,
                recency_half_life=self.structure_recency_half_life,
            )
            raw = reference - (level - self.structure_buffer_atr_k * atr_value)
        return max(raw, self.slow_min_atr_k * atr_value)

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

        speed = directional_speed(
            [candle.mid_close for candle in candles[-self.speed_lookback :]],
            atr_value=atr_value,
            sign=sign,
        )
        # 1.0 → accelerating with the trade (noise stop), 0.0 → crawling or moving
        # against it (structure stop). A degenerate band collapses to structure.
        span = self.fast_speed - self.slow_speed
        blend = _clamp01((speed - self.slow_speed) / span) if span > 0 else 0.0

        noise_distance = self.noise_atr_k * atr_value
        structure_distance = self._structure_distance(
            candles, reference=reference, direction=direction, atr_value=atr_value
        )
        distance = blend * noise_distance + (1.0 - blend) * structure_distance

        min_distance = max(
            self.min_stop_spread_k * spread,
            self.min_stop_atr_k * atr_value,
        )
        distance = max(distance, min_distance)
        if self.max_stop_atr_k > 0:
            # The floor always wins over the cap, so a misconfigured cap can never
            # tighten the stop below ``min_distance``.
            distance = min(distance, max(self.max_stop_atr_k * atr_value, min_distance))
        return reference - sign * distance
