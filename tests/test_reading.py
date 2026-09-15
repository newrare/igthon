"""Tests for the curve-reading layer — CurveReading and PositionReading."""

import math
from datetime import UTC, datetime, time

import pytest

from src.core.reading import (
    CurveReading,
    MovePhase,
    PositionReading,
    Regime,
    Trend,
    read_all,
)
from src.feed.price_buffer import Candle, EpicBuffer, PriceBuffer

BASE = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def _buffer(
    bids: list[float],
    *,
    spread: float = 0.2,
    wick: float = 0.0,
    epic: str = "CS.D.EURUSD.CFD.IP",
    mirror: bool = False,
) -> EpicBuffer:
    """Build a buffer of 1-minute candles from a series of bid closes.

    ``wick`` widens each candle symmetrically around its close, so highs and lows
    exist without changing the closes. ``mirror`` builds the *short* image of the
    same series — the offer becomes ``200 − bid`` — which is what lets a test assert
    that a SELL reads exactly like the mirrored BUY.
    """
    buf = EpicBuffer(epic=epic, max_candles=500)
    for index, bid in enumerate(bids):
        if mirror:
            offer_close = 200.0 - bid
            bid_close = offer_close - spread
        else:
            bid_close = bid
            offer_close = bid + spread
        buf.add(
            Candle(
                timestamp=BASE.replace(minute=0) + (index * _MINUTE),
                bid_open=bid_close,
                bid_close=bid_close,
                bid_high=bid_close + wick,
                bid_low=bid_close - wick,
                offer_open=offer_close,
                offer_close=offer_close,
                offer_high=offer_close + wick,
                offer_low=offer_close - wick,
            )
        )
    return buf


_MINUTE = datetime(2026, 1, 1, 0, 1, tzinfo=UTC) - datetime(
    2026, 1, 1, 0, 0, tzinfo=UTC
)


class TestSnapshotAndFreshness:
    """A reading is a one-tick object built on a copy of the candles."""

    def test_candles_are_snapshotted(self):
        buf = _buffer([100.0, 100.5, 101.0])
        reading = CurveReading.of(buf)
        assert reading.minutes == 3
        buf.add(buf.candles[-1])
        assert reading.minutes == 3
        assert reading.is_stale

    def test_empty_buffer_answers_instead_of_raising(self):
        reading = CurveReading.of(EpicBuffer(epic="X"))
        assert reading.minutes == 0
        assert reading.price == 0.0
        assert reading.regime is Regime.UNKNOWN
        assert reading.trend_15m.direction is Trend.FLAT
        assert reading.ticks(15).total == 0
        assert reading.supports == []
        assert reading.age_minutes == math.inf

    def test_rejects_an_unknown_side(self):
        with pytest.raises(ValueError):
            CurveReading.of(_buffer([1.0, 2.0]), side="ask")

    def test_windows_are_clamped_to_history(self):
        reading = CurveReading.of(_buffer([100.0 + i for i in range(5)]))
        assert reading.trend(60).minutes == 5
        assert reading.is_warm(5)
        assert not reading.is_warm(6)


class TestSpread:
    """Spread reads — the cost side, which no price measure shows."""

    def test_spread_ratio_flags_a_widening_quote(self):
        buf = _buffer([100.0] * 30, spread=0.2)
        wide = buf.candles[-1]
        buf.add(
            Candle(
                timestamp=wide.timestamp + _MINUTE,
                bid_open=100.0,
                bid_close=100.0,
                bid_high=100.0,
                bid_low=100.0,
                offer_open=101.0,
                offer_close=101.0,
                offer_high=101.0,
                offer_low=101.0,
            )
        )
        reading = CurveReading.of(buf)
        assert reading.spread == pytest.approx(1.0)
        assert reading.spread_ratio > 3

    def test_spread_in_atr_is_zero_without_movement(self):
        assert CurveReading.of(_buffer([100.0] * 20)).spread_in_atr == 0.0


