"""Tests for the trend-aware ATR-trailing close profile.

``atr_trailing_positive`` keeps the reference initial stop and the free upward
ratchet *once the trade is secured*, but while the position is underwater it
steers the stop by the trend since the position opened. These tests cover each
slope regime (flat / bullish / bearish soft / bearish steep) on hand-built
price paths with a fake position — no entry strategy involved.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.exit import AtrTrailingPositiveExit, get_close_profile
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
        "strategy_positive_trend_period": 30,
        "strategy_positive_trend_min_period": 5,
        "strategy_positive_flat_slope_k": 0.015,
        "strategy_positive_steep_slope_k": 0.05,
        "strategy_positive_down_step_ratio": 0.3,
        "strategy_positive_max_extra_k": 1.0,
        "strategy_positive_noise_k": 0.5,
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


def _osc(
    n: int, *, drift: float, amp: float = 5.0, base: float = 8000.0
) -> list[float]:
    """Closes with a controlled net drift plus an oscillation.

    The oscillation gives the path a sizeable ATR (~2 x amp) while the linear
    regression slope tracks ``drift``, so ``slope / ATR`` ≈ ``drift / (2 amp)``
    lands predictably in a chosen regime band — the way real ATR scales behave.
    """
    return [base + drift * i + amp * (1 if i % 2 else -1) for i in range(n)]


def _position(**overrides) -> SimpleNamespace:
    base = {
        "level_open": 8000.0,
        "level_win": 0.0,
        "level_loose": 0.0,
        "level_zero": 0.0,
        "level_follower": 0.0,
        "euro_per_point": 0.0,
        "euro_stop": 0.0,
        # Opened at the very start so the whole buffer counts as "since open".
        "opened_at": _START,
        "stop_updates": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRegistry:
    def test_known_name_resolves(self):
        prof = get_close_profile("atr_trailing_positive", _settings())
        assert isinstance(prof, AtrTrailingPositiveExit)

    def test_is_close_profile_instance(self):
        assert isinstance(AtrTrailingPositiveExit(), CloseProfile)

    def test_parameters_are_class_constants(self):
        # from_settings ignores settings — parameters are class constants. The
        # override below is NOT picked up; the constructor still tunes them.
        prof = get_close_profile(
            "atr_trailing_positive", _settings(strategy_positive_max_extra_k=2.0)
        )
        assert prof.max_extra_k == 1.0  # class constant, not the ignored setting
        assert prof.stop_atr_k == 2.5  # inherited atr_trailing knob
        assert AtrTrailingPositiveExit(max_extra_k=2.0).max_extra_k == 2.0


class TestSecuredRegime:
    def test_deep_in_profit_ratchets_up(self):
        # A long clean up-trend: the prospective stop is well above entry, so the
        # profile behaves like atr_trailing and ratchets the stop up.
        buf = _buffer([8000.0 + i for i in range(60)])
        pos = _position(level_open=8000.0, level_follower=8000.0)
        bid = buf.last.bid_close
        decision = AtrTrailingPositiveExit.from_settings(_settings()).evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > pos.level_follower
        assert decision.new_stop_level < bid

    def test_end_of_day_forces_close(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        decision = AtrTrailingPositiveExit.from_settings(_settings()).evaluate(
            _position(), current_bid=8030.0, buf=buf, is_close_hour=True
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "end_of_day"


class TestUnderwaterRegime:
    def _prof(self, **kw):
        return AtrTrailingPositiveExit.from_settings(_settings(**kw))

    def test_flat_trend_keeps_initial_stop(self):
        # Underwater + flat (drift 0): hold, never touch the stop.
        buf = _buffer(_osc(40, drift=0.0))
        pos = _position(level_open=8030.0, level_follower=7970.0, level_loose=7970.0)
        decision = self._prof().evaluate(
            pos, current_bid=8000.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD

    def test_underwater_bullish_ratchets_up(self):
        # Underwater but recovering (slope/ATR above flat band): trail up.
        buf = _buffer(_osc(40, drift=0.5))  # +0.5 / ~10 ATR ≈ +0.05
        initial = 7970.0
        pos = _position(level_open=8060.0, level_follower=initial, level_loose=initial)
        decision = self._prof().evaluate(
            pos, current_bid=8010.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > initial  # raised

    def test_soft_downtrend_lowers_stop_but_bounded(self):
        # Underwater + gentle down-slope (≈ -0.03 ATR/candle): lower the stop one
        # notch, never below initial - max_extra_k * ATR.
        buf = _buffer(_osc(40, drift=-0.3))
        initial = 7970.0
        pos = _position(level_open=8030.0, level_follower=initial, level_loose=initial)
        decision = self._prof().evaluate(
            pos, current_bid=8000.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level < initial  # lowered to give room
        from src.core.indicators import atr as _atr

        floor = initial - 1.0 * _atr(list(buf.candles), 14)
        assert decision.new_stop_level >= floor - 1e-6  # bounded

    def test_steep_downtrend_tightens_stop_up_with_noise_gap(self):
        # Underwater + steep down-slope (≈ -0.15 ATR/candle): cut the loss →
        # raise the stop toward price, keeping a noise gap below it.
        buf = _buffer(_osc(40, drift=-1.5))
        pos = _position(level_open=8060.0, level_follower=7900.0, level_loose=7900.0)
        bid = 7960.0
        decision = self._prof().evaluate(
            pos, current_bid=bid, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level > pos.level_follower  # raised to cut loss
        assert decision.new_stop_level < bid  # but kept below price (noise gap)

    def test_too_few_candles_since_open_holds(self):
        # Opened just now: not enough history to trust a slope → keep the stop.
        buf = _buffer(_osc(40, drift=-1.0))
        recent = _START + timedelta(minutes=38)  # only ~2 candles since open
        pos = _position(
            level_open=8030.0,
            level_follower=7970.0,
            level_loose=7970.0,
            opened_at=recent,
        )
        decision = self._prof().evaluate(
            pos, current_bid=8000.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD
