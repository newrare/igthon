"""Tests for the trading service."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.position import Position
from src.services.compute import RegressionResult, TradingLevels, TradingSignal
from src.services.price_buffer import Candle, EpicBuffer
from src.services.trading import (
    TradeConfig,
    TradingService,
)


def _service() -> TradingService:
    """A TradingService with no client/session — for testing pure helpers."""
    return TradingService(client=None, db_session=None, config=TradeConfig())


def _buffer_with_atr2(n: int = 20, close: float = 100.0, spread: float = 0.0):
    """Buffer whose candles have a constant True Range of 2 -> ATR == 2.0.

    Each candle spans [close-1, close+1] with a flat close, so every TR is 2
    regardless of the offset, giving a deterministic ATR for trailing tests.
    """
    buf = EpicBuffer(epic="X", max_candles=200)
    for _ in range(n):
        buf.add(
            Candle(
                timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
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


def _trailing_service():
    """Service with mocked client/db for exercising the trailing-stop path."""
    client = AsyncMock()
    db = AsyncMock()
    service = TradingService(client=client, db_session=db, config=TradeConfig())
    return service, client, db


class TestEuroPnl:
    """_euro_pnl uses the stored euro_per_point (currency-aware)."""

    def test_uses_euro_per_point(self):
        # USD/JPY: 545.4959 €/point, moves -0.005 -> -2.73 €
        pos = Position(
            level_open=Decimal("160.239"),
            euro_per_point=Decimal("545.495871"),
        )
        assert _service()._euro_pnl(pos, 160.234) == pytest.approx(-2.73, abs=0.01)

    def test_gbp_eur_small_move(self):
        # GBP/EUR: 100000 €/point, moves -0.0008 -> -80 € (sub-0.001 move that
        # the old Numeric(10,3) storage truncated to zero)
        pos = Position(
            level_open=Decimal("1.15729"),
            euro_per_point=Decimal("100000"),
        )
        assert _service()._euro_pnl(pos, 1.15649) == pytest.approx(-80.0, abs=0.01)

    def test_legacy_fallback_without_euro_per_point(self):
        # No euro_per_point -> derive a per-pip value from euro_stop/size/quantity
        pos = Position(
            level_open=Decimal("100.0"),
            euro_per_point=None,
            euro_stop=Decimal("20.0"),
            size=10,
            quantity=1,
        )
        # per-pip = 20/10/1 = 2.0; move +5 -> 10.0
        assert _service()._euro_pnl(pos, 105.0) == pytest.approx(10.0)


class TestApplyTransaction:
    """_apply_transaction writes IG's authoritative figures onto a position."""

    def test_overwrites_euro_and_levels(self):
        pos = Position(level_open=Decimal("160.0"), level_close=Decimal("160.0"))
        txn = {
            "profitAndLoss": "E-2.73",
            "openLevel": "160.239",
            "closeLevel": "160.234",
        }
        assert _service()._apply_transaction(pos, txn) is True
        assert pos.euro == Decimal("-2.730")
        assert pos.level_open == Decimal("160.239")
        assert pos.level_close == Decimal("160.234")
        assert pos.win == 0

    def test_marks_win_when_positive(self):
        pos = Position()
        txn = {"profitAndLoss": "E12.50", "openLevel": "1.0", "closeLevel": "1.1"}
        assert _service()._apply_transaction(pos, txn) is True
        assert pos.win == 1
        assert pos.euro == Decimal("12.500")

    def test_rejects_unparseable_pnl(self):
        pos = Position()
        assert _service()._apply_transaction(pos, {"profitAndLoss": "n/a"}) is False
        assert pos.euro is None


class TestTransactionMatching:
    """IG transaction instrument names carry a 'converted at …' suffix and the
    reference is unrelated to our stored deal id — matching is name + level."""

    @pytest.mark.parametrize(
        "epic_name,instrument_name,expected",
        [
            ("USD/JPY", "USD/JPY converted at 0.005454958709088", True),
            ("GBP/EUR", "GBP/EUR", True),
            ("France 40 ", "France 40 Cash (€10)", True),  # epic_name 10-char trunc
            ("AUD/USD", "AUD/USD converted at 0.874624464", True),
            ("USD/JPY", "EUR/JPY converted at 0.005", False),
            ("GBP/EUR", "GBP/JPY", False),
            ("USD/JPY", None, False),
            (None, "USD/JPY", False),
        ],
    )
    def test_names_match(self, epic_name, instrument_name, expected):
        assert TradingService._names_match(epic_name, instrument_name) is expected

    def test_level_distance_prefers_closest(self):
        pos = Position(level_open=Decimal("1.157"), level_close=Decimal("1.157"))
        near = {"openLevel": "1.15729", "closeLevel": "1.15649"}
        far = {"openLevel": "1.15755", "closeLevel": "1.15732"}
        assert TradingService._level_distance(
            pos, near
        ) < TradingService._level_distance(pos, far)