class TestTicks:
    """Candle balance."""

    def test_counts_up_down_and_flat(self):
        # +1, -1, then two moves smaller than half the 0.2 spread.
        buf = _buffer([100.0, 101.0, 100.0, 100.05, 100.02])
        counts = CurveReading.of(buf).ticks(10)
        assert (counts.up, counts.down, counts.flat) == (1, 1, 2)
        assert counts.total == 4
        assert counts.bias == pytest.approx(0.0)
        assert counts.flat_ratio == pytest.approx(0.5)
        assert counts.dominant is Trend.FLAT

    def test_strict_threshold_removes_the_flat_bucket(self):
        counts = CurveReading.of(_buffer([100.0, 100.05, 100.02])).ticks(
            10, flat_threshold=0
        )
        assert counts.flat == 0
        assert (counts.up, counts.down) == (1, 1)

    def test_bias_is_blind_to_amplitude(self):
        # Three small ups and one big down: pressure positive, trend negative.
        reading = CurveReading.of(_buffer([100.0, 100.5, 101.0, 101.5, 95.0]))
        assert reading.ticks(10).bias > 0
        assert reading.trend(5).direction is Trend.DOWN

    def test_streak_is_signed(self):
        assert CurveReading.of(_buffer([1.0, 2.0, 3.0, 4.0])).streak == 3
        assert CurveReading.of(_buffer([4.0, 3.0, 2.0])).streak == -2
        assert CurveReading.of(_buffer([1.0, 2.0, 2.0])).streak == 0


class TestTrend:
    """Fitted trends and their derived reads."""

    def test_clean_ramp(self):
        reading = CurveReading.of(_buffer([100.0 + i * 0.5 for i in range(30)]))
        trend = reading.trend(30)
        assert trend.direction is Trend.UP
        assert trend.r_squared == pytest.approx(1.0)
        assert trend.is_clean
        assert trend.pct > 0
        assert trend.net_pct == pytest.approx(14.5 / 100 * 100, rel=1e-3)
        # A perfect line spans its own range, so the chart angle is about 45°.
        assert trend.degrees == pytest.approx(45, abs=2)

    def test_flat_market_has_no_direction(self):
        reading = CurveReading.of(_buffer([100.0] * 20))
        assert reading.trend(20).direction is Trend.FLAT
        assert reading.trend(20).degrees == 0.0

    def test_alignment_and_horizons(self):
        reading = CurveReading.of(_buffer([100.0 + i * 0.1 for i in range(80)]))
        assert reading.trend_5m.direction is Trend.UP
        assert reading.trend_60m.direction is Trend.UP
        assert reading.alignment == pytest.approx(1.0)
        assert reading.is_aligned

    def test_contradicting_horizons_lower_the_alignment(self):
        rising = [100.0 + i * 0.1 for i in range(60)]
        falling = [rising[-1] - i * 0.2 for i in range(1, 11)]
        reading = CurveReading.of(_buffer(rising + falling))
        assert reading.trend_5m.direction is Trend.DOWN
        assert reading.trend_60m.direction is Trend.UP
        assert abs(reading.alignment) < 1.0

    def test_fading_trend_is_detected(self):
        # 30 minutes up, but the last 5 have stalled: speed below the average.
        values = [100.0 + i * 0.5 for i in range(25)] + [112.0] * 5
        reading = CurveReading.of(_buffer(values))
        assert reading.trend(30).direction is Trend.UP
        assert reading.acceleration < 0
        assert reading.is_fading


