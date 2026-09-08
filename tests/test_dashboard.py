"""Tests for the dashboard route — fragment rendering and live polling endpoint.

These cover the "unified Option A" live-update mechanism: a single
``/api/dashboard-fragments`` endpoint returns the HTML for every dynamic region,
which the client swaps in place every two seconds instead of reloading the page.
"""

import asyncio
import re
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import src.web.routes.dashboard.router  # noqa: F401  (ensure submodule imported)
from src.feed.price_buffer import PriceBuffer
from src.models.epic import Epic
from src.models.position import Position, PositionState
from src.web.app import create_app
from src.web.routes.dashboard import _build_fragments, _render_dashboard

# The dashboard package re-exports the ``router`` APIRouter, shadowing the
# submodule attribute, so reach the real module object via sys.modules to patch
# its globals (e.g. TradingService) in the manual-open tests.
_ROUTER_MOD = sys.modules["src.web.routes.dashboard.router"]

# ── Fixtures / builders ─────────────────────────────────────────────────────

_FRAGMENT_KEYS = {
    "title_bar",
    "kpi_bar",
    "market_rows",
    "day_history",
    "queue_modal",
    "epic_list_modal",
    "positions_modal",
    "closed_positions_modal",
    "epic_result_modal",
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
        "closed_epics": 0,
        "avg_pnl_per_epic": 0.0,
        "winning_epics": 0,
        "losing_epics": 0,
        "avg_epic_win": 0.0,
        "avg_epic_loss": 0.0,
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
        close_zonesecure="hold",
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
                "win_rate_today": 1.0,
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


def _closed(epic: str, name: str, pnl: float, **overrides) -> SimpleNamespace:
    """Closed position stand-in carrying the columns the per-epic stats read."""
    base = dict(
        epic=epic,
        epic_name=name,
        date=date(2026, 6, 8),
        time_open=time(10, 0, 0),
        time_close=time(11, 0, 0),
        direction="BUY",
        level_open=18000.0,
        level_close=18010.0,
        level_stop=17900.0,
        pip_spread=1.0,
        euro_per_point=1.0,
        quantity=1,
        euro=pnl,
        reason_open="auto",
        reason_close="loose" if pnl < 0 else "win",
        stop_history=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEpicDayPnls:
    """Per-epic daily netting feeding the "Avg / epic" tile."""

    def test_positions_are_netted_per_epic(self):
        from src.web.routes.dashboard.state import _epic_day_pnls

        totals = _epic_day_pnls(
            [
                _closed("DAX", "DAX", -4.0),
                _closed("DAX", "DAX", 10.0),
                _closed("CAC", "CAC", -20.0),
            ]
        )
        # Two trades on DAX count as ONE instrument result, netted.
        assert set(totals) == {"DAX", "CAC"}
        assert totals["DAX"] == pytest.approx(6.0)
        assert totals["CAC"] == pytest.approx(-20.0)


class TestLossExplanation:
    """The five loss archetypes surfaced next to a losing epic."""

    def test_no_explanation_for_a_winner(self):
        from src.web.routes.dashboard.state import _loss_explanation

        assert _loss_explanation(_closed("A", "A", 5.0), 5.0) == ""

    def test_forced_exit(self):
        from src.web.routes.dashboard.state import _loss_explanation

        pos = _closed("A", "A", -20.0, reason_close="end_of_day")
        assert "Forced exit" in _loss_explanation(pos, -20.0)

    def test_spread_only_loss(self):
        from src.web.routes.dashboard.state import _loss_explanation

        # Spread cost = 2 points × 3 €/point = 6 €; a 5 € loss never really moved.
        pos = _closed("A", "A", -5.0, pip_spread=2.0, euro_per_point=3.0)
        assert "Spread never earned back" in _loss_explanation(pos, -5.0)

    def test_gain_given_back_when_the_stop_ratcheted(self):
        from src.web.routes.dashboard.state import _loss_explanation

        pos = _closed(
            "A",
            "A",
            -20.0,
            stop_history=[
                {"t": "2026-06-08T10:00:00Z", "level": 17900.0},
                {"t": "2026-06-08T10:30:00Z", "level": 18010.0},
            ],
        )
        assert "Gain given back" in _loss_explanation(pos, -20.0)

    def test_stop_too_tight_when_stopped_out_immediately(self):
        from src.web.routes.dashboard.state import _loss_explanation

        pos = _closed("A", "A", -20.0, time_close=time(10, 1, 0))
        assert "Stop too tight" in _loss_explanation(pos, -20.0)

    def test_stop_too_tight_when_the_stop_sits_inside_the_spread(self):
        from src.web.routes.dashboard.state import _loss_explanation

        # Stop 1 point away with a 1-point spread: inside the instrument's noise.
        pos = _closed("A", "A", -20.0, level_stop=17999.0)
        assert "Stop too tight" in _loss_explanation(pos, -20.0)

    def test_market_reversed_is_the_default(self):
        from src.web.routes.dashboard.state import _loss_explanation

        pos = _closed("A", "A", -20.0)
        assert "Market reversed after the open" in _loss_explanation(pos, -20.0)


class TestEpicResultModal:
    """The per-epic result modal (opened from the "Avg / epic" tile)."""

    def test_rows_ordered_worst_first_with_explanations(self):
        state = _base_state(
            closed_positions=[
                _closed("IX.D.DAX.IFMM.IP", "DAX", 15.0),
                _closed("IX.D.CAC.IFMM.IP", "CAC", -25.0),
            ],
            kpis={
                **_base_kpis(),
                "closed_epics": 2,
                "avg_pnl_per_epic": -5.0,
                "winning_epics": 1,
                "losing_epics": 1,
                "avg_epic_win": 15.0,
                "avg_epic_loss": -25.0,
            },
        )
        modal = _build_fragments(state)["epic_result_modal"]
        assert modal.index("CAC") < modal.index("DAX")  # biggest loss first
        assert "-25.00€" in modal and "+15.00€" in modal
        assert "Market reversed after the open" in modal
        assert "openChartModal('IX.D.CAC.IFMM.IP', event)" in modal

    def test_one_row_per_open_close_cycle(self):
        # Two trades on the same epic stay two lines, each with its own result.
        state = _base_state(
            closed_positions=[
                _closed("IX.D.DAX.IFMM.IP", "DAX", 10.0, time_close=time(11, 0, 0)),
                _closed("IX.D.DAX.IFMM.IP", "DAX", -4.0, time_close=time(15, 0, 0)),
            ]
        )
        modal = _build_fragments(state)["epic_result_modal"]
        assert modal.count("openChartModal('IX.D.DAX.IFMM.IP', event)") == 2
        assert "-4.00€" in modal and "+10.00€" in modal
        # Close times (Paris) distinguish the two lines: 11:00 UTC -> 13:00 CEST.
        assert "13:00:00" in modal and "17:00:00" in modal
        # Aggregate-only columns are gone.
        assert "W / L" not in modal and "Trades" not in modal

    def test_empty_state(self):
        modal = _build_fragments(_base_state())["epic_result_modal"]
        assert "No epic closed today" in modal

    def test_tile_shows_average_per_epic_and_opens_the_modal(self):
        state = _base_state(
            kpis={
                **_base_kpis(),
                "closed_epics": 3,
                "avg_pnl_per_epic": -1.5,
                "winning_epics": 1,
                "losing_epics": 2,
                "avg_epic_win": 12.0,
                "avg_epic_loss": -7.75,
            }
        )
        kpi_bar = _build_fragments(state)["kpi_bar"]
        assert "Avg / epic" in kpi_bar
        assert "-1.50€" in kpi_bar
        assert "1 up +12.00€" in kpi_bar
        assert "2 down -7.75€" in kpi_bar
        assert "openEpicResultModal()" in kpi_bar

    def test_tile_empty_state_when_nothing_closed(self):
        kpi_bar = _build_fragments(_base_state())["kpi_bar"]
        assert "No epic closed today" in kpi_bar


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
        # Nothing queued on this trade.
        assert ov["euroTarget"] is None

    def test_armed_euro_target_reaches_the_chart(self):
        # The chart states the standing "close above X €" order, so it has to
        # travel with the trade overlay.
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=9,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=None,
            level_open=18000.0,
            euro=-12.0,
            close_when_euro_above=-20.0,
        )
        assert _trade_overlay(pos)["euroTarget"] == -20.0

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

    def test_short_overlay_mirrors_entry_breakeven_exit(self):
        # Regression (#16): for a SELL the entry sits on the bid (the sell fill =
        # level_open, NOT level_zero - spread), and break-even / margin / exit —
        # all offer-referenced (buy-to-close) — map one spread BELOW their stored
        # levels on the bid curve the chart draws.
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=20,
            direction="SELL",
            date=date(2026, 7, 2),
            time_open=time(16, 0, 0),
            time_close=time(16, 30, 0),
            level_open=1.10000,  # sell fill on the bid
            level_zero=1.10000,  # break-even reference (offer terms)
            level_margin=1.09900,
            level_stop=1.10120,
            level_follower=1.10100,
            level_win=0.0,
            level_close=1.09800,  # buy-to-close fill on the offer
            pip_spread=0.00020,
            euro=18.0,
        )
        ov = _trade_overlay(pos)
        assert ov["direction"] == "SELL"
        assert ov["openBid"] == pytest.approx(1.10000)
        assert ov["zero"] == pytest.approx(1.10000 - 0.00020)
        assert ov["margin"] == pytest.approx(1.09900 - 0.00020)
        assert ov["close"] == pytest.approx(1.09800 - 0.00020)

    def test_display_pnl_mirrors_for_short(self):
        # Regression (#15): a short that won (price fell) must show a positive
        # display P&L / win, not the long-signed negative.
        from src.web.routes.dashboard.state import _display_pnl

        pos = SimpleNamespace(
            direction="SELL",
            euro=None,  # force the level-based fallback
            level_open=1.10000,
            level_close=1.09000,  # price fell 0.01 -> a winning short
            euro_per_point=20000.0,
            quantity=1,
        )
        assert _display_pnl(pos) == pytest.approx(200.0)

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

    def test_broker_loose_line_uses_per_point_broker_level(self):
        """A ratchet point carrying the pushed broker level (a spread below the
        software follower) drives the Loose line; the follower line stays on the
        software stop and never leaks the internal ``broker`` key."""
        from src.web.routes.dashboard.router import _trade_overlay

        pos = SimpleNamespace(
            id=13,
            date=date(2026, 6, 8),
            time_open=time(10, 0, 0),
            time_close=time(11, 0, 0),
            level_open=460.0,
            level_zero=460.4,
            level_stop=458.3,  # initial broker (coincides with the open follower)
            level_follower=459.8,
            level_win=0.0,
            level_close=459.2,
            euro=-5.0,
            stop_history=[
                {"t": "2026-06-08T10:00:00+00:00", "level": 458.3},  # open seed
                # ratchet: follower 459.8, broker posted a spread below at 459.3.
                {"t": "2026-06-08T10:30:00+00:00", "level": 459.8, "broker": 459.3},
            ],
        )
        ov = _trade_overlay(pos)
        assert ov["stopsFollower"] == [
            {"t": "2026-06-08T10:00:00+00:00", "level": 458.3},
            {"t": "2026-06-08T10:30:00+00:00", "level": 459.8},
        ]
        assert ov["stopsLoose"] == [
            {"t": "2026-06-08T10:00:00+00:00", "level": 458.3},  # initial broker
            {"t": "2026-06-08T10:30:00+00:00", "level": 459.3},  # per-point broker
        ]

    def test_short_stop_lines_shift_onto_bid_curve(self):
        # Regression: for a SELL both protective stops are decided in OFFER terms
        # (the software backstop fires on the buy-to-close offer, and the trail is
        # computed from it), so on the bid curve the chart draws they must sit one
        # spread BELOW their stored levels — exactly like zero/margin/close. Before
        # the fix the follower (red) line was drawn a spread too high, so the bid
        # never appeared to reach it at the (correctly-firing) close.
        #
        # It also locks in that the Loose line's first point uses the SEEDED open
        # broker level (``stop_history[0]["broker"]``), not the mutated current
        # ``level_stop``: for a short the broker ratchets DOWN, so reading the final
        # ``level_stop`` at the open instant produced a spurious step UP.
        from src.web.routes.dashboard.router import _trade_overlay

        spread = 0.00030
        pos = SimpleNamespace(
            id=21,
            direction="SELL",
            date=date(2026, 7, 24),
            time_open=time(13, 5, 0),
            time_close=time(13, 46, 0),
            level_open=1.62780,
            level_zero=1.62780,
            level_margin=1.62750,
            # Broker ratcheted DOWN over the trade: final (current) level_stop is
            # the LOWEST, while the seeded open broker (in history[0]) is highest.
            level_stop=1.62890,
            level_follower=1.62810,
            level_win=0.0,
            level_close=1.62840,
            pip_spread=spread,
            euro=-24.14,
            stop_history=[
                # open seed: follower + broker both high (offer terms).
                {"t": "2026-07-24T13:05:00+00:00", "level": 1.63000, "broker": 1.63080},
                # ratchet down.
                {"t": "2026-07-24T13:20:00+00:00", "level": 1.62810, "broker": 1.62890},
            ],
        )
        ov = _trade_overlay(pos)
        assert ov["direction"] == "SELL"
        # Follower steps: raw offer levels minus one spread.
        assert ov["stopsFollower"] == [
            {
                "t": "2026-07-24T13:05:00+00:00",
                "level": pytest.approx(1.63000 - spread),
            },
            {
                "t": "2026-07-24T13:20:00+00:00",
                "level": pytest.approx(1.62810 - spread),
            },
        ]
        # Loose steps: the SEEDED broker at index 0 (not the final level_stop),
        # both shifted onto the bid curve. Index 0 must be ABOVE index 1 (the
        # broker ratchets down for a short) — never a step up.
        assert ov["stopsLoose"] == [
            {
                "t": "2026-07-24T13:05:00+00:00",
                "level": pytest.approx(1.63080 - spread),
            },
            {
                "t": "2026-07-24T13:20:00+00:00",
                "level": pytest.approx(1.62890 - spread),
            },
        ]
        assert ov["stopsLoose"][0]["level"] > ov["stopsLoose"][1]["level"]

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
        # The names come from the state snapshot (the title bar is a live
        # fragment now), sourced from .env in ``_selection_names``.
        state = _base_state(
            selection={
                "open_strategy": "open_donchian",
                "stop_strategy": "stop_support",
                "close_zones": "hold/hold/hold/trailing_ratchet",
            }
        )
        html = _render_dashboard(_settings(), state)
        # The .env selection is the single source of truth: the title bar only
        # DISPLAYS the active open/stop/close names, read-only — there is no
        # dropdown and no runtime switching.
        assert 'class="strategy-name"' in html
        assert "open_donchian" in html
        assert "stop_support" in html
        # The close side shows the four per-zone selections.
        assert "hold/hold/hold/trailing_ratchet" in html
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
        assert "/static/dashboard.js?v=44" in html

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

    def test_title_bar_allowed_state(self):
        html = _render_dashboard(_settings(), _base_state())
        bar = _build_fragments(_base_state())["title_bar"]
        # The fragment container must be in the shell so the poll can refresh it.
        assert 'id="frag-title_bar"' in html
        assert "autoopen-on" in bar
        # The state has no wording in the bar (no room): the lock icon + the bar
        # colour carry it and the tooltip spells it out.
        assert 'title="Openings allowed"' in bar
        assert ">Openings allowed<" not in bar
        # The button offers the opposite action (block), wired to the toggle.
        assert "Block for today" in bar
        assert "toggleAutoOpen(this)" in bar
        # The handler the markup calls must actually exist in the served script.
        js = (
            Path(__file__).resolve().parents[1] / "src/web/static/dashboard.js"
        ).read_text()
        assert "function toggleAutoOpen(" in js
        assert "/api/auto-open/" in js

    def test_title_bar_blocked_state(self):
        state = _base_state(auto_open_enabled=False)
        bar = _build_fragments(state)["title_bar"]
        assert "autoopen-off" in bar
        assert 'title="Openings blocked"' in bar
        assert "Allow openings" in bar

    def test_title_bar_keeps_both_day_buttons_on_one_row(self):
        # Both controls sit in the same nowrap group as the state icon, so they
        # stay on the bar's line instead of pushing a second row.
        bar = _build_fragments(_base_state())["title_bar"]
        actions = bar.split('class="dashboard-title-actions"')[1]
        assert "Block for today" in actions
        assert "End day" in actions

    def test_selection_names_are_not_rendered_as_buttons(self):
        # The .env selection is read-only: plain labels, never <button>/<select>
        # and no clickable affordance in the markup.
        state = _base_state(
            selection={
                "open_strategy": "open_ranking",
                "stop_strategy": "stop_atr",
                "close_zones": "hold/hold/hold/trailing_ratchet",
            }
        )
        bar = _build_fragments(state)["title_bar"]
        chips = bar.split('class="dashboard-title-actions"')[0]
        assert "<button" not in chips
        assert "<select" not in chips
        assert "onclick" not in chips
        assert 'class="dashboard-title-strategy"' in chips
        # The flat (non-pill) look is driven by CSS: no background/border there.
        css = (
            Path(__file__).resolve().parents[1] / "src/web/static/style.css"
        ).read_text()
        block = css.split(".dashboard-title-strategy,")[1].split("}")[0]
        assert "background: none;" in block
        assert "border: none;" in block
        assert "cursor: default;" in block

    def test_title_bar_merges_identity_and_selection(self):
        # The bot title and the active .env selection now live in the same bar as
        # the auto-open switch — there is no separate title block any more.
        state = _base_state(
            selection={
                "open_strategy": "open_ranking",
                "stop_strategy": "stop_atr",
                "close_zones": "hold/hold/hold/trailing_ratchet",
            }
        )
        bar = _build_fragments(state)["title_bar"]
        assert "IG Trading Bot" in bar
        assert "open_ranking" in bar
        assert "stop_atr" in bar
        assert "hold/hold/hold/trailing_ratchet" in bar
        html = _render_dashboard(_settings(), state)
        assert html.count("IG Trading Bot</h1>") == 1

    def test_title_bar_carries_end_day_button(self):
        bar = _build_fragments(_base_state())["title_bar"]
        assert "End day" in bar
        assert "endDay(this)" in bar
        js = (
            Path(__file__).resolve().parents[1] / "src/web/static/dashboard.js"
        ).read_text()
        assert "async function endDay(" in js
        assert "/api/end-day" in js

    def test_python_commands_section_removed(self):
        html = _render_dashboard(_settings(), _base_state())
        assert "Python Commands" not in html
        assert "python -m src.main" not in html
        assert 'data-sid="commands"' not in html


