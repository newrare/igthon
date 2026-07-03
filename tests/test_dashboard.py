"""Tests for the dashboard route — fragment rendering and live polling endpoint.

These cover the "unified Option A" live-update mechanism: a single
``/api/dashboard-fragments`` endpoint returns the HTML for every dynamic region,
which the client swaps in place every two seconds instead of reloading the page.
"""

import asyncio
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import src.web.routes.dashboard.router  # noqa: F401  (ensure submodule imported)
from src.feed.price_buffer import PriceBuffer
from src.web.app import create_app
from src.web.routes.dashboard import _build_fragments, _render_dashboard

# The dashboard package re-exports the ``router`` APIRouter, shadowing the
# submodule attribute, so reach the real module object via sys.modules to patch
# its globals (e.g. TradingService) in the manual-open tests.
_ROUTER_MOD = sys.modules["src.web.routes.dashboard.router"]

# ── Fixtures / builders ─────────────────────────────────────────────────────

_FRAGMENT_KEYS = {
    "kpi_bar",
    "market_rows",
    "week_summary",
    "day_history",
    "queue_modal",
    "epic_list_modal",
    "positions_modal",
    "closed_positions_modal",
    "actions",
    "logs_section",
}


def _base_kpis() -> dict:
    """Minimal KPI payload with every key the fragments read."""
    return {
        "available_epics": 1,
        "open_trades": 0,
        "open_pnl": 0.0,
        "closed_trades": 0,
        "daily_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "win_rate_today": 0.0,
        "total_wins": 0,
        "total_losses": 0,
        "total_closed": 0,
        "win_rate": 0.0,
        "all_epics_count": 100,
        "epic_kpi_color": "#4ade80",
        "refresh_label": "Today 10:00",
        "tradable_count": 40,
        "tradable_kpi_color": "#4ade80",
        "tradable_refresh_label": "Today 10:00",
        "wallet_available": 1234.5,
        "wallet_used": 200.0,
    }


def _base_state(**overrides) -> dict:
    state = {
        "market_summary": [],
        "kpis": _base_kpis(),
        "guard_stats": None,
        "error_entries": [],
        "queue_stats": None,
        "queue_recent": [],
        "queue_pending_tasks": [],
        "open_positions": [],
        "closed_positions": [],
        "bot_paused": False,
        "scheduler_available": True,
        "jobs": _base_jobs(),
    }
    state.update(overrides)
    return state


def _base_jobs() -> list[dict]:
    """Two job entries covering an automatic and a manual (danger) job."""
    return [
        {
            "action": "collect_and_analyze",
            "name": "Collect & Analyze",
            "description": "Fetch prices, compute signals, open positions.",
            "schedule": "Every 30s · 08–17 · Mon–Fri",
            "danger": "safe",
            "auto": True,
        },
        {
            "action": "end_of_day",
            "name": "End of Day",
            "description": "Force close ALL open positions immediately.",
            "schedule": "Daily 21:30 · Mon–Fri",
            "danger": "danger",
            "auto": False,
        },
    ]


def _settings() -> SimpleNamespace:
    """Settings stand-in carrying only the attributes the shell reads."""
    return SimpleNamespace(
        ig_env=SimpleNamespace(value="demo"),
        open_strategy="open_donchian",
        stop_strategy="stop_support",
        close_zonestart="hold",
        close_zonemarge="hold",
        close_zoneprofit="trailing_ratchet",
        web_port=8000,
        # Extra knobs read by TradeConfig.from_settings (manual-open path).
        strategy_close_margin_minutes=5,
        strategy_atr_period=14,
        strategy_atr_k_pre=2.5,
        strategy_atr_k_post=1.5,
        strategy_trailing_step_ratio=0.3,
    )


# ── _build_fragments ────────────────────────────────────────────────────────


class TestBuildFragments:
    """Unit tests for the fragment builder (pure, no app or DB)."""

    def test_returns_all_fragment_keys(self):
        frags = _build_fragments(_base_state())
        assert set(frags) == _FRAGMENT_KEYS
        assert all(isinstance(v, str) for v in frags.values())

    def test_market_rows_contains_epic_data(self):
        state = _base_state(
            market_summary=[
                {
                    "epic": "IX.D.DAX.IFMM.IP",
                    "bid": 18000.0,
                    "dots": 30,
                    "high": 18100.0,
                    "low": 17900.0,
                    "spread_cost": 2.5,
                }
            ]
        )
        rows = _build_fragments(state)["market_rows"]
        assert "IX.D.DAX.IFMM.IP" in rows
        assert "18100.0" in rows
        assert "30" in rows

    def test_market_rows_render_spread_cost(self):
        state = _base_state(
            market_summary=[
                {
                    "epic": "IX.D.DAX.IFMM.IP",
                    "bid": 18000.0,
                    "dots": 30,
                    "high": 18100.0,
                    "low": 17900.0,
                    "spread_cost": 1.5,
                }
            ]
        )
        rows = _build_fragments(state)["market_rows"]
        assert "1.50€" in rows

    def test_market_rows_spread_cost_dash_when_missing(self):
        state = _base_state(
            market_summary=[
                {
                    "epic": "IX.D.DAX.IFMM.IP",
                    "bid": 18000.0,
                    "dots": 30,
                    "high": 18100.0,
                    "low": 17900.0,
                    "spread_cost": None,
                }
            ]
        )
        rows = _build_fragments(state)["market_rows"]
        assert "€" not in rows

    def test_no_bot_tile_in_kpi_bar(self):
        # The Bot pause/resume KPI tile was replaced by the Actions section.
        frags = _build_fragments(_base_state())
        assert 'id="kpi-bot"' not in frags["kpi_bar"]

    def test_wallet_tile_shows_available_and_used(self):
        frags = _build_fragments(_base_state())
        kpi_bar = frags["kpi_bar"]
        assert "Wallet" in kpi_bar
        assert "1,234.50€" in kpi_bar  # available
        assert "In use: 200.00€" in kpi_bar

    def test_wallet_tile_handles_missing_balance(self):
        state = _base_state(
            kpis={**_base_kpis(), "wallet_available": None, "wallet_used": None}
        )
        kpi_bar = _build_fragments(state)["kpi_bar"]
        assert "Wallet" in kpi_bar
        assert "In use: —" in kpi_bar

    def test_open_positions_rendered_in_modal(self):
        pos = SimpleNamespace(
            id=1,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            epic="IX.D.DAX.IFMM.IP",
            epic_name="DAX",
            level_open=18000.0,
            level_zero=18000.0,
            level_win=18050.0,
            level_stop=17950.0,
            quantity=2,
            euro=5.0,
            strategy=SimpleNamespace(value="target"),
        )
        state = _base_state(
            open_positions=[pos],
            kpis={**_base_kpis(), "open_trades": 1, "open_pnl": 5.0},
        )
        modal = _build_fragments(state)["positions_modal"]
        assert "DAX" in modal
        assert "No open positions" not in modal
        # Row opens the chart modal for the epic; levels/markers are fetched from
        # /api/chart/{epic} (all trades for the day), not embedded in the row.
        # ``event`` is passed so the chart carousel can rebuild the table's list.
        assert "openChartModal('IX.D.DAX.IFMM.IP', event)" in modal
        # The Close button must not bubble up into the row's chart handler.
        assert "event.stopPropagation(); closePosition" in modal

    def test_positions_modal_empty_state(self):
        modal = _build_fragments(_base_state())["positions_modal"]
        assert "No open positions" in modal

    def test_closed_positions_rendered_in_modal(self):
        pos = SimpleNamespace(
            id=2,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=time(11, 30, 0),
            epic="IX.D.DAX.IFMM.IP",
            epic_name="DAX",
            level_open=18000.0,
            level_close=18050.0,
            quantity=2,
            euro=12.5,
            reason_open="manual",
            reason_close="win",
        )
        state = _base_state(
            closed_positions=[pos],
            kpis={
                **_base_kpis(),
                "closed_trades": 1,
                "daily_pnl": 12.5,
                "wins": 1,
                "win_rate": 1.0,
            },
        )
        modal = _build_fragments(state)["closed_positions_modal"]
        assert "DAX" in modal
        assert "08/06/2026" in modal  # open/close date
        # Times are stored UTC and rendered on the Europe/Paris wall clock
        # (CEST = UTC+2 in June): 10:00 UTC -> 12:00, 11:30 UTC -> 13:30.
        assert "12:00:00" in modal  # open time (Paris)
        assert "13:30:00" in modal  # close time (Paris)
        assert "11:30:00" not in modal  # raw UTC must not leak through
        assert "Manual" in modal  # open reason label
        assert "Target hit" in modal  # close reason label
        assert "No closed positions" not in modal
        # Row opens the chart modal for the epic; entry/exit markers are fetched
        # from /api/chart/{epic}, not embedded in the row.
        assert "openChartModal('IX.D.DAX.IFMM.IP', event)" in modal

    def test_closed_positions_modal_empty_state(self):
        modal = _build_fragments(_base_state())["closed_positions_modal"]
        assert "No closed positions" in modal


class TestTradeOverlay:
    """`_trade_overlay` serialises a position's chart levels/markers for /api/chart."""

    def test_open_trade_serialisation(self):
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=7,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=None,
            level_open=18000.0,
            level_zero=18002.0,
            level_stop=17950.0,
            level_win=18050.0,
            level_close=None,
            euro=5.0,
        )
        ov = _trade_overlay(pos)
        assert ov["id"] == 7
        assert ov["open"] == 18000.0
        assert ov["zero"] == 18002.0
        assert ov["stopLoose"] == 17950.0
        assert ov["target"] == 18050.0
        assert ov["openTime"] == "2026-06-08T10:00:00+00:00"
        # An open trade has no close level/time yet.
        assert ov["close"] is None
        assert ov["closeTime"] is None
        assert ov["pnl"] == 5.0

    def test_closed_trade_has_close_marker(self):
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=8,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=time(11, 30, 0),
            level_open=18000.0,
            level_zero=18002.0,
            level_stop=17950.0,
            level_win=18050.0,
            level_close=18050.0,
            euro=12.5,
        )
        ov = _trade_overlay(pos)
        assert ov["close"] == 18050.0
        assert ov["closeTime"] == "2026-06-08T11:30:00+00:00"

    def test_stop_history_serialised_as_stepped_points(self):
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=9,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=time(11, 30, 0),
            level_open=18000.0,
            level_zero=18002.0,
            level_stop=17950.0,
            level_win=18050.0,
            level_close=18020.0,
            euro=-3.0,
            stop_history=[
                {"t": "2026-06-08T10:00:00+00:00", "level": 17950.0},
                {"t": "2026-06-08T10:30:00+00:00", "level": 18010.0},
            ],
        )
        ov = _trade_overlay(pos)
        # The bot software-stop trajectory is the raw ratchet history.
        assert ov["stopsFollower"] == [
            {"t": "2026-06-08T10:00:00+00:00", "level": 17950.0},
            {"t": "2026-06-08T10:30:00+00:00", "level": 18010.0},
        ]
        # The frozen initial stop is still exposed for the pre-history fallback.
        assert ov["stopLoose"] == 17950.0

    def test_bot_and_ig_stop_lines_diverge_at_open(self):
        """The IG broker line keeps the (wider) clamped open stop; the bot line
        keeps the tighter software stop. They share every later ratchet point."""
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=12,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=time(11, 0, 0),
            level_open=460.0,
            level_zero=460.4,
            level_stop=458.3,  # broker stop: widened to IG's min distance
            level_follower=459.3,  # software stop: tighter, what the bot enforces
            level_win=0.0,
            level_close=459.2,
            euro=-57.0,
            # Seeded with the software stop; the ratchet at 10:30 was pushed to IG.
            stop_history=[
                {"t": "2026-06-08T10:00:00+00:00", "level": 459.3},
                {"t": "2026-06-08T10:30:00+00:00", "level": 459.8},
            ],
        )
        ov = _trade_overlay(pos)
        assert ov["stopsFollower"] == [
            {"t": "2026-06-08T10:00:00+00:00", "level": 459.3},
            {"t": "2026-06-08T10:30:00+00:00", "level": 459.8},
        ]
        # IG line: broker's initial clamped level, then the same pushed ratchet.
        assert ov["stopsLoose"] == [
            {"t": "2026-06-08T10:00:00+00:00", "level": 458.3},
            {"t": "2026-06-08T10:30:00+00:00", "level": 459.8},
        ]
        assert ov["stopFollower"] == 459.3
        assert ov["stopLoose"] == 458.3

    def test_stop_history_absent_yields_none(self):
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=10,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=None,
            level_open=18000.0,
            level_zero=18002.0,
            level_stop=17950.0,
            level_follower=17960.0,
            level_win=18050.0,
            level_close=None,
            euro=0.0,
        )
        ov = _trade_overlay(pos)
        # No ratchet history → both trajectories are None; the chart falls back to
        # the flat scalars (bot = level_follower, IG = level_stop).
        assert ov["stopsFollower"] is None
        assert ov["stopsLoose"] is None
        assert ov["stopFollower"] == 17960.0
        assert ov["stopLoose"] == 17950.0

    def test_stop_history_drops_malformed_and_zero_levels(self):
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=11,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=None,
            level_open=18000.0,
            level_zero=18002.0,
            level_stop=17950.0,
            level_win=18050.0,
            level_close=None,
            euro=0.0,
            stop_history=[
                {"t": "2026-06-08T10:00:00+00:00", "level": 17950.0},
                {"t": "2026-06-08T10:05:00+00:00", "level": 0},  # unset → dropped
                {"level": 18010.0},  # missing time → dropped
                "garbage",  # not a dict → dropped
            ],
        )
        assert _trade_overlay(pos)["stopsFollower"] == [
            {"t": "2026-06-08T10:00:00+00:00", "level": 17950.0},
        ]

    def test_blocked_guard_renders_block_info(self):
        guard = SimpleNamespace(
            total_calls=99,
            calls_last_minute=60,
            calls_last_second=5,
            is_available=False,
            is_blocked=True,
            blocked_since=datetime(2026, 6, 8, 10, 0, tzinfo=UTC),
            blocked_until=datetime(2026, 6, 8, 10, 5, tzinfo=UTC),
            blocked_reason="rate limited",
            max_per_minute=60,
            max_per_second=5,
        )
        frags = _build_fragments(_base_state(guard_stats=guard))
        # A blocked guard turns the Queue tile border red...
        assert (
            'border-left-color:#ef4444; position:relative;" onclick="openQueueModal()"'
            in frags["kpi_bar"]
        )
        # ...and the guard detail (status + block info) lives in the Queue modal.
        assert "BLOCKED" in frags["queue_modal"]
        assert "Blocked since" in frags["queue_modal"]


# ── Shell rendering ──────────────────────────────────────────────────────────


class TestRenderDashboard:
    """The full page embeds fragment containers and the polling engine."""

    def test_page_contains_fragment_containers(self):
        html = _render_dashboard(_settings(), _base_state())
        for key in _FRAGMENT_KEYS:
            assert f'id="frag-{key}"' in html

    def test_page_polls_instead_of_reloading(self):
        html = _render_dashboard(_settings(), _base_state())
        # The polling engine now lives in the external static script.
        assert "/static/dashboard.js" in html
        assert "location.reload()" not in html
        assert "Live — updating every 1 s" in html

    def test_title_displays_active_strategy_names_readonly(self):
        html = _render_dashboard(_settings(), _base_state())
        # The .env selection is the single source of truth: the title bar only
        # DISPLAYS the active open/stop/close names, read-only — there is no
        # dropdown and no runtime switching.
        assert 'class="strategy-name"' in html
        assert "open_donchian" in html
        assert "stop_support" in html
        # The close side shows the three per-zone selections (start/margin/profit).
        assert "hold/hold/trailing_ratchet" in html
        assert 'class="strategy-select"' not in html
        assert "switchStrategy(" not in html

    def test_degraded_mode_shows_connection_error_banner(self):
        # When IG login fails the web server still serves the dashboard; it must
        # explain the degraded state instead of crashing or rendering blank.
        state = {**_base_state(), "startup_error": "403 error.security.api-key-invalid"}
        html = _render_dashboard(_settings(), state)
        assert "conn-banner-error" in html
        assert "Not connected to IG" in html
        assert "api-key-invalid" in html

    def test_healthy_state_has_no_connection_banner(self):
        html = _render_dashboard(_settings(), _base_state())
        assert "conn-banner" not in html

    def test_engine_script_polls_fragments_endpoint(self):
        # The extracted JS engine still drives the live fragment polling.
        js = (
            Path(__file__).resolve().parents[1] / "src/web/static/dashboard.js"
        ).read_text()
        assert "/api/dashboard-fragments" in js
        assert "location.reload()" not in js

    def test_no_runtime_strategy_switching_ui(self):
        # There is no dashboard switching any more: no confirm modal, no switch
        # handlers in the JS. The selection is edited in .env only.
        html = _render_dashboard(_settings(), _base_state())
        assert "strategy-confirm-modal" not in html
        js = (
            Path(__file__).resolve().parents[1] / "src/web/static/dashboard.js"
        ).read_text()
        assert "openStrategyConfirmModal" not in js
        assert "switchStopDistance" not in js
        assert "switchStrategy" not in js
        assert "switchCloseProfile" not in js

    def test_dashboard_js_cache_version_is_current(self):
        # The script tag must point at the committed dashboard.js; bump the
        # ``?v=`` query whenever the file changes so browsers don't serve a
        # stale cached copy (the reason a JS change can appear to "not work").
        html = _render_dashboard(_settings(), _base_state())
        assert "/static/dashboard.js?v=20" in html

    def test_page_has_per_section_refresh_stamps(self):
        html = _render_dashboard(_settings(), _base_state())
        for stamp_id in (
            "refresh-kpi",
            "refresh-market",
            "refresh-queue",
            "refresh-positions",
        ):
            assert f'id="{stamp_id}"' in html

    def test_actions_section_renders_jobs_with_switches(self):
        html = _render_dashboard(_settings(), _base_state())
        frags = _build_fragments(_base_state())
        # Section renamed from "Manual Actions" to "Actions".
        assert "Manual Actions" not in html
        assert "> Actions<" in html
        # Each job becomes a card carrying a switch and its action key.
        assert 'data-action="collect_and_analyze"' in frags["actions"]
        assert 'data-action="end_of_day"' in frags["actions"]
        # The fragment container is present in the shell.
        assert 'id="frag-actions"' in html
        assert "toggleJobMode(" in html

    def test_actions_auto_job_is_checked_and_hides_run(self):
        frags = _build_fragments(_base_state())
        # The automatic job (collect_and_analyze) renders checked with a hidden
        # Run button; the manual danger job (end_of_day) shows a confirming Run.
        assert "checked" in frags["actions"]
        assert "display:none;" in frags["actions"]
        assert "runAction('end_of_day', this, true)" in frags["actions"]

    def test_actions_bulk_controls_present(self):
        html = _render_dashboard(_settings(), _base_state())
        assert "setAllJobs(true, this)" in html
        assert "setAllJobs(false, this)" in html


