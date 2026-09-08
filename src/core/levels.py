"""Readable price levels — where the curve has already turned, and round numbers.

Shared, purely-functional infrastructure (no trading decision here): it turns a
price series into the small set of **levels a human would draw on the chart**, so
strategies can reason about structure instead of re-deriving it each time.

Two families of level, found by two unrelated methods:

- **Swing levels** (:func:`swing_pivots` → :func:`cluster_levels`) are empirical:
  a pivot is a candle whose high (or low) dominates its neighbours, so it marks a
  price the market actually turned at. Pivots at nearly the same price are then
  merged into one level whose ``touches`` count is the number of times the market
  respected it — the whole point, since a level touched four times is structure
  and a level touched once is an accident.
- **Psychological levels** (:func:`psychological_levels`) are a priori: the round
  numbers (1.1000, 18 500, 2 400) where orders cluster because people place them
  there, whether or not this particular curve has ever visited them.

Both are returned as :class:`PriceLevel`, so a caller handles one type, and both
are *unsigned* — support versus resistance is decided by comparing the level with
the live price at read time (:func:`support_resistance`), because a resistance
that gets broken becomes a support without moving.

Distances are meaningless in absolute points across a universe of 50 epics, so
every consumer is expected to divide by a volatility unit (ATR / noise band). The
helpers here take their tolerance in the same spirit: it is passed in, in price
units, by a caller that knows the epic's scale — see
:class:`~src.core.reading.CurveReading`, which is the intended entry point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Strength weights: how much of a level's 0-1 score comes from how often it was
# respected versus how recently. Touches dominate — a level the market turned at
# three times outranks a single fresh pivot, because that is what separates
# structure from an accident — while recency still breaks the tie between two
# equally touched levels, the older of which the market may have left behind.
_TOUCH_WEIGHT = 0.7
_RECENCY_WEIGHT = 0.3
#: Touch count at which the touch half of the score saturates.
_TOUCH_SATURATION = 4


@dataclass(frozen=True, slots=True)
class Pivot:
    """A candle whose extreme dominates its neighbours — a local turn.

    Attributes:
        index: Position in the series it was found in (0 = oldest).
        price: The extreme price (the high for ``kind="high"``, the low otherwise).
        kind: ``"high"`` (local top) or ``"low"`` (local bottom).
    """

    index: int
    price: float
    kind: str


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """A price the curve is expected to react at.

    Attributes:
        price: The level itself (the mean of the merged pivots for a swing level).
        kind: ``"high"``, ``"low"``, ``"mixed"`` (pivots of both kinds merged — the
            strongest case, price turned there in both directions) or ``"round"``
            for a psychological level.
        touches: Number of pivots merged into the level; ``0`` for a round level,
            which is postulated rather than observed.
        last_index: Series index of the most recent touch; ``-1`` for a round level.
        width: Spread of the merged pivots in price units — how *thick* the level
            is. A wide cluster is a zone, not a line.
        strength: 0-1 score combining touches and recency, for ranking only.
    """

    price: float
    kind: str
    touches: int
    last_index: int
    width: float
    strength: float

    def distance(self, price: float) -> float:
        """Absolute distance from ``price`` to this level, in price units."""
        return abs(self.price - price)


def swing_pivots(
    highs: list[float], lows: list[float], strength: int = 2
) -> list[Pivot]:
    """Locate the local tops and bottoms of a candle series.

    A candle is a **high pivot** when its high is the maximum of the window of
    ``strength`` candles on each side, and a **low pivot** when its low is that
    window's minimum. ``strength`` is therefore the confirmation cost: a bigger
    value returns fewer, more meaningful turns, and the last ``strength`` candles
    can never be pivots because their right-hand side does not exist yet — which is
    deliberate, an unconfirmed turn is not a level.

    Plateaus (several candles sharing the extreme) yield a single pivot — the
    **last** candle of the plateau, the most recent touch — so a flat top is not
    counted as several touches of itself.

    Args:
        highs: Ordered highs, oldest first.
        lows: Ordered lows, same length as ``highs``.
        strength: Number of neighbours required on each side.

    Returns:
        Pivots in chronological order; empty when the series is too short.
    """
    n = min(len(highs), len(lows))
    if strength < 1 or n < 2 * strength + 1:
        return []

    pivots: list[Pivot] = []
    for i in range(strength, n - strength):
        left = slice(i - strength, i)
        right = slice(i + 1, i + strength + 1)

        # ``>=`` on the left and ``>`` on the right keeps the last candle of a
        # plateau and drops the repeats.
        if highs[i] >= max(highs[left]) and highs[i] > max(highs[right]):
            pivots.append(Pivot(index=i, price=highs[i], kind="high"))
        if lows[i] <= min(lows[left]) and lows[i] < min(lows[right]):
            pivots.append(Pivot(index=i, price=lows[i], kind="low"))

    return pivots


def cluster_levels(
    pivots: list[Pivot], tolerance: float, *, series_length: int = 0
) -> list[PriceLevel]:
    """Merge pivots sitting within ``tolerance`` of each other into single levels.

    Without this step a level the market respected four times reads as four
    unrelated pivots a few points apart, and the one thing that distinguishes
    structure from noise — the touch count — is lost. Pivots are walked in price
    order and merged into the running cluster while they stay within ``tolerance``
    of its *mean*, which keeps a cluster from creeping arbitrarily far by chaining
    small gaps.

    Args:
        pivots: Pivots from :func:`swing_pivots` (order irrelevant).
        tolerance: Maximum distance, in price units, between a pivot and its
            cluster's mean. Callers pass a volatility-scaled value (a fraction of
            ATR), never a hardcoded number.
        series_length: Length of the series the pivots came from, used to score
            recency. ``0`` scores every level on touches only.

    Returns:
        Levels sorted by descending :attr:`~PriceLevel.strength`.
    """
    if not pivots or tolerance <= 0:
        return []

    clusters: list[list[Pivot]] = []
    for pivot in sorted(pivots, key=lambda p: p.price):
        if clusters:
            current = clusters[-1]
            mean = sum(p.price for p in current) / len(current)
            if abs(pivot.price - mean) <= tolerance:
                current.append(pivot)
                continue
        clusters.append([pivot])

    levels = [_level_from_cluster(cluster, series_length) for cluster in clusters]
    return sorted(levels, key=lambda level: level.strength, reverse=True)


def _level_from_cluster(cluster: list[Pivot], series_length: int) -> PriceLevel:
    """Summarise one cluster of pivots into a :class:`PriceLevel`."""
    prices = [p.price for p in cluster]
    kinds = {p.kind for p in cluster}
    last_index = max(p.index for p in cluster)

    touch_score = min(1.0, len(cluster) / _TOUCH_SATURATION)
    recency = (last_index / (series_length - 1)) if series_length > 1 else 1.0
    strength = _TOUCH_WEIGHT * touch_score + _RECENCY_WEIGHT * min(1.0, recency)

    return PriceLevel(
        price=sum(prices) / len(prices),
        kind="mixed" if len(kinds) > 1 else kinds.pop(),
        touches=len(cluster),
        last_index=last_index,
        width=max(prices) - min(prices),
        strength=strength,
    )


def support_resistance(
    levels: list[PriceLevel], price: float
) -> tuple[list[PriceLevel], list[PriceLevel]]:
    """Split levels into supports (below ``price``) and resistances (above it).

    The split is done at read time rather than stored on the level because a level
    changes role without moving: the ceiling price broke through this morning is
    this afternoon's floor. Each list is ordered **nearest first**, so
    ``supports[0]`` is the next thing under the market.

    Args:
        levels: Levels from :func:`cluster_levels` and/or
            :func:`psychological_levels`.
        price: Live price to split around.

    Returns:
        ``(supports, resistances)``, each nearest-first.
    """
    supports = sorted(
        (level for level in levels if level.price < price),
        key=lambda level: price - level.price,
    )
    resistances = sorted(
        (level for level in levels if level.price > price),
        key=lambda level: level.price - price,
    )
    return supports, resistances


def round_step(price: float) -> float:
    """The natural "round number" increment for a price of this magnitude.

    Psychological levels are relative to the scale of the quote: for EUR/USD near
    1.08 the round numbers people watch are 0.01 apart (1.08, 1.09), for a DAX near
    18 000 they are 100 apart. Both are the same rule — **one hundredth of the
    price's own order of magnitude** — which is what this returns, so no per-epic
    table has to be maintained.

    Args:
        price: Live price (must be positive).

    Returns:
        The increment in price units, or 0.0 for a non-positive price.
    """
    if price <= 0:
        return 0.0
    return 10 ** (math.floor(math.log10(price)) - 2)


def psychological_levels(
    price: float, radius: float, step: float | None = None
) -> list[PriceLevel]:
    """Round-number levels within ``radius`` of ``price``, strongest first.

    Round numbers matter because orders pile up at them — stops, limits and manual
    targets are typed as round figures — so price tends to accelerate through them
    or stall just before. They are not read from the curve at all, which is exactly
    why they complement the swing levels: a level nobody has traded yet still
    stops a move.

    Not all round numbers are equally round. A multiple of 100 steps (a "big
    figure": 1.1000, 18 000) is watched far more than an intermediate one, so the
    :attr:`~PriceLevel.strength` returned grades roundness: 1.0 for a multiple of
    100 steps, 0.8 for 50, 0.6 for 10, 0.4 for 5, 0.2 otherwise. Every returned
    level still sits **on** the round grid — 0.2 means "an ordinary round number",
    not "not round".

    Args:
        price: Live price.
        radius: How far either side of ``price`` to look, in price units. Callers
            pass a volatility-scaled distance (e.g. two ATR) so a quiet market
            does not report levels it cannot reach today.
        step: Override for the increment; defaults to :func:`round_step`.

    Returns:
        Levels with ``kind="round"``, sorted by descending strength then by
        distance to ``price``; empty when the inputs are degenerate.
    """
    increment = round_step(price) if step is None else step
    if increment <= 0 or radius <= 0:
        return []

    first = math.ceil((price - radius) / increment)
    last = math.floor((price + radius) / increment)
    # A generous radius on a tiny step would enumerate thousands of levels; the
    # cap keeps the call cheap and the result readable (they are ranked anyway).
    if last - first > 400:
        return []

    levels = [
        PriceLevel(
            price=multiple * increment,
            kind="round",
            touches=0,
            last_index=-1,
            width=0.0,
            strength=_roundness(multiple),
        )
        for multiple in range(first, last + 1)
    ]
    return sorted(levels, key=lambda level: (-level.strength, abs(level.price - price)))


def _roundness(multiple: int) -> float:
    """Grade how "round" a step multiple is (see :func:`psychological_levels`)."""
    for divisor, score in ((100, 1.0), (50, 0.8), (10, 0.6), (5, 0.4)):
        if multiple % divisor == 0:
            return score
    return 0.2
