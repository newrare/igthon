"""Tests for the compute module — indicator calculations."""

from datetime import UTC, datetime

import pytest

from src.core.indicators import (
    adverse_tick_noise,
    atr,
    linear_regression,
    rate_of_change,
)
from src.feed.price_buffer import Candle


def _candle(high: float, low: float, close: float) -> Candle:
    """Build a candle with the bid OHLC values that ATR relies on."""
    return Candle(
        timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        bid_open=close,
        bid_close=close,
        bid_high=high,
        bid_low=low,
        offer_open=close,
        offer_close=close,
        offer_high=high,
        offer_low=low,
    )


class TestATR:
    """Tests for the Average True Range calculation."""

    def test_basic_atr(self):
        # TR(c1)=max(3, |103-100|, |100-100|)=3 ; TR(c2)=max(3, |104-102|, |101-102|)=3
        candles = [
            _candle(high=101, low=99, close=100),
            _candle(high=103, low=100, close=102),
            _candle(high=104, low=101, close=101),
        ]
        assert atr(candles, period=2) == pytest.approx(3.0)

    def test_gap_dominates_true_range(self):
        # A jump from prev_close 100 to a 109-111 candle: TR driven by the gap.
        candles = [
            _candle(high=100, low=100, close=100),
            _candle(high=111, low=109, close=110),
        ]
        assert atr(candles, period=1) == pytest.approx(11.0)

    def test_insufficient_data(self):
        # period + 1 candles are required.
        candles = [_candle(101, 99, 100), _candle(103, 100, 102)]
        assert atr(candles, period=2) == 0.0

    def test_non_positive_period(self):
        candles = [_candle(101, 99, 100), _candle(103, 100, 102)]
        assert atr(candles, period=0) == 0.0

    def test_empty(self):
        assert atr([], period=14) == 0.0


class TestAdverseTickNoise:
    """Tests for the adverse (downward) tick-noise band."""

    def test_pure_uptrend_has_zero_adverse_noise(self):
        # Every step is upward → no down-move contributes → 0.0.
        assert adverse_tick_noise([100.0, 101.0, 102.0, 103.0]) == 0.0

    def test_only_downward_moves_are_counted(self):
        # Up-drift is ignored; only the down-ticks size the band. Steps here are
        # +5, -2, +5 → downs = [0, 2, 0]: mean=2/3, var=8/9, std=2√2/3.
        closes = [100.0, 105.0, 103.0, 108.0]
        expected = 2 / 3 + 2.0 * ((8 / 9) ** 0.5)
        assert adverse_tick_noise(closes, window=20, std_k=2.0) == pytest.approx(
            expected
        )

    def test_symmetric_noise_ignores_up_legs(self):
        # A saw-tooth: the +1/-1 legs give the same down-band whatever the drift.
        assert adverse_tick_noise(
            [100.0, 99.0, 100.0, 99.0, 100.0], std_k=0.0
        ) == pytest.approx(0.5)

    def test_window_limits_the_lookback(self):
        # An old large down-tick outside the window is excluded.
        closes = [100.0, 80.0] + [100.0 + i for i in range(30)]
        assert adverse_tick_noise(closes, window=5) == 0.0

    def test_insufficient_data(self):
        assert adverse_tick_noise([100.0]) == 0.0
        assert adverse_tick_noise([]) == 0.0


class TestLinearRegression:
    """Tests for linear regression function."""

    def test_perfect_uptrend(self):
        """Perfect uptrend should have slope > 0 and R² = 1.0."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = linear_regression(values)
        assert result.slope == pytest.approx(1.0)
        assert result.r_squared == pytest.approx(1.0)

    def test_perfect_downtrend(self):
        """Perfect downtrend should have slope < 0 and R² = 1.0."""
        values = [5.0, 4.0, 3.0, 2.0, 1.0]
        result = linear_regression(values)
        assert result.slope == pytest.approx(-1.0)
        assert result.r_squared == pytest.approx(1.0)

    def test_flat(self):
        """Flat data should have slope = 0."""
        values = [3.0, 3.0, 3.0, 3.0, 3.0]
        result = linear_regression(values)
        assert result.slope == pytest.approx(0.0)

    def test_noisy_data(self):
        """Noisy data should have lower R²."""
        values = [1.0, 5.0, 2.0, 6.0, 3.0]
        result = linear_regression(values)
        assert result.r_squared < 0.7

    def test_insufficient_data(self):
        """Single value should return zeroes."""
        result = linear_regression([5.0])
        assert result.slope == 0.0
        assert result.r_squared == 0.0

    def test_empty_data(self):
        """Empty list should return zeroes."""
        result = linear_regression([])
        assert result.slope == 0.0


class TestROC:
    """Tests for Rate of Change."""

    def test_positive_roc(self):
        values = [100.0, 102.0, 105.0, 108.0, 110.0]
        roc = rate_of_change(values, 3)
        # (110 - 102) / 102 * 100 = 7.84
        assert roc == pytest.approx(7.843, rel=1e-2)

    def test_negative_roc(self):
        values = [110.0, 108.0, 105.0, 102.0, 100.0]
        roc = rate_of_change(values, 3)
        assert roc < 0

    def test_insufficient_data(self):
        values = [100.0, 110.0]
        roc = rate_of_change(values, 5)
        assert roc == 0.0