class TestTradingSignalStructure:
    """Verify TradingSignal dataclass integrity."""

    def test_buy_signal(self):
        signal = TradingSignal(
            epic="IX.D.DAX.IFMM.IP",
            score=0.85,
            direction="BUY",
            regression=RegressionResult(slope=0.5, intercept=100.0, r_squared=0.85),
            sma_fast=25240.0,
            sma_slow=25220.0,
            roc=0.5,
            spread=1.8,
            avg_spread=1.8,
            position_in_range=55.0,
            levels=TradingLevels(
                bid=25240.0,
                offer=25241.8,
                spread=1.8,
                high=25280.0,
                low=25200.0,
                scope=80.0,
                average=25235.0,
                level_follower=25234.6,
                level_win=25247.2,
                level_zero=25241.8,
                level_loose=25225.6,
                level_security=25216.6,
                stop_distance=26,
            ),
        )
        assert signal.direction == "BUY"
        assert signal.score > 0.75
        assert signal.levels.stop_distance > 0
        assert signal.levels.level_win > signal.levels.bid
        assert signal.levels.level_loose < signal.levels.bid


class TestClampTrailingDistance:
    """_clamp_trailing_distance bounds the ATR distance between two limits."""

    def test_ceiling_is_initial_euro_risk(self):
        # euro_stop 10 / euro_per_point 5 -> max distance 2; raw 5 is clamped.
        svc = _service()
        pos = Position(euro_stop=Decimal("10"), euro_per_point=Decimal("5"))
        assert svc._clamp_trailing_distance(5.0, pos, spread=0.0) == pytest.approx(2.0)

    def test_floor_is_two_spreads(self):
        # No euro ceiling; raw 2 lifted to floor 2 x spread (3) = 6.
        svc = _service()
        pos = Position()
        assert svc._clamp_trailing_distance(2.0, pos, spread=3.0) == pytest.approx(6.0)

    def test_unbounded_passes_through(self):
        svc = _service()
        pos = Position()
        assert svc._clamp_trailing_distance(5.0, pos, spread=0.0) == pytest.approx(5.0)


class TestTrailingStop:
    """ATR-based trailing stop: ratchet, two-speed regime, IG push."""

    async def test_ratchets_up_before_break_even(self):
        svc, client, db = _trailing_service()
        buf = _buffer_with_atr2()  # ATR == 2 -> k_pre 2.5 -> distance 5
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),  # not yet reached at bid 105
            level_follower=None,
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        assert float(pos.level_follower) == pytest.approx(100.0)  # 105 - 5
        assert pos.stop_update == 1
        client.put.assert_awaited_once()
        endpoint = client.put.await_args.args[0]
        assert endpoint == "/positions/otc/DEAL1"
        assert client.put.await_args.kwargs["version"] == 2
        db.commit.assert_awaited()

    async def test_tightens_after_break_even(self):
        svc, client, _ = _trailing_service()
        buf = _buffer_with_atr2()
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("101"),  # cleared at bid 105 -> k_post 1.5 -> dist 3
            level_follower=Decimal("100"),
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        assert float(pos.level_follower) == pytest.approx(102.0)  # 105 - 3
        client.put.assert_awaited_once()

    async def test_trails_naturally_without_breakeven_pin(self):
        # The stop is NOT pinned up to level_zero on the first tick of profit
        # (that pin strangled trades flat). It trails a full ATR distance below
        # price; the upward-only ratchet then locks break-even organically as
        # the trade keeps running.
        svc, _, _ = _trailing_service()
        buf = _buffer_with_atr2()
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("104"),  # past zero at bid 105 -> k_post 1.5 -> dist 3
            level_follower=Decimal("100"),
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        assert float(pos.level_follower) == pytest.approx(102.0)  # 105 - 3, not 104

    async def test_does_not_move_down_or_below_step(self):
        svc, client, db = _trailing_service()
        buf = _buffer_with_atr2()
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),
            level_follower=Decimal("103"),  # candidate 100 < current -> no change
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        assert float(pos.level_follower) == pytest.approx(103.0)
        client.put.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_no_buffer_is_noop(self):
        svc, client, _ = _trailing_service()
        pos = Position(epic="X", deal_id="DEAL1", level_follower=Decimal("100"))

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=None)

        client.put.assert_not_awaited()


