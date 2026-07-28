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
from src.exit import CloseZoneProfit
from src.feed.price_buffer import Candle, EpicBuffer
from src.models.position import Position, PositionState
from src.stops import StopAtr


def _profile() -> CloseZoneProfit:
    """The composed close profile with a deterministic flat-ATR initial stop.

    The flat-ATR distance keeps the open-time stop assertion exact (entry −
    2.5×ATR); the per-tick zone routing is the profile's own.
    """
    return CloseZoneProfit(stop_distance=StopAtr())


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


def _buffer_rising(n: int = 60, start: float = 100.0, step: float = 1.0) -> EpicBuffer:
    """Steadily rising buffer (rising last-3 bids) for the profit-zone ratchet."""
    buf = EpicBuffer(epic="X", max_candles=n + 10)
    prev = start
    for i in range(n):
        close = start + i * step
        high = max(prev, close) + 1
        low = min(prev, close) - 1
        buf.add(
            Candle(
                timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
                bid_open=prev,
                bid_close=close,
                bid_high=high,
                bid_low=low,
                offer_open=prev,
                offer_close=close,
                offer_high=high,
                offer_low=low,
            )
        )
        prev = close
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
        svc, client, _ = _service(close_profile=_profile())
        buf = _buffer_atr2(close=100.0)  # ATR 2 -> stop 2.5*2 = 5 below entry
        client.get.side_effect = [
            _market(),
            {"dealStatus": "ACCEPTED", "dealId": "D1", "level": 100.0},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF"})

        pos = await svc.open_from_intent(EntryIntent(epic="X", direction="BUY"), buf)

        assert pos is not None
        # The software stop was chosen by the close profile's stop-distance policy
        # (entry - 2.5*ATR = 95), not by the entry strategy.
        assert float(pos.level_follower) == pytest.approx(95.0)
        # The stop posted at IG sits one spread (0 here) plus the ATR noise cushion
        # (0.5 * ATR 2 = 1) below the software stop, so it opens at 94.
        payload = client.post.await_args.args[1]
        assert payload["stopLevel"] == pytest.approx(94.0)
        # The position remembers which profile manages its exit.
        assert pos.close_profile == "close_zoneprofit"

    async def test_requires_a_close_profile(self):
        svc, _, _ = _service(close_profile=None)
        buf = _buffer_atr2()
        with pytest.raises(ValueError, match="close profile"):
            await svc.open_from_intent(EntryIntent(epic="X", direction="BUY"), buf)


class TestManagePosition:
    async def test_ratchets_stop_via_profile(self):
        # Never a close-hour so the profit-zone trailing branch is exercised. The
        # bid is far above the (open-frozen) margin and the tail is rising, so the
        # profit-gated profile ratchets the stop up below the bid.
        svc, client, db = _service(close_profile=_profile())
        buf = _buffer_rising()  # rising last-3 bids → momentum confirmed
        bid = buf.last.bid_close
        pos = Position(
            epic="X",
            deal_id="D1",
            level_open=Decimal("100"),
            level_zero=Decimal("100"),  # break-even at entry
            level_follower=Decimal("0"),
            level_win=Decimal("0"),
            level_loose=Decimal("0"),
        )

        closed = await svc.manage_position(pos, current_bid=bid, buf=buf)

        assert closed is False
        # Stop ratcheted up: strictly below the bid and above the initial 0.
        assert 0.0 < float(pos.level_follower) < bid
        assert pos.stop_update == 1
        client.put.assert_awaited_once()
        db.commit.assert_awaited()

    async def test_holds_below_entry(self):
        # Below break-even (underwater zone) → hold, stop untouched.
        svc, client, _ = _service(close_profile=_profile())
        buf = _buffer_atr2()
        pos = Position(
            epic="X",
            deal_id="D1",
            level_open=Decimal("100"),
            level_zero=Decimal("100"),
            level_follower=Decimal("0"),
            level_win=Decimal("0"),
            level_loose=Decimal("0"),
        )

        closed = await svc.manage_position(pos, current_bid=95.0, buf=buf)

        assert closed is False
        client.put.assert_not_awaited()

    async def test_end_of_day_closes_via_profile(self):
        # At the epic's close hour → profile returns CLOSE(end_of_day).
        svc, client, _ = _service(close_profile=_profile())
        svc._is_epic_close_hour = AsyncMock(return_value=True)
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
        svc, client, _ = _service(close_profile=None)
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
    """The per-epic close gate: driven solely by the epic's own close time."""

    async def test_no_force_close_when_close_time_unknown(self):
        # Unknown per-epic close time -> NO hard fallback: never a close-hour.
        svc, _, db = _service()
        db.scalar = AsyncMock(return_value=None)  # unknown -> None
        assert await svc._is_epic_close_hour("X") is False

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


class TestPreOpenCloseSoonGate:
    """The pre-open gate: block opening when the epic's market closes soon."""

    async def test_unknown_close_time_never_blocks(self):
        # 24h market (or a market we could not open anyway) -> allowed.
        svc, _, db = _service()
        db.scalar = AsyncMock(return_value=None)
        assert await svc._is_epic_close_soon("X") is False

    async def test_blocks_within_close_margin_plus_buffer(self, monkeypatch):
        from datetime import time

        import src.execution.trading as trading_mod

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 6, 26, 15, 0, tzinfo=tz)

        monkeypatch.setattr(trading_mod, "datetime", _FrozenDT)

        # margin 5 + buffer 60 = block when the market closes within 65 min.
        svc, _, db = _service(close_margin_minutes=5, open_close_buffer_minutes=60)
        # now 15:00, close 15:30 -> 30 min <= 65 -> block.
        db.scalar = AsyncMock(return_value=time(15, 30))
        assert await svc._is_epic_close_soon("X") is True
        # now 15:00, close 16:30 -> 90 min > 65 -> allow.
        db.scalar = AsyncMock(return_value=time(16, 30))
        assert await svc._is_epic_close_soon("X") is False


