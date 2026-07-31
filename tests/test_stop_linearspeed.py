"""Tests for the speed-adaptive initial stop-distance policy.

``StopLinearSpeed`` interpolates the initial stop between a pure noise margin
(when the last ``speed_lookback`` candles travelled fast in the trade's direction)
and a structure stop behind the last hour's support/resistance (when they barely
moved). These tests cover the two helpers (``directional_speed``,
``weighted_resistance``), the registry wiring and ``initial_stop``: the two
regimes, the blend in between, the floor/cap and the BUY/SELL symmetry.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopLinearSpeed, get_stop_distance, weighted_resistance
from src.stops.base import StopDistance
from src.stops.stop_linearspeed import directional_speed
from src.stops.stop_support import weighted_support

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


class TestDirectionalSpeed:
    def test_clean_trend_measured_in_atr_units(self):
        # A perfect +2/candle line over 11 values travels 20 points; with ATR = 4
        # that is 5 ATR of directional travel.
        values = [100.0 + 2 * i for i in range(11)]
        assert directional_speed(values, atr_value=4.0, sign=1) == pytest.approx(5.0)

    def test_sign_flips_for_a_sell(self):
        # The same rising window is *adverse* for a short → negative speed.
        values = [100.0 + 2 * i for i in range(11)]
        assert directional_speed(values, atr_value=4.0, sign=-1) == pytest.approx(-5.0)

    def test_chop_scores_far_below_a_clean_trend(self):
        # The regression of an oscillation is nearly flat: a ±5 zig-zag scores a
        # small fraction of a clean one-way move covering the same 10-point range.
        zig = [100.0 + (5.0 if i % 2 else -5.0) for i in range(20)]
        clean = [100.0 + (10.0 / 19) * i for i in range(20)]
        chop_speed = directional_speed(zig, atr_value=2.0, sign=1)
        clean_speed = directional_speed(clean, atr_value=2.0, sign=1)
        assert abs(chop_speed) < 0.25 * clean_speed

    def test_single_spike_barely_moves_the_measure(self):
        # A flat window with one freak tick at the end: a last-minus-first delta
        # would read 10 points, the regression slope only a fraction of it.
        spiky = [100.0] * 19 + [110.0]
        assert directional_speed(spiky, atr_value=1.0, sign=1) < 3.0

    def test_degenerate_inputs_are_slow(self):
        assert directional_speed([], atr_value=1.0, sign=1) == 0.0
        assert directional_speed([100.0], atr_value=1.0, sign=1) == 0.0
        # Unknown/zero volatility cannot be normalised → treated as slow.
        assert directional_speed([100.0, 110.0], atr_value=0.0, sign=1) == 0.0


class TestWeightedResistance:
    def test_mirrors_weighted_support(self):
        # Negating the series must map the low quantile onto the high quantile.
        highs = [100.0, 101.0, 99.0, 105.0, 102.0, 100.5]
        assert weighted_resistance(highs, percentile=0.2) == pytest.approx(
            -weighted_support([-h for h in highs], percentile=0.2)
        )

    def test_sits_near_the_top_of_the_distribution(self):
        highs = [100.0] * 20 + [101.0]
        # A lone spike high is outvoted by the mass at 100.
        assert weighted_resistance(highs, percentile=0.2) == pytest.approx(100.0)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            weighted_resistance([])


class TestRegistry:
    def test_known_name_resolves(self):
        dist = get_stop_distance("stop_linearspeed", object())
        assert isinstance(dist, StopLinearSpeed)

    def test_is_stop_distance_instance(self):
        assert isinstance(StopLinearSpeed(), StopDistance)


class TestInitialStopBuy:
    def test_fast_window_gets_the_noise_margin(self):
        # A steep clean rise: > fast_speed ATR travelled over the last 10 candles,
        # so the distance is the pure noise margin, not the hourly support.
        buf = _buffer([8000.0 + 3 * i for i in range(60)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(dist.noise_atr_k * atr_v)

    def test_slow_window_falls_back_on_the_hourly_support(self):
        # A flat-then-crawling window: the structure leg governs, so the stop sits
        # below the weighted support of the last hour's lows.
        closes = [8000.0 - 20.0] * 20 + [8000.0 + 0.01 * i for i in range(40)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(
            min_stop_atr_k=0.0, min_stop_spread_k=0.0, max_stop_atr_k=0.0
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        lows = [candle.bid_low for candle in buf.candles][-dist.structure_lookback :]
        support = weighted_support(
            lows,
            percentile=dist.structure_percentile,
            recency_half_life=dist.structure_recency_half_life,
        )
        assert stop == pytest.approx(support - dist.structure_buffer_atr_k * atr_v)

    def test_faster_window_never_risks_more(self):
        # Monotonicity of the regime: same epic shape, three closing speeds — the
        # faster the last 10 minutes, the tighter the stop. Distances are compared
        # in ATR units, since a faster tail also inflates the absolute ATR.
        dist = StopLinearSpeed(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        risks = []
        for drift in (0.0, 0.5, 1.5):
            # Same ±2 oscillation throughout (so ATR is identical); only the drift
            # of the last 10 candles changes → speed ≈ 0.27 / 1.38 / 3.60 ATR.
            closes = [8000.0 + (2.0 if i % 2 else -2.0) for i in range(50)]
            closes += [
                closes[-1] + drift * (i + 1) + (2.0 if i % 2 else -2.0)
                for i in range(10)
            ]
            buf = _buffer(closes)
            entry = buf.last.bid_close
            atr_v = atr(list(buf.candles), 14)
            stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
            risks.append((entry - stop) / atr_v)
        assert risks[0] > risks[1] > risks[2]
        # Fastest tail ends on the pure noise margin; the flat one on the slow leg,
        # and the middle one strictly between the two (the blend at work).
        assert risks[2] == pytest.approx(dist.noise_atr_k)
        assert risks[0] == pytest.approx(dist.slow_min_atr_k)
        assert dist.noise_atr_k < risks[1] < risks[0]

    def test_adverse_window_uses_structure(self):
        # Falling into the entry (negative speed) must not earn a tight stop: the
        # distance equals the fully-slow one.
        falling = _buffer([8000.0 - 2 * i for i in range(60)])
        dist = StopLinearSpeed(
            min_stop_atr_k=0.0, min_stop_spread_k=0.0, max_stop_atr_k=0.0
        )
        entry = falling.last.bid_close
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=falling)
        expected = dist._structure_distance(
            list(falling.candles),
            reference=entry,
            direction="BUY",
            atr_value=atr(list(falling.candles), 14),
        )
        assert entry - stop == pytest.approx(expected)

    def test_spread_floor_governs_on_a_dead_market(self):
        # A perfectly flat market: ATR ≈ 0.2 (the ±0.1 wicks) and no structure
        # distance, so the spread floor decides.
        buf = _buffer([8000.0] * 40, spread=4.0)
        entry = buf.last.bid_close
        dist = StopLinearSpeed(min_stop_atr_k=0.0, min_stop_spread_k=2.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(2.0 * 4.0)

    def test_cap_clips_a_far_structure(self):
        # A deep hourly low would put an absurd distance at risk on a slow window;
        # the cap clips it to max_stop_atr_k × ATR.
        closes = [8000.0, 7000.0] + [8000.0 + 0.01 * i for i in range(58)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
            structure_percentile=0.0,
            structure_recency_half_life=0.0,
            max_stop_atr_k=2.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(2.0 * atr_v)

    def test_floor_wins_over_a_misconfigured_cap(self):
        buf = _buffer([8000.0 + 0.5 * i for i in range(60)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(
            min_stop_atr_k=3.0, min_stop_spread_k=0.0, max_stop_atr_k=0.5
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(3.0 * atr_v)


class TestInitialStopSell:
    def test_fast_fall_gets_the_noise_margin_above_the_offer(self):
        # A steep clean fall is *fast* for a short: the stop sits one noise margin
        # above the offer (the side a short's stop is triggered on).
        buf = _buffer([8000.0 - 3 * i for i in range(60)])
        entry = buf.last.bid_close
        offer = buf.last.offer_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        stop = dist.initial_stop(entry_level=entry, direction="SELL", buf=buf)
        assert stop == pytest.approx(offer + dist.noise_atr_k * atr_v)
        assert stop > entry

    def test_slow_window_uses_the_hourly_resistance(self):
        # Crawling down: the structure leg governs and anchors above the weighted
        # resistance of the last hour's offer highs.
        closes = [8000.0 + 20.0] * 20 + [8000.0 - 0.01 * i for i in range(40)]
        buf = _buffer(closes)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(
            min_stop_atr_k=0.0, min_stop_spread_k=0.0, max_stop_atr_k=0.0
        )
        stop = dist.initial_stop(entry_level=entry, direction="SELL", buf=buf)
        highs = [candle.offer_high for candle in buf.candles][
            -dist.structure_lookback :
        ]
        resistance = weighted_resistance(
            highs,
            percentile=dist.structure_percentile,
            recency_half_life=dist.structure_recency_half_life,
        )
        assert stop == pytest.approx(resistance + dist.structure_buffer_atr_k * atr_v)

    def test_symmetry_of_the_two_directions(self):
        # A mirrored price path must give a mirrored distance: the same rise that
        # is "fast" for a BUY is "fast" for a SELL once the path is flipped.
        up = _buffer([8000.0 + 2 * i for i in range(60)])
        down = _buffer([8000.0 - 2 * i for i in range(60)])
        dist = StopLinearSpeed(min_stop_atr_k=0.0, min_stop_spread_k=0.0)
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

    def test_rising_market_is_slow_for_a_short(self):
        # Adverse momentum for a short → structure stop, wider than the noise one.
        buf = _buffer([8000.0 + 2 * i for i in range(60)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopLinearSpeed(
            min_stop_atr_k=0.0, min_stop_spread_k=0.0, max_stop_atr_k=0.0
        )
        stop = dist.initial_stop(entry_level=entry, direction="SELL", buf=buf)
        assert stop - buf.last.offer_close > dist.noise_atr_k * atr_v
