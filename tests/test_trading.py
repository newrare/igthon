"""Tests for the trading service."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.api.client import IGAPIError
from src.core.indicators import RegressionResult, TradingLevels, TradingSignal
from src.execution.trading import (
    CONFIRM_MAX_ATTEMPTS,
    MARKET_ORDER_NOT_SUPPORTED_CODE,
    TradeConfig,
    TradingService,
)
from src.feed.price_buffer import Candle, EpicBuffer
from src.models.position import Position, PositionState


def _ig_error(status: int, code: str = "") -> IGAPIError:
    """An IGAPIError carrying a real HTTP status and IG error code."""
    request = httpx.Request("GET", "https://demo-api.ig.com/gateway/deal/confirms/REF1")
    response = httpx.Response(status, request=request)
    return IGAPIError(
        f"HTTP {status}", request=request, response=response, ig_error_code=code
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
        # No euro_per_point -> reconstruct €/price-unit from euro_stop and the
        # PRICE distance to the stop (scalingFactor-independent). Forex row: a
        # 0.00100 (10-pip) stop risking 20€ -> 20000 €/price-unit; a +0.00050 move
        # is +10€. The old size-based formula understated this by 10^4.
        pos = Position(
            level_open=Decimal("1.10000"),
            euro_per_point=None,
            euro_stop=Decimal("20.0"),
            level_loose=Decimal("1.09900"),  # stop 0.00100 below entry
            quantity=1,
        )
        assert _service()._euro_pnl(pos, 1.10050) == pytest.approx(10.0)

    def test_legacy_fallback_mirrors_for_short(self):
        # Short: profits as the price falls. Stop 0.00100 ABOVE entry risking 20€;
        # a 0.00050 fall is +10€ (the long formula would sign it -10€).
        pos = Position(
            direction="SELL",
            level_open=Decimal("1.10000"),
            euro_per_point=None,
            euro_stop=Decimal("20.0"),
            level_loose=Decimal("1.10100"),  # short stop sits above the entry
            quantity=1,
        )
        assert _service()._euro_pnl(pos, 1.09950) == pytest.approx(10.0)


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

    async def test_ratchet_appends_to_stop_history(self):
        svc, _, _ = _trailing_service()
        buf = _buffer_with_atr2()
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),
            level_follower=None,
            stop_history=[{"t": "2026-06-08T10:00:00+00:00", "level": 95.0}],
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        # Initial seed kept, the ratchet appended a new point at the new level.
        assert len(pos.stop_history) == 2
        assert pos.stop_history[0] == {"t": "2026-06-08T10:00:00+00:00", "level": 95.0}
        assert pos.stop_history[-1]["level"] == pytest.approx(100.0)
        assert "t" in pos.stop_history[-1]

    async def test_broker_stop_pushed_one_spread_below_follower(self):
        # The software follower is what the bot closes on; the stop posted at IG
        # rests one spread below it (live spread), so the app-side stop is hit
        # first and the broker order is a deeper safety net.
        svc, client, _ = _trailing_service()
        buf = _buffer_with_atr2(spread=2.0)  # ATR 2 -> dist 5; live spread 2
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),
            level_follower=None,
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        # Follower unchanged: 105 - 5 = 100.
        assert float(pos.level_follower) == pytest.approx(100.0)
        # Broker stop pushed a spread (2) below the follower.
        payload = client.put.await_args.args[1]
        assert payload["stopLevel"] == pytest.approx(98.0)
        # History records both levels so the chart can draw the two lines apart.
        assert pos.stop_history[-1]["level"] == pytest.approx(100.0)
        assert pos.stop_history[-1]["broker"] == pytest.approx(98.0)

    async def test_no_ratchet_leaves_stop_history_untouched(self):
        svc, _, _ = _trailing_service()
        buf = _buffer_with_atr2()
        seed = [{"t": "2026-06-08T10:00:00+00:00", "level": 103.0}]
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),
            level_follower=Decimal("103"),  # candidate 100 < current -> no change
            stop_history=list(seed),
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        assert pos.stop_history == seed


class TestBrokerStopClamp:
    """The broker stop never sits inside IG's minimum-distance floor (fix 2)."""

    def test_buy_pulled_back_when_too_close(self):
        svc = _service()
        pos = Position(epic="X", direction="BUY", min_stop_distance=Decimal("1.0"))
        # Desired broker 104.5 is only 0.5 below price 105 — inside the 1.0 floor.
        clamped = svc._clamp_broker_stop_to_min_distance("BUY", 104.5, 105.0, pos)
        assert clamped == pytest.approx(104.0)  # price - min_dist

    def test_buy_left_alone_when_far_enough(self):
        svc = _service()
        pos = Position(epic="X", direction="BUY", min_stop_distance=Decimal("1.0"))
        clamped = svc._clamp_broker_stop_to_min_distance("BUY", 103.0, 105.0, pos)
        assert clamped == pytest.approx(103.0)

    def test_sell_pushed_out_when_too_close(self):
        svc = _service()
        pos = Position(epic="X", direction="SELL", min_stop_distance=Decimal("1.0"))
        # A short's stop sits above price; 105.5 is only 0.5 above -> pushed to 106.
        clamped = svc._clamp_broker_stop_to_min_distance("SELL", 105.5, 105.0, pos)
        assert clamped == pytest.approx(106.0)  # price + min_dist

    def test_unknown_floor_is_noop(self):
        svc = _service()
        pos = Position(epic="X", direction="BUY", min_stop_distance=None)
        clamped = svc._clamp_broker_stop_to_min_distance("BUY", 104.9, 105.0, pos)
        assert clamped == pytest.approx(104.9)


