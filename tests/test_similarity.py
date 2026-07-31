"""Tests for the curve-shape similarity maths (src/core/similarity.py).

The module answers one question — *are these two candidate trades the same bet?* —
from the numbers alone, so the tests are built on synthetic curve pairs whose
relationship is known by construction: a scaled copy (two listings of one
commodity), a mirror (an anti-correlated pair), and two independent zig-zags. They
also cover the three abstention paths (short overlap, flat series, unbuildable
signature), which must read as "cannot judge" and never as "duplicate".
"""

from datetime import UTC, datetime, timedelta

from src.core.similarity import (
    bet_redundancy,
    deduplicate,
    shape_correlation,
    shape_signature,
)
from src.feed.price_buffer import Candle


def _candles(
    closes: list[float], start: datetime | None = None, spread: float = 0.5
) -> list[Candle]:
    """One candle per minute from the given bid closes."""
    stamp = start or datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    out: list[Candle] = []
    prev = closes[0]
    for close in closes:
        out.append(
            Candle(
                timestamp=stamp,
                bid_open=prev,
                bid_close=close,
                bid_high=max(prev, close),
                bid_low=min(prev, close),
                offer_open=prev + spread,
                offer_close=close + spread,
                offer_high=max(prev, close) + spread,
                offer_low=min(prev, close) + spread,
            )
        )
        prev = close
        stamp += timedelta(minutes=1)
    return out


def _wobbly_rise(n: int = 40, start: float = 100.0) -> list[float]:
    """A rising curve with an irregular, non-repeating wobble on top."""
    return [start + i * 0.5 + (i % 7) * 0.3 - (i % 3) * 0.4 for i in range(n)]


def _independent(n: int = 40, start: float = 100.0) -> list[float]:
    """A curve whose step pattern shares no rhythm with :func:`_wobbly_rise`."""
    return [start + i * 0.4 + (i % 5) * 0.6 - (i % 11) * 0.25 for i in range(n)]


def _signature(closes: list[float], window: int = 60, **kwargs):
    return shape_signature("EPIC", _candles(closes, **kwargs), window)


class TestShapeSignature:
    def test_returns_are_scale_free(self):
        """The same shape at two price scales yields the same return series.

        This is the whole point of using returns rather than prices: London and New
        York cocoa quote the same commodity at different absolute levels, and the
        signature must not see a difference.
        """
        base = _wobbly_rise()
        small = _signature(base)
        large = _signature([c * 137.0 for c in base])

        assert small is not None and large is not None
        assert len(small.returns) == len(base) - 1
        for a, b in zip(small.returns, large.returns):
            assert abs(a - b) < 1e-12
        # ...and the compact identifier follows the shape, not the price level.
        assert small.fingerprint == large.fingerprint

    def test_window_bounds_the_signature(self):
        signature = _signature(_wobbly_rise(40), window=10)

        assert signature is not None
        assert len(signature.returns) == 9

    def test_stamps_pair_with_the_returns(self):
        closes = _wobbly_rise(5)
        signature = _signature(closes)

        assert signature is not None
        # A return is stamped with the *closing* candle of its step, so the first
        # candle contributes no return.
        assert len(signature.stamps) == len(signature.returns) == 4
        assert signature.stamps[0] == datetime(2024, 1, 1, 9, 1, tzinfo=UTC)

    def test_too_short_a_curve_has_no_signature(self):
        assert _signature([100.0]) is None
        assert shape_signature("EPIC", _candles(_wobbly_rise()), window=1) is None

    def test_non_positive_price_has_no_signature(self):
        # A relative return against a zero price is meaningless, so the whole
        # series is refused rather than silently patched.
        assert _signature([100.0, 0.0, 100.0]) is None

    def test_flat_curve_fingerprints_as_flat(self):
        signature = _signature([100.0] * 30)

        assert signature is not None
        assert signature.fingerprint == "flat"