class TestAmplitude:
    """The four amplitude measures and what separates them."""

    def test_band_noise_ignores_the_trend_but_range_does_not(self):
        ramp = CurveReading.of(_buffer([100.0 + i for i in range(30)])).amplitude(30)
        assert ramp.range == pytest.approx(29.0)
        assert ramp.band_noise == pytest.approx(0.0, abs=1e-6)
        assert ramp.efficiency == pytest.approx(1.0)

    def test_retracement_spans_several_candles(self):
        # A slow slide: each step gives back 1 point, the sequence gives back 4 —
        # which is what a stop floored on a single step would fail to survive.
        values = [100.0, 102.0, 104.0, 103.0, 102.0, 101.0, 100.0]
        amplitude = CurveReading.of(_buffer(values)).amplitude(7)
        assert amplitude.retracement == pytest.approx(4.0)
        assert amplitude.adverse_noise < amplitude.retracement
        assert amplitude.is_choppy

    def test_atr_uses_the_candle_range(self):
        reading = CurveReading.of(_buffer([100.0] * 20, wick=1.0))
        assert reading.atr == pytest.approx(2.0)
        assert reading.in_atr(4.0) == pytest.approx(2.0)
        assert reading.in_pct(1.0) == pytest.approx(1.0, rel=1e-2)

    def test_atr_is_zero_without_history(self):
        assert CurveReading.of(_buffer([100.0])).atr == 0.0
        assert CurveReading.of(_buffer([100.0])).in_atr(5.0) == 0.0


class TestRegime:
    """Regime classification."""

    def test_clean_ramp_is_a_trend(self):
        assert (
            CurveReading.of(_buffer([100.0 + i * 0.3 for i in range(40)])).regime
            is Regime.TREND_UP
        )

    def test_clean_fall_is_a_down_trend(self):
        reading = CurveReading.of(_buffer([100.0 - i * 0.3 for i in range(40)]))
        assert reading.regime is Regime.TREND_DOWN
        assert reading.is_trending

    def test_tight_chop_is_a_range(self):
        values = [100.0 + (0.1 if i % 2 else -0.1) for i in range(40)]
        reading = CurveReading.of(_buffer(values, wick=0.1))
        assert reading.regime is Regime.RANGE
        assert not reading.is_trending

    def test_a_spike_over_a_calm_baseline_is_volatile(self):
        # Two hours of quiet chop, then 30 minutes of wide swings going nowhere:
        # the recent candles are far bigger than this epic's own normal.
        calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(120)]
        storm = [100.0 + 8 * math.sin(i / 10 * 2 * math.pi) for i in range(30)]
        reading = CurveReading.of(_buffer(calm + storm))
        assert reading.volatility_ratio > 1.5
        assert reading.regime is Regime.VOLATILE

    def test_volatility_ratio_is_flat_without_a_baseline(self):
        # A window as long as the whole buffer cannot look unusual to itself.
        values = [100.0 + 8 * math.sin(i / 10 * 2 * math.pi) for i in range(40)]
        reading = CurveReading.of(_buffer(values))
        assert reading.volatility_ratio == pytest.approx(1.0, abs=0.2)
        assert reading.regime is Regime.RANGE

    def test_short_history_is_unknown(self):
        assert CurveReading.of(_buffer([100.0] * 5)).regime is Regime.UNKNOWN


