"""Support-anchored, noise-aware initial stop (the active distance policy).

Anchors the initial protective stop **below a real support level** rather than a
flat ATR distance from the entry, and makes that distance **per-epic and
noise-aware**. This is the placement extracted from the old
``close_zoneprofit`` close profile, now a standalone, swappable
:class:`~src.stops.base.StopDistance`.

Weighted support (the noise measure)
------------------------------------
Rather than the single lowest bid low of the window — which one freak wick can
drag to an extreme, putting an absurd amount at risk — the support is a
**recency-weighted low quantile** of the last ``stop_lookback`` bid lows (see
:func:`weighted_support`): a lone spike low is outvoted by the mass of the
distribution, and recent candles weigh more than hour-old ones, so the stop sits
under the level the market *actually defends now*. The stop is then that support
minus a small ``stop_buffer_atr_k × ATR`` cushion.

Distance floor / cap
--------------------
The final distance is floored at ``max(min_stop_atr_k × ATR, min_stop_spread_k ×
spread)`` — never tighter than the reference ATR stop, never inside a couple of
spreads — and optionally capped at ``max_stop_atr_k × ATR`` (``0`` = no cap). The
floor always wins over the cap, so a misconfigured cap can never tighten the stop
below the floor.

Only the BUY stop is support-derived (the live pipeline is long-only); a SELL
falls back to a flat ``stop_atr_k × ATR`` above the offer.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr
from src.feed.price_buffer import EpicBuffer
from src.stops.base import StopDistance


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def weighted_support(
    lows: list[float],
    *,
    percentile: float = 0.10,
    recency_half_life: float = 30.0,
) -> float:
    """Recency-weighted low quantile of a bid-low series — a robust support.

    ``lows`` is the candle bid lows over the support window, oldest first. The
    support is the weighted ``percentile`` quantile of those lows using
    exponential recency weights: the most recent candle has weight ``1.0`` and
    weights halve every ``recency_half_life`` candles into the past. Compared to
    a plain ``min(lows)`` this gives two robustness properties:

    * a single aberrant wick below the market is outvoted by the mass of the
      distribution — the support is not dragged down to a one-off spike;
    * recent structure counts more than hour-old lows, so the support follows
      the level the market is currently defending.

    Args:
        lows: Candle bid lows, oldest first (at least one value).
        percentile: Target quantile in [0, 1]; lower → nearer the bottom of the
            low distribution (wider stop). Clamped to [0, 1].
        recency_half_life: Half-life of the recency weighting, in candles. ``0``
            (or negative) disables it and every candle weighs the same.

    Returns:
        The weighted-quantile low. The single value when ``lows`` has length 1.

    Raises:
        ValueError: when ``lows`` is empty.
    """
    if not lows:
        raise ValueError("weighted_support requires at least one low")
    n = len(lows)
    if recency_half_life > 0:
        # Newest candle (index n-1) weighs 1.0; older candles decay by half every
        # ``recency_half_life`` candles.
        weights = [0.5 ** ((n - 1 - i) / recency_half_life) for i in range(n)]
    else:
        weights = [1.0] * n
    # Weighted quantile: sort by low ascending, walk the cumulative weight until
    # it reaches ``percentile`` of the total weight.
    pairs = sorted(zip(lows, weights), key=lambda pair: pair[0])
    total = sum(weight for _, weight in pairs)
    target = _clamp01(percentile) * total
    cumulative = 0.0
    for low, weight in pairs:
        cumulative += weight
        if cumulative >= target:
            return low
    return pairs[-1][0]


@dataclass
class StopSupport(StopDistance):
    """Initial stop anchored below a recency-weighted support (BUY); ATR for SELL."""

    name = "stop_support"

    # Defaults tuned on a 6-day recorded-candle backtest (see
    # docs/strategies/close_zoneprofit.md): cap=4×ATR + the 20th percentile
    # roughly matched the reference distance's return while cutting noise stop-outs
    # ~in half and lifting the win rate from 35% to 50%.
    atr_period: int = 14
    stop_lookback: int = 60  # support window (candles ≈ last hour on 1-min data)
    stop_buffer_atr_k: float = 0.5  # ATR cushion placed below the detected support
    support_percentile: float = 0.20  # weighted low quantile → robust support
    support_recency_half_life: float = 30.0  # recency weighting, in candles
    min_stop_atr_k: float = 2.5  # distance floor (× ATR) — never tighter than this
    min_stop_spread_k: float = 2.0  # distance floor (× spread) — never inside noise
    max_stop_atr_k: float = 4.0  # distance cap (× ATR); 0 = no cap (support as-is)
    stop_atr_k: float = 2.5  # flat distance used for the SELL fallback

    @classmethod
    def from_settings(cls, settings) -> StopSupport:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``. Tune by editing
        # the constants; select the policy at runtime from the dashboard.
        return cls()

    def initial_stop(
        self,
        *,
        entry_level: float,
        direction: str,
        buf: EpicBuffer,
        day_extreme: float | None = None,  # unused: this window fits in the buffer
    ) -> float:
        candles = list(buf.candles)
        atr_value = atr(candles, self.atr_period)
        last = buf.last
        spread = last.spread if last else 0.0

        if direction == "SELL":
            # Long-only live pipeline; keep a flat ATR stop above the offer.
            offer = last.offer_close if last else entry_level
            return offer + self.stop_atr_k * atr_value

        lows = [candle.bid_low for candle in candles[-self.stop_lookback :]]
        support = (
            weighted_support(
                lows,
                percentile=self.support_percentile,
                recency_half_life=self.support_recency_half_life,
            )
            if lows
            else entry_level
        )

        raw_stop = support - self.stop_buffer_atr_k * atr_value
        # Floor the distance: never tighter than the reference ATR stop, never
        # inside a couple of spreads. No upper cap on the distance by default.
        min_distance = max(
            self.min_stop_atr_k * atr_value,
            self.min_stop_spread_k * spread,
        )
        distance = max(entry_level - raw_stop, min_distance)
        # Optional upper cap: clip a far support to ``max_stop_atr_k × ATR`` so a
        # single deep-support trade cannot risk the whole hourly range. The floor
        # always wins over the cap, so a misconfigured cap can never tighten the
        # stop below ``min_distance``.
        if self.max_stop_atr_k > 0:
            distance = min(distance, max(self.max_stop_atr_k * atr_value, min_distance))
        return entry_level - distance
