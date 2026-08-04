"""Tests for the shape-selected initial stop-distance policy.

``StopShape`` differs from every sibling in :mod:`src.stops` by choosing *which*
recent level to anchor on before deciding a distance. These tests therefore cover
the classifier (:func:`classify_shape`) on its own, then that each shape actually
routes the stop to its own candidate level — a clean trend to the last hour's
extreme, a noisy trend to the three-hour one, chop to the session extreme supplied
from outside the buffer — plus the ``day_extreme`` degradation path, the wick
cushion, the shared floors, the cap, the registry wiring and BUY/SELL symmetry.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopShape, classify_shape, get_stop_distance
from src.stops.base import StopDistance
from src.stops.stop_shape import CHOP, CLEAN_TREND, NOISY_TREND

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


def _clean_ramp(
    count: int = 200, base: float = 8000.0, step: float = 2.0
) -> list[float]:
    """A straight climb — ER = 1, R² = 1."""
    return [base + step * i for i in range(count)]


def _oscillation(
    amplitude: float, count: int = 200, base: float = 8000.0
) -> list[float]:
    """A directionless sine wave — net move ~0, path travelled large (ER ≈ 0)."""
    return [base + amplitude * math.sin(2 * math.pi * i / 12) for i in range(count)]


def _noisy_climb(
    count: int = 200, base: float = 8000.0, step: float = 2.0, amplitude: float = 40.0
) -> list[float]:
    """A rise that gets there through deep retracements — ER decent, R² poor."""
    return [
        base + step * i + amplitude * math.sin(2 * math.pi * i / 15)
        for i in range(count)
    ]


class TestClassifyShape:
    def test_straight_ramp_is_a_clean_trend(self):
        candles = list(_buffer(_clean_ramp(60)).candles)
        assert (
            classify_shape(candles, min_efficiency=0.15, min_r_squared=0.5)
            is CLEAN_TREND
        )

    def test_oscillation_is_chop(self):
        candles = list(_buffer(_oscillation(20.0, 60)).candles)
        assert classify_shape(candles, min_efficiency=0.15, min_r_squared=0.5) is CHOP

    def test_deep_retracements_make_a_noisy_trend(self):
        # Directional enough to clear the ER floor, but the points follow their own
        # trend line poorly — the pull-back case the three-hour level is for.
        candles = list(_buffer(_noisy_climb(60)).candles)
        assert (
            classify_shape(candles, min_efficiency=0.05, min_r_squared=0.95)
            is NOISY_TREND
        )

    def test_verdict_ignores_direction(self):
        # R² measures cleanliness, not direction: the entry already chose the side,
        # so a tightly fitted *fall* is as "clean" as a tightly fitted rise.
        falling = list(_buffer(_clean_ramp(60, step=-2.0)).candles)
        assert (
            classify_shape(falling, min_efficiency=0.15, min_r_squared=0.5)
            is CLEAN_TREND
        )

    def test_unmeasurable_window_is_chop_not_a_trend(self):
        # Two candles cannot show a shape; the conservative verdict is the one that
        # does not claim structure.
        candles = list(_buffer([8000.0, 8010.0]).candles)
        assert classify_shape(candles, min_efficiency=0.15, min_r_squared=0.5) is CHOP

    def test_empty_window_is_chop(self):
        assert classify_shape([], min_efficiency=0.15, min_r_squared=0.5) is CHOP

    def test_efficiency_floor_is_checked_before_the_fit(self):
        # A high min_efficiency rejects even a perfectly fitted ramp, proving the
        # ER gate is the outer test and R² only splits the survivors.
        candles = list(_buffer(_clean_ramp(60)).candles)
        assert classify_shape(candles, min_efficiency=1.01, min_r_squared=0.0) is CHOP


class TestRegistry:
    def test_known_name_resolves(self):
        assert isinstance(get_stop_distance("stop_shape", object()), StopShape)

    def test_is_stop_distance_instance(self):
        assert isinstance(StopShape(), StopDistance)


class TestLevelSelection:
    """Each shape must route the stop to its *own* candidate level."""

    def test_clean_trend_anchors_on_the_hour(self):
        buf = _buffer(_clean_ramp(200))
        entry = buf.last.bid_close
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        hour = list(buf.candles)[-60:]
        assert stop == pytest.approx(min(c.bid_low for c in hour))

    def test_clean_trend_ignores_the_wider_windows(self):
        # The three-hour and session lows are far below; a clean trend must not
        # reach for them.
        buf = _buffer(_clean_ramp(200))
        entry = buf.last.bid_close
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(
            entry_level=entry, direction="BUY", buf=buf, day_extreme=1.0
        )
        assert stop > min(c.bid_low for c in buf.candles)

    def test_noisy_trend_anchors_on_the_long_window(self):
        buf = _buffer(_noisy_climb(200))
        entry = buf.last.bid_close
        dist = StopShape(
            min_efficiency=0.05,
            min_r_squared=0.99,  # force the NOISY_TREND branch
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        long_window = list(buf.candles)[-180:]
        assert stop == pytest.approx(min(c.bid_low for c in long_window))

    def test_noisy_trend_is_wider_than_a_clean_trend_on_the_same_curve(self):
        buf = _buffer(_noisy_climb(200))
        entry = buf.last.bid_close
        common = dict(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        noisy = StopShape(
            min_efficiency=0.05, min_r_squared=0.99, **common
        ).initial_stop(entry_level=entry, direction="BUY", buf=buf)
        clean = StopShape(
            min_efficiency=0.05, min_r_squared=0.0, **common
        ).initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert noisy < clean  # a lower stop for a BUY is a wider one

    def test_chop_anchors_on_the_supplied_session_extreme(self):
        buf = _buffer(_oscillation(20.0, 200))
        entry = buf.last.bid_close
        session_low = min(c.bid_low for c in buf.candles) - 500.0
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(
            entry_level=entry, direction="BUY", buf=buf, day_extreme=session_low
        )
        assert stop == pytest.approx(session_low)

    def test_chop_without_a_session_extreme_falls_back_to_the_buffer(self):
        # Degradation, not failure: the widest window the buffer holds is used.
        buf = _buffer(_oscillation(20.0, 200))
        entry = buf.last.bid_close
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        long_window = list(buf.candles)[-180:]
        assert stop == pytest.approx(min(c.bid_low for c in long_window))

    def test_session_extreme_is_ignored_outside_the_chop_branch(self):
        # Only the chop branch reaches past the buffer, so a clean trend places the
        # same stop whether or not a session extreme is available.
        buf = _buffer(_clean_ramp(200))
        entry = buf.last.bid_close
        dist = StopShape()
        with_extreme = dist.initial_stop(
            entry_level=entry, direction="BUY", buf=buf, day_extreme=1.0
        )
        without = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert with_extreme == pytest.approx(without)


class TestBufferFloorAndCap:
    def test_default_cushion_sits_below_the_level_not_on_it(self):
        # The 0.3-point-wick lesson: the default must not rest ON the level.
        assert StopShape().buffer_atr_k > 0

    def test_cushion_pushes_the_stop_beyond_the_level(self):
        buf = _buffer(_clean_ramp(200))
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopShape(
            buffer_atr_k=0.5,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        hour_low = min(c.bid_low for c in list(buf.candles)[-60:])
        assert stop == pytest.approx(hour_low - 0.5 * atr_v)

    def test_spread_floor_governs_on_a_flat_market(self):
        buf = _buffer([8000.0] * 200, spread=4.0)
        entry = buf.last.bid_close
        dist = StopShape(buffer_atr_k=0.0, min_stop_atr_k=0.0, min_stop_spread_k=2.0)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(2.0 * 4.0)

    def test_atr_floor_governs_when_the_level_is_on_the_wrong_side(self):
        # Falling into the entry: every low sits above the current bid, so the raw
        # distance is negative and the floor must take over.
        buf = _buffer([8000.0 - 2 * i for i in range(200)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=1.5,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(1.5 * atr_v)

    def test_noise_floor_widens_a_chopping_market(self):
        buf = _buffer(_oscillation(20.0, 200))
        entry = buf.last.bid_close
        common = dict(buffer_atr_k=0.0, min_stop_atr_k=0.0, min_stop_spread_k=0.0)
        # A tiny session extreme keeps the raw distance small so the floor decides.
        near = entry - 0.05
        floored = StopShape(noise_chop_k=2.0, **common).initial_stop(
            entry_level=entry, direction="BUY", buf=buf, day_extreme=near
        )
        unfloored = StopShape(
            noise_trend_k=0.0, noise_chop_k=0.0, **common
        ).initial_stop(entry_level=entry, direction="BUY", buf=buf, day_extreme=near)
        assert floored < unfloored

    def test_floor_beats_the_cap(self):
        buf = _buffer([8000.0] * 200, spread=4.0)
        entry = buf.last.bid_close
        dist = StopShape(
            buffer_atr_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=2.0,
            max_stop_atr_k=0.01,  # absurdly tight cap
        )
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert entry - stop == pytest.approx(2.0 * 4.0)

    def test_cap_clips_a_distant_session_extreme(self):
        buf = _buffer(_oscillation(2.0, 200))
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
            max_stop_atr_k=2.0,
        )
        stop = dist.initial_stop(
            entry_level=entry, direction="BUY", buf=buf, day_extreme=entry - 5000.0
        )
        assert entry - stop == pytest.approx(2.0 * atr_v)

    def test_empty_buffer_degrades_to_the_floor_instead_of_raising(self):
        buf = EpicBuffer(epic="TEST.EPIC", max_candles=10)
        stop = StopShape().initial_stop(entry_level=8000.0, direction="BUY", buf=buf)
        assert stop == pytest.approx(8000.0)


class TestSellSymmetry:
    def test_clean_trend_anchors_on_the_hourly_offer_high(self):
        buf = _buffer(_clean_ramp(200, step=-2.0))  # falling: a short's trend
        offer = buf.last.offer_close
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(
            entry_level=buf.last.bid_close, direction="SELL", buf=buf
        )
        hour = list(buf.candles)[-60:]
        assert stop == pytest.approx(max(c.offer_high for c in hour))
        assert stop > offer

    def test_chop_anchors_on_the_session_high(self):
        buf = _buffer(_oscillation(20.0, 200))
        offer = buf.last.offer_close
        session_high = max(c.offer_high for c in buf.candles) + 500.0
        dist = StopShape(
            buffer_atr_k=0.0,
            noise_trend_k=0.0,
            noise_chop_k=0.0,
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
        )
        stop = dist.initial_stop(
            entry_level=buf.last.bid_close,
            direction="SELL",
            buf=buf,
            day_extreme=session_high,
        )
        assert stop == pytest.approx(session_high)
        assert stop > offer

    def test_stop_is_above_the_offer_on_every_shape(self):
        for closes in (_clean_ramp(200), _noisy_climb(200), _oscillation(20.0, 200)):
            buf = _buffer(closes)
            stop = StopShape().initial_stop(
                entry_level=buf.last.bid_close, direction="SELL", buf=buf
            )
            assert stop > buf.last.offer_close