class TestMovePhase:
    """The age of the current momentum leg: start / middle / end."""

    #: 30 minutes of two-tick chop — a market with no leg at all.
    CHOP = [100.0 + (0.1 if i % 2 else -0.1) for i in range(30)]

    def _phase(self, bids: list[float], wick: float = 0.0) -> MovePhase:
        return CurveReading.of(_buffer(bids, wick=wick)).move_phase

    def test_chop_is_no_move(self):
        # The 9/21 averages cross constantly in a chop, but they stay inside the
        # candle noise: those crossings are not legs.
        reading = CurveReading.of(_buffer(self.CHOP, wick=0.1))
        assert reading.move_phase is MovePhase.NONE
        assert reading.move_direction is Trend.FLAT

    def test_flat_line_is_no_move(self):
        assert self._phase([100.0] * 40) is MovePhase.NONE

    def test_short_history_is_no_move(self):
        assert self._phase([100.0] * 5) is MovePhase.NONE

    def test_a_fresh_leg_out_of_a_chop_is_a_start(self):
        # Three clean candles up after half an hour of chop: the crossing is
        # recent, so the leg is young — and this is the case a regime cannot
        # report, since it still reads RANGE here.
        bids = self.CHOP + [100.0 + i * 0.4 for i in range(1, 4)]
        reading = CurveReading.of(_buffer(bids, wick=0.1))
        assert reading.move_phase is MovePhase.START
        assert reading.move_direction is Trend.UP
        assert reading.regime is Regime.RANGE

    def test_a_fresh_fall_is_a_start_pointing_down(self):
        bids = self.CHOP + [100.0 - i * 0.4 for i in range(1, 4)]
        reading = CurveReading.of(_buffer(bids, wick=0.1))
        assert reading.move_phase is MovePhase.START
        assert reading.move_direction is Trend.DOWN

    def test_an_established_leg_is_the_middle(self):
        # Twelve candles in: the crossing has held and the price is still at the
        # top of the range it travelled.
        bids = self.CHOP + [100.0 + i * 0.4 for i in range(1, 13)]
        assert self._phase(bids, wick=0.1) is MovePhase.MIDDLE

    def test_a_clean_ramp_is_the_middle_not_the_end(self):
        # A relentless ramp leaves the RSI pinned overbought for its whole
        # length; reading that as exhaustion would call every strong move
        # finished, so it must not.
        assert self._phase([100.0 + i * 0.3 for i in range(40)]) is MovePhase.MIDDLE

    def test_a_clean_fall_is_the_middle(self):
        reading = CurveReading.of(_buffer([100.0 - i * 0.3 for i in range(40)]))
        assert reading.move_phase is MovePhase.MIDDLE
        assert reading.move_direction is Trend.DOWN

    def test_a_leg_that_gave_back_half_its_range_is_the_end(self):
        # Forty candles up, then a pull-back through the middle of the window's
        # range: the leg has handed back more than it kept.
        bids = [100.0 + i * 0.5 for i in range(40)] + [
            119.5 - i * 1.2 for i in range(7)
        ]
        assert self._phase(bids) is MovePhase.END

    def test_a_fall_that_bounced_back_is_the_end(self):
        # Mirror: a down leg is judged from the low it came from, so a bounce
        # through the middle finishes it exactly like the rise above.
        bids = [100.0 - i * 0.5 for i in range(40)] + [80.5 + i * 1.5 for i in range(6)]
        assert self._phase(bids) is MovePhase.END

    def test_direction_is_flat_exactly_when_there_is_no_phase(self):
        for bids, wick in (([100.0] * 40, 0.0), (self.CHOP, 0.1)):
            reading = CurveReading.of(_buffer(bids, wick=wick))
            assert (reading.move_phase is MovePhase.NONE) == (
                reading.move_direction is Trend.FLAT
            )


class TestChannelAndMomentum:
    """Channel position, moving averages, crossings, RSI."""

    def test_position_in_range(self):
        rising = CurveReading.of(_buffer([100.0 + i for i in range(30)]))
        assert rising.channel(30).position == pytest.approx(1.0)
        assert rising.channel(30).from_high == pytest.approx(0.0)
        assert rising.channel(30).from_low == pytest.approx(29.0)
        assert rising.position_in_range == pytest.approx(1.0)

    def test_flat_channel_reads_the_middle(self):
        assert CurveReading.of(_buffer([100.0] * 30)).channel(30).position == 0.5

    def test_moving_averages_and_roc(self):
        reading = CurveReading.of(_buffer([100.0 + i for i in range(21)]))
        assert reading.sma(5) == pytest.approx(118.1)  # mid = bid + half a spread
        assert reading.ema(5) > reading.sma(21)  # the fast one leads a rising tape
        assert reading.roc(10) > 0

    def test_cross_detects_a_fresh_turn(self):
        values = [100.0 - i * 0.5 for i in range(40)] + [
            80.5 + i * 1.5 for i in range(12)
        ]
        cross = CurveReading.of(_buffer(values)).cross(9, 21)
        assert cross.state is Trend.UP
        assert cross.crossed_in_history
        assert cross.bars_since_cross < 8

    def test_cross_reports_an_established_side(self):
        cross = CurveReading.of(_buffer([100.0 + i for i in range(60)])).cross(9, 21)
        assert cross.state is Trend.UP
        assert cross.is_established
        assert not cross.just_crossed

    def test_cross_needs_history_and_ordered_periods(self):
        assert CurveReading.of(_buffer([100.0] * 5)).cross(9, 21).fast == 0.0
        with pytest.raises(ValueError):
            CurveReading.of(_buffer([100.0] * 30)).cross(21, 9)

    def test_rsi_bounds(self):
        assert CurveReading.of(_buffer([100.0 + i for i in range(30)])).is_stretched_up
        assert CurveReading.of(
            _buffer([100.0 - i for i in range(30)])
        ).is_stretched_down
        assert CurveReading.of(_buffer([100.0] * 3)).rsi() == 50.0


