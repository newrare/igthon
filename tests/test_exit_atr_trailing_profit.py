"""Tests for the profit-gated ATR-trailing close profile.

``atr_trailing_profit`` holds the initial stop untouched (never lowered) until
the price is in profit beyond the noise margin, then ratchets up like the
reference ``atr_trailing``. These tests cover the gate (below → hold, above →
ratchet), the never-lower invariant, and the close triggers.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.core.indicators import atr
from src.exit import AtrTrailingProfitExit, get_close_profile
from src.exit.base import ACTION_CLOSE, ACTION_HOLD, ACTION_UPDATE_STOP, CloseProfile
from src.feed.price_buffer import Candle, EpicBuffer

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "strategy_atr_period": 14,
        "strategy_donchian_stop_atr_k": 2.5,
        "strategy_atr_k_pre": 2.5,
        "strategy_atr_k_post": 2.5,
        "strategy_trailing_step_ratio": 0.3,
        "strategy_profit_noise_k": 0.5,
        "strategy_profit_atr_k": 2.5,
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
        "euro_per_point": 0.0,
        "euro_stop": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRegistry:
    def test_known_name_resolves(self):
        prof = get_close_profile("atr_trailing_profit", _settings())
        assert isinstance(prof, AtrTrailingProfitExit)

    def test_is_close_profile_instance(self):
        assert isinstance(AtrTrailingProfitExit(), CloseProfile)

    def test_parameters_are_class_constants(self):
        # from_settings ignores settings — parameters are class constants. The
        # raised noise_k below is NOT picked up; the constructor still tunes it.
        prof = get_close_profile(
            "atr_trailing_profit", _settings(strategy_profit_noise_k=1.2)
        )
        assert prof.noise_k == 0.5  # class constant, not the ignored setting
        assert prof.stop_atr_k == 2.5  # inherited atr_trailing knob
        assert AtrTrailingProfitExit(noise_k=1.2).noise_k == 1.2

    def test_dedicated_trailing_width_feeds_both_multipliers(self):
        # The profile owns its trailing width as its own atr_k_pre/atr_k_post
        # constants, kept equal so the width applies before and after break-even.
        assert AtrTrailingProfitExit().atr_k_pre == AtrTrailingProfitExit().atr_k_post
        prof = AtrTrailingProfitExit(atr_k_pre=3.2, atr_k_post=3.2)
        assert prof.atr_k_pre == 3.2
        assert prof.atr_k_post == 3.2

    def test_wider_trailing_width_pushes_stop_further_from_bid(self):
        # A larger trailing width must place the ratcheted stop further below the
        # bid (a wider gap against chop).
        buf = _buffer([8000.0 + i for i in range(60)])
        bid = buf.last.bid_close
        pos = _position(level_open=8000.0, level_zero=8000.0, level_follower=8000.0)
        narrow = AtrTrailingProfitExit(atr_k_pre=2.5, atr_k_post=2.5).evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        wide = AtrTrailingProfitExit(atr_k_pre=3.5, atr_k_post=3.5).evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert narrow.action == ACTION_UPDATE_STOP
        assert wide.action == ACTION_UPDATE_STOP
        # Wider k → stop sits lower (further from the bid).
        assert wide.new_stop_level < narrow.new_stop_level


class TestGate:
    def _prof(self, **kw):
        return AtrTrailingProfitExit(**kw)

    def test_below_gate_holds_initial_stop(self):
        # In profit, but only by a noise-sized amount → hold, never touch the stop.
        buf = _buffer([8000.0 + i for i in range(40)])
        atr_v = atr(list(buf.candles), 14)
        pos = _position(level_open=8030.0, level_follower=8010.0)
        # bid just a fraction of the noise margin above entry.
        bid = 8030.0 + 0.5 * max(0.5 * atr_v, 0.5 * 2.0)
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD

    def test_underwater_holds_without_lowering(self):
        # Below entry but above the follower → hold, no stop change (never lower).
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_follower=8000.0)
        decision = self._prof().evaluate(
            pos, current_bid=8020.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD
        assert decision.new_stop_level is None

    def test_above_gate_ratchets_up(self):
        # Clear profit beyond the noise margin → standard chandelier ratchet up.
        buf = _buffer([8000.0 + i for i in range(60)])
        bid = buf.last.bid_close  # far above entry
        pos = _position(level_open=8000.0, level_follower=8000.0)
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > pos.level_follower
        assert decision.new_stop_level < bid

    def test_higher_noise_k_widens_the_gate(self):
        # A position that trails with the default noise_k must hold when noise_k
        # is raised enough that the same profit no longer clears the gate. The
        # follower starts at the initial stop (well below entry), as set at open.
        buf = _buffer([8000.0 + i * 3.0 for i in range(40)])
        atr_v = atr(list(buf.candles), 14)
        pos = _position(level_open=8000.0, level_follower=8000.0 - 2.5 * atr_v)
        bid = 8000.0 + 0.8 * atr_v  # 0.8 ATR of profit
        assert (
            self._prof(noise_k=0.5)
            .evaluate(pos, current_bid=bid, buf=buf, is_close_hour=False)
            .action
            == ACTION_UPDATE_STOP
        )
        assert (
            self._prof(noise_k=1.5)
            .evaluate(pos, current_bid=bid, buf=buf, is_close_hour=False)
            .action
            == ACTION_HOLD
        )


class TestMomentumConfirmation:
    def _prof(self, **kw):
        return AtrTrailingProfitExit(**kw)

    def test_single_spike_does_not_ratchet(self):
        # Long climb (so ATR/profit gate is cleared) ending on a single up spike
        # preceded by a down-step → only one rising step → hold, stop untouched.
        closes = [8000.0 + i for i in range(40)]
        closes[-2] = closes[-3] - 5.0  # dip just before the latest bid
        buf = _buffer(closes)
        bid = buf.last.bid_close
        pos = _position(level_open=8000.0, level_follower=8000.0)
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD
        assert decision.new_stop_level is None

    def test_two_rising_bids_ratchets(self):
        # Steadily rising tail → two up-steps → ratchet proceeds as usual.
        buf = _buffer([8000.0 + i for i in range(60)])
        bid = buf.last.bid_close
        pos = _position(level_open=8000.0, level_follower=8000.0)
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > pos.level_follower


class TestDeadBand:
    def _prof(self, **kw):
        return AtrTrailingProfitExit(**kw)

    def test_stop_never_lands_in_zero_margin_band(self):
        # Price is in profit beyond noise and rising, but the chandelier stop
        # (bid - k*ATR) would still fall between level_zero and the margin level.
        # The profile must hold the initial stop rather than park it in the band.
        buf = _buffer([8000.0 + i for i in range(40)])
        atr_v = atr(list(buf.candles), 14)
        spread = buf.last.spread
        noise_margin = max(0.5 * atr_v, spread * 2.0)
        level_zero = 8000.0
        # Pick a bid so the trailed stop (bid - 2.5*ATR) sits just inside the band.
        bid = level_zero + 0.5 * noise_margin + 2.5 * atr_v
        pos = _position(
            level_open=level_zero,
            level_zero=level_zero,
            level_follower=level_zero - 2.5 * atr_v,
        )
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD

    def test_frozen_margin_is_respected_over_per_tick(self):
        # A position carrying a margin level frozen at open must hold the stop
        # below that frozen level even if the (smaller) per-tick noise margin
        # would allow a lower stop — the band the stop must clear never drifts.
        buf = _buffer([8000.0 + i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        level_zero = 8000.0
        bid = buf.last.bid_close
        frozen_stop = bid - 2.5 * atr_v  # the chandelier stop this tick
        pos = _position(
            level_open=level_zero,
            level_zero=level_zero,
            level_follower=level_zero - 2.5 * atr_v,
            # Freeze the margin just above where the stop would land → hold.
            level_margin=frozen_stop + 1.0,
        )
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD

    def test_initial_plan_freezes_margin_above_break_even(self):
        # initial_plan must persist a margin level above break-even on the plan.
        buf = _buffer([8000.0 + i for i in range(40)])
        plan = self._prof().initial_plan(
            entry_level=buf.last.bid_close, direction="BUY", buf=buf
        )
        assert plan.level_margin > plan.level_zero

    def test_stop_ratchets_once_it_clears_the_margin(self):
        # Far enough in profit that bid - k*ATR clears the margin level → ratchet,
        # and the resulting stop is strictly above the margin level.
        buf = _buffer([8000.0 + i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        spread = buf.last.spread
        noise_margin = max(0.5 * atr_v, spread * 2.0)
        level_zero = 8000.0
        bid = buf.last.bid_close  # far above entry
        pos = _position(
            level_open=level_zero,
            level_zero=level_zero,
            level_follower=level_zero - 2.5 * atr_v,
        )
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > level_zero + noise_margin


class TestCloseTriggers:
    def test_end_of_day_forces_close(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        decision = AtrTrailingProfitExit.from_settings(_settings()).evaluate(
            _position(), current_bid=8030.0, buf=buf, is_close_hour=True
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "end_of_day"

    def test_stop_backstop_closes(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_follower=8010.0)
        decision = AtrTrailingProfitExit.from_settings(_settings()).evaluate(
            pos, current_bid=8005.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "stop"
