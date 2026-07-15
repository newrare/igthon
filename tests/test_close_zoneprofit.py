"""Tests for the composed close profile ``close_zoneprofit``.

The profile owns nothing about stop placement or per-tick stop moves: it composes
a stop-distance policy (``src/stops/``) at open and delegates each tick to the
per-zone stop updaters (``src/exit/zones/``). These tests cover the composition —
``from_settings`` picking the distance, ``initial_plan`` wiring it and freezing
the break-even/margin references, and ``evaluate`` routing to the right zone —
plus the two hard close triggers it keeps for itself.
"""

from datetime import UTC, date, datetime, time, timedelta
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

    def test_bid_above_margin_below_profit_trigger_locks_the_marge_support(self):
        # Regression (CS.D.CHFJPY.CFD.IP): the bid held just above the MARGIN line
        # for many ticks then collapsed, yet the stop was never raised — because
        # anything above the (thin) margin was classified PROFIT and the margin
        # updater (breakeven_half) never ran. Now the break-even band extends up to
        # the PROFIT TRIGGER (margin + one more noise margin), so a bid above the
        # margin but below that trigger still routes to CLOSE_ZONEMARGE and locks
        # the 25 % support inside the break-even→margin band.
        #
        # level_zero=8000, level_margin=8010 → profit trigger = 8020, and the
        # breakeven_half support = 8000 + 0.25×(8010−8000) = 8002.5. The rising
        # tail clears the 8010 margin so the one-shot lock arms.
        prof = get_close_profile(_settings(close_zonemarge="breakeven_half"))
        buf = _buffer([8000.0 + i * 0.5 for i in range(30)])  # ends ≈ 8014.5
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        decision = prof.evaluate(pos, current_bid=8015.0, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == pytest.approx(8002.5)

    def test_pre_entry_spike_above_margin_does_not_arm_the_lock(self):
        # Regression (IX.D.HSTECH.FWM2.IP): the margin-zone lock (breakeven_half)
        # armed on a pre-entry rally, not a post-open move. The live EpicBuffer is a
        # rolling window fed continuously, so it still held candles from BEFORE the
        # position opened; ``_rising_above_margin`` scans the whole buffer and found
        # a rising streak clearing the (open-frozen) margin in that pre-entry
        # history, raising the stop even though nothing after the open ever
        # approached the margin. ``evaluate`` now bounds the buffer to the open.
        #
        # Candles 0-9 (09:00-09:09, before the open) spike above margin=8010;
        # candles 10-29 (from 09:10 on) hover ~8004-8005, never near the margin. The
        # position opened at 09:10, so only the flat post-open window must count.
        prof = get_close_profile(_settings(close_zonemarge="breakeven_half"))
        pre_entry = [
            8011.0,
            8012.0,
            8013.0,
            8014.0,
            8015.0,
            8014.0,
            8013.0,
            8012.0,
            8011.0,
            8010.0,
        ]
        post_entry = [8004.0, 8005.0] * 10  # 20 candles, all well below margin
        buf = _buffer(pre_entry + post_entry)
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            date=date(2024, 1, 1),
            time_open=time(9, 10),  # cuts the buffer at candle 10
        )
        decision = prof.evaluate(pos, current_bid=8005.0, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD

    def test_pre_entry_spike_arms_the_lock_without_an_open_time(self):
        # Companion to the regression above: the SAME buffer, but a position with no
        # persisted open instant, so the buffer is not sliced. The pre-entry spike is
        # then in scope and arms the lock (raise to the 25 % support = 8002.5). This
        # documents exactly what the open-time slice suppresses.
        prof = get_close_profile(_settings(close_zonemarge="breakeven_half"))
        pre_entry = [
            8011.0,
            8012.0,
            8013.0,
            8014.0,
            8015.0,
            8014.0,
            8013.0,
            8012.0,
            8011.0,
            8010.0,
        ]
        post_entry = [8004.0, 8005.0] * 10
        buf = _buffer(pre_entry + post_entry)
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )  # no date / time_open -> no slice
        decision = prof.evaluate(pos, current_bid=8005.0, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == pytest.approx(8002.5)

    def test_bid_above_profit_trigger_enters_the_profit_zone(self):
        # Same open levels; a bid above the profit trigger (8020) is real profit →
        # the profit-trailing zone governs and ratchets the stop up above the
        # margin, not the flat 25 % margin-zone support.
        prof = get_close_profile(_settings(close_zonemarge="breakeven_half"))
        buf = _buffer([8000.0 + i for i in range(60)])  # strong rising trend
        atr_v = atr(list(buf.candles), 14)
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        bid = buf.last.bid_close  # far above the 8020 trigger
        decision = prof.evaluate(pos, current_bid=bid, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > 8010.0  # trailed above the margin
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