class TestLevels:
    """Readable supports, resistances and round numbers."""

    def test_repeated_low_becomes_the_nearest_support(self):
        # A saw-tooth bouncing three times on 100 and topping out near 104.
        wave = [100.0, 101.5, 103.0, 104.0, 102.5, 100.0]
        reading = CurveReading.of(_buffer(wave * 4 + [102.0, 102.5], wick=0.1))
        support = reading.nearest_support
        assert support is not None
        assert support.price == pytest.approx(100.0, abs=0.3)
        assert support.touches >= 2
        assert reading.room_below == pytest.approx(reading.price - support.price)
        resistance = reading.nearest_resistance
        assert resistance is not None
        assert resistance.price > reading.price

    def test_no_level_means_infinite_room(self):
        reading = CurveReading.of(_buffer([100.0 + i for i in range(30)]))
        assert reading.room_above == math.inf
        assert reading.nearest_resistance is None

    def test_swing_high_and_low(self):
        reading = CurveReading.of(
            _buffer([100.0, 101.0, 105.0, 101.0, 100.0, 99.0, 98.0, 99.0, 100.0])
        )
        assert reading.swing_high == pytest.approx(105.0, abs=0.1)
        assert reading.swing_low is not None

    def test_round_levels_are_in_reach(self):
        reading = CurveReading.of(
            _buffer(
                [1.0940 + i * 0.0002 for i in range(30)],
                spread=0.0002,
                wick=0.002,
            )
        )
        assert 1.10 in [pytest.approx(level.price) for level in reading.round_levels]
        nearest = reading.nearest_round_level
        assert nearest is not None
        assert nearest.distance(reading.price) <= reading.distance_to_round + 1e-9
        assert all(level.strength >= 0.6 for level in reading.strong_round_levels)

    def test_summary_exposes_the_headline_fields(self):
        summary = CurveReading.of(
            _buffer([100.0 + i * 0.2 for i in range(40)], wick=0.1)
        ).as_dict()
        assert summary["epic"] == "CS.D.EURUSD.CFD.IP"
        assert summary["regime"] == "TREND_UP"
        assert summary["minutes"] == 40
        assert summary["trend_15m_r2"] == pytest.approx(1.0)


class TestReadAll:
    """Cross-epic entry point."""

    def test_one_reading_per_tracked_epic(self):
        buffer = PriceBuffer(max_candles=100)
        for epic in ("A", "B"):
            for candle in _buffer([100.0, 101.0, 102.0], epic=epic).candles:
                buffer.add_candle(epic, candle)
        readings = read_all(buffer)
        assert set(readings) == {"A", "B"}
        assert readings["A"].trend(3).direction is Trend.UP


class _StubPosition:
    """Minimal duck-typed stand-in for a persisted ``Position`` row."""

    def __init__(self, **fields):
        self.epic = "CS.D.EURUSD.CFD.IP"
        self.direction = "BUY"
        self.date = BASE.date()
        self.time_open = time(10, 2)
        self.level_open = 100.0
        self.level_zero = 100.2
        self.level_margin = 101.0
        self.level_follower = 99.0
        self.euro_per_point = 2.0
        for key, value in fields.items():
            setattr(self, key, value)


