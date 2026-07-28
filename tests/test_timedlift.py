"""Tests for the ``timedlift`` zone-1 updater.

``timedlift`` re-computes the under-water stop once per period (default 10
minutes) from the bid's evolution over the last completed period: it lifts the
stop under the floor the market has just built, never lowers it, never places it
at/above break-even, and holds rather than crowd the live bid.
"""

from datetime import UTC, datetime, timedelta

from src.exit.zones import (
    ZONESTART_UPDATERS,
    StopContext,
    UnderwaterTimedLiftStop,
    build_zone_updater,
)
from src.exit.zones.timedlift import UnderwaterTimedLiftStop as TimedLift
from src.feed.price_buffer import Candle, EpicBuffer

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
_SPREAD = 0.5


def _buffer(closes: list[float]) -> EpicBuffer:
    """One 1-minute candle per close, starting at ``_START`` (the open)."""
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
                offer_open=prev + _SPREAD,
                offer_close=close + _SPREAD,
                offer_high=high + _SPREAD,
                offer_low=low + _SPREAD,
            )
        )
        prev = close
    return buf


def _ctx(
    buf: EpicBuffer,
    *,
    current_bid: float,
    level_zero: float = 8000.0,
    level_follower: float = 7950.0,
    atr_value: float = 2.0,
    min_stop_distance: float = 0.0,
    direction: str = "BUY",
) -> StopContext:
    return StopContext(
        current_price=current_bid,
        level_open=level_zero,
        level_zero=level_zero,
        level_margin=level_zero + 10.0,
        level_follower=level_follower,
        atr_value=atr_value,
        spread=_SPREAD,
        euro_per_point=1.0,
        buf=buf,
        direction=direction,
        min_stop_distance=min_stop_distance,
    )


def _window_low(closes: list[float], first: int, last: int) -> float:
    """Lowest bid low printed by the candles of minutes ``first..last``."""
    return min(min(closes[i - 1], closes[i]) - 0.1 for i in range(first, last + 1))


# A drop over the first ten minutes, then a floor built ten points higher:
# minutes 0-10 fall 7990 -> 7975, minutes 10-20 climb back to 7995.
_FALL_THEN_FLOOR = [7990.0 - 1.5 * i for i in range(11)] + [
    7975.0 + 2.0 * i for i in range(1, 11)
]


class TestGracePeriod:
    # Eight minutes of a steady climb — a path the updater would happily lift the
    # stop on, so the only thing holding it back below is the elapsed time.
    _EARLY_CLIMB = [7975.0 + 2.0 * i for i in range(8)]

    def test_holds_before_the_first_period_has_elapsed(self):
        buf = _buffer(self._EARLY_CLIMB)
        assert TimedLift().propose(_ctx(buf, current_bid=7992.0)) is None

    def test_proposes_once_a_full_period_has_elapsed(self):
        buf = _buffer(self._EARLY_CLIMB)
        # Same history, a 5-minute cadence: one period is complete, so the stop
        # is reviewed — proving the hold above is the grace period, not the data.
        updater = TimedLift(period_minutes=5.0)
        assert updater.propose(_ctx(buf, current_bid=7992.0)) is not None


class TestLift:
    def test_lifts_the_stop_under_the_floor_of_the_last_period(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        # 21 candles (minutes 0..20) → two periods complete; the reviewed window
        # is minutes 10..19, whose floor sits around 7974.9.
        floor = _window_low(_FALL_THEN_FLOOR, 10, 19)
        new_stop = TimedLift().propose(_ctx(buf, current_bid=7996.0))
        assert new_stop is not None
        # Under the floor the market defended, above the open stop, and still
        # below break-even (this zone reduces risk, it never locks a profit).
        assert 7950.0 < new_stop < floor < 8000.0

    def test_reviews_the_new_period_after_the_next_boundary(self):
        closes = _FALL_THEN_FLOOR + [7995.0 + 0.5 * i for i in range(1, 11)]
        buf = _buffer(closes)
        # Minute 30 → three periods complete, reviewed window is minutes 20..29
        # whose floor is higher, so the stop steps up again.
        earlier = TimedLift().propose(
            _ctx(_buffer(_FALL_THEN_FLOOR), current_bid=7999.0)
        )
        later = TimedLift().propose(_ctx(buf, current_bid=7999.0))
        assert earlier is not None and later is not None
        assert later > earlier

    def test_level_is_constant_within_a_period(self):
        # Cushion driven by ATR alone (noise term disabled) so the comparison is
        # exact: within one period the reviewed window does not move, so neither
        # does the proposal — the stop is re-computed per period, not per tick.
        updater = TimedLift(cushion_noise_mult=0.0)
        first = updater.propose(_ctx(_buffer(_FALL_THEN_FLOOR), current_bid=7996.0))
        # One more minute inside the same period (minute 21 of 20..30).
        buf_next = _buffer([*_FALL_THEN_FLOOR, 7996.0])
        second = updater.propose(_ctx(buf_next, current_bid=7996.0))
        assert first is not None
        assert second == first


class TestNeverWorsensTheStop:
    def test_never_lowers_the_stop(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        # A follower already above the period's floor: the proposal would be a
        # downgrade, so the stop holds where it is.
        assert (
            TimedLift().propose(_ctx(buf, current_bid=7996.0, level_follower=7985.0))
            is None
        )

    def test_requires_a_minimum_advance(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        floor = _window_low(_FALL_THEN_FLOOR, 10, 19)
        # Follower a hair under the candidate: not worth a broker push.
        assert (
            TimedLift().propose(
                _ctx(buf, current_bid=7996.0, level_follower=floor - 0.5)
            )
            is None
        )

    def test_holds_once_the_follower_reached_break_even(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        # A prior excursion already locked a level at/above break-even: nothing
        # for this zone to tighten any more.
        assert (
            TimedLift().propose(_ctx(buf, current_bid=7996.0, level_follower=8000.0))
            is None
        )


class TestSafetyClearance:
    def test_holds_when_the_candidate_would_crowd_the_live_bid(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        floor = _window_low(_FALL_THEN_FLOOR, 10, 19)
        # The bid has fallen back onto the period's floor: lifting the stop there
        # would put it within ordinary pull-back range, so the updater holds.
        assert TimedLift().propose(_ctx(buf, current_bid=floor + 0.2)) is None

    def test_respects_the_brokers_minimum_stop_distance(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        # The level that is fine with no broker floor…
        assert TimedLift().propose(_ctx(buf, current_bid=7996.0)) is not None
        # …is refused once IG's minimum distance makes it unpostable.
        assert (
            TimedLift().propose(_ctx(buf, current_bid=7996.0, min_stop_distance=50.0))
            is None
        )

    def test_volatile_epics_keep_a_wider_distance(self):
        buf = _buffer(_FALL_THEN_FLOOR)
        calm = TimedLift().propose(_ctx(buf, current_bid=7996.0, atr_value=1.0))
        wild = TimedLift().propose(_ctx(buf, current_bid=7996.0, atr_value=10.0))
        assert calm is not None and wild is not None
        # The noisier the epic, the deeper the stop sits under the same floor.
        assert wild < calm


class TestRegistration:
    def test_selectable_as_close_zonestart(self):
        assert ZONESTART_UPDATERS["timedlift"] is UnderwaterTimedLiftStop
        updater = build_zone_updater(ZONESTART_UPDATERS, "timedlift", object())
        assert isinstance(updater, UnderwaterTimedLiftStop)