# ── Market-table sort persistence & chart modal chrome ───────────────────────


def _static(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "src/web/static" / name).read_text()


def _js_function(name: str, script: str = "dashboard.js") -> str:
    """The source of one top-level JS function, so a test can assert on *its*
    body instead of on the whole file (where a string can drift in from an
    unrelated helper)."""
    js = _static(script)
    start = js.index(f"function {name}(")
    end = js.index("\n}", start)
    return js[start:end]


class TestSortPersistence:
    """A live poll must not throw away the column the user sorted on.

    The market table re-renders every second (server order); before this the
    chosen sort survived less than a second. The sort state is kept on the
    ``<table>`` element — which the poll never replaces, it only swaps the
    ``<tbody>`` markup — and re-applied after each swap.
    """

    def test_tables_js_exposes_the_reapply_hook(self):
        js = _static("tables.js")
        assert "window.reapplyTableSorts = reapplyTableSorts" in js
        # Re-applying keeps the direction instead of toggling it.
        assert "keepDirection" in js

    def test_engine_reapplies_sorts_after_a_fragment_swap(self):
        js = _static("dashboard.js")
        assert "window.reapplyTableSorts()" in js

    def test_page_loads_the_current_tables_script(self):
        # Bump ``?v=`` whenever tables.js changes or browsers serve a stale copy.
        html = _render_dashboard(_settings(), _base_state())
        assert "/static/tables.js?v=2" in html

    def test_chart_carousel_follows_the_visible_row_order(self):
        # The carousel snapshots the source table in DOM order, so with the sort
        # preserved the ‹ › arrows walk the epics in the user's chosen order.
        js = _static("dashboard.js")
        assert "tr.clickable-row" in js


