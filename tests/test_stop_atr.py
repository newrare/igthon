"""Tests for the flat-ATR stop-distance policy."""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopAtr, get_stop_distance
from src.stops.base import StopDistance

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


class TestRegistry:
    def test_known_name_resolves(self):
        dist = get_stop_distance("stop_atr", object())
        assert isinstance(dist, StopAtr)

    def test_is_stop_distance_instance(self):
        assert isinstance(StopAtr(), StopDistance)


class TestInitialStop:
    def test_buy_stop_is_k_atr_below_entry(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        dist = StopAtr(stop_atr_k=2.5)
        stop = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert stop == pytest.approx(entry - 2.5 * atr_v)
        assert stop < entry

    def test_sell_stop_is_k_atr_above_offer(self):
        buf = _buffer([8000.0 - i for i in range(40)], spread=0.5)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        offer = buf.last.offer_close
        dist = StopAtr(stop_atr_k=2.5)
        stop = dist.initial_stop(entry_level=entry, direction="SELL", buf=buf)
        assert stop == pytest.approx(offer + 2.5 * atr_v)
        assert stop > entry

    def test_wider_k_places_a_wider_stop(self):
        buf = _buffer([8000.0] * 40)
        entry = buf.last.bid_close
        narrow = StopAtr(stop_atr_k=2.0).initial_stop(
            entry_level=entry, direction="BUY", buf=buf
        )
        wide = StopAtr(stop_atr_k=3.5).initial_stop(
            entry_level=entry, direction="BUY", buf=buf
        )
        assert wide < narrow  # wider distance → lower stop
