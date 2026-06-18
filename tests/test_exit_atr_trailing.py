"""Tests for the decoupled ATR-trailing *close* profile (src/exit/atr_trailing.py).

The whole point of the decoupling: a close profile can be tested on hand-built
price paths and a fake position, with **no entry strategy involved**. These
tests cover the initial stop plan and each per-tick decision (hold, close on
target/stop/end-of-day, ratchet the trailing stop).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.exit import AtrTrailingExit, get_close_profile
from src.exit.base import (
    ACTION_CLOSE,
    ACTION_HOLD,
    ACTION_UPDATE_STOP,
    CloseProfile,
)
from src.services.compute import atr
from src.services.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "strategy_atr_period": 14,
        "strategy_donchian_stop_atr_k": 2.5,
        "strategy_atr_k_pre": 2.5,
        "strategy_atr_k_post": 2.5,
        "strategy_trailing_step_ratio": 0.3,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        buf.add(
            Candle(
                timestamp=start + timedelta(minutes=i),
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


def _up(n: int = 30, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _position(**overrides) -> SimpleNamespace:
    """Fake open position carrying only the fields the close profile reads."""
    base = {
        "level_open": 8000.0,
        "level_win": 0.0,
        "level_loose": 0.0,
        "level_zero": 0.0,
        "level_follower": 0.0,
        "euro_per_point": 0.0,
        "euro_stop": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRegistry:
    def test_known_name_resolves(self):
        assert isinstance(
            get_close_profile("atr_trailing", _settings()), AtrTrailingExit
        )

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="Unknown close profile"):
            get_close_profile("nope", _settings())

    def test_from_settings_maps_parameters(self):
        prof = get_close_profile(
            "atr_trailing", _settings(strategy_donchian_stop_atr_k=3.0)
        )
        assert prof.stop_atr_k == 3.0

    def test_is_close_profile_instance(self):
        assert isinstance(AtrTrailingExit(), CloseProfile)


class TestInitialPlan:
    def test_buy_stop_is_k_atr_below_entry(self):
        buf = _buffer(_up(40))
        entry = buf.last.bid_close
        prof = AtrTrailingExit(stop_atr_k=2.5)
        plan = prof.initial_plan(entry_level=entry, direction="BUY", buf=buf)
        expected_distance = 2.5 * atr(list(buf.candles), prof.atr_period)
        assert plan.stop_level == pytest.approx(entry - expected_distance)
        assert plan.target_level == 0.0  # no fixed take-profit by default
        assert plan.profile == "atr_trailing"
        # break-even reference is the entry offer for a BUY
        assert plan.level_zero == pytest.approx(buf.last.offer_close)

    def test_stop_sits_below_entry(self):
        buf = _buffer(_up(40))
        entry = buf.last.bid_close
        plan = AtrTrailingExit().initial_plan(
            entry_level=entry, direction="BUY", buf=buf
        )
        assert plan.stop_level < entry


class TestEvaluate:
    def test_end_of_day_forces_close(self):
        buf = _buffer(_up(40))
        decision = AtrTrailingExit().evaluate(
            _position(), current_bid=8030.0, buf=buf, is_close_hour=True
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "end_of_day"

    def test_target_hit_closes_win(self):
        buf = _buffer(_up(40))
        pos = _position(level_win=8020.0)
        decision = AtrTrailingExit().evaluate(
            pos, current_bid=8025.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "win"

    def test_stop_hit_closes_loose(self):
        buf = _buffer(_up(40))
        pos = _position(level_loose=7990.0)
        decision = AtrTrailingExit().evaluate(
            pos, current_bid=7985.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "loose"

    def test_in_profit_ratchets_the_trailing_stop(self):
        buf = _buffer(_up(40))
        # In profit (bid well above entry), follower far below → stop must advance.
        pos = _position(level_open=8000.0, level_follower=0.0)
        current_bid = buf.last.bid_close
        decision = AtrTrailingExit().evaluate(
            pos, current_bid=current_bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level is not None
        assert decision.new_stop_level < current_bid

    def test_below_entry_holds_without_touching_stop(self):
        buf = _buffer(_up(40))
        # Below entry, no close trigger → hold, no trailing update.
        pos = _position(level_open=8000.0)
        decision = AtrTrailingExit().evaluate(
            pos, current_bid=7995.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD
        assert decision.new_stop_level is None