class TestChartModalChrome:
    """The chart modal names the market, states its shape, and can always close."""

    def test_spread_badge_is_rendered(self):
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-modal-spread"' in html

    def test_spread_badge_is_filled_from_the_chart_payload(self):
        js = _static("dashboard.js")
        assert "_renderChartSpread(data.spread)" in js

    def test_spread_badge_states_only_the_euro_cost(self):
        # The raw offer − bid point distance says nothing across instruments on
        # its own; the badge states the euro cost of a round trip and nothing
        # else (the point distance is only used by the noise dial below).
        body = _js_function("_renderChartSpread")
        assert "euro" in body
        assert "points" not in body

    def test_title_is_the_market_name_not_the_epic(self):
        # The heading names the instrument the user recognises; the epic
        # identifier is only the placeholder until the payload lands.
        js = _static("dashboard.js")
        assert "_setChartTitle(data.name || epic)" in js

    def test_epic_identifier_is_a_discreet_footnote(self):
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-modal-epic"' in html
        js = _static("dashboard.js")
        assert "_setChartEpicLabel(epic)" in js

    def test_shape_badge_is_filled_from_the_chart_payload(self):
        # Regime + one trend arrow per horizon, at the top of the chart.
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-modal-shape"' in html
        js = _static("dashboard.js")
        assert "_renderChartShape(data.shape)" in js

    def test_close_button_starts_hidden(self):
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-close-btn"' in html
        assert "closeChartPosition(this)" in html

    def test_close_button_shows_on_a_losing_trade_too(self):
        # Cutting a loss by hand is as legitimate as banking a gain: the button
        # now depends on the trade being OPEN, not on the sign of its P&L.
        body = _js_function("_updateChartCloseControls")
        assert "pnl <= 0" not in body
        assert "t.state === 'open'" in body

    def test_trend_arrows_shrink_from_the_newest_horizon(self):
        # Most recent first (5 → 60 min) and biggest first: what the market is
        # doing now weighs more than what it did over the last hour.
        js = _static("dashboard.js")
        sizes = re.search(r"_TREND_SIZES = \[([^\]]+)\]", js).group(1)
        values = [float(v) for v in sizes.split(",")]
        assert len(values) == 4
        assert values == sorted(values, reverse=True)
        body = _js_function("_renderChartShape")
        assert "(a.minutes || 0) - (b.minutes || 0)" in body

    def test_noise_dial_compares_the_noise_band_with_the_spread(self):
        # Two concentric discs on one scale: is the tick noise a stop has to
        # survive bigger than what a round trip costs?
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-modal-noise"' in html
        js = _static("dashboard.js")
        assert "_renderChartNoise(candles[candles.length - 1], data.spread)" in js
        body = _js_function("_renderChartNoise")
        assert "candle.noise" in body
        assert "spread.points" in body

    def test_queued_close_buttons_are_rendered(self):
        # "As soon as it is positive" and "as soon as it is above x €" are the
        # same order with a different threshold.
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-arm-zero-btn"' in html
        assert "armChartEuroTarget(this, 0)" in html
        assert 'id="chart-arm-custom-btn"' in html
        assert "armChartEuroTargetPrompt(this)" in html

    def test_armed_order_is_shown_with_a_cancel(self):
        html = _render_dashboard(_settings(), _base_state())
        assert 'id="chart-modal-armed"' in html
        body = _js_function("_renderChartArmed")
        assert "trade.euroTarget" in body
        assert "disarmChartEuroTarget(this)" in body

    def test_close_from_the_chart_tolerates_having_no_table_row(self):
        # closePosition() is shared with the positions table, where it dims the
        # row; called from the modal header there is no row to dim.
        js = _static("dashboard.js")
        assert "if (row) row.style.opacity" in js


