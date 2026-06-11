"""Tests for the compute module — indicator calculations."""

from datetime import UTC, datetime

import pytest

from src.services.compute import (
    atr,
    compute_levels,
    linear_regression,
    position_in_range,
    rate_of_change,
    sma,
)
from src.services.price_buffer import Candle


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


class TestSMA:
    """Tests for Simple Moving Average."""

    def test_basic_sma(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert sma(values, 3) == pytest.approx(40.0)  # (30+40+50)/3
        assert sma(values, 5) == pytest.approx(30.0)  # (10+20+30+40+50)/5

    def test_insufficient_data(self):
        values = [10.0, 20.0]
        assert sma(values, 5) == 0.0

    def test_single_period(self):
        values = [10.0, 20.0, 30.0]
        assert sma(values, 1) == pytest.approx(30.0)


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


class TestPositionInRange:
    """Tests for position_in_range."""

    def test_mid_range(self):
        assert position_in_range(50.0, 100.0, 0.0) == pytest.approx(50.0)

    def test_at_low(self):
        assert position_in_range(0.0, 100.0, 0.0) == pytest.approx(0.0)

    def test_at_high(self):
        assert position_in_range(100.0, 100.0, 0.0) == pytest.approx(100.0)

    def test_same_high_low(self):
        assert position_in_range(50.0, 50.0, 50.0) == pytest.approx(50.0)


class TestComputeLevels:
    """Tests for trading level calculations."""

    def test_spread_tactic(self):
        levels = compute_levels(
            bid=100.0,
            offer=101.0,
            high=110.0,
            low=90.0,
            bids=[95.0, 98.0, 100.0],
            tactic="spread",
        )
        # Spread = 1.0
        assert levels.spread == pytest.approx(1.0)
        assert levels.level_win > levels.bid
        assert levels.level_loose < levels.bid
        assert levels.level_security < levels.level_loose
        assert levels.stop_distance > 0

    def test_point_tactic(self):
        levels = compute_levels(
            bid=100.0,
            offer=101.0,
            high=110.0,
            low=90.0,
            bids=[100.0],
            follower_mult=3,
            win_mult=3,
            loose_mult=8,
            security_mult=5,
            tactic="point",
        )
        # With point tactic, base = 1.0
        assert levels.level_follower == pytest.approx(97.0)  # 100 - 3*1
        assert levels.level_win == pytest.approx(104.0)  # 100 + 1 + 3*1
        assert levels.stop_distance == pytest.approx(14)  # ceil(1 + 8 + 5)