class TestPositionReadingLong:
    """Since-open measures for a long."""

    @staticmethod
    def _reading(**overrides) -> PositionReading:
        # bid path since open: 100.2 → 103 → 101 → 102, wicks ±0.5.
        buf = _buffer([99.0, 100.0, 100.2, 103.0, 101.0, 102.0], wick=0.5)
        defaults = dict(
            direction="BUY",
            level_zero=100.2,
            level_open=100.0,
            opened_at=BASE + 2 * _MINUTE,
            level_margin=101.2,
            level_follower=99.5,
            euro_per_point=2.0,
        )
        defaults.update(overrides)
        return PositionReading(buf, **defaults)

    def test_history_is_bounded_to_the_open(self):
        reading = self._reading()
        assert reading.minutes_open == 4
        assert reading.market.minutes == 6

    def test_gain_is_counted_from_breakeven(self):
        reading = self._reading()
        assert reading.gain == pytest.approx(1.8)
        assert reading.gain_from_open == pytest.approx(2.0)
        assert reading.gain_euro == pytest.approx(3.6)
        assert reading.is_profitable
        assert reading.gain_in_atr > 0

    def test_excursions_use_the_candle_extremes(self):
        reading = self._reading()
        assert reading.mfe == pytest.approx(3.3)  # 103.5 high − 100.2
        assert reading.mae == pytest.approx(0.5)  # 100.2 − 99.7 low
        assert reading.mfe_euro == pytest.approx(6.6)
        assert reading.giveback == pytest.approx(1.5)
        assert reading.giveback_ratio == pytest.approx(1.5 / 3.3)
        assert reading.edge_ratio == pytest.approx(3.3 / 0.5)
        assert reading.peak_price == pytest.approx(103.5)
        assert reading.trough_price == pytest.approx(99.7)
        assert reading.minutes_since_peak == 2

    def test_behaviour_since_open(self):
        reading = self._reading()
        assert reading.crossed_breakeven
        assert reading.minutes_to_breakeven == 1
        assert reading.time_in_profit == pytest.approx(0.75)
        assert reading.favourable_streak == 1
        assert reading.trend_since_open.minutes == 4
        assert 0.0 <= reading.efficiency_since_open <= 1.0

    def test_never_in_profit_reads_cleanly(self):
        reading = self._reading(level_zero=110.0, level_margin=111.0)
        assert not reading.crossed_breakeven
        assert reading.minutes_to_breakeven is None
        assert reading.time_in_profit == 0.0
        assert reading.mfe == 0.0
        assert reading.giveback_ratio == 0.0
        assert reading.edge_ratio == 0.0  # nothing offered, pain suffered
        assert not reading.is_profitable

    def test_stop_distances(self):
        reading = self._reading()
        assert reading.stop_room == pytest.approx(2.5)
        assert reading.locked_gain == pytest.approx(-0.7)
        assert not reading.is_secured
        assert reading.stop_room_in_atr > 0
        secured = self._reading(level_follower=101.0)
        assert secured.is_secured
        assert secured.locked_gain == pytest.approx(0.8)

    def test_unknown_stop_and_margin_are_infinite_or_zero(self):
        reading = self._reading(level_follower=None, level_margin=None)
        assert reading.stop_room == math.inf
        assert reading.stop_room_in_atr == math.inf
        assert reading.stop_room_in_noise == math.inf
        assert reading.locked_gain == 0.0
        assert reading.progress_to_margin == 0.0

    def test_progress_to_margin(self):
        reading = self._reading()
        assert reading.progress_to_margin == pytest.approx(1.8)

    def test_stalling(self):
        flat = PositionReading(
            _buffer([100.0] * 10, wick=0.05),
            direction="BUY",
            level_zero=100.0,
            opened_at=BASE,
        )
        assert flat.is_stalling(5)
        moving = PositionReading(
            _buffer([100.0 + i for i in range(10)], wick=0.05),
            direction="BUY",
            level_zero=100.0,
            opened_at=BASE,
        )
        assert not moving.is_stalling(5)

    def test_opened_this_minute_has_nothing_to_measure(self):
        reading = PositionReading(
            _buffer([100.0, 101.0]),
            direction="BUY",
            level_zero=100.0,
            current_price=101.0,
            opened_at=BASE + 10 * _MINUTE,
        )
        assert reading.minutes_open == 0
        assert reading.gain == pytest.approx(1.0)
        assert reading.mfe == pytest.approx(1.0)
        assert reading.time_in_profit == 0.0
        assert reading.minutes_since_peak == 0
        assert not reading.is_stalling(5)

    def test_summary_exposes_the_headline_fields(self):
        summary = self._reading().as_dict()
        assert summary["direction"] == "BUY"
        assert summary["minutes_open"] == 4
        assert summary["gain"] == pytest.approx(1.8)
        assert "regime" in summary


