"""Tests for the hourly-extreme initial stop-distance policy.

``StopHourLow`` places the initial stop at the raw lowest bid low of the last
``lookback`` candles for a BUY (the highest offer high for a SELL). These tests
cover the helpers (``window_extreme``, ``noise_floor_distance``), the registry
wiring and ``initial_stop``: the extreme placement, the lookback truncation, the
wick sensitivity that distinguishes it from ``stop_support``, the buffer, the
curve-state noise floor, the ATR/spread back-stops, the cap and the BUY/SELL
symmetry.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr, band_noise
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopHourLow, get_stop_distance, window_extreme
from src.stops.base import StopDistance
from src.stops.stop_hourlow import noise_floor_distance
from src.stops.stop_support import StopSupport

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _oscillation(
    amplitude: float, count: int = 60, base: float = 8000.0
) -> list[float]:
    """A directionless sine wave — net move ~0, path travelled large (ER ≈ 0)."""
    return [base + amplitude * math.sin(2 * math.pi * i / 12) for i in range(count)]


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


class TestWindowExtreme:
    def test_buy_takes_the_lowest_bid_low(self):
        buf = _buffer([8000.0, 7990.0, 8010.0, 8005.0])
        candles = list(buf.candles)
        lows = [candle.bid_low for candle in candles]
        assert window_extreme(candles, direction="BUY") == pytest.approx(min(lows))

    def test_sell_takes_the_highest_offer_high(self):
        buf = _buffer([8000.0, 7990.0, 8010.0, 8005.0])
        candles = list(buf.candles)
        highs = [candle.offer_high for candle in candles]
        assert window_extreme(candles, direction="SELL") == pytest.approx(max(highs))

    def test_empty_window_returns_none(self):
        assert window_extreme([], direction="BUY") is None


class TestNoiseFloorDistance:
    def test_flat_curve_has_no_band(self):
        buf = _buffer([8000.0] * 60)
        floor = noise_floor_distance(list(buf.candles), trend_k=0.5, chop_k=2.0)
        assert floor == pytest.approx(0.0)

    def test_clean_ramp_scores_the_trend_multiplier_on_a_zero_band(self):
        # A perfect line has no residual at all, so however steep it is the floor
        # collapses to zero — the extreme of a trending hour is real structure.
        buf = _buffer([8000.0 + 2 * i for i in range(60)])
        floor = noise_floor_distance(list(buf.candles), trend_k=0.5, chop_k=2.0)
        assert floor == pytest.approx(0.0, abs=1e-9)

    def test_oscillation_is_floored_near_the_chop_multiplier(self):
        # Net move ≈ 0 over a large travelled path → ER ≈ 0 → the full chop
        # multiplier applies to the band.
        buf = _buffer(_oscillation(20.0))
        candles = list(buf.candles)
        band = band_noise([c.mid_close for c in candles])
        floor = noise_floor_distance(candles, trend_k=0.5, chop_k=2.0)
        assert band > 0
        assert floor == pytest.approx(2.0 * band, rel=0.05)

    def test_chop_earns_a_wider_floor_than_a_trend_of_the_same_amplitude(self):
        chop = _buffer(_oscillation(20.0))
        # Same 20-point wander, but riding a strong up-trend: same band, ER high.
        trend = _buffer([c + 4.0 * i for i, c in enumerate(_oscillation(20.0))])
        chop_floor = noise_floor_distance(list(chop.candles), trend_k=0.5, chop_k=2.0)
        trend_floor = noise_floor_distance(list(trend.candles), trend_k=0.5, chop_k=2.0)
        assert chop_floor > trend_floor

    def test_too_short_a_window_is_unmeasurable(self):
        buf = _buffer([8000.0, 8010.0])
        assert noise_floor_distance(
            list(buf.candles), trend_k=0.5, chop_k=2.0
        ) == pytest.approx(0.0)


class TestRegistry:
    def test_known_name_resolves(self):
        dist = get_stop_distance("stop_hourlow", object())
        assert isinstance(dist, StopHourLow)

    def test_is_stop_distance_instance(self):
        assert isinstance(StopHourLow(), StopDistance)


class TestInitialStopBuy:
    def test_stop_sits_on_the_hourly_low(self):
        buf = _buffer([8000.0 + 2 * i for i in range(60)])
        entry = buf.last.bid_close
        dist = StopHourLow(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert stop == pytest.approx(min(c.bid_low for c in buf.candles))

    def test_only_the_lookback_window_counts(self):
        # A much deeper low sits just outside the 60-candle window (it and the
        # candle climbing back out of it): it must be ignored, the stop anchors on
        # the window's own low.
        closes = [7000.0] + [8000.0 + 0.5 * i for i in range(61)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        dist = StopHourLow(lookback=60, min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        window = list(buf.candles)[-60:]
        assert stop == pytest.approx(min(c.bid_low for c in window))
        assert stop > 7500.0

    def test_a_lone_wick_is_honoured_unlike_stop_support(self):
        # The defining difference with ``stop_support``: the raw extreme takes the
        # spike, the weighted quantile outvotes it.
        closes = [8000.0, 7900.0] + [8000.0 + 0.01 * i for i in range(58)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        hourlow = StopHourLow(min_stop_atr_k=0.0, min_stop_spread_k=0.0).initial_stop(
            entry_level=entry, direction="BUY", buf=buf
        )
        support = StopSupport().initial_stop(
            entry_level=entry, direction="BUY", buf=buf
        )
        assert hourlow == pytest.approx(min(c.bid_low for c in buf.candles))
        assert hourlow < support

    def test_buffer_pushes_the_stop_below_the_low(self):
        buf = _buffer([8000.0 + 2 * i for i in range(60)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopHourLow(buffer_atr_k=0.5, min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        low = min(c.bid_low for c in buf.candles)
        assert stop == pytest.approx(low - 0.5 * atr_v)

    def test_spread_floor_governs_on_a_flat_market(self):
        # A perfectly flat hour: the low is 0.1 below the entry, well inside the
        # bid/offer churn, so the spread floor decides.
        buf = _buffer([8000.0] * 60, spread=4.0)
        entry = buf.last.bid_close
        dist = StopHourLow(min_stop_atr_k=0.0, min_stop_spread_k=2.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(2.0 * 4.0)

    def test_low_above_the_entry_degrades_to_the_floor(self):
        # Falling into the entry: every hourly low sits above the current bid, so
        # the raw distance is negative and the floor takes over.
        buf = _buffer([8000.0 - 2 * i for i in range(60)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopHourLow(min_stop_atr_k=1.5, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(1.5 * atr_v)

    def test_optional_cap_clips_a_deep_low(self):
        closes = [8000.0, 7000.0] + [8000.0 + 0.01 * i for i in range(58)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        # Every floor off, including the noise one: the 1000-point crash is a huge
        # detrended residual, so the curve-state floor would otherwise (rightly)
        # outrank the cap under test here.
        dist = StopHourLow(
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
            max_stop_atr_k=2.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(2.0 * atr_v)

    def test_noise_floor_overrides_an_extreme_sitting_inside_the_band(self):
        # The regression case (IX.D.DOW.IFE.IP, 2026-07-31 09:37): a directionless
        # band whose low is a few points under the entry. The raw extreme would put
        # the stop inside the oscillation that printed it; the curve-state floor
        # pushes it outside the band instead.
        buf = _buffer(_oscillation(20.0), spread=4.0)
        entry = buf.last.bid_close
        candles = list(buf.candles)
        raw = entry - min(c.bid_low for c in candles)
        band = band_noise([c.mid_close for c in candles])
        stop = StopHourLow().initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop > raw
        assert entry - stop == pytest.approx(2.0 * band, rel=0.05)

    def test_noise_floor_leaves_a_trending_hour_on_its_extreme(self):
        # Same policy, clean trend: nothing to protect against, the stop stays on
        # the hourly low exactly as the policy intends.
        buf = _buffer([8000.0 + 2 * i for i in range(60)])
        entry = buf.last.bid_close
        stop = StopHourLow(min_stop_atr_k=0.0, min_stop_spread_k=0.0).initial_stop(
            entry_level=entry, direction="BUY", buf=buf
        )
        assert stop == pytest.approx(min(c.bid_low for c in buf.candles))

    def test_noise_floor_can_be_measured_on_its_own_window(self):
        # ``noise_lookback`` decouples the band measurement from the extreme's
        # window: a 20-candle band is thinner than the 60-candle one here.
        buf = _buffer(_oscillation(20.0), spread=4.0)
        entry = buf.last.bid_close
        wide = StopHourLow().initial_stop(entry_level=entry, direction="BUY", buf=buf)
        narrow = StopHourLow(noise_lookback=12).initial_stop(
            entry_level=entry, direction="BUY", buf=buf
        )
        assert entry - narrow != pytest.approx(entry - wide)

    def test_floor_wins_over_a_misconfigured_cap(self):
        buf = _buffer([8000.0 + 0.5 * i for i in range(60)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopHourLow(
            min_stop_atr_k=3.0, min_stop_spread_k=0.0, max_stop_atr_k=0.5
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(3.0 * atr_v)


class TestInitialStopSell:
    def test_stop_sits_on_the_hourly_high_above_the_offer(self):
        buf = _buffer([8000.0 - 2 * i for i in range(60)])
        entry = buf.last.bid_close
        dist = StopHourLow(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="SELL", buf=buf)
        assert stop == pytest.approx(max(c.offer_high for c in buf.candles))
        assert stop > entry

    def test_symmetry_of_the_two_directions(self):
        up = _buffer([8000.0 + 2 * i for i in range(60)])
        down = _buffer([8000.0 - 2 * i for i in range(60)])
        dist = StopHourLow(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        buy_distance = up.last.bid_close - dist.initial_stop(
            entry_level=up.last.bid_close, direction="BUY", buf=up
        )
        sell_distance = (
            dist.initial_stop(
                entry_level=down.last.bid_close, direction="SELL", buf=down
            )
            - down.last.offer_close
        )
        assert buy_distance == pytest.approx(sell_distance, rel=1e-6)
