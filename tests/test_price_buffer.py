"""Tests for the price buffer module."""

from datetime import UTC, datetime

import pytest

from src.services.price_buffer import Candle, EpicBuffer, PriceBuffer


def _make_candle(
    bid: float, offer: float | None = None, ts: str = "2026-01-01 10:00"
) -> Candle:
    """Helper to create a test candle."""
    if offer is None:
        offer = bid + 1.8
    return Candle(
        timestamp=datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=UTC),
        bid_open=bid - 0.5,
        bid_close=bid,
        bid_high=bid + 1.0,
        bid_low=bid - 1.0,
        offer_open=offer - 0.5,
        offer_close=offer,
        offer_high=offer + 1.0,
        offer_low=offer - 1.0,
        volume=10,
    )


class TestCandle:
    """Tests for the Candle dataclass."""

    def test_mid_close(self):
        c = _make_candle(100.0, 101.8)
        assert c.mid_close == (100.0 + 101.8) / 2

    def test_spread(self):
        c = _make_candle(100.0, 101.8)
        assert c.spread == pytest.approx(1.8)


class TestEpicBuffer:
    """Tests for the EpicBuffer."""

    def test_add_candle(self):
        buf = EpicBuffer(epic="TEST", max_candles=5)
        buf.add(_make_candle(100.0))
        assert len(buf) == 1
        assert buf.last.bid_close == 100.0

    def test_rolling_window(self):
        buf = EpicBuffer(epic="TEST", max_candles=3)
        for i in range(5):
            buf.add(_make_candle(100.0 + i))
        assert len(buf) == 3
        assert buf.bid_closes == [102.0, 103.0, 104.0]

    def test_empty_buffer(self):
        buf = EpicBuffer(epic="TEST", max_candles=5)
        assert buf.last is None
        assert buf.bid_closes == []

    def test_spreads(self):
        buf = EpicBuffer(epic="TEST", max_candles=5)
        buf.add(_make_candle(100.0, 101.8))
        buf.add(_make_candle(200.0, 201.8))
        assert buf.spreads == pytest.approx([1.8, 1.8])


class TestPriceBuffer:
    """Tests for the central PriceBuffer."""

    def test_get_or_create(self):
        buffer = PriceBuffer(max_candles=10)
        buf = buffer.get_or_create("EPIC_A")
        assert buf.epic == "EPIC_A"
        assert buffer.get("EPIC_A") is buf

    def test_update(self):
        buffer = PriceBuffer(max_candles=10)
        candles = [_make_candle(100.0 + i) for i in range(5)]
        buffer.update("EPIC_A", candles)
        assert len(buffer.get("EPIC_A")) == 5

    def test_clear(self):
        buffer = PriceBuffer(max_candles=10)
        buffer.update("EPIC_A", [_make_candle(100.0)])
        buffer.update("EPIC_B", [_make_candle(200.0)])
        assert len(buffer) == 2
        buffer.clear()
        assert len(buffer) == 0

    def test_tracked_epics(self):
        buffer = PriceBuffer(max_candles=10)
        buffer.update("EPIC_A", [_make_candle(100.0)])
        buffer.update("EPIC_B", [_make_candle(200.0)])
        assert set(buffer.tracked_epics) == {"EPIC_A", "EPIC_B"}

    def test_add_candle(self):
        buffer = PriceBuffer(max_candles=10)
        buffer.add_candle("NEW_EPIC", _make_candle(150.0))
        assert buffer.get("NEW_EPIC").last.bid_close == 150.0
