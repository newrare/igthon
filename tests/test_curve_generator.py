"""Tests for the synthetic curve generator.

The generator's contract: a seeded call yields a reproducible, coherent list
of standard ``Candle`` objects — nothing else about the generation internals
is asserted (they are free to change).
"""

from datetime import timedelta

import pytest

from src.services.curve_generator import PROFILES, generate_curve


class TestContract:
    """Public contract: count, ordering, reproducibility, validation."""

    def test_returns_requested_number_of_candles(self):
        assert len(generate_curve("random", seed=1, num_candles=120)) == 120

    def test_same_seed_same_curve(self):
        a = generate_curve("random", seed=42, num_candles=200)
        b = generate_curve("random", seed=42, num_candles=200)
        assert [c.bid_close for c in a] == [c.bid_close for c in b]

    def test_different_seeds_differ(self):
        a = generate_curve("random", seed=1, num_candles=200)
        b = generate_curve("random", seed=2, num_candles=200)
        assert [c.bid_close for c in a] != [c.bid_close for c in b]

    def test_unknown_profile_rejected(self):
        with pytest.raises(ValueError):
            generate_curve("does_not_exist", seed=1)

    def test_timestamps_are_one_minute_apart(self):
        candles = generate_curve("sideways", seed=3, num_candles=60)
        for prev, cur in zip(candles, candles[1:]):
            assert cur.timestamp - prev.timestamp == timedelta(minutes=1)


class TestCoherence:
    """OHLC and bid/offer must stay internally consistent for every profile."""

    @pytest.mark.parametrize("profile", PROFILES)
    def test_ohlc_and_spread_coherent(self, profile):
        candles = generate_curve(profile, seed=7, num_candles=300)
        for c in candles:
            assert c.bid_high >= max(c.bid_open, c.bid_close)
            assert c.bid_low <= min(c.bid_open, c.bid_close)
            assert c.offer_close > c.bid_close  # positive spread
            assert c.spread > 0

    def test_spread_ratio_tradable(self):
        """Spreads stay below the strategy's max_spread_ratio gate (0.0015) —
        otherwise no simulated signal could ever fire."""
        candles = generate_curve("random", seed=11, num_candles=600)
        ratios = [c.spread / c.bid_close for c in candles]
        assert sum(ratios) / len(ratios) < 0.0015


class TestProfiles:
    """Profiles shape the drift as advertised (checked over several seeds)."""

    def _net_moves(self, profile: str) -> list[float]:
        moves = []
        for seed in range(10):
            candles = generate_curve(profile, seed=seed, num_candles=400)
            moves.append(candles[-1].bid_close - candles[0].bid_close)
        return moves

    def test_trend_up_mostly_rises(self):
        moves = self._net_moves("trend_up")
        assert sum(1 for m in moves if m > 0) >= 8

    def test_trend_down_mostly_falls(self):
        moves = self._net_moves("trend_down")
        assert sum(1 for m in moves if m < 0) >= 8

    def test_mixte_is_a_supported_profile(self):
        assert "mixte" in PROFILES

    def test_mixte_mixes_both_directions(self):
        """Mixing every behaviour, "mixte" should produce both up and down
        net moves across seeds (not a single dominant direction)."""
        moves = self._net_moves("mixte")
        assert any(m > 0 for m in moves) and any(m < 0 for m in moves)
