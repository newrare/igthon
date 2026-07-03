"""Tests for the mirrored short exit (recovery_short) and its trailing maths."""

from datetime import UTC, datetime
from decimal import Decimal

from src.exit.base import ACTION_CLOSE, ACTION_HOLD, ACTION_UPDATE_STOP
from src.exit.recovery_short import RecoveryShortProfile
from src.exit.trailing import compute_trailing_stop, compute_trailing_stop_short
from src.feed.price_buffer import Candle, EpicBuffer
from src.models.position import Position


class _Cfg:
    """Minimal TrailingConfig (equal pre/post widths)."""

    atr_k_pre = 2.5
    atr_k_post = 2.5
    trailing_step_ratio = 0.3


class TestComputeTrailingStopShort:
    """Short chandelier: stop above price, ratchets down, mirrors the long."""

    def test_stop_sits_above_price(self):
        stop = compute_trailing_stop_short(
            100.0,
            atr_value=2.0,
            spread=0.0,
            level_zero=0.0,
            level_follower=1e9,  # very high -> the down-ratchet always accepts
            euro_per_point=0.0,
            euro_stop=0.0,
            config=_Cfg(),
        )
        assert stop is not None
        assert stop > 100.0  # 100 + 2.5*2 = 105

    def test_only_ratchets_down(self):
        # Current follower already tighter (lower) than the proposed stop -> hold.
        assert (
            compute_trailing_stop_short(
                100.0,
                atr_value=2.0,
                spread=0.0,
                level_zero=0.0,
                level_follower=101.0,
                euro_per_point=0.0,
                euro_stop=0.0,
                config=_Cfg(),
            )
            is None
        )

    def test_is_mirror_of_long(self):
        # Same magnitude of distance on both sides of the price.
        long_stop = compute_trailing_stop(
            100.0,
            atr_value=2.0,
            spread=0.0,
            level_zero=0.0,
            level_follower=-1e9,
            euro_per_point=0.0,
            euro_stop=0.0,
            config=_Cfg(),
        )
        short_stop = compute_trailing_stop_short(
            100.0,
            atr_value=2.0,
            spread=0.0,
            level_zero=0.0,
            level_follower=1e9,
            euro_per_point=0.0,
            euro_stop=0.0,
            config=_Cfg(),
        )
        assert (100.0 - long_stop) == (short_stop - 100.0)


def _falling_buffer(levels: list[float], spread: float = 0.0) -> EpicBuffer:
    """Buffer whose bid/offer closes follow ``levels`` (TR=2 each candle)."""
    buf = EpicBuffer(epic="X", max_candles=200)
    for lv in levels:
        buf.add(
            Candle(
                timestamp=datetime(2026, 7, 2, 16, 0, tzinfo=UTC),
                bid_open=lv,
                bid_close=lv,
                bid_high=lv + 1,
                bid_low=lv - 1,
                offer_open=lv + spread,
                offer_close=lv + spread,
                offer_high=lv + 1 + spread,
                offer_low=lv - 1 + spread,
            )
        )
    return buf


class TestRecoveryShortProfile:
    """The short profile: backstop close, momentum-gated down-ratchet, EOD."""

    def _profile(self) -> RecoveryShortProfile:
        return RecoveryShortProfile()

    def test_initial_plan_places_stop_above_entry(self):
        buf = _falling_buffer([100.0] * 30)
        plan = self._profile().initial_plan(
            entry_level=100.0, direction="SELL", buf=buf
        )
        assert plan.stop_level > 100.0  # short stop is above the entry
        assert plan.level_zero == 100.0
        assert plan.level_margin < plan.level_zero  # profit lies below break-even
        assert plan.profile == "recovery_short"

    def test_end_of_day_closes(self):
        buf = _falling_buffer([100.0] * 30)
        pos = Position(direction="SELL", level_follower=Decimal("105"))
        decision = self._profile().evaluate(pos, 100.0, buf, is_close_hour=True)
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "end_of_day"

    def test_backstop_closes_when_offer_reaches_stop(self):
        # Offer (=bid, spread 0) at 105 has reached the short stop at 105.
        buf = _falling_buffer([105.0] * 30)
        pos = Position(direction="SELL", level_follower=Decimal("105"))
        decision = self._profile().evaluate(pos, 105.0, buf, is_close_hour=False)
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "stop"

    def test_ratchets_down_in_deep_profit_with_falling_momentum(self):
        # Price has fallen far below entry (deep short profit) with the last two
        # bids falling -> the stop ratchets down below the current follower.
        buf = _falling_buffer([100.0] * 27 + [84.0, 82.0, 80.0])
        pos = Position(
            direction="SELL",
            level_open=Decimal("100"),
            level_zero=Decimal("100"),
            level_margin=Decimal("99"),
            level_follower=Decimal("105"),
        )
        decision = self._profile().evaluate(pos, 80.0, buf, is_close_hour=False)
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level < 105.0  # only moves down
        assert decision.new_stop_level > 80.0  # stays above the price

    def test_holds_without_falling_momentum(self):
        # Deep profit but the last bids are flat -> momentum gate holds.
        buf = _falling_buffer([80.0] * 30)
        pos = Position(
            direction="SELL",
            level_open=Decimal("100"),
            level_zero=Decimal("100"),
            level_margin=Decimal("99"),
            level_follower=Decimal("105"),
        )
        decision = self._profile().evaluate(pos, 80.0, buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD
