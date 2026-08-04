"""Tests for the strategy simulator and its web routes.

The simulator replays the project's real open/close rules on synthetic
curves; these tests check that runs are deterministic, internally consistent
(every trade closed, P&L adds up, gates respected) and exposed correctly over
the ``/simulator`` API.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.backtest.simulator import (
    SessionExtremes,
    SimulationConfig,
    run_close_visual,
    run_open_visual,
    run_simulation,
)
from src.feed.price_buffer import Candle, PriceBuffer
from src.web.app import create_app


def _settings() -> SimpleNamespace:
    """Settings stand-in with every strategy attribute the simulator reads."""
    return SimpleNamespace(
        ig_env=SimpleNamespace(value="demo"),
        web_port=8000,
        # Decoupled open/close: the reference entry + close profile.
        open_strategy="open_donchian",
        stop_strategy="stop_support",
        close_zonestart="hold",
        close_zonemarge="hold",
        close_zonesecure="hold",
        close_zoneprofit="trailing_ratchet",
        strategy_close_margin_minutes=5,
        strategy_atr_period=14,
        strategy_atr_k_pre=2.5,
        strategy_atr_k_post=1.5,
        strategy_trailing_step_ratio=0.3,
        # Global .env re-open policy the simulator mirrors (see TradeConfig).
        allow_same_day_reopen=True,
    )


def _run(target: int = 30, seed: int = 42, settings=None, **overrides):
    config = SimulationConfig(target_trades=target, seed=seed, **overrides)
    return run_simulation(settings or _settings(), config)


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

    def test_same_day_reopen_policy_limits_openings_per_epic(self):
        # The global ALLOW_SAME_DAY_REOPEN policy is mirrored by the simulator, so
        # a backtest reports what the live bot would do. With it off, an epic can
        # be opened at most once per simulated day.
        strict = _settings()
        strict.allow_same_day_reopen = False
        result = _run(target=200, seed=11, settings=strict, max_days=5)
        per_epic_day = [(t.epic, t.day) for t in result.trades]
        assert len(per_epic_day) == len(set(per_epic_day))
        # The permissive policy is what allows several openings on one epic/day.
        loose = _run(target=200, seed=11, max_days=5)
        loose_keys = [(t.epic, t.day) for t in loose.trades]
        assert len(loose_keys) > len(set(loose_keys))

    def test_stops_at_max_days_when_no_signals(self):
        # A flat sideways market with a tiny day cap: the run must terminate.
        result = _run(target=1000, profile="sideways", max_days=3)
        assert result.days_simulated == 3


class TestCloseVisual:
    """Single open→close cycle used by the close-profile visual test."""

    def _visual(self, **kw):
        return run_close_visual(_settings(), seed=42, **kw)

    def test_shape_and_ordering(self):
        r = self._visual(curve_profile="random")
        assert len(r["bids"]) == len(r["timestamps"]) == 600
        # The first stop sits at the open; the close never precedes the open.
        assert r["stops"][0]["index"] == r["open"]["index"]
        assert r["close"]["index"] >= r["open"]["index"]
        assert r["close"]["reason"] in {
            "win",
            "loose",
            "stop",
            "follower",
            "end_of_day",
        }

    def test_deterministic_with_seed(self):
        a = self._visual(curve_profile="volatile")
        b = self._visual(curve_profile="volatile")
        assert a["open"] == b["open"]
        assert a["close"] == b["close"]
        assert a["euro"] == b["euro"]

    def test_stop_only_ratchets_up(self):
        # On a clean uptrend the protective stop must never step down.
        r = self._visual(curve_profile="trend_up")
        levels = [s["level"] for s in r["stops"]]
        assert all(b >= a for a, b in zip(levels, levels[1:]))
        assert r["stop_updates"] > 0

    def test_pinned_open_index(self):
        r = self._visual(curve_profile="random", open_index=120)
        assert r["open"]["index"] == 120


class TestOpenVisual:
    """Walk-to-first-open cycle used by the entry-strategy open-trigger test."""

    def _visual(self, **kw):
        return run_open_visual(_settings(), seed=42, **kw)

    def test_truncates_curve_at_open(self):
        # When the strategy opens, the curve must stop exactly at the open tick
        # so the future stays hidden.
        r = self._visual(curve_profile="trend_up")
        assert r["opened"] is True
        assert r["open"]["direction"] == "BUY"
        idx = r["open"]["index"]
        assert len(r["bids"]) == len(r["timestamps"]) == idx + 1
        assert len(r["offers"]) == idx + 1
        assert idx + 1 <= r["candles_total"]

    def test_full_curve_when_no_open(self):
        # A flat market never triggers: the whole day is returned, open is null.
        r = self._visual(curve_profile="sideways", num_candles=600)
        assert r["opened"] is False
        assert r["open"] is None
        assert len(r["bids"]) == r["candles_total"] == 600

    def test_deterministic_with_seed(self):
        a = self._visual(curve_profile="trend_up")
        b = self._visual(curve_profile="trend_up")
        assert a["open"] == b["open"]
        assert a["bids"] == b["bids"]


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

    @pytest.mark.asyncio
    async def test_close_profile_endpoint(self, client):
        resp = await client.get(
            "/api/simulator/close-profile",
            params={
                "curve_profile": "trend_up",
                "close_profile": "close_zoneprofit",
                "seed": 7,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["seed"] == 7
        assert data["close_profile"] == "close_zoneprofit"
        assert len(data["bids"]) == 600
        assert data["stops"][0]["index"] == data["open"]["index"]

    @pytest.mark.asyncio
    async def test_close_profile_rejects_unknown_profile(self, client):
        resp = await client.get(
            "/api/simulator/close-profile", params={"close_profile": "nope"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_open_strategy_endpoint(self, client):
        resp = await client.get(
            "/api/simulator/open-strategy",
            params={
                "curve_profile": "trend_up",
                "strategy": "open_donchian",
                "seed": 7,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["seed"] == 7
        assert data["strategy"] == "open_donchian"
        # Truncated at the open: the curve ends at the open tick.
        if data["opened"]:
            assert len(data["bids"]) == data["open"]["index"] + 1

    @pytest.mark.asyncio
    async def test_open_strategy_rejects_unknown_strategy(self, client):
        resp = await client.get(
            "/api/simulator/open-strategy", params={"strategy": "nope"}
        )
        assert resp.status_code == 400


def _candle(bid_low: float, offer_high: float) -> Candle:
    return Candle(
        timestamp=datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        bid_open=bid_low,
        bid_close=bid_low,
        bid_high=offer_high,
        bid_low=bid_low,
        offer_open=offer_high,
        offer_close=offer_high,
        offer_high=offer_high,
        offer_low=bid_low,
    )


class TestSessionExtremes:
    """The backtest's stand-in for the live ``day_extreme`` database query.

    Mirrors ``TradingService._session_extreme``; the property that makes it
    faithful rather than merely convenient is that it is fed as candles arrive, so
    it can never expose a level the live path would not yet know.
    """

    def test_starts_unknown_for_every_epic(self):
        session = SessionExtremes.for_curves(3)
        assert [session.get(e, "BUY") for e in range(3)] == [None, None, None]
        assert [session.get(e, "SELL") for e in range(3)] == [None, None, None]

    def test_buy_keeps_the_lowest_bid_low(self):
        session = SessionExtremes.for_curves(1)
        for low in (8000.0, 7950.0, 8010.0):
            session.update(0, _candle(low, low + 1))
        assert session.get(0, "BUY") == 7950.0

    def test_sell_keeps_the_highest_offer_high(self):
        session = SessionExtremes.for_curves(1)
        for high in (8000.0, 8100.0, 8050.0):
            session.update(0, _candle(high - 1, high))
        assert session.get(0, "SELL") == 8100.0

    def test_epics_are_tracked_independently(self):
        session = SessionExtremes.for_curves(2)
        session.update(0, _candle(7000.0, 7001.0))
        session.update(1, _candle(9000.0, 9001.0))
        assert session.get(0, "BUY") == 7000.0
        assert session.get(1, "BUY") == 9000.0

    def test_carries_no_look_ahead(self):
        # The extreme must only ever reflect what has already been ingested: a
        # deeper low arriving later cannot retroactively widen an earlier open.
        session = SessionExtremes.for_curves(1)
        session.update(0, _candle(8000.0, 8001.0))
        at_open = session.get(0, "BUY")
        session.update(0, _candle(7000.0, 7001.0))
        assert at_open == 8000.0
        assert session.get(0, "BUY") == 7000.0

    def test_a_fresh_instance_resets_the_day(self):
        session = SessionExtremes.for_curves(1)
        session.update(0, _candle(7000.0, 7001.0))
        assert SessionExtremes.for_curves(1).get(0, "BUY") is None