class TestBrokerStopTruth:
    """The persisted broker stop / chart's Loose line reflects only what IG took."""

    async def test_accepted_push_advances_persisted_broker(self):
        svc, client, _ = _trailing_service()
        buf = _buffer_with_atr2(spread=1.0)  # pre-BE dist 5 -> follower 100
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            direction="BUY",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),
            level_follower=None,
            level_stop=Decimal("95"),  # last accepted broker
            level_security=Decimal("95"),
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        # Broker accepted at 99 (follower 100 − spread 1): persisted levels and the
        # Loose history point advance to the accepted level.
        assert float(pos.level_follower) == pytest.approx(100.0)
        assert float(pos.level_stop) == pytest.approx(99.0)
        assert float(pos.level_security) == pytest.approx(99.0)
        assert pos.stop_history[-1]["broker"] == pytest.approx(99.0)

    async def test_rejected_push_keeps_last_accepted_broker(self):
        svc, client, _ = _trailing_service()
        client.put = AsyncMock(side_effect=_ig_error(400, "error.trailing.too-close"))
        buf = _buffer_with_atr2(spread=1.0)
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            direction="BUY",
            level_open=Decimal("100"),
            level_zero=Decimal("110"),
            level_follower=None,
            level_stop=Decimal("95"),  # last accepted broker
            level_security=Decimal("95"),
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        # Software follower still advances — it is the local guard.
        assert float(pos.level_follower) == pytest.approx(100.0)
        assert pos.stop_update == 1
        # IG refused the broker move, so the persisted broker level and the Loose
        # history point stay at the last accepted level (no phantom 99 stop).
        assert float(pos.level_stop) == pytest.approx(95.0)
        assert float(pos.level_security) == pytest.approx(95.0)
        assert pos.stop_history[-1]["broker"] == pytest.approx(95.0)
        assert pos.stop_history[-1]["level"] == pytest.approx(100.0)

    async def test_broker_clamped_to_floor_before_push(self):
        svc, client, _ = _trailing_service()
        buf = _buffer_with_atr2(spread=1.0)  # post-BE dist 3 -> follower 102
        pos = Position(
            epic="X",
            deal_id="DEAL1",
            direction="BUY",
            level_open=Decimal("100"),
            level_zero=Decimal("101"),  # cleared at 105 -> post-BE regime
            level_follower=Decimal("100"),
            level_stop=Decimal("95"),
            level_security=Decimal("95"),
            min_stop_distance=Decimal("5.0"),  # floor = 105 − 5 = 100
        )

        await svc._update_trailing_stop(pos, current_bid=105.0, buf=buf)

        # Raw broker 101 (follower 102 − spread 1) is only 4 below price 105, inside
        # the 5.0 floor: pulled back to 100 (the deepest still-accepted level).
        assert float(pos.level_follower) == pytest.approx(102.0)
        payload = client.put.await_args.args[1]
        assert payload["stopLevel"] == pytest.approx(100.0)
        assert float(pos.level_stop) == pytest.approx(100.0)


class _StubProfile:
    """Close profile that always proposes ``new_stop`` as a stop update."""

    name = "stub"

    def __init__(self, new_stop: float) -> None:
        self._new_stop = new_stop

    def evaluate(self, position, current_bid, buf, *, is_close_hour):
        from src.exit.base import ACTION_UPDATE_STOP, CloseDecision

        return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=self._new_stop)


class TestManagePositionRatchet:
    """``manage_position`` enforces the ratchet invariant when applying a stop."""

    def _svc_with_profile(self, profile):
        svc, client, db = _trailing_service()
        svc._is_epic_close_hour = AsyncMock(return_value=False)
        svc._close_profile = profile
        return svc, client, db

    async def test_long_stop_is_never_lowered(self):
        # A lower stop than the current follower (e.g. a pull-back re-entering a
        # lower zone) must be rejected — a long's stop only ratchets up.
        svc, _, db = self._svc_with_profile(_StubProfile(new_stop=1.62408))
        pos = Position(
            epic="X", deal_id="D", direction="BUY", level_follower=Decimal("1.62413")
        )
        closed = await svc.manage_position(
            pos, current_bid=1.6250, buf=_buffer_with_atr2()
        )
        assert closed is False
        assert float(pos.level_follower) == pytest.approx(1.62413)  # unchanged
        db.commit.assert_not_awaited()

    async def test_long_stop_raise_is_stored_at_full_precision(self):
        # A higher stop is applied and persisted at 5 dp — the coarser 3 dp round
        # would have collapsed 1.62418 to 1.624 and desynced the up-only guard.
        svc, _, db = self._svc_with_profile(_StubProfile(new_stop=1.62418))
        pos = Position(
            epic="X", deal_id="D", direction="BUY", level_follower=Decimal("1.62413")
        )
        await svc.manage_position(pos, current_bid=1.6250, buf=_buffer_with_atr2())
        assert float(pos.level_follower) == pytest.approx(1.62418)
        db.commit.assert_awaited()

    async def test_short_stop_is_never_raised(self):
        # Mirror for a short: the stop only ratchets down, so a higher proposal is
        # rejected. The short path routes through the recovery short profile.
        svc, _, db = _trailing_service()
        svc._is_epic_close_hour = AsyncMock(return_value=False)
        svc._recovery_short_profile = _StubProfile(new_stop=1.62500)
        pos = Position(
            epic="X", deal_id="D", direction="SELL", level_follower=Decimal("1.62413")
        )
        closed = await svc.manage_position(
            pos, current_bid=1.6230, buf=_buffer_with_atr2()
        )
        assert closed is False
        assert float(pos.level_follower) == pytest.approx(1.62413)  # unchanged
        db.commit.assert_not_awaited()


class _ZoneStubProfile:
    """Close profile stub proposing a stop and reporting a fixed current zone."""

    name = "zonestub"

    def __init__(self, new_stop: float, zone) -> None:
        self._new_stop = new_stop
        self._zone = zone

    def evaluate(self, position, current_bid, buf, *, is_close_hour):
        from src.exit.base import ACTION_UPDATE_STOP, CloseDecision

        return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=self._new_stop)

    def current_zone(self, position, current_bid, buf):
        return self._zone