# ── /api/dashboard-fragments endpoint ────────────────────────────────────────


@pytest.fixture
def app():
    """Minimal app: real (empty) price buffer, no DB / scheduler / guard."""
    return create_app(settings=_settings(), buffer=PriceBuffer())


class TestChartEndpoint:
    async def test_chart_payload_carries_the_dealing_spread(self, app):
        # The chart states what a round trip on the market costs, read from the
        # background-refreshed spread cache (never awaited here).
        app.state.spread_cost_cache = {
            "IX.D.DAX.IFMM.IP": (datetime.now(UTC), 2.5, 1.25)
        }
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get("/api/chart/IX.D.DAX.IFMM.IP")).json()
        assert data["spread"] == {"euro": 2.5, "points": 1.25}

    async def test_chart_payload_spread_is_none_when_uncached(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get("/api/chart/CS.D.EURUSD.MINI.IP")).json()
        assert data["spread"] is None

    async def test_chart_payload_names_the_instrument(self):
        # The chart titles itself with the epic's description (the market table's
        # "Name"), so the payload has to carry it.
        epic = "IX.D.DAX.IFMM.IP"
        buffer = PriceBuffer()
        buffer.add_candle(epic, _candle(1000.0))
        app = create_app(settings=_settings(), buffer=buffer)
        app.state.session_factory = lambda: _ChartSession("Germany 40 Cash")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/chart/{epic}")).json()
        assert data["name"] == "Germany 40 Cash"

    async def test_chart_payload_carries_the_market_shape(self):
        # Regime + the four standard trend horizons, in increasing order, read
        # from the in-memory buffer (no IG call).
        epic = "IX.D.DAX.IFMM.IP"
        buffer = PriceBuffer()
        buffer.add_candle(epic, _candle(1000.0))
        app = create_app(settings=_settings(), buffer=buffer)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/chart/{epic}")).json()
        shape = data["shape"]
        assert shape["regime"]
        assert [t["minutes"] for t in shape["trends"]] == [5, 15, 30, 60]
        assert all(t["direction"] in {"UP", "DOWN", "FLAT"} for t in shape["trends"])

    async def test_chart_payload_shape_is_none_without_history(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get("/api/chart/CS.D.EURUSD.MINI.IP")).json()
        assert data["shape"] is None


