"""Curve-shape similarity — deciding "same shape" from the maths, not the name.

Shared, purely-functional infrastructure (no trading decision here): it turns a
price curve into a scale-free **signature** and answers one question about a pair
of candidate trades — *are these two the same bet wearing two names?*

Why a mathematical measure rather than the epic label
-----------------------------------------------------

A cross-epic ranker that opens the top N of a tournament has a blind spot: the
best-scoring curves are often the *same* curve. London cocoa and New York cocoa,
the CAC and the EuroStoxx, or gold in dollars and gold in euros move together, so
a "diversified" basket of five can in truth be one position sized five times —
five stops that all fire on the same tick. Neither the epic string nor the market
description reveals that reliably (IG names are inconsistent, and genuinely
independent markets sometimes share a word), so the test here is on the numbers:
two markets are duplicates when their recent **return paths** are correlated.

The signature
-------------

:func:`shape_signature` reduces the last ``window`` candles to the series of
timestamp-keyed **relative returns** ``(pₜ − pₜ₋₁) / pₜ₋₁``. Returns rather than
prices, for two reasons: they are dimensionless, so an index quoted at 8000 and a
forex pair at 1.08 are directly comparable, and they remove the level, so what is
left is only the *shape* of the move. Each signature also carries a short
``fingerprint`` string — the compact identifier for logs and the dashboard (see
its definition below).

Redundancy: correlation, signed by the two trade directions
-----------------------------------------------------------

Raw correlation is not quite the right measure, because a duplicated *bet* is not
the same thing as a duplicated *curve*. What a position expresses is
``direction × return``, so two trades are redundant when

    dir_a · dir_b · corr(returns_a, returns_b)

is high — :func:`bet_redundancy`. This handles both traps with one formula:

- **two names, one market** — cocoa LDN long + cocoa NY long: ``+1 · +1 · 0.95 =
  0.95`` → duplicate, as expected;
- **mirrored pair** — EUR/USD long + USD/CHF short, whose curves are *anti*
  correlated: ``+1 · −1 · (−0.90) = +0.90`` → also a duplicate, which a plain
  ``corr`` would have read as ``−0.90`` and waved through. It is the same bet on
  the dollar taken twice.

A high *negative* redundancy is the opposite situation — two opposing bets on
correlated markets, a hedge rather than a duplicate — and is deliberately not
filtered: it adds no concentration risk.

Alignment and the abstention rule
---------------------------------

Correlation is only meaningful on *simultaneous* observations, so the two return
series are intersected **by timestamp** before being compared; a market that came
online late, or whose subscription stalled, simply contributes fewer aligned
points. Below ``min_overlap`` common points — or when either series is perfectly
flat, which leaves the correlation undefined — every function here returns
``None`` rather than a number. Callers must read ``None`` as *"cannot judge"*, and
:func:`deduplicate` treats it as **not** a duplicate: refusing a candidate on the
strength of a correlation that could not be computed would silently shrink the
basket for a data reason rather than a market one.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from src.feed.price_buffer import Candle

logger = logging.getLogger(__name__)

#: Fingerprint alphabet — z-score cut points and the symbol per bucket. Five
#: buckets keep the encoding readable while still separating a sharp move from a
#: mild one; the outer cuts sit at ±1.5σ so ordinary jitter lands in the middle.
_FINGERPRINT_CUTS: tuple[float, ...] = (-1.5, -0.5, 0.5, 1.5)
_FINGERPRINT_SYMBOLS = "abcde"


@dataclass(slots=True, frozen=True)
class ShapeSignature:
    """Scale-free identity of one curve's recent shape.

    Attributes:
        epic: Market the signature was built from.
        stamps: Timestamp of each return (that of the *closing* candle of the
            step), used to align two signatures before comparing them.
        returns: Relative returns ``(pₜ − pₜ₋₁) / pₜ₋₁``, oldest first and
            positionally paired with ``stamps``. Dimensionless, so curves of any
            price scale are comparable.
        fingerprint: Short hexadecimal identifier of the *quantised* path — the
            compact id to print in a log line or show in the UI. Two signatures
            sharing a fingerprint have an identical quantised path, i.e. they are
            the same curve; the converse does not hold, so the fingerprint is an
            identity shortcut and **not** the similarity test. Merely *similar*
            curves get different fingerprints and are caught by
            :func:`bet_redundancy`.
    """

    epic: str
    stamps: tuple[datetime, ...]
    returns: tuple[float, ...]
    fingerprint: str

    def __len__(self) -> int:
        return len(self.returns)


def _fingerprint(returns: Sequence[float]) -> str:
    """Short hex id of the z-scored, quantised return path.

    The returns are standardised (so scale and average drift drop out), each
    mapped to one of five symbols by :data:`_FINGERPRINT_CUTS`, and the resulting
    word is hashed to eight hex characters — short enough for a log line, and
    stable across runs (unlike :func:`hash`, which is salted per process).

    A degenerate curve (fewer than two points, or zero variance) has no shape to
    encode and yields ``"flat"``.
    """
    if len(returns) < 2:
        return "flat"
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    if variance <= 0:
        return "flat"
    deviation = math.sqrt(variance)

    word = []
    for value in returns:
        z = (value - mean) / deviation
        bucket = sum(1 for cut in _FINGERPRINT_CUTS if z >= cut)
        word.append(_FINGERPRINT_SYMBOLS[bucket])
    digest = hashlib.blake2s("".join(word).encode(), digest_size=4)
    return digest.hexdigest()


def shape_signature(
    epic: str, candles: Sequence[Candle], window: int
) -> ShapeSignature | None:
    """Build the shape signature of the last ``window`` candles, or ``None``.

    Uses the **bid** closes (the curve every other decision in the bot is written
    on) and converts them to relative returns, so the signature carries the shape
    of the move and nothing about the price level or the instrument's unit.

    Args:
        epic: Market identifier, carried on the result for logging.
        candles: Ordered candles, oldest first. Fewer than ``window`` are used as
            they come — a market that came online late still gets a (shorter)
            signature, and the timestamp alignment in :func:`shape_correlation`
            makes the comparison honest.
        window: Number of most-recent candles to read, yielding at most
            ``window - 1`` returns.

    Returns:
        The signature, or ``None`` when there are fewer than two usable candles
        or a non-positive price makes a relative return meaningless.
    """
    if window < 2:
        return None
    recent = list(candles[-window:])
    if len(recent) < 2:
        return None

    stamps: list[datetime] = []
    returns: list[float] = []
    for previous, current in zip(recent, recent[1:]):
        if previous.bid_close <= 0:
            return None  # a non-positive price makes the whole series unusable
        stamps.append(current.timestamp)
        returns.append((current.bid_close - previous.bid_close) / previous.bid_close)

    return ShapeSignature(
        epic=epic,
        stamps=tuple(stamps),
        returns=tuple(returns),
        fingerprint=_fingerprint(returns),
    )


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation of two equal-length series, or ``None`` if undefined.

    ``None`` means the coefficient does not exist rather than "no correlation":
    fewer than two points, or a series with zero variance (a perfectly flat
    curve has no shape to correlate with anything).
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    var_x = sum(d * d for d in dx)
    var_y = sum(d * d for d in dy)
    if var_x <= 0 or var_y <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / math.sqrt(var_x * var_y)


def shape_correlation(
    a: ShapeSignature, b: ShapeSignature, *, min_overlap: int
) -> float | None:
    """Correlation of two curves over the timestamps they actually share.

    The two return series are intersected **by timestamp** first: correlating
    unaligned samples would compare Monday's move on one market with Tuesday's on
    the other and report a meaningless figure. Epics on the same one-minute feed
    align exactly; one that came online late or stalled contributes fewer points.

    Args:
        a: First signature.
        b: Second signature.
        min_overlap: Minimum number of shared timestamps required to trust the
            result.

    Returns:
        The correlation in [-1, 1], or ``None`` when the overlap is shorter than
        ``min_overlap`` or the coefficient is undefined (a flat series).
    """
    shared = {stamp: value for stamp, value in zip(a.stamps, a.returns)}
    xs: list[float] = []
    ys: list[float] = []
    for stamp, value in zip(b.stamps, b.returns):
        other = shared.get(stamp)
        if other is not None:
            xs.append(other)
            ys.append(value)
    if len(xs) < max(2, min_overlap):
        return None
    return _pearson(xs, ys)


def bet_redundancy(
    a: ShapeSignature,
    b: ShapeSignature,
    direction_a: str,
    direction_b: str,
    *,
    min_overlap: int,
) -> float | None:
    """How much two candidate trades are **the same bet**, in [-1, 1].

    ``dir_a · dir_b · corr(a, b)`` — the correlation of the two curves signed by
    the two trade directions (see the module docstring). Close to ``+1`` the two
    positions rise and fall together and are one bet taken twice, whether they are
    two listings of the same commodity (correlated curves, same side) or a
    mirrored pair (anti-correlated curves, opposite sides). Close to ``-1`` they
    offset each other — a hedge, not a duplicate.

    Signatures sharing a :attr:`~ShapeSignature.fingerprint` are the same curve by
    construction, so their correlation is taken as ``1.0`` without needing the
    timestamp overlap; the direction product is still applied.

    Returns:
        The signed redundancy, or ``None`` when the curves cannot be compared
        (insufficient overlap, or a flat series) — read as *"cannot judge"*.
    """
    if a.fingerprint == b.fingerprint and a.fingerprint != "flat":
        correlation: float | None = 1.0
    else:
        correlation = shape_correlation(a, b, min_overlap=min_overlap)
    if correlation is None:
        return None
    sign_a = 1.0 if direction_a == "BUY" else -1.0
    sign_b = 1.0 if direction_b == "BUY" else -1.0
    return sign_a * sign_b * correlation


@dataclass(slots=True, frozen=True)
class DuplicateDrop:
    """One candidate refused as a shape duplicate of an already-kept one.

    Attributes:
        index: Position (in the input sequence) of the candidate that was dropped.
        against: Position of the kept candidate it duplicates.
        redundancy: The signed redundancy that triggered the veto.
    """

    index: int
    against: int
    redundancy: float


def deduplicate(
    items: Sequence[tuple[ShapeSignature, str]],
    *,
    max_redundancy: float,
    min_overlap: int,
) -> tuple[list[int], list[DuplicateDrop]]:
    """Keep one representative per distinct shape, scanning ``items`` in order.

    Greedy and **order-sensitive by design**: the caller passes its candidates
    best-first (a ranking), the first occurrence of a shape is kept and any later
    candidate too redundant with something already kept is dropped. So of two
    listings of the same commodity the better-ranked one survives — which is the
    intent, and the reason this is not a symmetric clustering.

    Redundancy is compared against **every** kept item, not only the previous one:
    similarity is not transitive, and a candidate may be a duplicate of the third
    survivor while being independent of the first two.

    Args:
        items: ``(signature, direction)`` pairs in preference order.
        max_redundancy: Veto threshold — a candidate whose signed redundancy with
            any kept item is **strictly greater** is dropped. ``1.0`` therefore
            keeps everything except exact-fingerprint matches, and a value ``>=
            1.0`` disables the filter for all practical purposes.
        min_overlap: Minimum shared timestamps for a comparison to count (see
            :func:`shape_correlation`). Pairs that cannot be compared are kept.

    Returns:
        ``(kept, dropped)`` — the indices kept in input order, and one
        :class:`DuplicateDrop` per refusal for the caller to log.
    """
    kept: list[int] = []
    dropped: list[DuplicateDrop] = []
    for index, (signature, direction) in enumerate(items):
        duplicate_of: DuplicateDrop | None = None
        for other in kept:
            other_signature, other_direction = items[other]
            redundancy = bet_redundancy(
                signature,
                other_signature,
                direction,
                other_direction,
                min_overlap=min_overlap,
            )
            if redundancy is not None and redundancy > max_redundancy:
                duplicate_of = DuplicateDrop(
                    index=index, against=other, redundancy=redundancy
                )
                break
        if duplicate_of is None:
            kept.append(index)
        else:
            dropped.append(duplicate_of)
    return kept, dropped