class TestManagePositionManualHold:
    """A manual stop override holds while the bid stays in its zone."""

    def _svc(self, profile):
        svc, client, db = _trailing_service()
        svc._is_epic_close_hour = AsyncMock(return_value=False)
        svc._close_profile = profile
        return svc, client, db

    async def test_hold_suppresses_auto_ratchet_in_same_zone(self):
        from src.exit.zones import StopZone

        # Auto would raise the stop, but the manual override was set in the zone
        # the bid is still in — the raise is suppressed and the stop is held.
        svc, _, db = self._svc(
            _ZoneStubProfile(new_stop=1.62500, zone=StopZone.BREAKEVEN_BAND)
        )
        pos = Position(
            epic="X",
            deal_id="D",
            direction="BUY",
            level_follower=Decimal("1.62413"),
            manual_stop_zone="breakeven_band",
        )
        closed = await svc.manage_position(
            pos, current_bid=1.6260, buf=_buffer_with_atr2()
        )
        assert closed is False
        assert float(pos.level_follower) == pytest.approx(1.62413)  # unchanged
        assert pos.manual_stop_zone == "breakeven_band"  # still held
        db.commit.assert_not_awaited()

    async def test_zone_change_releases_hold_and_resumes_auto(self):
        from src.exit.zones import StopZone

        # The bid has crossed into a different zone: the override is cleared and
        # the auto proposal (a higher stop) is applied normally.
        svc, _, db = self._svc(
            _ZoneStubProfile(new_stop=1.62500, zone=StopZone.PROFIT)
        )
        pos = Position(
            epic="X",
            deal_id="D",
            direction="BUY",
            level_follower=Decimal("1.62413"),
            manual_stop_zone="breakeven_band",
        )
        closed = await svc.manage_position(
            pos, current_bid=1.6280, buf=_buffer_with_atr2()
        )
        assert closed is False
        assert pos.manual_stop_zone is None  # released
        assert float(pos.level_follower) == pytest.approx(1.62500)  # auto applied
        db.commit.assert_awaited()


class TestRaiseStopManually:
    """The dashboard chart buttons raise the stop and pin its zone."""

    async def test_long_raise_sets_follower_broker_and_zone(self):
        from src.exit.zones import StopZone

        svc, client, db = _trailing_service()
        buf = _buffer_with_atr2(close=105.0, spread=2.0)  # bid 105, live spread 2
        pos = Position(
            epic="X", deal_id="D", direction="BUY", level_follower=Decimal("95")
        )
        profile = _ZoneStubProfile(new_stop=0.0, zone=StopZone.BREAKEVEN_BAND)

        ok, msg = await svc.raise_stop_manually(pos, 100.0, buf, profile=profile)

        assert ok is True
        assert float(pos.level_follower) == pytest.approx(100.0)
        assert pos.manual_stop_zone == "breakeven_band"
        # Broker stop pushed a spread (2) below the follower.
        payload = client.put.await_args.args[1]
        assert payload["stopLevel"] == pytest.approx(98.0)
        assert pos.stop_history[-1]["level"] == pytest.approx(100.0)
        db.commit.assert_awaited()

    async def test_long_rejects_stop_at_or_above_bid(self):
        svc, client, _ = _trailing_service()
        buf = _buffer_with_atr2()  # bid 100
        pos = Position(epic="X", deal_id="D", direction="BUY")

        ok, msg = await svc.raise_stop_manually(pos, 101.0, buf, profile=None)

        assert ok is False
        assert "below the current bid" in msg
        client.put.assert_not_awaited()

    async def test_long_rejects_lowering_the_stop(self):
        svc, client, _ = _trailing_service()
        buf = _buffer_with_atr2()  # bid 100
        pos = Position(
            epic="X", deal_id="D", direction="BUY", level_follower=Decimal("97")
        )

        ok, msg = await svc.raise_stop_manually(pos, 96.0, buf, profile=None)

        assert ok is False
        assert "raised" in msg
        client.put.assert_not_awaited()


class TestReconcileVanishedCloseLevel:
    """``_reconcile_vanished`` backs the close level out of the P&L, direction-aware."""

    def test_short_close_level_is_above_entry_on_a_loss(self):
        # Regression (#8): a short stopped out ABOVE its entry has a negative P&L;
        # close = open - euro/epp must land ABOVE the open, not below it (the
        # long-only ``open + euro/epp`` put it below and read as a phantom win).
        svc = _service()
        pos = Position(
            direction="SELL",
            level_open=Decimal("1.10000"),
            level_close=None,
            euro=Decimal("-20.0"),  # loss: price rose against the short
            euro_per_point=Decimal("20000"),
        )
        svc._reconcile_vanished(pos)
        assert pos.reason_close == "closed_externally"
        # close = 1.10000 - (-20 / 20000) = 1.10100 (above the entry)
        assert float(pos.level_close) == pytest.approx(1.10100)
        assert float(pos.euro) == pytest.approx(-20.0)
        assert pos.win == 0


class TestManagePositionShortRouting:
    """A recovery SELL must never fall into the long-only ``check_and_close``."""

    async def test_short_with_no_buffer_is_not_closed_by_long_maths(self):
        # Regression: on the first monitor tick after a restart the epic's price
        # buffer is not yet streamed (``buf is None``). Falling back to
        # ``check_and_close`` there is long-only: it fires ``loose`` on
        # ``bid <= level_loose``, but a short's ``level_loose`` sits ABOVE the
        # price, so that is true on nearly every tick and would close the
        # double-size short at market regardless of P&L. It must hold instead and
        # rely on the broker-side stop pushed at open.
        svc, _, _ = _trailing_service()
        svc._is_epic_close_hour = AsyncMock(return_value=False)
        svc._close_position = AsyncMock(return_value=True)
        pos = Position(
            epic="X",
            deal_id="D",
            direction="SELL",
            level_open=Decimal("1.6200"),
            level_loose=Decimal("1.6250"),  # a short's stop sits ABOVE the entry
            level_win=Decimal("0"),
        )
        closed = await svc.manage_position(pos, current_bid=1.6205, buf=None)
        assert closed is False
        svc._close_position.assert_not_awaited()