class TestEuroTargetEndpoint:
    """POST /api/positions/euro-target/{id} arms and disarms the queued close."""

    def _app(self, position: Position):
        app = create_app(settings=_settings(), buffer=PriceBuffer())
        app.state.api_queue = _FakeQueue({})
        app.state.session_factory = lambda: _PositionSession(position)
        return app

    def _open_position(self) -> Position:
        return Position(
            id=3,
            epic="IX.D.DAX.IFMM.IP",
            epic_name="DAX",
            date=date(2026, 6, 9),
            state=PositionState.OPEN,
            direction="BUY",
        )

    async def test_arming_stores_the_threshold(self):
        position = self._open_position()
        transport = ASGITransport(app=self._app(position))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/api/positions/euro-target/3", json={"euro": -20})
        assert res.status_code == 200
        assert res.json()["euro"] == -20.0
        assert float(position.close_when_euro_above) == -20.0

    async def test_zero_threshold_means_as_soon_as_positive(self):
        position = self._open_position()
        transport = ASGITransport(app=self._app(position))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/api/positions/euro-target/3", json={"euro": 0})
        assert res.status_code == 200
        assert float(position.close_when_euro_above) == 0.0

    async def test_null_disarms(self):
        position = self._open_position()
        position.close_when_euro_above = Decimal("-20")
        transport = ASGITransport(app=self._app(position))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/api/positions/euro-target/3", json={"euro": None})
        assert res.status_code == 200
        assert position.close_when_euro_above is None

    async def test_a_missing_threshold_is_rejected(self):
        position = self._open_position()
        transport = ASGITransport(app=self._app(position))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/api/positions/euro-target/3", json={})
        assert res.status_code == 400
        assert position.close_when_euro_above is None

    async def test_a_closed_position_cannot_be_armed(self):
        position = self._open_position()
        position.state = PositionState.CLOSE
        transport = ASGITransport(app=self._app(position))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/api/positions/euro-target/3", json={"euro": 0})
        assert res.status_code == 400
        assert position.close_when_euro_above is None


