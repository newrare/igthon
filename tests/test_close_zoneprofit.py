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

from src.core.indicators import adverse_tick_noise, atr
from src.exit import CloseZoneProfit, get_close_profile
from src.exit.base import ACTION_CLOSE, ACTION_HOLD, ACTION_UPDATE_STOP, CloseProfile
from src.exit.zones import SmartGroupParams, SmartGroupStop, StopUpdater
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops import StopAtr, StopSupport

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "stop_strategy": "stop_support",
        "close_zonestart": "hold",
        "close_zonemarge": "hold",
        "close_zonesecure": "hold",
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


class _FixedStop(StopUpdater):
    """A zone updater that always proposes the same level (deterministic seam)."""

    name = "fixed"

    def __init__(self, level: float) -> None:
        self._level = level

    def propose(self, ctx):
        return self._level


class _BufferProbe(StopUpdater):
    """Records the buffer the composer hands the zone updater; never moves a stop."""

    name = "probe"

    def __init__(self) -> None:
        self.seen: list = []

    def propose(self, ctx):
        self.seen = list(ctx.buf.candles)
        return None


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


class TestCurrentZone:
    """``current_zone`` classifies the live bid using the open-frozen references.

    Break-even=8000, margin=8010 → profit trigger = 2×8010 − 8000 = 8020, so the
    four zones are: <8000 underwater, 8000→8010 break-even band, 8010→8020 secure,
    >8020 profit. Powers the dashboard manual stop-raise "hold" (which zone to pin
    to).
    """

    def _profile(self):
        return get_close_profile(_settings())

    def test_below_break_even_is_underwater(self):
        from src.exit.zones import StopZone

        pos = _position(level_zero=8000.0, level_margin=8010.0)
        zone = self._profile().current_zone(pos, 7999.0, _buffer([8000.0] * 20))
        assert zone is StopZone.UNDERWATER

    def test_between_break_even_and_margin_is_breakeven_band(self):
        from src.exit.zones import StopZone

        pos = _position(level_zero=8000.0, level_margin=8010.0)
        zone = self._profile().current_zone(pos, 8005.0, _buffer([8000.0] * 20))
        assert zone is StopZone.BREAKEVEN_BAND

    def test_between_margin_and_profit_trigger_is_secure(self):
        # The zone that used to be swallowed by the break-even band: past the
        # margin line, short of the profit trigger.
        from src.exit.zones import StopZone

        pos = _position(level_zero=8000.0, level_margin=8010.0)
        zone = self._profile().current_zone(pos, 8015.0, _buffer([8000.0] * 20))
        assert zone is StopZone.SECURE

    def test_on_the_margin_line_is_still_the_breakeven_band(self):
        from src.exit.zones import StopZone

        pos = _position(level_zero=8000.0, level_margin=8010.0)
        zone = self._profile().current_zone(pos, 8010.0, _buffer([8000.0] * 20))
        assert zone is StopZone.BREAKEVEN_BAND

    def test_above_profit_trigger_is_profit(self):
        from src.exit.zones import StopZone

        pos = _position(level_zero=8000.0, level_margin=8010.0)
        zone = self._profile().current_zone(pos, 8025.0, _buffer([8000.0] * 20))
        assert zone is StopZone.PROFIT

    def test_no_candle_returns_none(self):
        pos = _position(level_zero=8000.0, level_margin=8010.0)
        empty = EpicBuffer(epic="TEST.EPIC", max_candles=10)
        assert self._profile().current_zone(pos, 8025.0, empty) is None


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
            SecureHoldStop,
            TrailingRatchetStop,
            UnderwaterStop,
        )

        prof = get_close_profile(_settings())
        assert isinstance(prof.underwater, UnderwaterStop)
        assert isinstance(prof.breakeven_band, BreakevenBandStop)
        assert isinstance(prof.secure, SecureHoldStop)
        assert isinstance(prof.trailing, TrailingRatchetStop)

    def test_from_settings_selects_the_requested_secure_updater(self):
        from src.exit.zones import BreakevenHalfStop

        prof = get_close_profile(_settings(close_zonesecure="breakeven_half"))
        assert isinstance(prof.secure, BreakevenHalfStop)

    def test_unknown_zone_updater_raises(self):
        with pytest.raises(ValueError):
            get_close_profile(_settings(close_zoneprofit="nope"))

    def test_unknown_secure_updater_raises(self):
        with pytest.raises(ValueError):
            get_close_profile(_settings(close_zonesecure="nope"))


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

    def test_bid_between_margin_and_trigger_secures_the_midpoint(self):
        # Regression (CS.D.CHFJPY.CFD.IP): the bid held just above the MARGIN line
        # for many ticks then collapsed, yet the stop was never raised — anything
        # above the (thin) margin was classified PROFIT while the profit trailing
        # was still suppressed. That region is now its own zone, CLOSE_ZONESECURE,
        # and ``breakeven_half`` secures it at once.
        #
        # level_zero=8000, level_margin=8010 → profit trigger = 8020, so a bid at
        # 8015 is in the secure zone and the stop goes to the midpoint of the
        # break-even→margin band: 8000 + 0.5 × (8010 − 8000) = 8005.
        prof = get_close_profile(_settings(close_zonesecure="breakeven_half"))
        buf = _buffer([8000.0 + i * 0.5 for i in range(30)])  # ends ≈ 8014.5
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        decision = prof.evaluate(pos, current_bid=8015.0, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == pytest.approx(8005.0)

    def test_secure_zone_never_gives_back_a_tighter_follower(self):
        prof = get_close_profile(_settings(close_zonesecure="breakeven_half"))
        buf = _buffer([8000.0 + i * 0.5 for i in range(30)])
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8008.0,  # already past the 8005 midpoint
        )
        decision = prof.evaluate(pos, current_bid=8015.0, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD

    def test_bid_inside_the_band_moves_the_stop_under_the_market(self):
        # ``limitloose`` in the margin zone: the instant price clears break-even the
        # stop comes up to a double noise band under the live price, with no
        # confirmation streak. Here the bid sits inside the break-even→margin band
        # (8000 → 8010), so the margin-zone updater governs.
        prof = get_close_profile(_settings(close_zonemarge="limitloose"))
        # A rising tape that pulls back 1.0 every other candle, so the epic has a
        # measurable adverse-noise band and the cushion is twice that band.
        buf = _buffer([8000.0 + i * 0.5 - (1.0 if i % 2 else 0.0) for i in range(30)])
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        decision = prof.evaluate(pos, current_bid=8005.0, buf=buf, is_close_hour=False)
        noise = adverse_tick_noise(buf.bid_closes, 20, 2.0)
        assert noise > 0
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == pytest.approx(8005.0 - 2 * noise)

    def test_flat_tape_holds_instead_of_parking_the_stop_on_the_price(self):
        # No adverse noise and no broker minimum → no cushion. Parking the stop on
        # the live price would have the software backstop close at once, so hold.
        prof = get_close_profile(_settings(close_zonemarge="limitloose"))
        buf = _buffer([8000.0 + i * 0.2 for i in range(30)])  # monotone, no pull-back
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        decision = prof.evaluate(pos, current_bid=8005.0, buf=buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD

    def test_buffer_is_sliced_at_the_open(self):
        # Regression (IX.D.HSTECH.FWM2.IP): the margin-zone lock armed on a
        # PRE-ENTRY rally. The live EpicBuffer is a rolling window fed continuously,
        # so it still holds candles recorded before the position opened, and every
        # updater that reads price levels against the open-frozen references would
        # judge them on history the position never traded. ``evaluate`` bounds the
        # buffer to the open instant before handing it to the zone updater.
        probe = _BufferProbe()
        prof = CloseZoneProfit(breakeven_band=probe)
        pre_entry = [8011.0 + i for i in range(10)]  # 09:00 → 09:09, before the open
        post_entry = [8004.0, 8005.0] * 10  # 09:10 onward, 20 candles
        buf = _buffer(pre_entry + post_entry)
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            date=date(2024, 1, 1),
            time_open=time(9, 10),  # cuts the buffer at candle 10
        )
        prof.evaluate(pos, current_bid=8005.0, buf=buf, is_close_hour=False)
        assert len(probe.seen) == len(post_entry)
        opened_at = datetime(2024, 1, 1, 9, 10, tzinfo=UTC)
        assert all(c.timestamp >= opened_at for c in probe.seen)

    def test_buffer_is_not_sliced_without_an_open_time(self):
        # Companion to the regression above: a position with no persisted open
        # instant cannot be sliced, so the whole rolling window stays in scope.
        probe = _BufferProbe()
        prof = CloseZoneProfit(breakeven_band=probe)
        buf = _buffer([8004.0, 8005.0] * 15)
        pos = _position(
            level_open=8000.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )  # no date / time_open -> no slice
        prof.evaluate(pos, current_bid=8005.0, buf=buf, is_close_hour=False)
        assert len(probe.seen) == 30

    def test_bid_above_profit_trigger_enters_the_profit_zone(self):
        # Same open levels; a bid above the profit trigger (8020) is real profit →
        # the profit-trailing zone governs and ratchets the stop up above the
        # margin, not the flat secure-zone midpoint.
        prof = get_close_profile(_settings(close_zonesecure="breakeven_half"))
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


class TestGroupPrePass:
    """Portfolio pre-pass wiring for the group-aware ``smartgroup`` zone-1 updater.

    The pure group maths is covered in ``tests/test_smartgroup.py``; here we check
    the composer's seam: awareness flag, member extraction, plan delegation, and
    that a pre-resolved ``group_tighten`` becomes an ``UPDATE_STOP`` for an
    underwater position (and ``None`` holds).
    """

    def _smart(self):
        return get_close_profile(_settings(close_zonestart="smartgroup"))

    def test_hold_profile_is_not_group_aware(self):
        assert get_close_profile(_settings()).is_group_aware is False

    def test_smartgroup_profile_is_group_aware(self):
        assert self._smart().is_group_aware is True

    def test_group_member_none_when_not_group_aware(self):
        buf = _buffer([8000.0] * 20)
        pos = _position(id=1, level_follower=7990.0)
        assert get_close_profile(_settings()).group_member(pos, 7995.0, buf) is None

    def test_group_member_signs_a_sell(self):
        # Shorts join the same pot: the member is built with sign −1 and the
        # close-out OFFER (bid + spread), not the bid.
        buf = _buffer([8000.0] * 20, spread=0.5)
        pos = _position(id=1, direction="SELL", level_follower=8010.0)
        m = self._smart().group_member(pos, 7995.0, buf)
        assert m is not None
        assert m.sign == -1.0
        assert m.current_price == 7995.5

    def test_group_member_none_without_candle(self):
        empty = EpicBuffer(epic="TEST.EPIC", max_candles=10)
        pos = _position(id=1, level_follower=7990.0)
        assert self._smart().group_member(pos, 7995.0, empty) is None

    def test_group_member_populated_for_buy(self):
        buf = _buffer([8000.0] * 20)
        pos = _position(
            id=7,
            direction="BUY",
            level_open=8000.0,
            level_zero=8000.5,
            level_follower=7990.0,
            euro_per_point=10.0,
            min_stop_distance=0.3,
        )
        m = self._smart().group_member(pos, 7999.0, buf)
        assert m is not None
        assert m.position_id == 7
        assert m.euro_per_point == 10.0
        assert m.min_stop_distance == 0.3
        assert m.sign == 1.0
        assert m.current_price == 7999.0

    def test_group_member_carries_the_execution_haircut(self):
        # The exit never fills on the stop, so the member ships the distance the
        # book must value it short by: k × (spread + cushion), with the cushion
        # floored at IG's minimum distance exactly like the candidate's.
        buf = _buffer([8000.0] * 20, spread=0.5)
        pos = _position(
            id=7,
            level_open=8000.0,
            level_follower=7990.0,
            euro_per_point=10.0,
            min_stop_distance=2.0,
        )
        m = self._smart().group_member(pos, 7999.0, buf)
        assert m is not None
        expected = SmartGroupParams().exec_slip_k * (0.5 + max(m.noise, 2.0))
        assert m.exec_slip == pytest.approx(expected)
        assert m.exec_slip > 0

    def test_plan_group_empty_when_not_group_aware(self):
        assert get_close_profile(_settings()).plan_group([]) == {}

    def test_plan_group_tightens_the_whole_book(self):
        buf = _buffer([8000.0] * 20)
        prof = self._smart()
        winner = _position(
            id=1,
            level_open=100.0,
            level_zero=100.5,
            level_follower=110.0,
            euro_per_point=10.0,
            min_stop_distance=0.2,
        )
        loser = _position(
            id=2,
            level_open=50.0,
            level_zero=50.5,
            level_follower=30.0,
            euro_per_point=10.0,
            min_stop_distance=0.2,
        )
        members = [
            prof.group_member(winner, 111.0, buf),
            prof.group_member(loser, 49.0, buf),
        ]
        plan = prof.plan_group(members)
        # The winner (+110 € at its candidate) carries the loser (−12 €), so the
        # book is green and BOTH stops are raised onto their noise band.
        assert set(plan) == {1, 2}
        assert 110.0 < plan[1] < 111.0
        assert 30.0 < plan[2] < 49.0

    def test_evaluate_applies_group_tighten_underwater(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_zero=8030.0, level_follower=8000.0)
        decision = self._smart().evaluate(
            pos, current_bid=8020.0, buf=buf, is_close_hour=False, group_tighten=8015.0
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == 8015.0

    def test_evaluate_holds_without_group_tighten(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_zero=8030.0, level_follower=8000.0)
        decision = self._smart().evaluate(
            pos, current_bid=8020.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD

    def test_evaluate_applies_group_tighten_in_the_breakeven_band(self):
        # The group decision is book-wide: a position that has cleared break-even
        # is tightened too, even though ``smartgroup`` is selected in zone 1.
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8000.0, level_zero=8000.0, level_margin=8010.0)
        pos.level_follower = 8002.0
        decision = self._smart().evaluate(
            pos, current_bid=8005.0, buf=buf, is_close_hour=False, group_tighten=8004.0
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == 8004.0

    def test_evaluate_applies_group_tighten_in_the_secure_zone(self):
        # Same rule between the margin line (8010) and the profit trigger (8020).
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8000.0, level_zero=8000.0, level_margin=8010.0)
        pos.level_follower = 8002.0
        decision = self._smart().evaluate(
            pos, current_bid=8015.0, buf=buf, is_close_hour=False, group_tighten=8012.0
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == 8012.0

    def test_evaluate_applies_group_tighten_in_the_profit_zone(self):
        # Profit trigger = 2×8010 − 8000 = 8020, so a bid at 8030 is in zone 3 and
        # still takes the (tighter) group level, above the margin line.
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8000.0, level_zero=8000.0, level_margin=8010.0)
        pos.level_follower = 8005.0
        decision = self._smart().evaluate(
            pos, current_bid=8030.0, buf=buf, is_close_hour=False, group_tighten=8029.0
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == 8029.0

    def test_zone_proposal_wins_when_tighter_than_the_group_level(self):
        # The group level never loosens what a zone updater already secured.
        prof = CloseZoneProfit(underwater=SmartGroupStop(), trailing=_FixedStop(8028.0))
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8000.0, level_zero=8000.0, level_margin=8010.0)
        pos.level_follower = 8005.0
        decision = prof.evaluate(
            pos, current_bid=8030.0, buf=buf, is_close_hour=False, group_tighten=8025.0
        )
        assert decision.new_stop_level == 8028.0

    def test_group_level_is_ignored_by_a_non_group_profile(self):
        # ``hold`` in zone 1: the profile is not group-aware, so a stray group
        # level must not leak into the decision.
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_zero=8030.0, level_follower=8000.0)
        decision = get_close_profile(_settings()).evaluate(
            pos, current_bid=8020.0, buf=buf, is_close_hour=False, group_tighten=8015.0
        )
        assert decision.action == ACTION_HOLD
