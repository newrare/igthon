"""Tests for the composed close profile ``close_zoneprofit``.

The profile owns nothing about stop placement or per-tick stop moves: it composes
a stop-distance policy (``src/stops/``) at open and delegates each tick to the
per-zone stop updaters (``src/exit/zones/``). These tests cover the composition —
``from_settings`` picking the distance, ``initial_plan`` wiring it and freezing
the break-even/margin references, and ``evaluate`` routing to the right zone —
plus the two hard close triggers it keeps for itself.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.indicators import atr
from src.exit import CloseZoneProfit, get_close_profile
from src.exit.base import ACTION_CLOSE, ACTION_HOLD, ACTION_UPDATE_STOP, CloseProfile
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopAtr, StopSupport

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "stop_strategy": "stop_support",
        "close_zonestart": "hold",
        "close_zonemarge": "hold",
        "close_zoneprofit": "trailing_ratchet",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


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


def _position(**overrides) -> SimpleNamespace:
    base = {
        "level_open": 8000.0,
        "level_win": 0.0,
        "level_loose": 0.0,
        "level_zero": 0.0,
        "level_follower": 0.0,
        "level_margin": 0.0,
        "euro_per_point": 0.0,
        "euro_stop": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestComposition:
    def test_get_close_profile_builds_the_composer(self):
        prof = get_close_profile(_settings())
        assert isinstance(prof, CloseZoneProfit)

    def test_is_close_profile_instance(self):
        assert isinstance(CloseZoneProfit(), CloseProfile)

    def test_from_settings_selects_the_stop_distance(self):
        assert isinstance(
            get_close_profile(_settings()).stop_distance,
            StopSupport,
        )
        assert isinstance(
            get_close_profile(_settings(stop_strategy="stop_atr")).stop_distance,
            StopAtr,
        )

    def test_from_settings_selects_each_zone_updater(self):
        from src.exit.zones import (
            BreakevenBandStop,
            TrailingRatchetStop,
            UnderwaterStop,
        )

        prof = get_close_profile(_settings())
        assert isinstance(prof.underwater, UnderwaterStop)
        assert isinstance(prof.breakeven_band, BreakevenBandStop)
        assert isinstance(prof.trailing, TrailingRatchetStop)

    def test_unknown_zone_updater_raises(self):
        with pytest.raises(ValueError):
            get_close_profile(_settings(close_zoneprofit="nope"))


class TestInitialPlan:
    def test_delegates_stop_to_the_distance_policy(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        entry = buf.last.bid_close
        dist = StopAtr(stop_atr_k=3.0)
        prof = CloseZoneProfit(stop_distance=dist)
        plan = prof.initial_plan(entry_level=entry, direction="BUY", buf=buf)
        expected = dist.initial_stop(entry_level=entry, direction="BUY", buf=buf)
        assert plan.stop_level == pytest.approx(expected)

    def test_freezes_margin_above_break_even(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        plan = CloseZoneProfit().initial_plan(
            entry_level=buf.last.bid_close, direction="BUY", buf=buf
        )
        assert plan.level_margin > plan.level_zero
        assert plan.level_zero == pytest.approx(buf.last.offer_close)
        assert plan.profile == "close_zoneprofit"


class TestEvaluateZones:
    def _prof(self):
        return CloseZoneProfit()

    def test_underwater_holds_without_lowering(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_zero=8030.0, level_follower=8000.0)
        decision = self._prof().evaluate(
            pos, current_bid=8020.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD
        assert decision.new_stop_level is None

    def test_break_even_band_holds(self):
        # Above break-even but inside the frozen noise band → hold.
        buf = _buffer([8000.0 + i for i in range(40)])
        atr_v = atr(list(buf.candles), 14)
        level_zero = 8000.0
        margin = level_zero + 1.5 * atr_v
        pos = _position(
            level_open=level_zero,
            level_zero=level_zero,
            level_margin=margin,
            level_follower=level_zero - 2.5 * atr_v,
        )
        bid = level_zero + (margin - level_zero) * 0.5  # inside the band
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD

    def test_profit_zone_ratchets_up(self):
        buf = _buffer([8000.0 + i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        level_zero = 8000.0
        bid = buf.last.bid_close  # far above entry, rising tail
        pos = _position(
            level_open=level_zero,
            level_zero=level_zero,
            level_follower=level_zero - 2.5 * atr_v,
        )
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > pos.level_follower
        assert decision.new_stop_level < bid


class TestCloseTriggers:
    def test_end_of_day_forces_close(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        decision = CloseZoneProfit.from_settings(_settings()).evaluate(
            _position(), current_bid=8030.0, buf=buf, is_close_hour=True
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "end_of_day"

    def test_stop_backstop_closes(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_follower=8010.0)
        decision = CloseZoneProfit.from_settings(_settings()).evaluate(
            pos, current_bid=8005.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "stop"

    def test_stop_backstop_fires_during_atr_warmup(self):
        # Regression (#9): fewer than atr_period candles -> atr()==0. The backstop
        # must still close a position whose bid has crossed the follower; the old
        # ordering ran the atr<=0 HOLD guard first and disabled it for ~15 min
        # after a restart.
        buf = _buffer([8000.0, 8000.0, 8000.0])  # 3 candles << atr_period(14)
        assert atr(list(buf.candles), 14) == 0.0
        pos = _position(level_open=8030.0, level_follower=8010.0)
        decision = CloseZoneProfit.from_settings(_settings()).evaluate(
            pos, current_bid=8005.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "stop"