class _PositionSession:
    """Session stub returning one position row from ``session.get``."""

    def __init__(self, position: Position) -> None:
        self._position = position
        self.committed = False

    async def __aenter__(self) -> "_PositionSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, _model, _pk):
        return self._position

    async def commit(self) -> None:
        self.committed = True


class _ChartSession:
    """Session stub for /api/chart: no trade rows, one enriched epic row."""

    def __init__(self, description: str | None) -> None:
        self._description = description

    async def __aenter__(self) -> "_ChartSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: [])

    async def scalar(self, _statement):
        return Epic(name="IX.D.DAX.IFMM.IP", description=self._description)


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


async def _guarded_open(
    trading, intent, buf, *, allow_short=False, allow_reopen=False, manual=False
):
    """Faithful stand-in for ``BotScheduler.open_epic_guarded``.

    Mirrors the real method's contract — gate first, then open — so the manual
    route's delegation is exercised without spinning up a full scheduler. The
    per-epic lock itself is covered by the scheduler unit tests. ``manual`` is
    accepted (and ignored) exactly as the real method ignores it once past the
    auto-open switch, which the scheduler unit tests cover.
    """
    allowed, reason = await trading.can_open_intent(
        intent, allow_short=allow_short, allow_reopen=allow_reopen
    )
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