# ── /api/dashboard-fragments endpoint ────────────────────────────────────────


@pytest.fixture
def app():
    """Minimal app: real (empty) price buffer, no DB / scheduler / guard."""
    return create_app(settings=_settings(), buffer=PriceBuffer())


class TestFragmentsEndpoint:
    async def test_endpoint_returns_fragments_and_metadata(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard-fragments")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["fragments"]) == _FRAGMENT_KEYS
        assert "server_time" in data
        # No scheduler injected → bot unavailable.
        assert data["scheduler_available"] is False
        assert data["bot_paused"] is None

    async def test_server_time_is_hh_mm_ss(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get("/api/dashboard-fragments")).json()
        hh, mm, ss = data["server_time"].split(":")
        assert len(hh) == 2 and len(mm) == 2 and len(ss) == 2

    async def test_main_page_renders(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
        assert resp.status_code == 200
        assert 'id="frag-kpi_bar"' in resp.text

    async def test_runtime_strategy_switch_endpoints_are_gone(self, app):
        # Strategy selection is edited in .env only; the runtime-switch endpoints
        # were removed, so they must no longer be routed.
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path in (
                "/api/strategy/open_donchian",
                "/api/close-profile/close_zoneprofit",
                "/api/stop-distance/stop_support",
            ):
                resp = await client.post(path)
                assert resp.status_code == 404

    async def test_poll_never_blocks_on_a_busy_queue(self):
        """The fragments poll must not await external IG calls.

        Regression: the dashboard renders the queue view, so if its data
        gathering awaited a queued ``/accounts`` or ``/markets`` call, a busy or
        rate-limited queue (e.g. during a market scan) would stall the poll and
        freeze the whole UI. The poll must return promptly from cache and only
        *schedule* the refreshes in the background.
        """

        class _BlockingQueue:
            def __init__(self) -> None:
                self.calls = 0
                self._never = asyncio.Event()

            async def get(self, endpoint: str, **kwargs) -> dict:
                self.calls += 1
                await self._never.wait()  # simulates a stuck/rate-limited worker
                return {}

            # Read-only snapshots the dashboard reads from memory (never blocks).
            def stats(self):
                return None

            def recent(self):
                return []

            def pending_tasks(self):
                return []

            def errors(self):
                return []

        buffer = PriceBuffer()
        buffer.add_candle("IX.D.DAX.IFMM.IP", _candle(1000.0))
        queue = _BlockingQueue()
        app = create_app(settings=_settings(), buffer=buffer, api_queue=queue)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Would hang forever before the fix; wait_for makes the failure a
            # timeout rather than a hung test.
            resp = await asyncio.wait_for(
                client.get("/api/dashboard-fragments"), timeout=3.0
            )
        assert resp.status_code == 200

        # The poll scheduled the background refresh (it just didn't await it).
        await asyncio.sleep(0.05)
        assert queue.calls >= 1
        queue._never.set()  # let the background tasks unwind cleanly


# ── /api/positions/funds/{epic} endpoint (BUY hover) ─────────────────────────


def _candle(bid: float) -> "object":
    """Build a minimal candle whose close prices are ``bid`` (offer = bid + 1)."""
    from src.feed.price_buffer import Candle

    return Candle(
        timestamp=datetime(2026, 6, 9, 10, 0, tzinfo=UTC),
        bid_open=bid,
        bid_close=bid,
        bid_high=bid,
        bid_low=bid,
        offer_open=bid + 1,
        offer_close=bid + 1,
        offer_high=bid + 1,
        offer_low=bid + 1,
    )


class _FakeQueue:
    """Stand-in for APIQueue: returns a fixed ``/markets`` payload, counts calls."""

    def __init__(self, market_data: dict) -> None:
        self._market_data = market_data
        self.calls = 0

    async def get(self, endpoint: str, **kwargs) -> dict:
        self.calls += 1
        return self._market_data


_MARKET_DATA = {
    "instrument": {
        "marginFactor": "5",
        "marginFactorUnit": "PERCENTAGE",
        "contractSize": "1",
        "currencies": [{"code": "EUR", "exchangeRate": 1.0}],
    },
    "dealingRules": {"minDealSize": {"value": 1}},
    "snapshot": {"marketStatus": "TRADEABLE"},
}


def _funds_app(market_data: dict, balance: dict | None = None) -> object:
    epic = "IX.D.DAX.IFMM.IP"
    buffer = PriceBuffer()
    buffer.add_candle(epic, _candle(1000.0))
    queue = _FakeQueue(market_data)
    app = create_app(settings=_settings(), buffer=buffer, api_queue=queue)
    if balance is not None:
        app.state.account_balance = balance
    return app, queue, epic


class TestPositionFundsEndpoint:
    async def test_returns_margin_and_sufficient_verdict(self):
        app, _queue, epic = _funds_app(_MARKET_DATA, balance={"available": 1000.0})
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/positions/funds/{epic}")).json()
        # margin = euro_per_point(1) × price(1000) × 5% = 50.
        assert data["margin_eur"] == 50.0
        assert data["available_eur"] == 1000.0
        assert data["sufficient"] is True

    async def test_insufficient_when_balance_below_margin(self):
        app, _queue, epic = _funds_app(_MARKET_DATA, balance={"available": 10.0})
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/positions/funds/{epic}")).json()
        assert data["margin_eur"] == 50.0
        assert data["sufficient"] is False

    async def test_market_data_cached_across_hovers(self):
        app, queue, epic = _funds_app(_MARKET_DATA, balance={"available": 1000.0})
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get(f"/api/positions/funds/{epic}")
            await client.get(f"/api/positions/funds/{epic}")
        # Second hover is served from the per-epic cache — only one IG call.
        assert queue.calls == 1

    async def test_unknown_margin_factor_yields_null_margin(self):
        market = {
            **_MARKET_DATA,
            "instrument": {
                "contractSize": "1",
                "currencies": [{"code": "EUR", "exchangeRate": 1.0}],
            },
        }
        app, _queue, epic = _funds_app(market, balance={"available": 1000.0})
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/positions/funds/{epic}")).json()
        assert data["margin_eur"] is None
        assert data["sufficient"] is False

    async def test_no_price_data_returns_400(self):
        app, _queue, _epic = _funds_app(_MARKET_DATA)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/positions/funds/UNKNOWN.EPIC")
        assert resp.status_code == 400


# ── POST /api/positions/open/{epic} (manual force-open) ──────────────────────


class _NoopSession:
    """Async-context-manager session stub: only ``commit`` is exercised."""

    async def __aenter__(self) -> "_NoopSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        return None


async def _guarded_open(trading, intent, buf):
    """Faithful stand-in for ``BotScheduler.open_epic_guarded``.

    Mirrors the real method's contract — gate first, then open — so the manual
    route's delegation is exercised without spinning up a full scheduler. The
    per-epic lock itself is covered by the scheduler unit tests.
    """
    allowed, reason = await trading.can_open_intent(intent)
    if not allowed:
        return None, reason
    return await trading.open_from_intent(intent, buf), None


def _manual_app() -> object:
    """App wired for the manual-open path: queue, session, scheduler, one candle."""
    epic = "IX.D.DAX.IFMM.IP"
    buffer = PriceBuffer()
    buffer.add_candle(epic, _candle(1000.0))
    app = create_app(settings=_settings(), buffer=buffer, api_queue=_FakeQueue({}))
    app.state.session_factory = lambda: _NoopSession()
    # The active close profile is opaque here — the route only forwards it.
    # ``open_epic_guarded`` mirrors the real scheduler so the route can delegate.
    app.state.scheduler = SimpleNamespace(
        close_profile=object(), open_epic_guarded=_guarded_open
    )
    return app, epic


class TestManualOpenEndpoint:
    async def test_no_scheduler_returns_503(self):
        # Minimal app: no scheduler / session_factory injected.
        app = create_app(settings=_settings(), buffer=PriceBuffer())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/positions/open/IX.D.DAX.IFMM.IP")
        assert resp.status_code == 503

    async def test_no_price_data_returns_400(self):
        app, _epic = _manual_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/positions/open/UNKNOWN.EPIC")
        assert resp.status_code == 400

    async def test_portfolio_gate_refusal_returns_400(self, monkeypatch):
        app, epic = _manual_app()

        class _GatedTrading:
            def __init__(self, *a, **k) -> None:
                pass

            async def can_open_intent(self, intent):
                return False, "Epic already open"

            async def open_from_intent(self, intent, buf):  # pragma: no cover
                raise AssertionError("must not open when the gate refuses")

        monkeypatch.setattr(_ROUTER_MOD, "TradingService", _GatedTrading)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/positions/open/{epic}")
        assert resp.status_code == 400
        assert resp.json()["error"] == "Epic already open"

    async def test_happy_path_forces_buy_through_close_profile(self, monkeypatch):
        app, epic = _manual_app()
        seen = {}
        position = SimpleNamespace(
            level_open=1000.0, quantity=1, deal_id="DEAL1", reason_open="auto"
        )

        class _OkTrading:
            def __init__(self, client, session, config, close_profile=None) -> None:
                seen["close_profile"] = close_profile

            async def can_open_intent(self, intent):
                seen["direction"] = intent.direction
                seen["epic"] = intent.epic
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                return position

        monkeypatch.setattr(_ROUTER_MOD, "TradingService", _OkTrading)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/positions/open/{epic}")

        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "status": "opened",
            "deal_id": "DEAL1",
            "level": 1000.0,
            "quantity": 1,
        }
        # Forced BUY on the requested epic, driven by the scheduler's close profile.
        assert seen["direction"] == "BUY"
        assert seen["epic"] == epic
        assert seen["close_profile"] is app.state.scheduler.close_profile
        # The manual origin is tagged on the persisted position.
        assert position.reason_open == "manual"

    async def test_open_rejected_returns_400(self, monkeypatch):
        app, epic = _manual_app()

        class _RejectTrading:
            def __init__(self, *a, **k) -> None:
                pass

            async def can_open_intent(self, intent):
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                return None  # market closed / risk cap / IG rejection

        monkeypatch.setattr(_ROUTER_MOD, "TradingService", _RejectTrading)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/positions/open/{epic}")
        assert resp.status_code == 400
