"""Tests for the momentum scalper strategy (src/strategies/momentum_scalper.py).

Covers the entry gates (spread, recent momentum, very-recent confirmation), the
fixed spread-multiple take-profit, the support-based smart stop and its ATR
distance cap, the settings mapping, and an end-to-end simulator run.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.services.compute import atr
from src.services.price_buffer import Candle, EpicBuffer
from src.strategies import MomentumScalper, get_strategy


def _settings(**overrides) -> SimpleNamespace:
    """Settings stand-in with every attribute the scalper reads."""
    base = {
        "strategy_scalper_momentum_period": 5,
        "strategy_scalper_min_roc": 0.02,
        "strategy_scalper_confirm_period": 2,
        "strategy_scalper_win_ratio": 1.5,
        "strategy_scalper_stop_lookback": 60,
        "strategy_scalper_stop_buffer_atr_k": 0.5,
        "strategy_scalper_max_stop_atr_k": 3.0,
        "strategy_atr_period": 14,
        "strategy_max_spread_ratio": 0.0015,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    """Build a buffer of synthetic candles from a list of bid closes."""
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


def _trending_up(n: int = 70, start: float = 8000.0, step: float = 2.0) -> list[float]:
    return [start + i * step for i in range(n)]


class TestSettings:
    def test_from_settings_maps_parameters(self):
        strat = MomentumScalper.from_settings(
            _settings(strategy_scalper_win_ratio=2.0, strategy_scalper_stop_lookback=30)
        )
        assert strat.win_ratio == 2.0
        assert strat.stop_lookback == 30
        assert strat.atr_period == 14

    def test_registry_resolves_by_name(self):
        strat = get_strategy("momentum_scalper", _settings())
        assert isinstance(strat, MomentumScalper)

    def test_warmup_covers_widest_window(self):
        strat = MomentumScalper(stop_lookback=60, atr_period=14)
        assert strat.warmup == 61


class TestEntry:
    def test_fresh_uptick_emits_buy_with_target_and_stop(self):
        strat = MomentumScalper()
        signal = strat.evaluate("TEST.EPIC", _buffer(_trending_up()))
        assert signal is not None
        assert signal.direction == "BUY"
        levels = signal.levels
        # Fixed take-profit, and a protective stop below the bid.
        assert levels.level_win > levels.bid
        assert levels.level_security < levels.bid
        # Stop / loose / follower all sit at the same detected support level.
        assert levels.level_loose == levels.level_security
        assert levels.level_follower == levels.level_security
        # Break-even is the offer (a BUY exits at the bid).
        assert levels.level_zero == levels.offer

    def test_take_profit_is_net_win_ratio_spreads(self):
        spread = 0.5
        strat = MomentumScalper(win_ratio=1.5)
        signal = strat.evaluate("TEST.EPIC", _buffer(_trending_up(), spread=spread))
        assert signal is not None
        # level_win = bid + spread (break-even) + win_ratio × spread (net gain).
        expected = signal.levels.bid + (1 + 1.5) * spread
        assert signal.levels.level_win == pytest.approx(expected)

    def test_spread_gate_blocks_wide_spread(self):
        strat = MomentumScalper(max_spread_ratio=0.0001)
        assert strat.evaluate("TEST.EPIC", _buffer(_trending_up(), spread=5.0)) is None

    def test_flat_market_has_no_momentum(self):
        strat = MomentumScalper()
        assert strat.evaluate("TEST.EPIC", _buffer([8000.0] * 70)) is None

    def test_very_recent_downtick_is_rejected(self):
        # Strong recent ROC, but the last close dips → fails the confirmation.
        closes = _trending_up(69)
        closes.append(closes[-1] - 1.0)
        strat = MomentumScalper()
        assert strat.evaluate("TEST.EPIC", _buffer(closes)) is None

    def test_insufficient_data_returns_none(self):
        strat = MomentumScalper()
        assert strat.evaluate("TEST.EPIC", _buffer(_trending_up(10))) is None

    def test_score_is_the_roc(self):
        strat = MomentumScalper()
        signal = strat.evaluate("TEST.EPIC", _buffer(_trending_up()))
        assert signal is not None
        assert signal.score == pytest.approx(signal.roc)
        assert signal.roc >= strat.min_roc


class TestSmartStop:
    def test_stop_distance_capped_at_max_atr(self):
        strat = MomentumScalper(max_stop_atr_k=3.0)
        buf = _buffer(_trending_up())
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        atr_value = atr(list(buf.candles), strat.atr_period)
        distance = signal.levels.bid - signal.levels.level_security
        # Far support in a steady climb → the ATR cap binds.
        assert distance == pytest.approx(3.0 * atr_value)

    def test_uncapped_stop_sits_below_detected_support(self):
        strat = MomentumScalper(max_stop_atr_k=999.0, stop_buffer_atr_k=0.5)
        buf = _buffer(_trending_up())
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        candles = list(buf.candles)
        support = min(c.bid_low for c in candles[-strat.stop_lookback :])
        atr_value = atr(candles, strat.atr_period)
        assert signal.levels.level_security == pytest.approx(support - 0.5 * atr_value)


class TestSimulatorIntegration:
    def test_scalper_runs_through_the_full_pipeline(self):
        """End-to-end: the registry strategy drives a deterministic simulation."""
        from src.services.simulator import SimulationConfig, run_simulation

        settings = SimpleNamespace(
            **vars(_settings()),
            strategy_name="momentum_scalper",
            strategy_max_positions=6,
            strategy_max_trades_day=50,
            strategy_daily_loss_limit=-500.0,
            strategy_daily_win_target=300.0,
            strategy_min_win_rate=0.40,
            strategy_hour_start=9,
            strategy_hour_end=16,
            strategy_hour_close=17,
            strategy_close_target="follower",
            strategy_compensate_loose=False,
            strategy_euro_loss=4000.0,
            strategy_atr_k_pre=2.5,
            strategy_atr_k_post=2.5,
            strategy_trailing_step_ratio=0.3,
        )
        config = SimulationConfig(target_trades=10, profile="trend_up", seed=42)
        a = run_simulation(settings, config, "momentum_scalper")
        b = run_simulation(settings, config, "momentum_scalper")
        # Deterministic for a fixed seed.
        assert [t.euro for t in a.trades] == [t.euro for t in b.trades]
        # A scalper has a fixed target, so "win" closes are reachable.
        assert all(
            t.reason_close in {"win", "stop", "follower", "loose", "end_of_day"}
            for t in a.trades
        )
