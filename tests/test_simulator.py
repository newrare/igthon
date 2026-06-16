"""Tests for the strategy simulator and its web routes.

The simulator replays the project's real open/close rules on synthetic
curves; these tests check that runs are deterministic, internally consistent
(every trade closed, P&L adds up, gates respected) and exposed correctly over
the ``/simulator`` API.
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.services.price_buffer import PriceBuffer
from src.services.simulator import SimulationConfig, run_simulation
from src.web.app import create_app


def _settings() -> SimpleNamespace:
    """Settings stand-in with every strategy attribute the simulator reads."""
    return SimpleNamespace(
        ig_env=SimpleNamespace(value="demo"),
        web_port=8000,
        # Default to the historical strategy so the engine tests below keep
        # exercising the long-standing trend-follower expectations.
        strategy_name="trend_follower",
        strategy_donchian_channel=20,
        strategy_donchian_stop_atr_k=2.5,
        strategy_efficiency_period=30,
        strategy_min_efficiency=0.45,
        strategy_lookback_points=20,
        strategy_sma_fast=5,
        strategy_sma_slow=20,
        strategy_roc_period=10,
        strategy_min_r2=0.70,
        strategy_min_score=0.75,
        strategy_max_spread_ratio=0.0015,
        strategy_stop_multiplier=2.5,
        strategy_target_multiplier=4.0,
        strategy_tactic="spread",
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
        strategy_atr_period=14,
        strategy_atr_k_pre=2.5,
        strategy_atr_k_post=1.5,
        strategy_trailing_step_ratio=0.3,
    )


def _run(target: int = 30, seed: int = 42, **overrides):
    config = SimulationConfig(target_trades=target, seed=seed, **overrides)
    return run_simulation(_settings(), config)


class TestSimulationRun:
    """End-to-end engine behaviour on generated curves."""

    def test_reaches_trade_target(self):
        result = _run(target=30)
        # The last day can close a couple of extra positions at once.
        assert 30 <= len(result.trades) <= 30 + SimulationConfig().epics_per_day

    def test_deterministic_with_seed(self):
        a = _run(target=20, seed=7)
        b = _run(target=20, seed=7)
        assert [t.euro for t in a.trades] == [t.euro for t in b.trades]

    def test_every_trade_is_closed_and_consistent(self):
        result = _run(target=30)
        for t in result.trades:
            assert t.reason_close in {"win", "loose", "stop", "follower", "end_of_day"}
            assert t.level_close is not None
            assert t.euro is not None
            assert t.win == (t.euro > 0)
            # Same euro formula as TradingService._euro_pnl (epp = 1 €/point).
            assert t.euro == pytest.approx(t.level_close - t.level_open, abs=0.01)

    def test_opens_only_within_trading_hours(self):
        result = _run(target=30)
        settings = _settings()
        for t in result.trades:
            hour = int(t.open_time.split(":")[0])
            assert settings.strategy_hour_start <= hour < settings.strategy_hour_end

    def test_summary_adds_up(self):
        result = _run(target=30)
        s = result.summary()
        assert s["trades"] == len(result.trades)
        assert s["wins"] + s["losses"] == s["trades"]
        assert s["total_pnl"] == pytest.approx(
            sum(t.euro for t in result.trades), abs=0.05
        )
        assert len(s["equity"]) == s["trades"]
        assert s["equity"][-1] == pytest.approx(s["total_pnl"], abs=0.05)
        assert len(s["daily_pnl"]) == s["days_simulated"]
        assert sum(s["close_reasons"].values()) == s["trades"]

    def test_stops_at_max_days_when_no_signals(self):
        # A flat sideways market with a tiny day cap: the run must terminate.
        result = _run(target=1000, profile="sideways", max_days=3)
        assert result.days_simulated == 3


class TestSimulatorRoutes:
    """The /simulator page and its JSON API."""

    @pytest.fixture
    def app(self):
        return create_app(settings=_settings(), buffer=PriceBuffer())

    @pytest.fixture
    def client(self, app):
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_page_renders(self, client):
        resp = await client.get("/simulator")
        assert resp.status_code == 200
        assert "Strategy Simulator" in resp.text
        assert "Curve Generator" in resp.text

    @pytest.mark.asyncio
    async def test_curve_endpoint(self, client):
        resp = await client.get(
            "/api/simulator/curve", params={"profile": "trend_up", "seed": 5}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["seed"] == 5
        assert len(data["bid_closes"]) == 600
        assert len(data["timestamps"]) == 600

    @pytest.mark.asyncio
    async def test_curve_endpoint_rejects_unknown_profile(self, client):
        resp = await client.get("/api/simulator/curve", params={"profile": "nope"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_run_endpoint(self, client):
        resp = await client.post(
            "/api/simulator/run",
            json={"profile": "random", "seed": 42, "target_trades": 10},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["seed"] == 42
        assert data["summary"]["trades"] >= 10
        assert data["summary"]["wins"] + data["summary"]["losses"] == (
            data["summary"]["trades"]
        )
        assert len(data["trades"]) == data["summary"]["trades"]

    @pytest.mark.asyncio
    async def test_run_endpoint_validates_body(self, client):
        resp = await client.post("/api/simulator/run", json={"target_trades": 100000})
        assert resp.status_code == 422