class TestShapeCorrelation:
    def test_a_scaled_copy_correlates_at_one(self):
        a = _signature(_wobbly_rise())
        b = _signature([c * 137.0 for c in _wobbly_rise()])

        correlation = shape_correlation(a, b, min_overlap=20)

        assert correlation is not None
        assert correlation > 0.999

    def test_a_mirror_correlates_at_minus_one(self):
        """A price-mirrored curve comes out near -1, but not exactly.

        Relative returns divide each step by the previous price, and the mirror's
        price *falls* where the original rises, so the two denominators drift
        apart. The measured -0.996 (rather than a clean -1.0) is that arithmetic,
        not noise — and it is why the veto threshold is set well below 1.
        """
        base = _wobbly_rise()
        a = _signature(base)
        b = _signature([200.0 - (c - 100.0) for c in base])

        correlation = shape_correlation(a, b, min_overlap=20)

        assert correlation is not None
        assert correlation < -0.99

    def test_independent_curves_stay_well_below_the_veto(self):
        a = _signature(_wobbly_rise())
        b = _signature(_independent())

        correlation = shape_correlation(a, b, min_overlap=20)

        assert correlation is not None
        assert abs(correlation) < 0.8

    def test_only_shared_timestamps_are_compared(self):
        """Series offset in time are intersected, not compared position by position.

        Both curves are the same shape but shifted by 20 minutes. Aligning on
        timestamps leaves only the genuinely simultaneous half, so the reported
        figure describes what actually happened at the same moment.
        """
        base = _wobbly_rise(40)
        a = _signature(base)
        b = _signature(base, start=datetime(2024, 1, 1, 9, 20, tzinfo=UTC))

        assert shape_correlation(a, b, min_overlap=20) is None  # only 19 shared
        assert shape_correlation(a, b, min_overlap=10) is not None

    def test_short_overlap_abstains(self):
        a = _signature(_wobbly_rise(40))
        b = _signature(_wobbly_rise(5))  # 4 returns only

        assert shape_correlation(a, b, min_overlap=20) is None

    def test_a_flat_series_abstains(self):
        # Zero variance leaves the coefficient undefined — that is not "0".
        a = _signature(_wobbly_rise(40))
        b = _signature([100.0] * 40)

        assert shape_correlation(a, b, min_overlap=20) is None


class TestBetRedundancy:
    def test_two_listings_of_one_commodity_bought_together(self):
        a = _signature(_wobbly_rise())
        b = _signature([c * 137.0 for c in _wobbly_rise()])

        redundancy = bet_redundancy(a, b, "BUY", "BUY", min_overlap=20)

        assert redundancy is not None
        assert redundancy > 0.99  # one bet taken twice

    def test_mirrored_pair_on_opposite_sides_is_also_one_bet(self):
        """The trap a plain correlation waves through.

        Long EUR/USD beside short USD/CHF: the curves are anti-correlated, so the
        raw coefficient is about -1 and reads as "unrelated or hedged". Signing by
        the two directions turns it back into the +1 it really is.
        """
        base = _wobbly_rise()
        a = _signature(base)
        b = _signature([200.0 - (c - 100.0) for c in base])

        assert shape_correlation(a, b, min_overlap=20) < -0.99
        redundancy = bet_redundancy(a, b, "BUY", "SELL", min_overlap=20)

        assert redundancy is not None
        assert redundancy > 0.99

    def test_opposite_sides_of_the_same_curve_is_a_hedge(self):
        a = _signature(_wobbly_rise())
        b = _signature([c * 137.0 for c in _wobbly_rise()])

        redundancy = bet_redundancy(a, b, "BUY", "SELL", min_overlap=20)

        # Strongly negative: the two positions offset each other rather than
        # stacking the same risk, so the duplicate filter must let it through.
        assert redundancy is not None
        assert redundancy < -0.99

    def test_identical_fingerprints_skip_the_overlap_requirement(self):
        """The same curve is the same curve, even with too little overlap to fit.

        Both signatures are built from the same closes, so the quantised paths — and
        therefore the fingerprints — are identical. There are only 4 shared returns,
        far below ``min_overlap``, yet this must still count as a duplicate.
        """
        closes = _wobbly_rise(5)
        a = _signature(closes)
        b = _signature(closes)

        assert shape_correlation(a, b, min_overlap=20) is None
        assert bet_redundancy(a, b, "BUY", "BUY", min_overlap=20) == 1.0

    def test_two_flat_curves_are_not_declared_identical(self):
        # Both fingerprint as "flat", which is the *absence* of a shape — it must
        # not be mistaken for a shared one.
        a = _signature([100.0] * 40)
        b = _signature([250.0] * 40)

        assert a.fingerprint == b.fingerprint == "flat"
        assert bet_redundancy(a, b, "BUY", "BUY", min_overlap=20) is None

    def test_abstains_when_the_curves_cannot_be_compared(self):
        a = _signature(_wobbly_rise(40))
        b = _signature(_independent(5))

        assert bet_redundancy(a, b, "BUY", "BUY", min_overlap=20) is None


class TestDeduplicate:
    def _items(self, *curves: tuple[list[float], str]):
        return [
            (shape_signature(f"E{i}", _candles(closes), 60), direction)
            for i, (closes, direction) in enumerate(curves)
        ]

    def test_keeps_the_first_of_a_duplicate_pair(self):
        base = _wobbly_rise()
        items = self._items(
            (base, "BUY"),
            ([c * 137.0 for c in base], "BUY"),
            (_independent(), "BUY"),
        )

        kept, dropped = deduplicate(items, max_redundancy=0.8, min_overlap=20)

        # Order is preference order, so the better-ranked twin (index 0) survives.
        assert kept == [0, 2]
        assert len(dropped) == 1
        assert (dropped[0].index, dropped[0].against) == (1, 0)
        assert dropped[0].redundancy > 0.99

    def test_compares_against_every_kept_item_not_just_the_last(self):
        """Similarity is not transitive, so the check cannot be pairwise-adjacent.

        Candidate 2 is independent of candidate 1 but a copy of candidate 0. Only a
        comparison against *all* survivors catches it.
        """
        base = _wobbly_rise()
        items = self._items(
            (base, "BUY"),
            (_independent(), "BUY"),
            ([c * 5.0 for c in base], "BUY"),
        )

        kept, dropped = deduplicate(items, max_redundancy=0.8, min_overlap=20)

        assert kept == [0, 1]
        assert (dropped[0].index, dropped[0].against) == (2, 0)

    def test_a_hedge_is_kept(self):
        base = _wobbly_rise()
        items = self._items((base, "BUY"), ([c * 137.0 for c in base], "SELL"))

        kept, dropped = deduplicate(items, max_redundancy=0.8, min_overlap=20)

        assert kept == [0, 1]
        assert dropped == []

    def test_uncomparable_pairs_are_kept(self):
        # Too little overlap to judge: refusing here would shrink the basket for a
        # data reason rather than a market one.
        base = _wobbly_rise()
        items = self._items((base, "BUY"), (base[:5], "BUY"))

        kept, _ = deduplicate(items, max_redundancy=0.8, min_overlap=20)

        assert kept == [0, 1]

    def test_a_high_threshold_disables_the_filter(self):
        base = _wobbly_rise()
        items = self._items((base, "BUY"), ([c * 137.0 for c in base], "BUY"))

        kept, dropped = deduplicate(items, max_redundancy=1.0, min_overlap=20)

        # Strictly-greater comparison, so a perfect copy at exactly 1.0 survives.
        assert kept == [0, 1]
        assert dropped == []

    def test_empty_input(self):
        assert deduplicate([], max_redundancy=0.8, min_overlap=20) == ([], [])