class TestAutoOpenEndpoint:
    """``POST /api/auto-open/{on,off}`` — the big dashboard switch."""

    def _app_with_switch(self):
        """App whose scheduler records the auto-open calls it receives."""
        app = create_app(settings=_settings(), buffer=PriceBuffer())
        calls: list[bool] = []

        async def _set(enabled: bool) -> None:
            calls.append(enabled)

        app.state.scheduler = SimpleNamespace(
            set_auto_open=_set, auto_open_enabled=True
        )
        return app, calls

    async def test_off_blocks_and_on_allows(self):
        app, calls = self._app_with_switch()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            off = await client.post("/api/auto-open/off")
            on = await client.post("/api/auto-open/on")
        assert off.status_code == 200 and off.json() == {"auto_open": False}
        assert on.status_code == 200 and on.json() == {"auto_open": True}
        assert calls == [False, True]

    async def test_unknown_mode_returns_400(self):
        app, calls = self._app_with_switch()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/auto-open/maybe")
        assert resp.status_code == 400
        assert calls == []

    async def test_no_scheduler_returns_503(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/auto-open/off")
        assert resp.status_code == 503

    async def test_state_is_exposed_to_the_poll_and_status(self):
        app, _ = self._app_with_switch()
        app.state.scheduler = SimpleNamespace(
            set_auto_open=AsyncMock(),
            auto_open_enabled=False,
            is_paused=True,
            all_epics=[],
            epic_last_refresh=None,
            tradable_epics=[],
            tradable_last_refresh=None,
            positions_synced_at=None,
            jobs_status=lambda: [],
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            frags = (await client.get("/api/dashboard-fragments")).json()
            status = (await client.get("/api/status")).json()
        assert frags["auto_open"] is False
        assert "autoopen-off" in frags["fragments"]["title_bar"]
        assert status["auto_open"] is False


class TestEndDayEndpoint:
    """``POST /api/end-day`` — close the whole book and stop opening today."""

    def _app_with_end_day(self, *, closed: int = 3, boom: bool = False):
        app = create_app(settings=_settings(), buffer=PriceBuffer())
        calls: list[str] = []

        async def _end_day() -> int:
            calls.append("end_day")
            if boom:
                raise RuntimeError("IG unreachable")
            return closed

        app.state.scheduler = SimpleNamespace(end_day=_end_day, auto_open_enabled=True)
        return app, calls

    async def test_closes_positions_and_blocks_openings(self):
        app, calls = self._app_with_end_day(closed=2)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/end-day")
        assert resp.status_code == 200
        assert resp.json() == {"closed": 2, "auto_open": False}
        assert calls == ["end_day"]

    async def test_failure_returns_500(self):
        app, _ = self._app_with_end_day(boom=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/end-day")
        assert resp.status_code == 500
        assert "IG unreachable" in resp.json()["error"]

    async def test_no_scheduler_returns_503(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/end-day")
        assert resp.status_code == 503


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

            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
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

            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
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
            "direction": "BUY",
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

            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                return None  # market closed / risk cap / IG rejection

        monkeypatch.setattr(_ROUTER_MOD, "TradingService", _RejectTrading)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/positions/open/{epic}")
        assert resp.status_code == 400

    async def test_manual_sell_passes_direction_and_allow_short(self, monkeypatch):
        # A dashboard SELL forwards direction="SELL" and lifts the long-only gate
        # via allow_short=True; the persisted origin stays "manual".
        app, epic = _manual_app()
        seen = {}
        position = SimpleNamespace(
            level_open=1000.0, quantity=1, deal_id="DEALS", reason_open="auto"
        )

        class _ShortTrading:
            def __init__(self, client, session, config, close_profile=None) -> None:
                pass

            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
                seen["direction"] = intent.direction
                seen["allow_short"] = allow_short
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                return position

        monkeypatch.setattr(_ROUTER_MOD, "TradingService", _ShortTrading)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/positions/open/{epic}", json={"direction": "SELL"}
            )

        assert resp.status_code == 200
        assert resp.json()["direction"] == "SELL"
        assert seen["direction"] == "SELL"
        assert seen["allow_short"] is True
        assert position.reason_open == "manual"

    async def test_manual_open_invalid_direction_returns_400(self, monkeypatch):
        app, epic = _manual_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/positions/open/{epic}", json={"direction": "HOLD"}
            )
        assert resp.status_code == 400


# ── Market reading panel (/api/reading/{epic} + its triggers) ─────────────────


class _ReadingSession:
    """Session stub whose ``scalars`` returns a fixed list of rows."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def __aenter__(self) -> "_ReadingSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def scalars(self, _query):
        return SimpleNamespace(first=lambda: self._rows[0] if self._rows else None)


def _reading_app(bids: list[float], rows: list | None = None) -> tuple[object, str]:
    """App with one warmed-up epic in the buffer (and optionally an open row)."""
    epic = "IX.D.DAX.IFMM.IP"
    buffer = PriceBuffer()
    for bid in bids:
        buffer.add_candle(epic, _candle(bid))
    app = create_app(settings=_settings(), buffer=buffer)
    if rows is not None:
        app.state.session_factory = lambda: _ReadingSession(rows)
    return app, epic


class TestReadingEndpoint:
    """``GET /api/reading/{epic}`` — the panel's only data source."""

    async def test_untracked_epic_is_a_404(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/reading/CS.D.EURUSD.CFD.IP")
        assert res.status_code == 404

    async def test_payload_carries_the_headline_and_sections(self):
        app, epic = _reading_app([18000.0 + i for i in range(40)])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/reading/{epic}")).json()
        assert data["epic"] == epic
        assert data["minutes"] == 40
        assert [chip["label"] for chip in data["headline"]][0] == "Regime"
        titles = [section["title"] for section in data["sections"]]
        assert "Trend per horizon" in titles
        assert "Readable levels" in titles

    async def test_spread_is_read_from_the_cache_not_the_api(self):
        app, epic = _reading_app([18000.0] * 40)
        app.state.spread_cost_cache = {epic: (datetime.now(UTC), 2.5, 1.25)}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/reading/{epic}")).json()
        momentum = next(
            section for section in data["sections"] if section["title"] == "Momentum"
        )
        spread = next(row for row in momentum["rows"] if row["label"] == "Spread")
        assert "2.50 €" in spread["detail"]

    async def test_an_open_position_adds_the_since_open_section(self):
        position = SimpleNamespace(
            epic="IX.D.DAX.IFMM.IP",
            epic_name="Germany 40",
            direction="BUY",
            date=datetime(2026, 6, 9, tzinfo=UTC).date(),
            time_open=time(9, 55),
            level_open=18000.0,
            level_zero=18001.0,
            level_margin=18010.0,
            level_follower=17990.0,
            euro_per_point=1.0,
        )
        app, epic = _reading_app([18000.0 + i for i in range(40)], rows=[position])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get(f"/api/reading/{epic}")).json()
        assert data["sections"][0]["title"] == "Open position (BUY)"
        assert data["name"] == "Germany 40"


class TestReadingPanelChrome:
    """The panel must be reachable from the dashboard and from a chart."""

    def test_page_loads_the_reading_script(self):
        html = _render_dashboard(_settings(), _base_state())
        assert "/static/reading.js?v=1" in html

    def test_chart_modal_header_offers_the_reading_button(self):
        html = _render_dashboard(_settings(), _base_state())
        assert "openReadingModal(_chartModalEpic, event)" in html

    def test_market_rows_offer_a_reading_button_per_epic(self):
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
        assert "openReadingModal('IX.D.DAX.IFMM.IP', event)" in rows

    def test_open_position_rows_offer_a_reading_button(self):
        position = SimpleNamespace(
            id=7,
            epic="IX.D.DAX.IFMM.IP",
            epic_name="Germany 40",
            date=date(2026, 6, 9),
            time_open=time(9, 55),
            level_open=18000.0,
            level_win=18100.0,
            level_stop=17950.0,
            quantity=1,
            euro=12.5,
            strategy=None,
        )
        frags = _build_fragments(_base_state(open_positions=[position]))
        assert "openReadingModal('IX.D.DAX.IFMM.IP', event)" in frags["positions_modal"]

    def test_script_reads_the_api_and_refreshes_itself(self):
        js = _static("reading.js")
        assert "'/api/reading/' + encodeURIComponent(epic)" in js
        assert "READING_REFRESH_MS" in js
        # A late answer for a previous epic must not paint over the current one.
        assert "if (_readingEpic !== epic) return;" in js

    def test_script_escapes_every_value_it_injects(self):
        js = _static("reading.js")
        assert "div.textContent" in js


class TestCatalogEndpoint:
    """The static market catalog: progress read-out and manual crawl trigger."""

    @staticmethod
    def _progress(**overrides):
        """A CatalogProgress-shaped snapshot the route can serialise."""
        from src.markets.catalog_crawler import CatalogProgress

        values = {
            "running": False,
            "nodes_total": 120,
            "nodes_pending": 8,
            "nodes_done": 110,
            "nodes_error": 2,
            "markets": 4211,
            "nodes_visited": 30,
            "markets_added": 400,
            "started_at": datetime(2026, 9, 8, 21, 0, tzinfo=UTC),
            "finished_at": None,
            "last_error": "quota",
        }
        values.update(overrides)
        return CatalogProgress(**values)

    async def test_reports_catalog_and_frontier_counts(self, app):
        app.state.scheduler = SimpleNamespace(
            catalog_progress=AsyncMock(return_value=self._progress())
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/catalog")
        assert resp.status_code == 200
        data = resp.json()
        assert data["markets"] == 4211
        assert data["nodes_pending"] == 8
        # 8 nodes still pending → the crawl was interrupted, not finished.
        assert data["complete"] is False
        assert data["started_at"].startswith("2026-09-08T21:00")

    async def test_complete_when_the_frontier_is_drained(self, app):
        app.state.scheduler = SimpleNamespace(
            catalog_progress=AsyncMock(
                return_value=self._progress(nodes_pending=0, nodes_done=118)
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            data = (await client.get("/api/catalog")).json()
        assert data["complete"] is True

    async def test_no_scheduler_returns_503(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/catalog")
        assert resp.status_code == 503

    async def test_crawl_is_a_registered_manual_action(self, app):
        triggered = asyncio.Event()

        async def _crawl() -> None:
            triggered.set()

        app.state.scheduler = SimpleNamespace(trigger_crawl_market_catalog=_crawl)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/actions/crawl_market_catalog")
        assert resp.status_code == 200
        await asyncio.wait_for(triggered.wait(), timeout=1)

    def test_action_card_is_manual_only(self):
        """No cron behind the crawl → a Run button, no auto/manual switch."""
        from src.core.scheduler import JOB_DEFINITIONS

        entry = next(
            e for e in JOB_DEFINITIONS if e["action"] == "crawl_market_catalog"
        )
        assert entry["toggleable"] is False
        frags = _build_fragments(
            _base_state(
                jobs=[
                    {
                        "action": "crawl_market_catalog",
                        "name": entry["name"],
                        "description": entry["description"],
                        "schedule": entry["schedule"],
                        "danger": entry["danger"],
                        "auto": False,
                        "toggleable": False,
                    }
                ]
            )
        )
        assert "runAction('crawl_market_catalog'" in frags["actions"]
        assert "toggleJobMode('crawl_market_catalog'" not in frags["actions"]
