"""Tests for the dashboard route — fragment rendering and live polling endpoint.

These cover the "unified Option A" live-update mechanism: a single
``/api/dashboard-fragments`` endpoint returns the HTML for every dynamic region,
which the client swaps in place every two seconds instead of reloading the page.
"""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.services.price_buffer import PriceBuffer
from src.web.app import create_app
from src.web.routes.dashboard import _build_fragments, _render_dashboard

# ── Fixtures / builders ─────────────────────────────────────────────────────

_FRAGMENT_KEYS = {
    "kpi_bar",
    "market_rows",
    "week_summary",
    "day_history",
    "queue_modal",
    "api_modal",
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
        strategy_hour_start=8,
        strategy_hour_end=20,
        strategy_hour_close=21,
        strategy_max_positions=3,
        strategy_max_trades_day=5,
        strategy_daily_loss_limit=100,
        strategy_daily_win_target=200,
        strategy_min_r2=0.7,
        strategy_min_score=5,
        strategy_stop_multiplier=2,
        strategy_target_multiplier=3,
        strategy_max_spread_ratio=0.1,
        strategy_close_target=0.8,
        web_port=8000,
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
                    "offer": 18002.0,
                    "spread": 2.0,
                    "candles": 30,
                    "high": 18100.0,
                    "low": 17900.0,
                }
            ]
        )
        rows = _build_fragments(state)["market_rows"]
        assert "IX.D.DAX.IFMM.IP" in rows
        assert "18000.0" in rows

    def test_no_bot_tile_in_kpi_bar(self):
        # The Bot pause/resume KPI tile was replaced by the Actions section.
        frags = _build_fragments(_base_state())
        assert 'id="kpi-bot"' not in frags["kpi_bar"]

    def test_open_positions_rendered_in_modal(self):
        pos = SimpleNamespace(
            id=1,
            time_open=time(10, 0, 0),
            epic="IX.D.DAX.IFMM.IP",
            epic_name="DAX",
            level_open=18000.0,
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
        assert "11:30:00" in modal  # close time
        assert "Manual" in modal  # open reason label
        assert "Target hit" in modal  # close reason label
        assert "No closed positions" not in modal

    def test_closed_positions_modal_empty_state(self):
        modal = _build_fragments(_base_state())["closed_positions_modal"]
        assert "No closed positions" in modal

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
        assert "BLOCKED" in frags["kpi_bar"]
        assert "Blocked since" in frags["api_modal"]


# ── Shell rendering ──────────────────────────────────────────────────────────


class TestRenderDashboard:
    """The full page embeds fragment containers and the polling engine."""

    def test_page_contains_fragment_containers(self):
        html = _render_dashboard(_settings(), _base_state())
        for key in _FRAGMENT_KEYS:
            assert f'id="frag-{key}"' in html

    def test_page_polls_instead_of_reloading(self):
        html = _render_dashboard(_settings(), _base_state())
        assert "/api/dashboard-fragments" in html
        assert "location.reload()" not in html
        assert "Live — updating every 2 s" in html

    def test_page_has_per_section_refresh_stamps(self):
        html = _render_dashboard(_settings(), _base_state())
        for stamp_id in (
            "refresh-kpi",
            "refresh-market",
            "refresh-queue",
            "refresh-api",
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
