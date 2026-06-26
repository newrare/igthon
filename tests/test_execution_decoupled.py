"""Tests for the decoupled open/close runtime path on TradingService.

These cover the seam where an exit-agnostic ``EntryIntent`` is composed with an
independently chosen ``CloseProfile``:

- ``open_from_intent`` asks the close profile for the stop, routes through the
  existing order pipeline, and stamps the position with the profile name.
- ``manage_position`` delegates each tick to that profile and applies its
  decision (close / ratchet stop / hold).
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.entry.base import EntryIntent
from src.execution.trading import TradeConfig, TradingService
from src.exit.atr_trailing import AtrTrailingExit
from src.feed.price_buffer import Candle, EpicBuffer
from src.models.position import Position, PositionState


def _buffer_atr2(n: int = 20, close: float = 100.0) -> EpicBuffer:
    """Buffer with a constant True Range of 2 → ATR == 2.0."""
    buf = EpicBuffer(epic="X", max_candles=200)
    for _ in range(n):
        buf.add(
            Candle(
                timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
                bid_open=close,
                bid_close=close,
                bid_high=close + 1,
                bid_low=close - 1,
                offer_open=close,
                offer_close=close,
                offer_high=close + 1,
                offer_low=close - 1,
            )
        )
    return buf


def _service(close_profile=None, **config_overrides):
    client = AsyncMock()
    db = AsyncMock()
    db.add = MagicMock()
    svc = TradingService(
        client=client,
        db_session=db,
        config=TradeConfig(**config_overrides),
        close_profile=close_profile,
    )
    return svc, client, db


def _market() -> dict:
    return {
        "instrument": {
            "name": "TEST",
            "expiry": "-",
            "currencies": [{"code": "EUR", "exchangeRate": 1.0, "isDefault": True}],
            "contractSize": "1",
        },
        "snapshot": {"marketStatus": "TRADEABLE", "scalingFactor": "1"},
        "dealingRules": {
            "minNormalStopOrLimitDistance": {"value": 1.0, "unit": "POINTS"},
            "maxStopOrLimitDistance": {"value": 1000, "unit": "POINTS"},
            "minDealSize": {"value": 1},
        },
    }


class TestOpenFromIntent:
    async def test_close_profile_chooses_stop_and_stamps_position(self):
        svc, client, _ = _service(close_profile=AtrTrailingExit())
        buf = _buffer_atr2(close=100.0)  # ATR 2 -> stop 2.5*2 = 5 below entry
        client.get.side_effect = [
            _market(),
            {"dealStatus": "ACCEPTED", "dealId": "D1", "level": 100.0},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF"})

        pos = await svc.open_from_intent(EntryIntent(epic="X", direction="BUY"), buf)

        assert pos is not None
        # The stop was chosen by the close profile (entry - 2.5*ATR = 95), not
        # by the entry strategy.
        payload = client.post.await_args.args[1]
        assert payload["stopLevel"] == pytest.approx(95.0)
        # The position remembers which profile manages its exit.
        assert pos.close_profile == "atr_trailing"

    async def test_requires_a_close_profile(self):
        svc, _, _ = _service(close_profile=None)
        buf = _buffer_atr2()
        with pytest.raises(ValueError, match="close profile"):
            await svc.open_from_intent(EntryIntent(epic="X", direction="BUY"), buf)


class TestManagePosition:
    async def test_ratchets_stop_via_profile(self):
        # Never a close-hour so the trailing branch is exercised deterministically.
        svc, client, db = _service(close_profile=AtrTrailingExit(), hour_close=99)
        buf = _buffer_atr2()  # ATR 2, k_pre 2.5 -> distance 5
        pos = Position(
            epic="X",
            deal_id="D1",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),  # not reached at bid 105
            level_follower=Decimal("0"),
            level_win=Decimal("0"),
            level_loose=Decimal("0"),
        )

        closed = await svc.manage_position(pos, current_bid=105.0, buf=buf)

        assert closed is False
        assert float(pos.level_follower) == pytest.approx(100.0)  # 105 - 5
        assert pos.stop_update == 1
        client.put.assert_awaited_once()
        db.commit.assert_awaited()

    async def test_holds_below_entry(self):
        svc, client, _ = _service(close_profile=AtrTrailingExit(), hour_close=99)
        buf = _buffer_atr2()
        pos = Position(
            epic="X",
            deal_id="D1",
            level_open=Decimal("100"),
            level_follower=Decimal("0"),
            level_win=Decimal("0"),
            level_loose=Decimal("0"),
        )

        closed = await svc.manage_position(pos, current_bid=95.0, buf=buf)

        assert closed is False
        client.put.assert_not_awaited()

    async def test_end_of_day_closes_via_profile(self):
        # hour_close 0 → always a close-hour → profile returns CLOSE(end_of_day).
        svc, client, _ = _service(close_profile=AtrTrailingExit(), hour_close=0)
        buf = _buffer_atr2()
        pos = Position(
            epic="X",
            deal_id="D1",
            level_open=Decimal("100"),
            level_follower=Decimal("0"),
            level_win=Decimal("0"),
            level_loose=Decimal("0"),
            quantity=1,
        )
        client.delete = AsyncMock(return_value={"dealReference": "R"})
        client.get = AsyncMock(return_value={})  # close confirmation unavailable

        closed = await svc.manage_position(pos, current_bid=105.0, buf=buf)

        assert closed is True
        assert pos.state == PositionState.CLOSE
        assert pos.reason_close == "end_of_day"
        client.delete.assert_awaited_once()

    async def test_without_profile_falls_back_to_check_and_close(self):
        # No close profile wired → legacy check_and_close path (no IG put here).
        svc, client, _ = _service(close_profile=None, hour_close=99)
        buf = _buffer_atr2()
        pos = Position(
            epic="X",
            deal_id="D1",
            level_open=Decimal("100"),
            level_follower=Decimal("0"),
            level_win=Decimal("0"),
            level_loose=Decimal("0"),
        )

        closed = await svc.manage_position(pos, current_bid=95.0, buf=buf)
        assert closed is False


class TestPerEpicCloseHour:
    """The per-epic close gate: epic-specific close time, else global fallback."""

    async def test_falls_back_to_global_hour_close_when_unknown(self):
        # No per-epic close time -> global hour_close governs.
        svc, _, db = _service(hour_close=0)
        db.scalar = AsyncMock(return_value=None)  # unknown -> None
        assert await svc._is_epic_close_hour("X") is True  # now.hour >= 0 always

        svc2, _, db2 = _service(hour_close=99)
        db2.scalar = AsyncMock(return_value=None)
        assert await svc2._is_epic_close_hour("X") is False  # now.hour >= 99 never

    async def test_uses_per_epic_close_minus_margin(self, monkeypatch):
        from datetime import time

        import src.execution.trading as trading_mod

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 6, 26, 15, 28, tzinfo=tz)

        monkeypatch.setattr(trading_mod, "datetime", _FrozenDT)

        svc, _, db = _service(close_margin_minutes=5)
        # now 15:28 >= 15:30 - 5min (15:25) -> close
        db.scalar = AsyncMock(return_value=time(15, 30))
        assert await svc._is_epic_close_hour("X") is True
        # now 15:28 < 16:00 - 5min (15:55) -> hold
        db.scalar = AsyncMock(return_value=time(16, 0))
        assert await svc._is_epic_close_hour("X") is False
