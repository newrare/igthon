"""Tests for the far quarter-hour-ATR stop and its ATR helper."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr, estimated_timeframe_atr, timeframe_atr
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopFar, get_stop_distance

_START = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)


def _candles(count: int, *, swing: float = 1.0, spread: float = 0.5) -> list[Candle]:
    """A saw-tooth: each quarter-hour bar spans exactly ``swing`` points."""
    out = []
    for i in range(count):
        bid = 100.0 + (swing if (i // 15) % 2 else 0.0)
        out.append(
            Candle(
                timestamp=_START + timedelta(minutes=i),
                bid_open=bid,
                bid_close=bid,
                bid_high=bid,
                bid_low=bid,
                offer_open=bid + spread,
                offer_close=bid + spread,
                offer_high=bid + spread,
                offer_low=bid + spread,
            )
        )
    return out


def _buffer(candles: list[Candle]) -> EpicBuffer:
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(candles) + 10)
    for candle in candles:
        buf.add(candle)
    return buf


class TestTimeframeAtr:
    def test_measures_the_quarter_hour_true_range(self):
        # Bars alternate 100 / 101: every true range is 1 point.
        assert timeframe_atr(_candles(16 * 15), 15) == pytest.approx(1.0)

    def test_shrinks_the_period_to_the_history(self):
        # 10 bars → 9 true ranges, still above the 8 minimum.
        assert timeframe_atr(_candles(10 * 15), 15) == pytest.approx(1.0)

    def test_too_short_a_history_gives_zero(self):
        assert timeframe_atr(_candles(8 * 15), 15) == 0.0

    def test_one_minute_falls_back_to_plain_atr(self):
        assert timeframe_atr(_candles(30), 1) == 0.0  # flat inside each bar


class TestEstimatedTimeframeAtr:
    def test_measures_five_bars_directly(self):
        assert estimated_timeframe_atr(_candles(5 * 15), 15) == pytest.approx(1.0)

    def test_scales_the_one_minute_atr_when_bars_are_missing(self):
        candles = _candles(40, swing=1.0)

        estimate = estimated_timeframe_atr(candles, 15)

        assert estimate == pytest.approx(atr(candles, 14) * math.sqrt(15))


class TestStopFar:
    def test_registered(self):
        assert isinstance(get_stop_distance("stop_far", object()), StopFar)

    def test_buy_sits_eight_atr15_below_the_entry(self):
        buf = _buffer(_candles(16 * 15))

        stop = StopFar().initial_stop(entry_level=100.0, direction="BUY", buf=buf)

        assert stop == pytest.approx(92.0)

    def test_sell_sits_eight_atr15_above_the_offer(self):
        buf = _buffer(_candles(16 * 15))
        offer = buf.last.offer_close

        stop = StopFar().initial_stop(entry_level=100.0, direction="SELL", buf=buf)

        assert stop == pytest.approx(offer + 8.0)