class TestPositionReadingShort:
    """A short must read exactly like the mirrored long."""

    @staticmethod
    def _pair() -> tuple[PositionReading, PositionReading]:
        bids = [99.0, 100.0, 100.2, 103.0, 101.0, 102.0]
        long_reading = PositionReading(
            _buffer(bids, wick=0.5),
            direction="BUY",
            level_zero=100.2,
            level_open=100.0,
            opened_at=BASE + 2 * _MINUTE,
            level_margin=101.2,
            level_follower=99.5,
            euro_per_point=2.0,
        )
        # Mirrored book: the offer is 200 − bid, so profit is down.
        short_reading = PositionReading(
            _buffer(bids, wick=0.5, mirror=True),
            direction="SELL",
            level_zero=200.0 - 100.2,
            level_open=200.0 - 100.0,
            opened_at=BASE + 2 * _MINUTE,
            level_margin=200.0 - 101.2,
            level_follower=200.0 - 99.5,
            euro_per_point=2.0,
        )
        return long_reading, short_reading

    def test_every_profit_field_matches(self):
        long_reading, short_reading = self._pair()
        for field in (
            "gain",
            "gain_from_open",
            "gain_euro",
            "mfe",
            "mae",
            "giveback",
            "giveback_ratio",
            "time_in_profit",
            "favourable_streak",
            "minutes_since_peak",
            "stop_room",
            "locked_gain",
            "progress_to_margin",
        ):
            assert getattr(short_reading, field) == pytest.approx(
                getattr(long_reading, field)
            ), field

    def test_close_out_side_is_the_offer(self):
        _, short_reading = self._pair()
        assert short_reading.curve.side == "offer"
        assert short_reading.market.side == "offer"
        assert short_reading.peak_price == pytest.approx(200.0 - 103.5)


class TestFromPosition:
    """Building a reading from a persisted row."""

    def test_reads_the_columns_and_slices_on_time_open(self):
        buf = _buffer([99.0, 100.0, 100.2, 103.0, 101.0, 102.0], wick=0.5)
        reading = PositionReading.from_position(_StubPosition(), buf)
        assert reading.direction == "BUY"
        assert reading.minutes_open == 4
        assert reading.level_zero == pytest.approx(100.2)
        assert reading.euro_per_point == pytest.approx(2.0)
        assert reading.current_price == pytest.approx(102.0)

    def test_falls_back_to_level_open_and_no_slicing(self):
        buf = _buffer([100.0, 101.0, 102.0])
        stub = _StubPosition(level_zero=None, time_open=None, level_follower=None)
        reading = PositionReading.from_position(stub, buf, current_price=101.5)
        assert reading.level_zero == pytest.approx(100.0)
        assert reading.minutes_open == 3  # nothing to slice on
        assert reading.current_price == pytest.approx(101.5)

    def test_refuses_a_row_without_any_reference_level(self):
        stub = _StubPosition(level_zero=None, level_open=None)
        with pytest.raises(ValueError):
            PositionReading.from_position(stub, _buffer([100.0, 101.0]))
