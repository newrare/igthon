"""Tests for the trading service."""

from decimal import Decimal

import pytest

from src.models.position import Position
from src.services.compute import RegressionResult, TradingLevels, TradingSignal
from src.services.trading import TradeConfig, TradingService


def _service() -> TradingService:
    """A TradingService with no client/session — for testing pure helpers."""
    return TradingService(client=None, db_session=None, config=TradeConfig())


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
