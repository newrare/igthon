"""Tests for the loss-recovery feature (detection + double-size SELL open)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.recovery import (
    RECOVERY_QTY_MULTIPLIER,
    is_recovery_trigger,
)
from src.execution.trading import TradeConfig, TradingService
from src.feed.price_buffer import Candle, EpicBuffer
from src.models.position import Position, PositionState


def _closed_long(
    *,
    win: int = 0,
    reason_close: str = "stop",
    reason_open: str = "auto",
    direction: str = "BUY",
    euro_max: float = 0.0,
    held_seconds: float = 120.0,
) -> Position:
    """A closed long shaped to match (or not) the recovery pattern."""
    opened = time(16, 4, 0)
    closed_dt = datetime.combine(date(2026, 7, 2), opened) + timedelta(
        seconds=held_seconds
    )
    return Position(
        epic="IX.D.EMGMKT.IFM.IP",
        epic_name="EMGMKT",
        direction=direction,
        date=date(2026, 7, 2),
        time_open=opened,
        time_close=closed_dt.time(),
        state=PositionState.CLOSE,
        reason_open=reason_open,
        reason_close=reason_close,
        win=win,
        euro=Decimal("-40"),
        euro_max=Decimal(str(euro_max)),
        quantity=1,
    )


def _buffer_atr2(close: float = 100.0, spread: float = 0.0, n: int = 30) -> EpicBuffer:
    """Buffer with constant TR=2 (ATR=2) and a flat close/offer."""
    buf = EpicBuffer(epic="IX.D.EMGMKT.IFM.IP", max_candles=200)
    for _ in range(n):
        buf.add(
            Candle(
                timestamp=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
                bid_open=close,
                bid_close=close,
                bid_high=close + 1,
                bid_low=close - 1,
                offer_open=close + spread,
                offer_close=close + spread,
                offer_high=close + 1 + spread,
                offer_low=close - 1 + spread,
            )
        )
    return buf


class TestIsRecoveryTrigger:
    """The pattern predicate — quick, never-in-profit long stop-out."""

    def test_matches_the_reversal_pattern(self):
        assert is_recovery_trigger(_closed_long()) is True

    def test_wins_are_never_recovered(self):
        assert is_recovery_trigger(_closed_long(win=1)) is False

    def test_only_stop_reasons(self):
        assert is_recovery_trigger(_closed_long(reason_close="end_of_day")) is False
        assert is_recovery_trigger(_closed_long(reason_close="win")) is False
        assert is_recovery_trigger(_closed_long(reason_close="loose")) is True

    def test_crossed_break_even_is_not_recovered(self):
        # A meaningfully positive favourable excursion means it went into profit.
        assert is_recovery_trigger(_closed_long(euro_max=5.0)) is False

    def test_slow_loss_is_not_recovered(self):
        assert is_recovery_trigger(_closed_long(held_seconds=3600)) is False

    def test_shorts_are_not_recovered(self):
        # Anti-loop by direction: a recovery SELL is itself a short.
        assert is_recovery_trigger(_closed_long(direction="SELL")) is False

    def test_recovery_open_is_not_re_recovered(self):
        # Anti-loop: a recovery that loses never spawns another recovery.
        assert is_recovery_trigger(_closed_long(reason_open="recovery")) is False


def _recovery_market() -> dict:
    """A TRADEABLE index-style /markets payload (scaling 1, wide stop band)."""
    return {
        "instrument": {
            "name": "Emerging Mkt",
            "expiry": "-",
            "currencies": [{"code": "EUR", "exchangeRate": 1, "isDefault": True}],
            "contractSize": "1",
        },
        "snapshot": {"marketStatus": "TRADEABLE", "scalingFactor": "1"},
        "dealingRules": {
            "minNormalStopOrLimitDistance": {"value": 1, "unit": "POINTS"},
            "maxStopOrLimitDistance": {"value": 1000, "unit": "POINTS"},
            "minDealSize": {"value": 1},
        },
    }


def _open_service():
    client = AsyncMock()
    db = AsyncMock()
    db.add = MagicMock()  # add() is synchronous in the code path
    svc = TradingService(client=client, db_session=db, config=TradeConfig())
    return svc, client, db


class TestOpenRecoveryShort:
    """open_recovery_short sells double size with a stop ABOVE the entry."""

    async def test_opens_double_size_sell_with_stop_above_entry(self):
        svc, client, db = _open_service()
        buf = _buffer_atr2(close=100.0)
        closed = _closed_long()  # quantity=1
        client.get.side_effect = [
            _recovery_market(),
            {"dealStatus": "ACCEPTED", "dealId": "DEALS", "level": 100.0},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REFS"})

        pos = await svc.open_recovery_short(closed, buf)

        assert pos is not None
        payload = client.post.await_args.args[1]
        assert payload["direction"] == "SELL"
        # Double the closed long's quantity.
        assert int(payload["size"]) == closed.quantity * RECOVERY_QTY_MULTIPLIER
        assert pos.quantity == closed.quantity * RECOVERY_QTY_MULTIPLIER
        # A short's protective stop sits ABOVE the entry.
        assert payload["stopLevel"] > float(pos.level_open)
        assert float(pos.level_follower) > float(pos.level_open)
        # Stamped so it is managed by the short profile and never re-recovered.
        assert pos.direction == "SELL"
        assert pos.reason_open == "recovery"
        assert pos.close_profile == "recovery_short"

    async def test_skips_when_market_not_tradeable(self):
        svc, client, db = _open_service()
        buf = _buffer_atr2()
        market = _recovery_market()
        market["snapshot"]["marketStatus"] = "CLOSED"
        client.get.side_effect = [market]

        assert await svc.open_recovery_short(_closed_long(), buf) is None
        client.post.assert_not_called()


class TestDirectionAwarePnl:
    """_euro_pnl mirrors the sign for a short position."""

    def test_short_profits_when_price_falls(self):
        pos = Position(
            direction="SELL",
            level_open=Decimal("100.0"),
            euro_per_point=Decimal("10"),
        )
        svc = TradingService(client=None, db_session=None, config=TradeConfig())
        # Price falls 5 -> short gains +50; rises 5 -> loses 50.
        assert svc._euro_pnl(pos, 95.0) == pytest.approx(50.0)
        assert svc._euro_pnl(pos, 105.0) == pytest.approx(-50.0)
