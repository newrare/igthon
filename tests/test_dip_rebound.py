"""Tests for the dip-rebound strategy (src/strategies/dip_rebound.py).

Covers the per-epic entry gates (enough data, spread, global up-trend, a
significant recent drop, an in-progress bounce), the reward/risk take-profit and
the dip-bottom ATR stop, plus the settings mapping and registry resolution.
"""

from types import SimpleNamespace

import pytest

from src.core.indicators import atr
from src.feed.price_buffer import Candle, EpicBuffer
from src.strategies import DipRebound, get_strategy


def _settings(**overrides) -> SimpleNamespace:
    """Settings stand-in with every attribute the strategy reads."""
    base = {
        "strategy_dip_rebound_trend_period": 60,
        "strategy_dip_rebound_min_trend_r2": 0.55,
        "strategy_dip_rebound_pullback_lookback": 30,
        "strategy_dip_rebound_min_pullback_atr_k": 1.5,
        "strategy_dip_rebound_rebound_period": 2,
        "strategy_dip_rebound_win_ratio": 2.0,
        "strategy_dip_rebound_stop_lookback": 10,
        "strategy_dip_rebound_stop_buffer_atr_k": 0.5,
        "strategy_atr_period": 14,
        "strategy_max_spread_ratio": 0.0015,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    """Build a buffer of synthetic candles from a list of bid closes.

    Highs/lows hug the close path (±0.1) so the swing low tracks the dip bottom.
    """
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    prev = closes[0]
    for close in closes:
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        buf.add(
            Candle(
                timestamp=None,
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


def _uptrend_with_dip() -> list[float]:
    """A clean rising line, a sharp dip near the end, then a 3-candle bounce.

    60 candles climbing 8000→8118 (step 2), a ~28-point drop over 4 candles, then
    three rising closes — the textbook dip-rebound setup (67 candles ≥ warmup).
    """
    rise = [8000.0 + i * 2.0 for i in range(60)]  # up-trend, ends 8118
    dip = [8110.0, 8102.0, 8096.0, 8090.0]  # sharp ~28pt drop from the high
    bounce = [8094.0, 8099.0, 8104.0]  # three rising closes off the bottom
    return rise + dip + bounce


class TestSettings:
    def test_from_settings_maps_parameters(self):
        strat = DipRebound.from_settings(
            _settings(
                strategy_dip_rebound_win_ratio=3.0,
                strategy_dip_rebound_min_pullback_atr_k=2.0,
            )
        )
        assert strat.win_ratio == 3.0
        assert strat.min_pullback_atr_k == 2.0
        assert strat.atr_period == 14

    def test_registry_resolves_by_name(self):
        strat = get_strategy("dip_rebound", _settings())
        assert isinstance(strat, DipRebound)

    def test_warmup_covers_widest_window(self):
        strat = DipRebound(trend_period=60, pullback_lookback=30, atr_period=14)
        assert strat.warmup == 61

    def test_per_epic_not_hourly(self):
        # Stays on the immediate-open path, unlike the cross-epic selector.
        assert DipRebound().hourly_selection is False


class TestEntry:
    def test_dip_in_uptrend_emits_buy(self):
        strat = DipRebound()
        signal = strat.evaluate("TEST.EPIC", _buffer(_uptrend_with_dip()))
        assert signal is not None
        assert signal.direction == "BUY"
        # Score is the pullback depth in ATR — a positive "size of the dip".
        assert signal.score > 0

    def test_insufficient_data_returns_none(self):
        strat = DipRebound()
        assert strat.evaluate("TEST.EPIC", _buffer([8000.0] * 30)) is None

    def test_flat_market_is_rejected(self):
        # No up-trend (zero slope) → no setup even if the curve wiggles.
        strat = DipRebound()
        assert strat.evaluate("TEST.EPIC", _buffer([8000.0] * 70)) is None

    def test_downtrend_is_rejected(self):
        # Negative slope → not a rising market.
        closes = [9000.0 - i * 2.0 for i in range(70)]
        assert DipRebound().evaluate("TEST.EPIC", _buffer(closes)) is None

    def test_uptrend_without_dip_is_rejected(self):
        # Clean rise, no significant pullback → nothing to rebound from.
        closes = [8000.0 + i * 2.0 for i in range(70)]
        assert DipRebound().evaluate("TEST.EPIC", _buffer(closes)) is None

    def test_falling_knife_is_rejected(self):
        # Up-trend then a deep drop still falling on the last candles (no bounce).
        rise = [8000.0 + i * 2.0 for i in range(60)]
        falling = [8110.0, 8100.0, 8090.0, 8080.0, 8070.0]  # last closes still down
        assert DipRebound().evaluate("TEST.EPIC", _buffer(rise + falling)) is None

    def test_shallow_dip_is_rejected(self):
        # Requiring a far deeper dip rejects an otherwise-valid rebound setup.
        strat = DipRebound(min_pullback_atr_k=20.0)  # demand an unreachable depth
        assert strat.evaluate("TEST.EPIC", _buffer(_uptrend_with_dip())) is None

    def test_spread_gate_blocks_wide_spread(self):
        strat = DipRebound(max_spread_ratio=0.0001)
        assert (
            strat.evaluate("TEST.EPIC", _buffer(_uptrend_with_dip(), spread=5.0))
            is None
        )


class TestLevels:
    def test_stop_sits_below_dip_bottom_with_cushion(self):
        strat = DipRebound(stop_lookback=10, stop_buffer_atr_k=0.5)
        buf = _buffer(_uptrend_with_dip())
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        candles = list(buf.candles)
        swing_low = min(c.bid_low for c in candles[-strat.stop_lookback :])
        atr_value = atr(candles, strat.atr_period)
        assert signal.levels.level_security == pytest.approx(
            swing_low - 0.5 * atr_value
        )
        # Stop levels are all pinned to the protective stop (ratchet-only trail).
        assert signal.levels.level_loose == signal.levels.level_security
        assert signal.levels.level_follower == signal.levels.level_security
        assert signal.levels.level_security < signal.levels.bid

    def test_take_profit_is_reward_risk_multiple(self):
        strat = DipRebound(win_ratio=2.0)
        signal = strat.evaluate("TEST.EPIC", _buffer(_uptrend_with_dip()))
        assert signal is not None
        risk = signal.levels.bid - signal.levels.level_security
        expected = signal.levels.bid + 2.0 * risk
        assert signal.levels.level_win == pytest.approx(expected)
        assert signal.levels.stop_distance == pytest.approx(risk)
