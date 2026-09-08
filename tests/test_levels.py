"""Tests for the readable-levels module — swing pivots, clusters, round numbers."""

import pytest

from src.core.levels import (
    Pivot,
    cluster_levels,
    psychological_levels,
    round_step,
    support_resistance,
    swing_pivots,
)


class TestSwingPivots:
    """Detection of local tops and bottoms."""

    def test_finds_top_and_bottom(self):
        highs = [1, 2, 5, 2, 1, 2, 3]
        lows = [h - 1 for h in highs]
        pivots = swing_pivots(highs, lows, strength=2)
        assert Pivot(index=2, price=5, kind="high") in pivots
        assert Pivot(index=4, price=0, kind="low") in pivots

    def test_last_candles_can_never_be_pivots(self):
        # An unconfirmed turn is not a level: the right-hand window must exist.
        highs = [1, 2, 3, 4, 9]
        lows = [h - 1 for h in highs]
        assert all(p.index <= 2 for p in swing_pivots(highs, lows, strength=2))

    def test_plateau_yields_one_pivot(self):
        # The most recent candle of the plateau is the one kept.
        highs = [1, 2, 5, 5, 2, 1, 1]
        lows = [h - 1 for h in highs]
        tops = [p for p in swing_pivots(highs, lows, strength=2) if p.kind == "high"]
        assert len(tops) == 1
        assert tops[0].index == 3

    def test_series_too_short(self):
        assert swing_pivots([1, 2, 3], [0, 1, 2], strength=2) == []


class TestClusterLevels:
    """Merging pivots into levels and scoring them."""

    def test_nearby_pivots_merge_and_count_touches(self):
        pivots = [
            Pivot(index=2, price=100.0, kind="low"),
            Pivot(index=10, price=100.2, kind="low"),
            Pivot(index=20, price=99.9, kind="low"),
            Pivot(index=30, price=120.0, kind="high"),
        ]
        levels = cluster_levels(pivots, tolerance=0.5, series_length=40)
        merged = [level for level in levels if level.price < 110]
        assert len(merged) == 1
        assert merged[0].touches == 3
        assert merged[0].price == pytest.approx(100.03, abs=0.01)
        assert merged[0].last_index == 20

    def test_mixed_kind_when_both_sides_turned_there(self):
        pivots = [
            Pivot(index=1, price=50.0, kind="low"),
            Pivot(index=5, price=50.1, kind="high"),
        ]
        assert cluster_levels(pivots, tolerance=0.5)[0].kind == "mixed"

    def test_touches_beat_recency_in_the_ranking(self):
        pivots = [
            Pivot(index=1, price=10.0, kind="low"),
            Pivot(index=2, price=10.0, kind="low"),
            Pivot(index=3, price=10.0, kind="low"),
            Pivot(index=39, price=80.0, kind="high"),
        ]
        levels = cluster_levels(pivots, tolerance=0.5, series_length=40)
        assert levels[0].price == pytest.approx(10.0)

    def test_chaining_does_not_drag_a_cluster(self):
        # Each step is within tolerance of the previous pivot but not of the mean,
        # so the cluster must not creep all the way from 100 to 103.
        pivots = [Pivot(index=i, price=100.0 + i, kind="low") for i in range(4)]
        levels = cluster_levels(pivots, tolerance=1.0, series_length=10)
        assert len(levels) > 1

    def test_degenerate_inputs(self):
        assert cluster_levels([], tolerance=1.0) == []
        assert cluster_levels([Pivot(0, 1.0, "low")], tolerance=0.0) == []


class TestSupportResistance:
    """Splitting levels around the live price."""

    def test_split_and_order(self):
        pivots = [
            Pivot(index=1, price=90.0, kind="low"),
            Pivot(index=2, price=95.0, kind="low"),
            Pivot(index=3, price=105.0, kind="high"),
            Pivot(index=4, price=110.0, kind="high"),
        ]
        levels = cluster_levels(pivots, tolerance=0.5, series_length=10)
        supports, resistances = support_resistance(levels, price=100.0)
        assert [level.price for level in supports] == [95.0, 90.0]
        assert [level.price for level in resistances] == [105.0, 110.0]


class TestPsychologicalLevels:
    """Round-number levels."""

    @pytest.mark.parametrize(
        "price,expected",
        [(1.0823, 0.01), (18_000, 100), (2400, 10), (0.0, 0.0)],
    )
    def test_round_step_follows_the_magnitude(self, price, expected):
        assert round_step(price) == pytest.approx(expected)

    def test_roundest_level_in_range_comes_first(self):
        # Step is 0.01 here, so 1.10 sits on the ten-step grid (0.6) while 1.09
        # and 1.11 are ordinary increments (0.2).
        levels = psychological_levels(1.0823, radius=0.03)
        assert levels[0].price == pytest.approx(1.10)
        assert levels[0].strength == pytest.approx(0.6)
        assert levels[0].kind == "round"

    def test_round_figure_outranks_its_neighbours(self):
        # Step is 100 here: 18 000 sits on the ten-step grid, 18 050 on the
        # five-step one, 18 100 on neither.
        strengths = {
            level.price: level.strength
            for level in psychological_levels(18_042, radius=1200)
        }
        assert strengths[18_000] == pytest.approx(0.6)
        assert strengths[18_500] == pytest.approx(0.4)
        assert strengths[18_100] == pytest.approx(0.2)

    def test_every_level_is_inside_the_radius(self):
        levels = psychological_levels(18_042, radius=250)
        assert levels
        assert all(abs(level.price - 18_042) <= 250 for level in levels)

    def test_degenerate_inputs(self):
        assert psychological_levels(1.08, radius=0) == []
        assert psychological_levels(0.0, radius=1) == []
        # A tiny step over a huge radius would enumerate thousands of levels.
        assert psychological_levels(1.08, radius=100, step=0.0001) == []