def _open_signal(*, bid: float, level_security: float) -> TradingSignal:
    """Minimal BUY signal for open_position; only epic/levels are read."""
    spread = 0.0003
    return TradingSignal(
        epic="CS.D.AUDNZD.CFD.IP",
        score=0.9,
        direction="BUY",
        regression=RegressionResult(slope=0.1, intercept=bid, r_squared=0.9),
        sma_fast=bid,
        sma_slow=bid,
        roc=0.1,
        spread=spread,
        avg_spread=spread,
        position_in_range=55.0,
        levels=TradingLevels(
            bid=bid,
            offer=bid + spread,
            spread=spread,
            high=bid + 0.01,
            low=bid - 0.01,
            scope=0.02,
            average=bid,
            level_follower=bid - 0.001,
            level_win=bid + 0.01,
            level_zero=bid + spread,
            level_loose=bid - 0.003,
            level_security=level_security,
            stop_distance=1,  # the legacy (buggy) ceil value — must be ignored now
        ),
    )


def _open_market(*, scaling_factor: str = "10000", min_stop: float = 4.0) -> dict:
    """A TRADEABLE AUD/NZD-style /markets payload for open_position."""
    return {
        "instrument": {
            "name": "AUD/NZD",
            "expiry": "-",
            "currencies": [{"code": "AUD", "exchangeRate": 0.6, "isDefault": True}],
            "contractSize": "1",
        },
        "snapshot": {"marketStatus": "TRADEABLE", "scalingFactor": scaling_factor},
        "dealingRules": {
            "minNormalStopOrLimitDistance": {"value": min_stop, "unit": "POINTS"},
            "maxStopOrLimitDistance": {"value": 1000, "unit": "POINTS"},
            "minDealSize": {"value": 1},
        },
    }


def _open_service():
    """Service wired for the open_position path with sync DB.add."""
    client = AsyncMock()
    db = AsyncMock()
    db.add = MagicMock()  # add() is synchronous in the code path
    svc = TradingService(client=client, db_session=db, config=TradeConfig())
    return svc, client, db


class TestOpenPosition:
    """open_position sends an absolute stopLevel and records a sane stop price.

    Regression guard for the unit-mismatch bug where the stop distance was
    computed in price terms, math.ceil'd to a tiny integer, sent to IG as a
    point distance (~1 pip), and subtracted from the entry price to record a
    nonsensical (often negative) ``level_stop``.
    """

    async def test_sends_stop_level_and_records_price(self):
        svc, client, db = _open_service()
        # Open at 1.21000 with the protective stop 45 points (0.0045) below.
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        client.get.side_effect = [
            _open_market(),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        payload = client.post.await_args.args[1]
        # Absolute level, NOT a point distance.
        assert "stopDistance" not in payload
        assert payload["stopLevel"] == pytest.approx(1.20550)
        # Recorded stop is a real price just below entry — never negative.
        assert float(pos.level_stop) == pytest.approx(1.20550)
        assert float(pos.level_stop) > 0
        # size is the distance expressed in IG points (0.0045 * 10000).
        assert pos.size == 45

    async def test_clamps_stop_out_to_minimum_distance(self):
        svc, client, _ = _open_service()
        # Strategy stop only 1 point away; IG minimum is 8 points (0.0008).
        signal = _open_signal(bid=1.21000, level_security=1.20990)
        client.get.side_effect = [
            _open_market(min_stop=8.0),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        payload = client.post.await_args.args[1]
        # Clamped to the 8-point minimum: 1.21000 - 0.0008.
        assert payload["stopLevel"] == pytest.approx(1.20920)
        assert float(pos.level_stop) == pytest.approx(1.20920)