class TestCloseRejectedByBroker:
    """A close IG explicitly REJECTS (e.g. market closed on an EDITS_ONLY market)
    must leave the position OPEN. Recording a fabricated close is the root cause
    of the weekend-held-deal duplication: the DB thought the position was gone
    while it stayed live at IG, so the next sync reused/duplicated its dealId and
    the real close P&L landed on the wrong row."""

    async def test_rejected_confirm_keeps_position_open(self):
        svc, client, db = _trailing_service()
        # IG accepts the DELETE (200 + dealReference) but the confirm reports the
        # market-closed refusal as dealStatus=REJECTED.
        client.delete = AsyncMock(return_value={"dealReference": "REF1"})
        client.get = AsyncMock(
            return_value={"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"}
        )
        pos = Position(
            epic="CC.D.LCC.UNC.IP",
            deal_id="DEALX",
            direction="BUY",
            state=PositionState.OPEN,
            quantity=1,
            level_open=Decimal("3762.8"),
            euro_stop=Decimal("400"),
            euro_per_point=Decimal("11.6"),
        )

        result = await svc._close_position(pos, 4128.9, "market_closed")

        # The close failed: position stays OPEN, no fabricated P&L, no commit.
        assert result is False
        assert pos.state == PositionState.OPEN
        assert pos.reason_close is None
        assert pos.euro is None
        db.commit.assert_not_awaited()

    async def test_accepted_confirm_still_closes(self):
        # Guard against over-eager rejection: a normal ACCEPTED confirm closes.
        svc, client, db = _trailing_service()
        client.delete = AsyncMock(return_value={"dealReference": "REF1"})
        client.get = AsyncMock(
            return_value={
                "dealStatus": "ACCEPTED",
                "level": 4128.9,
                "profit": 4247.01,
                "profitCurrency": "EUR",
            }
        )
        pos = Position(
            epic="CC.D.LCC.UNC.IP",
            deal_id="DEALX",
            direction="BUY",
            state=PositionState.OPEN,
            quantity=1,
            level_open=Decimal("3762.8"),
            euro_stop=Decimal("400"),
            euro_per_point=Decimal("11.6"),
        )

        result = await svc._close_position(pos, 4128.9, "stop")

        assert result is True
        assert pos.state == PositionState.CLOSE
        assert pos.reason_close == "stop"
        assert float(pos.euro) == pytest.approx(4247.01)


def _close_pos(**overrides) -> Position:
    """An OPEN long with a dealId, ready to be closed."""
    base = dict(
        epic="EN.D.ICENG.FWS9.IP",
        deal_id="DEALX",
        direction="BUY",
        state=PositionState.OPEN,
        quantity=1,
        level_open=Decimal("100.0"),
        euro_stop=Decimal("400"),
        euro_per_point=Decimal("11.6"),
    )
    base.update(overrides)
    return Position(**base)


def _accepted_confirm() -> dict:
    return {
        "dealStatus": "ACCEPTED",
        "level": 99.8,
        "profit": 12.0,
        "profitCurrency": "EUR",
    }


