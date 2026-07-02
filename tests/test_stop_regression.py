"""Tests for the regression-channel, trend-and-noise-aware stop-distance policy.

``StopRegression`` places the initial protective stop a residual-noise band
below the entry: the band width is the std of the residuals around the price's
linear regression (noise with the trend removed), scaled up when the move is
choppy (low efficiency ratio). These tests cover the ``residual_sigma`` helper,
the registry wiring and ``initial_stop`` (floor, choppiness widening, the
trend-independence of the noise measure, and the SELL fallback).
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopAtr, StopRegression, get_stop_distance
from src.stops.base import StopDistance
from src.stops.stop_regression import residual_sigma

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        buf.add(
            Candle(
                timestamp=_START + timedelta(minutes=i),
                bid_open=prev,
                bid_close=close,
                bid_high=high,
                bid_low=low,
                offer_open=prev + spread,
                offer_close=close + spread,
                offer_high=high + spread,
                offer_low=low + spread,
            )
        )
        prev = close
    return buf


class TestResidualSigma:
    def test_perfect_line_has_zero_residual(self):
        # Points exactly on a line leave no residual dispersion.
        assert residual_sigma([float(i) for i in range(20)]) == pytest.approx(0.0)

    def test_short_series_returns_zero(self):
        assert residual_sigma([]) == 0.0
        assert residual_sigma([42.0]) == 0.0

    def test_detrended_noise_only(self):
        # A pure trend has zero residual sigma; the same trend plus a symmetric
        # zig-zag has a strictly positive residual sigma of the zig-zag size.
        n = 40
        trend = [100.0 + i for i in range(n)]
        assert residual_sigma(trend) == pytest.approx(0.0, abs=1e-9)
        zig = [v + (1.0 if i % 2 else -1.0) for i, v in enumerate(trend)]
        # ≈ 1.0: the regression absorbs a tiny bias from the alternating pattern.
        assert residual_sigma(zig) == pytest.approx(1.0, abs=2e-3)

    def test_independent_of_trend_slope(self):
        # Two series with the SAME noise (identical zig-zag) but different slopes
        # must have the SAME residual sigma — the noise measure is trend-free.
        n = 40
        flat = [100.0 + (1.0 if i % 2 else -1.0) for i in range(n)]
        steep = [100.0 + 5 * i + (1.0 if i % 2 else -1.0) for i in range(n)]
        assert residual_sigma(flat) == pytest.approx(residual_sigma(steep), abs=1e-6)


class TestRegistry:
    def test_known_name_resolves(self):
        dist = get_stop_distance("stop_regression", object())
        assert isinstance(dist, StopRegression)

    def test_is_stop_distance_instance(self):
        assert isinstance(StopRegression(), StopDistance)


class TestInitialStop:
    def test_floor_governs_on_a_flat_window(self):
        # A perfectly flat market has ~zero residual sigma, so the distance is
        # clamped to the ATR floor (spread floor disabled here).
        buf = _buffer([8000.0] * 40)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopRegression(min_stop_atr_k=3.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert stop == pytest.approx(entry - 3.0 * atr_v)

    def test_band_governs_when_noise_is_large(self):
        # A noisy window: the residual band beats the floor and drives the stop.
        # Disable the floors to isolate the band = sigma_k × σ × (1 + β(1−ER)).
        closes = [8000.0 + (10.0 if i % 2 else -10.0) for i in range(60)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        dist = StopRegression(min_stop_atr_k=0.0, min_stop_spread_k=0.0, chop_beta=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        # chop_beta=0 removes the ER term, so distance == sigma_k × residual_sigma.
        expected = dist.sigma_k * residual_sigma(closes)
        assert entry - stop == pytest.approx(expected)
        assert stop < entry

    def test_chop_widens_relative_to_clean_trend(self):
        # Same noise amplitude, but a choppy path (low ER) must get a WIDER stop
        # than a clean uptrend (high ER) — the trend factor at work.
        zig = 8.0
        chop = [8000.0 + (zig if i % 2 else -zig) for i in range(60)]
        trend = [8000.0 + 3 * i + (zig if i % 2 else -zig) for i in range(60)]
        dist = StopRegression(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        chop_buf, trend_buf = _buffer(chop), _buffer(trend)
        chop_dist = chop_buf.last.bid_close - dist.initial_stop(
            entry_level=chop_buf.last.bid_close, direction="BUY", buf=chop_buf
        )
        trend_dist = trend_buf.last.bid_close - dist.initial_stop(
            entry_level=trend_buf.last.bid_close, direction="BUY", buf=trend_buf
        )
        assert chop_dist > trend_dist

    def test_sell_falls_back_to_the_flat_atr_stop(self):
        # Long-only pipeline: SELL is not band-derived.
        buf = _buffer([8000.0 - i for i in range(40)])
        entry = buf.last.bid_close
        ref = StopAtr().initial_stop(entry_level=entry, direction="SELL", buf=buf)
        got = StopRegression().initial_stop(entry_level=entry, direction="SELL", buf=buf)
        assert got == pytest.approx(ref)