class TestSameDayReopenPolicy:
    """The global ``ALLOW_SAME_DAY_REOPEN`` policy on the shared open path.

    It is carried by ``TradeConfig`` (from ``.env``) rather than by the entry
    strategy, so every open profile obeys it — per-epic loops included.
    """

    def _gate_ready(self, svc, *, traded_today: bool):
        """Neutralise the other gates so only the re-open rule is exercised."""
        svc._is_epic_open = AsyncMock(return_value=False)
        svc._is_epic_close_soon = AsyncMock(return_value=False)
        svc._is_epic_traded_today = AsyncMock(return_value=traded_today)

    async def test_policy_off_blocks_a_second_open_the_same_day(self):
        svc, _, _ = _service(allow_same_day_reopen=False)
        self._gate_ready(svc, traded_today=True)
        allowed, reason = await svc.can_open_intent(
            EntryIntent(epic="X", direction="BUY")
        )
        assert not allowed and "already traded today" in reason

    async def test_policy_off_still_allows_an_unused_epic(self):
        svc, _, _ = _service(allow_same_day_reopen=False)
        self._gate_ready(svc, traded_today=False)
        allowed, _ = await svc.can_open_intent(EntryIntent(epic="X", direction="BUY"))
        assert allowed is True

    async def test_policy_on_allows_and_skips_the_lookup(self):
        svc, _, _ = _service(allow_same_day_reopen=True)
        self._gate_ready(svc, traded_today=True)
        allowed, _ = await svc.can_open_intent(EntryIntent(epic="X", direction="BUY"))
        assert allowed is True
        svc._is_epic_traded_today.assert_not_awaited()  # no needless DB round-trip

    async def test_manual_open_bypasses_the_policy(self):
        # ``allow_reopen`` mirrors ``allow_short``: an explicit human open is not
        # refused because a strategy already used this epic today.
        svc, _, _ = _service(allow_same_day_reopen=False)
        self._gate_ready(svc, traded_today=True)
        allowed, _ = await svc.can_open_intent(
            EntryIntent(epic="X", direction="BUY"), allow_reopen=True
        )
        assert allowed is True

    async def test_policy_off_blocks_the_opposite_direction_too(self):
        # One opening per epic per day covers BUY *and* SELL.
        svc, _, _ = _service(allow_same_day_reopen=False)
        self._gate_ready(svc, traded_today=True)
        allowed, reason = await svc.can_open_intent(
            EntryIntent(epic="X", direction="SELL"), allow_short=True
        )
        assert not allowed and "already traded today" in reason

    async def test_traded_today_reads_todays_openings(self):
        from datetime import date

        svc, _, db = _service(allow_same_day_reopen=False)
        result = MagicMock()
        result.first.return_value = (1,)  # one opening recorded today
        db.execute = AsyncMock(return_value=result)
        assert await svc._is_epic_traded_today("X") is True
        result.first.return_value = None
        assert await svc._is_epic_traded_today("X") is False
        # Filtered on the epic and today's trading day, not on the position state.
        query = db.execute.await_args.args[0]
        sql = str(query)
        assert "position.epic" in sql and "position.date" in sql
        assert "state" not in sql  # a closed opening still counts as "used today"
        assert query.compile().params["date_1"] == date.today()