class TestCloseMarketOrderFallback:
    """Mirror of the open path: an epic that rejects orderType=MARKET
    (``market-orders.not-supported-for-epic``, typically forwards) must close via
    a marketable LIMIT instead of failing the close and leaving the position
    unprotected."""

    async def test_falls_back_to_limit_when_market_rejected_at_deal_time(self):
        svc, client, _ = _trailing_service()
        client.delete = AsyncMock(
            side_effect=[
                _ig_error(400, MARKET_ORDER_NOT_SUPPORTED_CODE),
                {"dealReference": "REF1"},
            ]
        )
        client.get = AsyncMock(return_value=_accepted_confirm())

        result = await svc._close_position(_close_pos(), 100.0, "stop")

        assert result is True
        assert client.delete.await_count == 2
        first = client.delete.await_args_list[0].args[1]
        second = client.delete.await_args_list[1].args[1]
        assert first["orderType"] == "MARKET"
        assert second["orderType"] == "LIMIT"
        # Closing a long is a SELL -> priced BELOW the touch by the slippage.
        assert second["level"] == pytest.approx(99.8)
        assert "EN.D.ICENG.FWS9.IP" in svc._market_order_unsupported

    async def test_known_bad_epic_closes_with_limit_directly(self):
        svc, client, _ = _trailing_service()
        svc._market_order_unsupported.add("EN.D.ICENG.FWS9.IP")
        client.delete = AsyncMock(return_value={"dealReference": "REF1"})
        client.get = AsyncMock(return_value=_accepted_confirm())

        result = await svc._close_position(_close_pos(), 100.0, "stop")

        assert result is True
        client.delete.assert_awaited_once()
        assert client.delete.await_args.args[1]["orderType"] == "LIMIT"

    async def test_unrelated_broker_error_aborts_without_retry(self):
        svc, client, _ = _trailing_service()
        client.delete = AsyncMock(
            side_effect=_ig_error(400, "error.trading.some-other")
        )

        result = await svc._close_position(_close_pos(), 100.0, "stop")

        assert result is False
        client.delete.assert_awaited_once()


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
        # Broker stop posted one spread (0.0003) BELOW the software stop, so the
        # app-side follower is hit first and the broker order is a safety net.
        assert payload["stopLevel"] == pytest.approx(1.20520)  # 1.20550 - 0.0003
        # Recorded broker line is a real price just below entry — never negative.
        assert float(pos.level_stop) == pytest.approx(1.20520)
        assert float(pos.level_stop) > 0
        # size is the distance expressed in IG points (0.0045 * 10000).
        assert pos.size == 45

    async def test_opens_marketable_limit_when_metadata_flags_no_market_orders(self):
        """An epic advertising marketOrderPreference=NOT_AVAILABLE (forwards, some
        futures) opens with a marketable LIMIT directly — no doomed MARKET first.
        The limit is priced a slippage buffer above the ask (a BUY fills at the
        offer) with timeInForce=EXECUTE_AND_ELIMINATE."""
        svc, client, _ = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        market = _open_market()
        market["instrument"]["marketOrderPreference"] = "NOT_AVAILABLE"
        client.get.side_effect = [
            market,
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        client.post.assert_awaited_once()
        payload = client.post.await_args.args[1]
        assert payload["orderType"] == "LIMIT"
        assert payload["timeInForce"] == "EXECUTE_AND_ELIMINATE"
        # offer (1.21030) priced 0.2% through the touch: 1.21030 * 1.002.
        assert payload["level"] == pytest.approx(1.21272)
        # The protective stop carries over unchanged.
        assert payload["stopLevel"] == pytest.approx(1.20520)

    async def test_falls_back_to_limit_when_market_rejected_at_deal_time(self):
        """Metadata says market orders are fine, but IG still bounces the MARKET
        POST with market-orders.not-supported-for-epic. The order is retried once
        as a marketable LIMIT."""
        svc, client, _ = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        client.get.side_effect = [
            _open_market(),  # no marketOrderPreference -> treated as supported
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(
            side_effect=[
                _ig_error(400, MARKET_ORDER_NOT_SUPPORTED_CODE),
                {"dealReference": "REF1"},
            ]
        )

        pos = await svc.open_position(signal)

        assert pos is not None
        assert client.post.await_count == 2
        first = client.post.await_args_list[0].args[1]
        second = client.post.await_args_list[1].args[1]
        assert first["orderType"] == "MARKET"
        assert second["orderType"] == "LIMIT"
        assert second["level"] == pytest.approx(1.21272)

    async def test_remembers_epic_and_skips_market_on_next_open(self):
        """After a MARKET rejection, a later open on the same epic goes straight to
        a marketable LIMIT — no repeated doomed MARKET attempt."""
        svc, client, _ = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)

        # First open: MARKET bounces, fallback to LIMIT succeeds.
        client.get.side_effect = [
            _open_market(),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(
            side_effect=[
                _ig_error(400, MARKET_ORDER_NOT_SUPPORTED_CODE),
                {"dealReference": "REF1"},
            ]
        )
        assert await svc.open_position(signal) is not None
        assert "CS.D.AUDNZD.CFD.IP" in svc._market_order_unsupported

        # Second open: the epic is known-bad, so only a single LIMIT POST fires.
        client.get.side_effect = [
            _open_market(),
            {"dealStatus": "ACCEPTED", "dealId": "DEALY", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF2"})
        assert await svc.open_position(signal) is not None
        client.post.assert_awaited_once()
        assert client.post.await_args.args[1]["orderType"] == "LIMIT"

    async def test_does_not_retry_on_unrelated_broker_error(self):
        """A rejection other than market-orders-not-supported aborts the open
        without a LIMIT retry."""
        svc, client, _ = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        client.get.side_effect = [_open_market()]
        client.post = AsyncMock(side_effect=_ig_error(400, "error.trading.some-other"))

        pos = await svc.open_position(signal)

        assert pos is None
        client.post.assert_awaited_once()

    async def test_opens_when_market_order_preference_available(self):
        """AVAILABLE_DEFAULT_ON must not trip the market-order guard."""
        svc, client, _ = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        market = _open_market()
        market["instrument"]["marketOrderPreference"] = "AVAILABLE_DEFAULT_ON"
        client.get.side_effect = [
            market,
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        client.post.assert_awaited_once()

    async def test_clamps_stop_out_to_minimum_distance(self):
        svc, client, _ = _open_service()
        # Isolate the clamp from the safety margin (tested separately).
        svc._config.stop_min_distance_margin = 0.0
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
        # Clamped to the 8-point minimum (1.21000 - 0.0008 = 1.20920), then the
        # broker stop posted a further spread (0.0003) below.
        assert payload["stopLevel"] == pytest.approx(1.20890)  # 1.20920 - 0.0003
        assert float(pos.level_stop) == pytest.approx(1.20890)

    async def test_stop_min_distance_margin_pads_the_floor(self):
        """The order stop is padded past IG's bare minimum by the safety margin,
        so a fast-moving market can't push it back under the floor ("Stop trop
        près"). With a 15% margin the 8-point minimum becomes 9.2 points."""
        svc, client, _ = _open_service()
        svc._config.stop_min_distance_margin = 0.15
        # Strategy stop 1 point away; IG minimum 8 points → floor padded to 9.2.
        signal = _open_signal(bid=1.21000, level_security=1.20990)
        client.get.side_effect = [
            _open_market(min_stop=8.0),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        payload = client.post.await_args.args[1]
        # 1.21000 - 0.0008 * 1.15 = 1.20908 (software stop), broker a spread below.
        assert payload["stopLevel"] == pytest.approx(1.20878)  # 1.20908 - 0.0003
        assert float(pos.level_stop) == pytest.approx(1.20878)

    async def test_clamp_realigns_decoupled_software_stop(self):
        """Decoupled path: the close profile sets ONE stop
        (follower == loose == security). When IG widens it to the minimum
        distance, every software stop level must follow — otherwise the bot
        enforces a stop tighter than the one resting at the broker and closes the
        position in the noise at a level IG would never have hit."""
        svc, client, _ = _open_service()
        # Isolate the clamp from the safety margin (tested separately).
        svc._config.stop_min_distance_margin = 0.0
        bid = 1.21000
        spread = 0.0003
        stop = 1.20990  # 1 point away; IG minimum is 8 points → must widen out
        signal = TradingSignal(
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
                level_follower=stop,
                level_win=bid + 0.01,
                level_zero=bid + spread,
                level_loose=stop,
                level_security=stop,
                stop_distance=1,
            ),
        )
        client.get.side_effect = [
            _open_market(min_stop=8.0),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": bid},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        # Software stop widened to the 8-point minimum (1.20920); the broker line
        # rests one spread (0.0003) below it, the deeper safety net.
        assert float(pos.level_stop) == pytest.approx(1.20890)  # 1.20920 - 0.0003
        assert float(pos.level_security) == pytest.approx(1.20890)
        # ...and every SOFTWARE stop level followed the clamp (none tighter).
        assert float(pos.level_follower) == pytest.approx(1.20920)
        assert float(pos.level_loose) == pytest.approx(1.20920)
        # The seeded stop trajectory uses the realigned follower, not the tight one.
        assert pos.stop_history[0]["level"] == pytest.approx(1.20920)
        # ...and carries the matching broker point a spread below.
        assert pos.stop_history[0]["broker"] == pytest.approx(1.20890)

    async def test_legacy_distinct_levels_not_collapsed_by_clamp(self):
        """A legacy strategy with an intentionally tighter follower/loose keeps
        them: only levels that equalled the pre-clamp security are realigned."""
        svc, client, _ = _open_service()
        # Isolate the clamp from the safety margin (tested separately).
        svc._config.stop_min_distance_margin = 0.0
        # _open_signal sets follower=bid-0.001, loose=bid-0.003, security=param
        # (all distinct). The clamp widens the order stop but must touch none.
        signal = _open_signal(bid=1.21000, level_security=1.20990)
        client.get.side_effect = [
            _open_market(min_stop=8.0),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        # Broker line is the clamped stop (1.20920) minus one spread (0.0003).
        assert float(pos.level_stop) == pytest.approx(1.20890)
        # Distinct legacy levels preserved (not folded to the broker stop).
        assert float(pos.level_follower) == pytest.approx(1.20900)  # bid - 0.001
        assert float(pos.level_loose) == pytest.approx(1.20700)  # bid - 0.003


class TestOpenPositionPersistence:
    """The position is recorded the instant IG accepts the order — before the
    /confirms round-trip — so a failed confirm can never leave a live position
    untracked (the "margin in use with no open position" bug)."""

    async def test_persists_before_confirm_and_keeps_row_when_confirm_fails(
        self, monkeypatch
    ):
        monkeypatch.setattr("src.execution.trading.asyncio.sleep", AsyncMock())
        svc, client, db = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)

        # market OK, then every confirm attempt 404s (deal not resolvable yet).
        def _get(endpoint, **_kw):
            if "confirms" in endpoint:
                raise _ig_error(404, "error.confirms.deal-not-found")
            return _open_market()

        client.get.side_effect = _get
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        # Row was written (add + commit happened before the confirm) and kept.
        assert pos is not None
        db.add.assert_called_once_with(pos)
        assert pos.deal_reference == "REF1"
        assert pos.deal_id is None  # unresolved — sync will bind it
        assert pos.state == PositionState.OPEN
        db.delete.assert_not_called()
        # The 404 (deal not ready) was retried, not abandoned on the first call.
        confirm_calls = [
            c for c in client.get.call_args_list if "confirms" in c.args[0]
        ]
        assert len(confirm_calls) == CONFIRM_MAX_ATTEMPTS

    async def test_confirm_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("src.execution.trading.asyncio.sleep", AsyncMock())
        svc, client, db = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)

        # 404 on the first confirm poll, then IG resolves the deal.
        seq = [
            _ig_error(404, "error.confirms.deal-not-found"),
            {"dealStatus": "ACCEPTED", "dealId": "D-OK", "level": 1.21000},
        ]
        confirm_iter = iter(seq)

        def _get(endpoint, **_kw):
            if "confirms" in endpoint:
                nxt = next(confirm_iter)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _open_market()

        client.get.side_effect = _get
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        assert pos.deal_id == "D-OK"  # bound on the retry
        db.delete.assert_not_called()

    async def test_confirm_permanent_4xx_is_not_retried(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr("src.execution.trading.asyncio.sleep", sleep_mock)
        svc, client, db = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)

        # A non-404 4xx (e.g. bad request) is permanent — stop immediately.
        def _get(endpoint, **_kw):
            if "confirms" in endpoint:
                raise _ig_error(400, "error.public-api.failure.validation")
            return _open_market()

        client.get.side_effect = _get
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        # Row kept (sync reconciles), but confirm tried exactly once — no backoff.
        assert pos is not None
        assert pos.deal_id is None
        confirm_calls = [
            c for c in client.get.call_args_list if "confirms" in c.args[0]
        ]
        assert len(confirm_calls) == 1
        sleep_mock.assert_not_awaited()
        db.delete.assert_not_called()

    async def test_rejected_deal_removes_the_draft_row(self):
        svc, client, db = _open_service()
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        client.get.side_effect = [
            _open_market(),
            {"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS"},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        # Draft was added then removed; caller sees a clean failure.
        assert pos is None
        db.add.assert_called_once()
        draft = db.add.call_args.args[0]
        db.delete.assert_awaited_once_with(draft)


def _ig_entry(
    *,
    deal_id: str,
    epic: str,
    direction: str = "BUY",
    level: float = 288.52,
    stop: float | None = 287.49,
    bid: float = 288.49,
) -> dict:
    """A GET /positions entry shaped like IG's real payload."""
    return {
        "position": {
            "dealId": deal_id,
            "dealReference": f"REF-{deal_id}",
            "direction": direction,
            "level": level,
            "stopLevel": stop,
            "limitLevel": None,
            "size": 1.0,
            "currency": "EUR",
            "contractSize": 50.0,
            "createdDateUTC": "2026-06-16T10:47:30",
        },
        "market": {
            "epic": epic,
            "bid": bid,
            "offer": bid + 0.1,
            "scalingFactor": 1,
            "instrumentName": "EU Stocks Banks",
        },
    }


def _sync_service(db_open: list[Position]):
    """Service whose DB returns ``db_open`` for the OPEN-positions query."""
    client = AsyncMock()
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = db_open
    db.execute = AsyncMock(return_value=result)
    svc = TradingService(client=client, db_session=db, config=TradeConfig())
    return svc, client, db


class TestSyncAdoption:
    """sync_open_positions reconciles BOTH directions: it adopts live IG
    positions the DB doesn't track and binds provisional rows to their dealId."""

    async def test_adopts_untracked_ig_position(self):
        svc, client, db = _sync_service(db_open=[])
        entry = _ig_entry(deal_id="D1", epic="IX.D.StoxxBank.FNI3.IP")
        market = {
            "instrument": {
                "contractSize": "50",
                "currencies": [{"code": "EUR", "exchangeRate": 1.0, "isDefault": True}],
            }
        }
        client.get = AsyncMock(side_effect=[{"positions": [entry]}, market])

        live = await svc.sync_open_positions()

        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert added.epic == "IX.D.StoxxBank.FNI3.IP"
        assert added.deal_id == "D1"
        assert added.reason_open == "adopted"
        assert added.state == PositionState.OPEN
        assert float(added.level_open) == pytest.approx(288.52)
        assert float(added.level_loose) == pytest.approx(287.49)
        # epp = size(1) * contractSize(50) * rate(1.0)
        assert float(added.euro_per_point) == pytest.approx(50.0)
        db.commit.assert_awaited()
        assert "IX.D.StoxxBank.FNI3.IP" in live

    async def test_does_not_re_adopt_a_known_deal_id(self):
        """Idempotency: a live IG position whose dealId is already recorded in the
        DB (in ANY state — e.g. an earlier adopted row since CLOSEd) is never
        adopted again. This is the fix for duplicate 'adopted' rows piling up
        across the 20s sync (observed: 6 rows for one DAX dealId)."""
        client = AsyncMock()
        db = AsyncMock()
        db.add = MagicMock()
        open_result = MagicMock()
        open_result.scalars.return_value.all.return_value = []  # no OPEN rows
        known_result = MagicMock()
        known_result.all.return_value = [("D1",)]  # D1 already known (any state)
        db.execute = AsyncMock(side_effect=[open_result, known_result])
        svc = TradingService(client=client, db_session=db, config=TradeConfig())
        entry = _ig_entry(deal_id="D1", epic="IX.D.StoxxBank.FNI3.IP")
        client.get = AsyncMock(return_value={"positions": [entry]})

        await svc.sync_open_positions()

        db.add.assert_not_called()

    async def test_does_not_adopt_non_buy(self):
        svc, client, db = _sync_service(db_open=[])
        entry = _ig_entry(deal_id="D2", epic="CS.D.EURUSD.CEF.IP", direction="SELL")
        client.get = AsyncMock(return_value={"positions": [entry]})

        await svc.sync_open_positions()

        db.add.assert_not_called()

    async def test_binds_provisional_row_by_epic_instead_of_adopting(self):
        prov = Position(
            epic="E1",
            epic_name="E1",
            deal_id=None,  # provisional: confirm never landed
            date=date(2026, 6, 16),
            state=PositionState.OPEN,
            level_open=Decimal("100.00000"),
            euro_per_point=Decimal("10.000000"),
        )
        svc, client, db = _sync_service(db_open=[prov])
        entry = _ig_entry(deal_id="D9", epic="E1", level=100.0, stop=99.0, bid=101.0)
        client.get = AsyncMock(return_value={"positions": [entry]})

        await svc.sync_open_positions()

        # Bound to the live dealId, NOT adopted as a duplicate row.
        assert prov.deal_id == "D9"
        db.add.assert_not_called()
        # Unrealized euro refreshed from the live bid: (101 - 100) * 10.
        assert float(prov.euro) == pytest.approx(10.0)

    async def test_binds_provisional_row_by_deal_reference_over_level(self):
        # An executed-but-unconfirmed order: the provisional row holds the
        # dealReference IG echoes on the live position. Binding must match on it
        # directly, NOT guess by closest level — which here would pick the wrong
        # entry. prov's ref points at the 105 entry while the 100 entry (closer
        # to prov's level) belongs to another row by exact dealId.
        prov = Position(
            epic="E1",
            epic_name="E1",
            deal_id=None,
            deal_reference="REF-X",
            date=date(2026, 6, 16),
            state=PositionState.OPEN,
            level_open=Decimal("100.0"),
            euro_per_point=Decimal("10.0"),
        )
        other = Position(
            epic="E1",
            epic_name="E1",
            deal_id="D-CLOSE",
            date=date(2026, 6, 16),
            state=PositionState.OPEN,
            level_open=Decimal("100.0"),
            euro_per_point=Decimal("10.0"),
        )
        svc, client, db = _sync_service(db_open=[prov, other])
        entry_match = _ig_entry(deal_id="D-REF", epic="E1", level=105.0, bid=105.0)
        entry_match["position"]["dealReference"] = "REF-X"
        entry_close = _ig_entry(deal_id="D-CLOSE", epic="E1", level=100.0, bid=100.0)
        entry_close["position"]["dealReference"] = "REF-OTHER"
        client.get = AsyncMock(return_value={"positions": [entry_match, entry_close]})

        await svc.sync_open_positions()

        # prov bound by dealReference (the 105 entry), not the closer-level one.
        assert prov.deal_id == "D-REF"
        assert other.deal_id == "D-CLOSE"
        db.add.assert_not_called()

    async def test_heals_rows_sharing_one_deal_id_without_duplicating(self):
        # Legacy corruption: three same-epic rows all stamped with the LAST
        # dealId by the old epic-keyed sync. The reconcile must spread the three
        # live dealIds across them — never grab one entry thrice and adopt the
        # other two as duplicates.
        def _row(level: float) -> Position:
            return Position(
                epic="E1",
                epic_name="E1",
                deal_id="C",  # all three share the last dealId
                date=date(2026, 6, 16),
                state=PositionState.OPEN,
                level_open=Decimal(str(level)),
                euro_per_point=Decimal("10.000000"),
            )

        rows = [_row(288.52), _row(288.62), _row(288.67)]
        svc, client, db = _sync_service(db_open=rows)
        client.get = AsyncMock(
            return_value={
                "positions": [
                    _ig_entry(deal_id="A", epic="E1", level=288.52, bid=288.52),
                    _ig_entry(deal_id="B", epic="E1", level=288.62, bid=288.62),
                    _ig_entry(deal_id="C", epic="E1", level=288.67, bid=288.67),
                ]
            }
        )

        await svc.sync_open_positions()

        # No new rows created, and every live dealId is now bound exactly once.
        db.add.assert_not_called()
        assert sorted(r.deal_id for r in rows) == ["A", "B", "C"]


@pytest.mark.asyncio
class TestSyncReconcileUnconfirmed:
    """sync_open_positions must not turn provisional rows whose open never
    confirmed (deal_id stayed None) into phantom closed_externally trades at €0.
    """

    def _provisional(self, *, opened: datetime, level: float = 100.0) -> Position:
        return Position(
            epic="E1",
            epic_name="E1",
            deal_id=None,  # provisional: /confirms never bound a dealId
            deal_reference="REF-PROV",
            date=opened.date(),
            time_open=opened.time(),
            state=PositionState.OPEN,
            level_open=Decimal(str(level)),
            euro_per_point=Decimal("10.000000"),
        )

    async def test_fresh_unconfirmed_row_is_left_alone_within_grace(self):
        # Opened a moment ago and absent from IG /positions (eventual
        # consistency). It must be neither closed nor adopted — just held so a
        # later sync can bind it.
        now = datetime.now(UTC).replace(tzinfo=None)
        prov = self._provisional(opened=now)
        svc, client, db = _sync_service(db_open=[prov])
        client.get = AsyncMock(return_value={"positions": []})

        await svc.sync_open_positions()

        assert prov.state == PositionState.OPEN
        assert prov.reason_close is None
        db.add.assert_not_called()

    async def test_stale_unconfirmed_row_is_marked_never_opened(self):
        # Past the grace window with still no dealId and still absent from IG:
        # the order never executed. It is flagged never_opened (NOT a real
        # closed_externally trade) so the dashboard excludes it from stats.
        old = datetime(2026, 6, 16, 10, 0)
        prov = self._provisional(opened=old)
        svc, client, db = _sync_service(db_open=[prov])
        client.get = AsyncMock(return_value={"positions": []})

        await svc.sync_open_positions()

        assert prov.state == PositionState.CLOSE
        assert prov.reason_close == "never_opened"
        assert float(prov.euro) == 0.0
        assert prov.win == 0
        # No phantom level move: close defaults to the open level.
        assert prov.level_close == prov.level_open
        db.commit.assert_awaited()

    def _real(self, *, euro: float = 12.5) -> Position:
        now = datetime.now(UTC).replace(tzinfo=None)
        return Position(
            epic="E1",
            epic_name="E1",
            deal_id="D-REAL",
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            level_open=Decimal("100.0"),
            euro=Decimal(str(euro)),
            euro_per_point=Decimal("10.000000"),
        )

    async def test_confirmed_row_that_vanishes_is_closed_externally_on_404(self):
        # A row WITH a real dealId that has dropped out of the bulk /positions
        # list is reconciled as closed_externally ONLY once the targeted
        # GET /positions/{dealId} authoritatively 404s (IG confirms it is gone).
        real = self._real()
        svc, client, db = _sync_service(db_open=[real])
        # 1st get() = bulk /positions (empty); 2nd = targeted probe → 404.
        client.get = AsyncMock(side_effect=[{"positions": []}, _ig_error(404)])

        await svc.sync_open_positions()

        assert real.state == PositionState.CLOSE
        assert real.reason_close == "closed_externally"

    async def test_confirmed_vanish_kept_open_when_targeted_refetch_still_open(self):
        # The false-positive this fixes: a transient bulk /positions miss (seen
        # right after a streaming reconnect rotated the session tokens) must NOT
        # close a position that is still live at IG. The targeted re-fetch returns
        # the position (200) → keep OPEN, never reconcile.
        real = self._real()
        svc, client, db = _sync_service(db_open=[real])
        client.get = AsyncMock(
            side_effect=[
                {"positions": []},  # bulk list transiently omits the position
                _ig_entry(deal_id="D-REAL", epic="E1"),  # targeted probe: still open
            ]
        )

        await svc.sync_open_positions()

        assert real.state == PositionState.OPEN
        assert real.reason_close is None

    async def test_confirmed_vanish_kept_open_on_transient_refetch_error(self):
        # If the targeted re-fetch itself errors (network / 5xx / any non-404),
        # the outcome is uncertain and the position must stay OPEN — a live
        # position is never closed on a guess.
        real = self._real()
        svc, client, db = _sync_service(db_open=[real])
        client.get = AsyncMock(side_effect=[{"positions": []}, _ig_error(500)])

        await svc.sync_open_positions()

        assert real.state == PositionState.OPEN
        assert real.reason_close is None
