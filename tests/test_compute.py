"""Tests for the compute module — indicator calculations."""

from datetime import UTC, datetime

import pytest

from src.core.indicators import (
    adverse_tick_noise,
    atr,
    channel_position,
    linear_regression,
    rate_of_change,
    trend_pct,
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


class TestTrendPct:
    """``trend_pct`` normalises the raw slope into a cross-epic comparable move."""

    def test_reports_the_implied_percentage_move_of_the_fit(self):
        # A perfectly linear +1/candle climb over 100 candles ending at 200.0:
        # the fit implies ~100 points of move, i.e. ~50% of the last price.
        closes = [100.0 + i for i in range(100)]
        pct, r2 = trend_pct(closes, 100)
        assert pct == pytest.approx(100 * 100 / 199.0, rel=1e-6)
        assert r2 == pytest.approx(1.0)

    def test_sign_follows_the_direction_of_the_move(self):
        rising = [100.0 + i for i in range(60)]
        falling = [100.0 - i * 0.5 for i in range(60)]
        assert trend_pct(rising, 60)[0] > 0
        assert trend_pct(falling, 60)[0] < 0

    def test_is_comparable_across_price_scales(self):
        """The point of the normalisation: a 1% move scores the same on a
        1.10 forex pair and on a 5000-point index."""
        forex = [1.10 * (1 + 0.01 * i / 59) for i in range(60)]
        index = [5000.0 * (1 + 0.01 * i / 59) for i in range(60)]
        assert trend_pct(forex, 60)[0] == pytest.approx(
            trend_pct(index, 60)[0], rel=1e-6
        )

    def test_uses_the_whole_series_when_shorter_than_the_period(self):
        closes = [100.0 + i for i in range(10)]
        assert trend_pct(closes, 60) == trend_pct(closes, 10)

    def test_degenerate_inputs_are_zero(self):
        assert trend_pct([], 60) == (0.0, 0.0)
        assert trend_pct([100.0], 60) == (0.0, 0.0)
        assert trend_pct([100.0, 200.0], 1) == (0.0, 0.0)
        assert trend_pct([0.0, 0.0], 2) == (0.0, 0.0)  # non-positive last price


class TestChannelPosition:
    """``channel_position`` locates the last bid inside its high/low channel."""

    @staticmethod
    def _candles(lows_highs, last_close):
        out = []
        for i, (lo, hi) in enumerate(lows_highs):
            close = last_close if i == len(lows_highs) - 1 else (lo + hi) / 2
            out.append(
                Candle(
                    timestamp=datetime(2024, 1, 1, 9, i, tzinfo=UTC),
                    bid_open=close,
                    bid_close=close,
                    bid_high=hi,
                    bid_low=lo,
                    offer_open=close,
                    offer_close=close,
                    offer_high=hi,
                    offer_low=lo,
                )
            )
        return out

    def test_reports_zero_at_the_low_and_one_at_the_high(self):
        band = [(100.0, 200.0)] * 5
        assert self._candles(band, 100.0) and channel_position(
            self._candles(band, 100.0), 5
        ) == (0.0, 200.0, 100.0)
        assert channel_position(self._candles(band, 200.0), 5) == (1.0, 200.0, 100.0)

    def test_reports_the_midpoint_in_the_middle(self):
        pos, high, low = channel_position(self._candles([(0.0, 10.0)] * 5, 5.0), 5)
        assert (pos, high, low) == (0.5, 10.0, 0.0)

    def test_flat_channel_is_reported_as_the_midpoint(self):
        """A degenerate channel has no meaningful position — never divide by zero."""
        pos, high, low = channel_position(self._candles([(7.0, 7.0)] * 5, 7.0), 5)
        assert pos == 0.5 and high == 7.0 and low == 7.0

    def test_only_the_last_period_candles_are_scanned(self):
        # An old 0-1000 spike must not widen the channel of the recent window.
        candles = self._candles([(0.0, 1000.0)] + [(100.0, 200.0)] * 4, 150.0)
        pos, high, low = channel_position(candles, 4)
        assert (high, low) == (200.0, 100.0)
        assert pos == pytest.approx(0.5)

    def test_empty_input_is_the_midpoint(self):
        assert channel_position([], 5) == (0.5, 0.0, 0.0)
