"""Tests for the pluggable strategy package (src/strategies/).

Covers the registry (selection by name), the Donchian breakout strategy with
its efficiency-ratio regime gate, the trend-follower adapter's parity with the
historical ``compute_signal`` path, and the ``level_win = 0`` (no fixed
take-profit) convention in the shared close rules.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.services.compute import compute_signal, efficiency_ratio
from src.services.price_buffer import Candle, EpicBuffer
from src.services.trading import decide_close_reason
from src.strategies import STRATEGIES, DonchianER, TrendFollower, get_strategy


def _settings(**overrides) -> SimpleNamespace:
    """Settings stand-in with every attribute the strategies read."""
    base = {
        "strategy_name": "donchian_er",
        "strategy_donchian_channel": 20,
        "strategy_donchian_stop_atr_k": 2.5,
        "strategy_efficiency_period": 30,
        "strategy_min_efficiency": 0.45,
        "strategy_atr_period": 14,
        "strategy_lookback_points": 20,
        "strategy_sma_fast": 5,
        "strategy_sma_slow": 20,
        "strategy_roc_period": 10,
        "strategy_min_r2": 0.70,
        "strategy_min_score": 0.75,
        "strategy_max_spread_ratio": 0.0015,
        "strategy_stop_multiplier": 2.5,
        "strategy_target_multiplier": 4.0,
        "strategy_tactic": "spread",
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


def _trending_up(n: int = 60, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _choppy(n: int = 60, base: float = 8000.0, amp: float = 2.0) -> list[float]:
    return [base + (amp if i % 2 else -amp) for i in range(n)]


class TestRegistry:
    def test_known_names_resolve_to_their_class(self):
        s = _settings()
        assert isinstance(get_strategy("donchian_er", s), DonchianER)
        assert isinstance(get_strategy("trend_follower", s), TrendFollower)

    def test_unknown_name_raises_with_available_list(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("nope", _settings())

    def test_registry_keys_match_class_names(self):
        for name, cls in STRATEGIES.items():
            assert cls.name == name

    def test_from_settings_maps_parameters(self):
        s = _settings(strategy_donchian_channel=33, strategy_min_efficiency=0.6)
        strat = get_strategy("donchian_er", s)
        assert strat.channel == 33
        assert strat.min_efficiency == 0.6


class TestEfficiencyRatio:
    def test_monotonic_series_is_fully_efficient(self):
        assert efficiency_ratio(_trending_up(30), 20) == pytest.approx(1.0)

    def test_choppy_series_is_inefficient(self):
        assert efficiency_ratio(_choppy(30), 20) < 0.2

    def test_insufficient_data_returns_zero(self):
        assert efficiency_ratio([1.0, 2.0], 20) == 0.0


class TestDonchianER:
    def test_breakout_in_clean_uptrend_emits_buy(self):
        strat = DonchianER()
        buf = _buffer(_trending_up(60))
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        assert signal.direction == "BUY"
        # No fixed take-profit: the trailing stop is the exit.
        assert signal.levels.level_win == 0.0
        # Protective stop sits below the bid by k × ATR.
        assert signal.levels.level_security < signal.levels.bid
        assert signal.levels.level_loose == signal.levels.level_security

    def test_breakdown_in_downtrend_emits_sell(self):
        strat = DonchianER()
        closes = [8000.0 - i * 1.0 for i in range(60)]
        signal = strat.evaluate("TEST.EPIC", _buffer(closes))
        assert signal is not None
        assert signal.direction == "SELL"
        assert signal.levels.level_security > signal.levels.offer

    def test_regime_gate_blocks_choppy_market(self):
        # Alternating closes break the band sometimes, but ER ≈ 0 blocks all.
        strat = DonchianER(min_efficiency=0.45)
        assert strat.evaluate("TEST.EPIC", _buffer(_choppy(80, amp=5.0))) is None

    def test_no_breakout_inside_the_band_stays_flat(self):
        # Trending warmup then a flat plateau: efficient enough early on, but
        # the last close sits inside the prior band → no signal.
        closes = _trending_up(40) + [8039.0] * 25
        strat = DonchianER(min_efficiency=0.0)  # isolate the band check
        assert strat.evaluate("TEST.EPIC", _buffer(closes)) is None

    def test_spread_gate_blocks_wide_spread(self):
        strat = DonchianER(max_spread_ratio=0.0001)
        assert (
            strat.evaluate("TEST.EPIC", _buffer(_trending_up(60), spread=5.0)) is None
        )

    def test_insufficient_data_returns_none(self):
        strat = DonchianER()
        assert strat.evaluate("TEST.EPIC", _buffer(_trending_up(10))) is None

    def test_score_is_the_efficiency_ratio(self):
        strat = DonchianER()
        signal = strat.evaluate("TEST.EPIC", _buffer(_trending_up(60)))
        assert signal is not None
        assert 0.0 <= signal.score <= 1.0
        assert signal.score >= strat.min_efficiency


class TestTrendFollowerAdapter:
    def test_parity_with_compute_signal(self):
        """The adapter must produce the identical signal as the legacy call."""
        strat = TrendFollower.from_settings(_settings())
        buf = _buffer(_trending_up(40, step=0.5), spread=0.2)
        adapted = strat.evaluate("TEST.EPIC", buf)
        direct = compute_signal(
            "TEST.EPIC",
            buf,
            regression_period=20,
            sma_fast_period=5,
            sma_slow_period=20,
            roc_period=10,
            min_r2=0.70,
            min_score=0.75,
            max_spread_ratio=0.0015,
            follower_mult=2.5,
            win_mult=4.0,
            loose_mult=7.5,
            security_mult=5.0,
            tactic="spread",
        )
        assert (adapted is None) == (direct is None)
        if adapted is not None:
            assert adapted.direction == direct.direction
            assert adapted.score == pytest.approx(direct.score)
            assert adapted.levels.level_win == pytest.approx(direct.levels.level_win)

    def test_warmup_is_slow_sma_period(self):
        assert TrendFollower.from_settings(_settings()).warmup == 20


class TestNoTargetCloseConvention:
    def test_level_win_zero_never_closes_as_win(self):
        reason = decide_close_reason(
            999_999.0, level_win=0.0, level_loose=10.0, is_close_hour=False
        )
        assert reason is None

    def test_positive_level_win_still_closes(self):
        reason = decide_close_reason(
            101.0, level_win=100.0, level_loose=10.0, is_close_hour=False
        )
        assert reason == "win"

    def test_loose_still_fires_with_no_target(self):
        reason = decide_close_reason(
            9.0, level_win=0.0, level_loose=10.0, is_close_hour=False
        )
        assert reason == "loose"


class TestSimulatorIntegration:
    def test_donchian_runs_through_the_full_pipeline(self):
        """End-to-end: the registry strategy drives a deterministic simulation."""
        from src.services.simulator import SimulationConfig, run_simulation

        settings = SimpleNamespace(
            **vars(_settings()),
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
            strategy_atr_k_post=1.5,
            strategy_trailing_step_ratio=0.3,
        )
        config = SimulationConfig(target_trades=20, profile="trend_up", seed=42)
        a = run_simulation(settings, config, "donchian_er")
        b = run_simulation(settings, config, "donchian_er")
        assert len(a.trades) >= 20
        assert [t.euro for t in a.trades] == [t.euro for t in b.trades]
        # No fixed target → "win" must never appear as a close reason.
        assert all(t.reason_close != "win" for t in a.trades)
