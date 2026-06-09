"""Dashboard route — main overview page."""

import asyncio
import html
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from src.models.day import Day
from src.models.epic import Epic
from src.models.position import Position, PositionState, PositionStrategy
from src.models.resume import Resume
from src.services.api_error_log import APIErrorEntry
from src.services.api_queue import Priority
from src.services.market_scanner import MarketInfo
from src.utils.tools import euro_per_point

logger = logging.getLogger(__name__)

_PARIS = ZoneInfo("Europe/Paris")

router = APIRouter()


def _kpi_freshness(last_refresh: datetime | None) -> tuple[str, str]:
    """Return (color, short label) describing how fresh a refresh timestamp is.

    Green when refreshed today, amber when stale (earlier day), red when never.
    """
    if last_refresh is None:
        return "#ef4444", "Not refreshed"
    local_refresh = last_refresh.astimezone(_PARIS)
    if local_refresh.date() == date.today():
        return "#4ade80", f"Today {local_refresh.strftime('%H:%M')}"
    return "#f59e0b", local_refresh.strftime("%d/%m %H:%M")


def _open_reason_label(reason: str | None) -> tuple[str, str]:
    """Map a stored ``reason_open`` value to a (label, color) for display."""
    mapping = {
        "manual": ("Manual", "#60a5fa"),
        "auto": ("Script / Job", "#a78bfa"),
    }
    if reason in mapping:
        return mapping[reason]
    if reason:
        return reason.replace("_", " ").title(), "#94a3b8"
    return "—", "#64748b"


def _close_reason_label(reason: str | None) -> tuple[str, str]:
    """Map a stored ``reason_close`` value to a (label, color) for display."""
    mapping = {
        "win": ("Target hit", "#4ade80"),
        "loose": ("Stop loss", "#ef4444"),
        "follower": ("Trailing stop", "#f59e0b"),
        "zero": ("Break-even", "#94a3b8"),
        "now": ("Forced close", "#f59e0b"),
        "end_of_day": ("End of day", "#60a5fa"),
        "manual": ("Manual", "#60a5fa"),
        "closed_externally": ("IG / external", "#a78bfa"),
        "not_found_in_ig": ("Not found (IG)", "#94a3b8"),
    }
    if reason in mapping:
        return mapping[reason]
    if reason:
        return reason.replace("_", " ").title(), "#94a3b8"
    return "—", "#64748b"


def _display_pnl(position: Position) -> float:
    """Return the P&L to display for a closed position.

    Mirrors the KPI computation: use the stored ``euro`` when it is non-zero,
    otherwise recompute from open/close levels (fallback for pre-fix positions).
    """
    stored = float(position.euro) if position.euro is not None else None
    if stored is not None and abs(stored) >= 0.001:
        return stored
    if position.level_open and position.level_close:
        move = float(position.level_close) - float(position.level_open)
        if position.euro_per_point is not None and float(position.euro_per_point) != 0:
            return move * float(position.euro_per_point)
        return move * (position.quantity or 1)
    return stored or 0.0


async def _gather_dashboard_state(request: Request) -> dict:
    """Collect every piece of live state the dashboard renders.

    Shared by the full-page route (:func:`dashboard`) and the live polling
    endpoint (:func:`api_dashboard_fragments`) so both always render from an
    identical snapshot.
    """
    buffer = request.app.state.buffer
    session_factory = request.app.state.session_factory
    scheduler = request.app.state.scheduler
    epics = buffer.tracked_epics

    # Epic list KPI — from scheduler in-memory list
    all_epics = scheduler.all_epics if scheduler else []
    epic_last_refresh: datetime | None = (
        scheduler.epic_last_refresh if scheduler else None
    )
    today = date.today()
    epic_kpi_color, refresh_label = _kpi_freshness(epic_last_refresh)

    # Tradable epic list KPI — open/TRADEABLE subset, refreshed hourly
    tradable_epics = scheduler.tradable_epics if scheduler else []
    tradable_last_refresh: datetime | None = (
        scheduler.tradable_last_refresh if scheduler else None
    )
    tradable_kpi_color, tradable_refresh_label = _kpi_freshness(tradable_last_refresh)

    # Build market data summary
    market_summary = []
    for epic in epics:
        buf = buffer.get(epic)
        if buf and buf.last:
            market_summary.append(
                {
                    "epic": epic,
                    "bid": buf.last.bid_close,
                    "offer": buf.last.offer_close,
                    "spread": buf.last.spread,
                    "candles": len(buf),
                    "high": max(buf.bid_closes) if buf.bid_closes else 0,
                    "low": min(buf.bid_closes) if buf.bid_closes else 0,
                }
            )

    # Fetch database statistics
    kpis: dict = {}
    open_positions: list[Position] = []
    closed_positions: list[Position] = []
    day_records: list[Day] = []
    resume_records: list[Resume] = []
    if session_factory:
        async with session_factory() as session:
            # Available epics for trading (tracked ones)
            kpis["available_epics"] = len(epics)

            # Today's positions
            open_pos = await session.scalars(
                select(Position).where(
                    Position.date == today, Position.state == PositionState.OPEN
                )
            )
            open_positions = list(open_pos)

            closed_pos = await session.scalars(
                select(Position)
                .where(Position.date == today, Position.state == PositionState.CLOSE)
                .order_by(Position.time_close.desc())
            )
            closed_positions = list(closed_pos)

            kpis["open_trades"] = len(open_positions)
            # Compute live unrealized PnL. Prefer the in-memory buffer price (fresh
            # to the second for streamed epics); fall back to the stored ``euro``,
            # which the sync_positions job refreshes from IG every 20s — this is the
            # only source for manually-opened epics not in the streaming set.
            live_open_pnl = 0.0
            for p in open_positions:
                if not p.level_open:
                    if p.euro is not None:
                        live_open_pnl += float(p.euro)
                    continue
                buf_entry = buffer.get(p.epic)
                if buf_entry and buf_entry.last:
                    move = buf_entry.last.bid_close - float(p.level_open)
                    if p.euro_per_point is not None and float(p.euro_per_point) != 0:
                        # Currency-converted euro value of one point of movement.
                        live_open_pnl += move * float(p.euro_per_point)
                    elif p.euro_stop and p.size and float(p.size) > 0:
                        live_open_pnl += move * float(p.euro_stop) / float(p.size)
                    else:
                        # Last-resort fallback: raw points × quantity.
                        live_open_pnl += move * (p.quantity or 1)
                elif p.euro is not None:
                    # No live buffer price — use the value synced from IG.
                    live_open_pnl += float(p.euro)
            kpis["open_pnl"] = live_open_pnl

            kpis["closed_trades"] = len(closed_positions)
            # Compute display PnL: use stored euro when non-zero,
            # else recompute from price levels (fallback for pre-fix positions)
            display_pnl = 0.0
            wins = 0
            losses = 0
            for p in closed_positions:
                stored = float(p.euro) if p.euro is not None else None
                if stored is not None and abs(stored) >= 0.001:
                    pnl_val = stored
                elif p.level_open and p.level_close:
                    move = float(p.level_close) - float(p.level_open)
                    if p.euro_per_point is not None and float(p.euro_per_point) != 0:
                        pnl_val = move * float(p.euro_per_point)
                    else:
                        pnl_val = move * (p.quantity or 1)
                else:
                    pnl_val = stored or 0.0
                display_pnl += pnl_val
                if pnl_val > 0:
                    wins += 1
                elif pnl_val < 0:
                    losses += 1
            kpis["daily_pnl"] = display_pnl
            # Today's win/loss split (shown in the Closed Positions modal).
            kpis["wins"] = wins
            kpis["losses"] = losses
            kpis["win_rate_today"] = (
                wins / len(closed_positions) if closed_positions else 0.0
            )

            # Win rate KPI is computed over ALL closed positions (whole history),
            # not just today, so it reflects the full track record. The "CLOSED"
            # tile and the closed modal stay scoped to today.
            all_closed = await session.scalars(
                select(Position).where(Position.state == PositionState.CLOSE)
            )
            total_wins = 0
            total_losses = 0
            total_closed = 0
            for p in all_closed:
                total_closed += 1
                pnl_val = _display_pnl(p)
                if pnl_val > 0:
                    total_wins += 1
                elif pnl_val < 0:
                    total_losses += 1
            kpis["total_wins"] = total_wins
            kpis["total_losses"] = total_losses
            kpis["total_closed"] = total_closed
            kpis["win_rate"] = total_wins / total_closed if total_closed else 0.0

            # Day history (last 30 days)
            thirty_ago = today - timedelta(days=30)
            day_res = await session.scalars(
                select(Day).where(Day.date >= thirty_ago).order_by(Day.date.desc())
            )
            day_records = list(day_res)

            # Week resume for current week
            week_str = today.strftime("%Y-W%W")
            resume_res = await session.scalars(
                select(Resume).where(Resume.week == week_str).order_by(Resume.epic)
            )
            resume_records = list(resume_res)
    else:
        kpis = {
            "available_epics": len(epics),
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
        }

    kpis["all_epics_count"] = len(all_epics)
    kpis["epic_kpi_color"] = epic_kpi_color
    kpis["refresh_label"] = refresh_label
    kpis["tradable_count"] = len(tradable_epics)
    kpis["tradable_kpi_color"] = tradable_kpi_color
    kpis["tradable_refresh_label"] = tradable_refresh_label

    # API Guard & Error log
    guard = request.app.state.guard
    error_log = request.app.state.error_log
    guard_stats = guard.stats() if guard else None
    error_entries = error_log.get_all() if error_log else []

    # API queue
    api_queue = getattr(request.app.state, "api_queue", None)
    queue_stats = api_queue.stats() if api_queue else None
    queue_recent = api_queue.recent() if api_queue else []
    queue_pending_tasks = api_queue.pending_tasks() if api_queue else []

    # Server log buffer
    log_buffer = getattr(request.app.state, "log_buffer", None)
    log_entries = log_buffer.get_all() if log_buffer else []

    return {
        "market_summary": market_summary,
        "kpis": kpis,
        "guard_stats": guard_stats,
        "error_entries": error_entries,
        "queue_stats": queue_stats,
        "queue_recent": queue_recent,
        "queue_pending_tasks": queue_pending_tasks,
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "day_records": day_records,
        "resume_records": resume_records,
        "bot_paused": scheduler.is_paused if scheduler else None,
        "scheduler_available": scheduler is not None,
        "jobs": scheduler.jobs_status() if scheduler else [],
        "log_entries": log_entries,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard page (initial full render)."""
    settings = request.app.state.settings
    state = await _gather_dashboard_state(request)
    return HTMLResponse(content=_render_dashboard(settings, state))


@router.get("/api/dashboard-fragments")
async def api_dashboard_fragments(request: Request) -> JSONResponse:
    """JSON API: HTML fragments for every live dashboard region.

    Polled by the dashboard every two seconds. The client swaps only the
    fragments whose markup changed since the previous poll, so a single request
    keeps the KPI bar, market table and modals current without a full-page
    reload. ``server_time`` is the Europe/Paris timestamp shown as each
    section's "last refresh" label.
    """
    state = await _gather_dashboard_state(request)
    fragments = _build_fragments(state)
    return JSONResponse(
        {
            "fragments": fragments,
            "bot_paused": state["bot_paused"],
            "scheduler_available": state["scheduler_available"],
            "server_time": datetime.now(_PARIS).strftime("%H:%M:%S"),
        }
    )


@router.get("/epics", response_class=HTMLResponse)
async def epic_list(request: Request) -> HTMLResponse:
    """Render the full epic list page (today's navigation tree crawl)."""
    scheduler = request.app.state.scheduler
    session_factory = request.app.state.session_factory

    all_epics: list[str] = scheduler.all_epics if scheduler else []
    epic_last_refresh: datetime | None = (
        scheduler.epic_last_refresh if scheduler else None
    )

    # Enrich with DB data where available
    db_epics: dict[str, Epic] = {}
    if session_factory and all_epics:
        async with session_factory() as session:
            result = await session.scalars(select(Epic).where(Epic.name.in_(all_epics)))
            for e in result:
                db_epics[e.name] = e

    html = _render_epic_list_page(all_epics, db_epics, epic_last_refresh)
    return HTMLResponse(content=html)


@router.get("/epics/tradable", response_class=HTMLResponse)
async def tradable_list(request: Request) -> HTMLResponse:
    """Render the tradable epic list (current open/TRADEABLE markets)."""
    scheduler = request.app.state.scheduler

    markets = scheduler.tradable_markets if scheduler else []
    last_refresh: datetime | None = (
        scheduler.tradable_last_refresh if scheduler else None
    )

    html = _render_tradable_list_page(markets, last_refresh)
    return HTMLResponse(content=html)


@router.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    """JSON API: bot status and buffer summary."""
    buffer = request.app.state.buffer
    scheduler = request.app.state.scheduler
    return JSONResponse(
        {
            "status": "running",
            "tracked_epics": len(buffer),
            "epics": buffer.tracked_epics,
            "bot_paused": scheduler.is_paused if scheduler else None,
            "scheduler_available": scheduler is not None,
        }
    )


@router.post("/api/bot/pause")
async def bot_pause(request: Request) -> JSONResponse:
    """Switch all scheduled jobs to manual mode."""
    scheduler = request.app.state.scheduler
    if scheduler:
        await scheduler.pause_bot()
    return JSONResponse({"bot_paused": True})


@router.post("/api/bot/resume")
async def bot_resume(request: Request) -> JSONResponse:
    """Switch all scheduled jobs to automatic mode."""
    scheduler = request.app.state.scheduler
    if scheduler:
        await scheduler.resume_bot()
    return JSONResponse({"bot_paused": False})


_ACTIONS = {
    "refresh_epic_list",
    "refresh_tradable_epics",
    "collect_and_analyze",
    "monitor_positions",
    "sync_positions",
    "end_of_day",
    "reconcile_pnl",
    "daily_summary",
    "weekly_summary",
    "daily_reset",
    "dump_and_purge_candles",
}


@router.post("/api/actions/{action}")
async def run_action(request: Request, action: str) -> JSONResponse:
    """Trigger a scheduled task manually; runs in the background."""
    scheduler = request.app.state.scheduler
    if not scheduler:
        return JSONResponse({"error": "Scheduler not available"}, status_code=503)
    if action not in _ACTIONS:
        return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)
    fn = getattr(scheduler, f"trigger_{action}")
    asyncio.create_task(fn())
    return JSONResponse({"status": "triggered", "action": action})


@router.get("/api/jobs")
async def api_jobs(request: Request) -> JSONResponse:
    """JSON API: every schedulable job with its current auto/manual mode."""
    scheduler = request.app.state.scheduler
    if not scheduler:
        return JSONResponse(
            {"jobs": [], "scheduler_available": False, "all_paused": True}
        )
    return JSONResponse(
        {
            "jobs": scheduler.jobs_status(),
            "scheduler_available": True,
            "all_paused": scheduler.is_paused,
        }
    )


@router.post("/api/jobs/{action}/{mode}")
async def set_job_mode(request: Request, action: str, mode: str) -> JSONResponse:
    """Switch a single job between automatic (``mode=auto``) and manual (``mode=manual``)."""
    scheduler = request.app.state.scheduler
    if not scheduler:
        return JSONResponse({"error": "Scheduler not available"}, status_code=503)
    if mode not in ("auto", "manual"):
        return JSONResponse({"error": f"Unknown mode: {mode}"}, status_code=400)
    if action not in _ACTIONS:
        return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)
    ok = await scheduler.set_job_mode(action, auto=(mode == "auto"))
    if not ok:
        return JSONResponse({"error": "Could not change job mode"}, status_code=400)
    return JSONResponse({"status": "ok", "action": action, "auto": mode == "auto"})


@router.get("/api/prices/{epic}")
async def api_prices(request: Request, epic: str) -> JSONResponse:
    """JSON API: price data for a specific epic."""
    buffer = request.app.state.buffer
    buf = buffer.get(epic)

    if not buf:
        return JSONResponse({"error": "Epic not tracked"}, status_code=404)

    last_50 = list(buf.candles)[-50:]
    return JSONResponse(
        {
            "epic": epic,
            "candles": len(buf),
            "bid_closes": [c.bid_close for c in last_50],
            "timestamps": [c.timestamp.isoformat() for c in last_50],
            "spreads": [c.spread for c in last_50],
            "last": (
                {
                    "bid": buf.last.bid_close,
                    "offer": buf.last.offer_close,
                    "spread": buf.last.spread,
                    "timestamp": buf.last.timestamp.isoformat(),
                }
                if buf.last
                else None
            ),
        }
    )


@router.get("/api/ig-guard")
async def api_ig_guard(request: Request) -> JSONResponse:
    """JSON API: IG API guard stats (rate-limit counters + blocked state)."""
    guard = request.app.state.guard
    if guard is None:
        return JSONResponse({"error": "Guard not configured"}, status_code=503)
    s = guard.stats()
    return JSONResponse(
        {
            "total_calls": s.total_calls,
            "calls_last_minute": s.calls_last_minute,
            "calls_last_second": s.calls_last_second,
            "is_available": s.is_available,
            "is_blocked": s.is_blocked,
            "blocked_since": s.blocked_since.isoformat() if s.blocked_since else None,
            "blocked_until": s.blocked_until.isoformat() if s.blocked_until else None,
            "blocked_reason": s.blocked_reason,
            "max_per_minute": s.max_per_minute,
            "max_per_second": s.max_per_second,
        }
    )


@router.get("/api/queue")
async def api_queue_status(request: Request) -> JSONResponse:
    """JSON API: queue counters + recently processed tasks."""
    api_queue = getattr(request.app.state, "api_queue", None)
    if api_queue is None:
        return JSONResponse({"error": "Queue not configured"}, status_code=503)
    s = api_queue.stats()
    return JSONResponse(
        {
            "pending": s.pending,
            "running": s.running,
            "enqueued": s.enqueued,
            "succeeded": s.succeeded,
            "failed": s.failed,
            "retried": s.retried,
            "rate_limited": s.rate_limited,
            "max_attempts": s.max_attempts,
            "recent": [
                {
                    "label": t.label,
                    "method": t.method,
                    "endpoint": t.endpoint,
                    "status": t.status,
                    "attempts": t.attempts,
                    "total_attempts": t.total_attempts,
                    "priority": t.priority,
                    "last_error": t.last_error,
                    "created_at": t.created_at.isoformat(),
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                }
                for t in api_queue.recent()
            ],
            "todo": [
                {
                    "label": t.label,
                    "method": t.method,
                    "endpoint": t.endpoint,
                    "priority": t.priority,
                    "attempts": t.attempts,
                    "created_at": t.created_at.isoformat(),
                }
                for t in api_queue.pending_tasks()
            ],
        }
    )


@router.post("/api/positions/open/{epic}")
async def open_position_manual(request: Request, epic: str) -> JSONResponse:
    """Open a BUY position at minimum deal size for the given epic (manual from dashboard)."""
    api_queue = getattr(request.app.state, "api_queue", None)
    session_factory = request.app.state.session_factory
    buffer = request.app.state.buffer

    if not api_queue or not session_factory:
        return JSONResponse({"error": "Trading not available"}, status_code=503)

    buf = buffer.get(epic)
    if not buf or not buf.last:
        return JSONResponse({"error": "No price data for this epic"}, status_code=400)

    current_bid = buf.last.bid_close
    current_spread = buf.last.spread

    try:
        market_data = await api_queue.get(
            f"/markets/{epic}",
            version=3,
            priority=Priority.URGENT,
            label=f"manual open {epic}: market",
        )
        instrument = market_data.get("instrument", {})
        snapshot = market_data.get("snapshot", {})
        dealing_rules = market_data.get("dealingRules", {})

        if snapshot.get("marketStatus") != "TRADEABLE":
            return JSONResponse(
                {"error": f"Market not TRADEABLE: {snapshot.get('marketStatus')}"},
                status_code=400,
            )

        min_stop_rule = dealing_rules.get("minNormalStopOrLimitDistance", {})
        min_deal_size = float(dealing_rules.get("minDealSize", {}).get("value", 1))
        min_stop = float(min_stop_rule.get("value", 5))
        if min_stop_rule.get("unit") == "PERCENTAGE":
            min_stop = min_stop * current_bid / 100

        stop_distance = max(min_stop, 1)
        quantity = max(int(min_deal_size), 1)
        scaling_factor = float(str(snapshot.get("scalingFactor", "1")).replace(",", ""))
        euro_per_pip_unit = 1.0 / scaling_factor if scaling_factor > 0 else 1.0
        euro_risk = quantity * stop_distance * euro_per_pip_unit
        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
        # Currency-converted euro value of one point of movement (basis for P&L).
        epp = euro_per_point(market_data, quantity, currency)
        expiry = instrument.get("expiry", "-")

        order_payload = {
            "epic": epic,
            "expiry": expiry,
            "direction": "BUY",
            "size": str(quantity),
            "orderType": "MARKET",
            "currencyCode": currency,
            "guaranteedStop": False,
            "stopDistance": str(int(stop_distance)),
            "forceOpen": True,
        }

        result = await api_queue.post(
            "/positions/otc",
            order_payload,
            version=2,
            priority=Priority.URGENT,
            label=f"manual open {epic}: order",
        )

        deal_reference = result.get("dealReference")
        if not deal_reference:
            return JSONResponse({"error": "No dealReference returned"}, status_code=500)

        # IG processes deals asynchronously — poll up to 4 times with 1 s delays
        confirmation = None
        for _attempt in range(4):
            try:
                confirmation = await api_queue.get(
                    f"/confirms/{deal_reference}",
                    version=1,
                    priority=Priority.URGENT,
                    label=f"manual open {epic}: confirm",
                )
                break
            except Exception:
                if _attempt < 3:
                    await asyncio.sleep(1)
                else:
                    raise

        if confirmation.get("dealStatus") != "ACCEPTED":
            reason = confirmation.get("reason", "UNKNOWN")
            return JSONResponse({"error": f"Deal rejected: {reason}"}, status_code=400)

        deal_id = confirmation.get("dealId", "")
        open_level = float(confirmation.get("level", current_bid))

        now = datetime.now(UTC)
        position = Position(
            epic=epic,
            epic_name=instrument.get("name", epic)[:10],
            deal_reference=deal_reference,
            deal_id=deal_id or None,
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            reason_open="manual",
            level_open=Decimal(str(round(open_level, 5))),
            level_win=Decimal(str(round(open_level + stop_distance * 2, 5))),
            level_zero=Decimal(str(round(open_level, 5))),
            level_follower=Decimal(str(round(open_level - stop_distance * 0.5, 5))),
            level_loose=Decimal(str(round(open_level - stop_distance, 5))),
            level_security=Decimal(str(round(open_level - stop_distance * 0.8, 5))),
            level_stop=Decimal(str(round(open_level - stop_distance, 5))),
            pip_spread=Decimal(str(round(current_spread, 5))),
            quantity=quantity,
            size=int(stop_distance),
            euro_stop=Decimal(str(round(euro_risk, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
        )
        async with session_factory() as session:
            session.add(position)
            await session.commit()

        logger.info(
            "Manual position opened: %s qty=%d level=%.3f stop=%.0f",
            epic,
            quantity,
            open_level,
            stop_distance,
        )
        return JSONResponse(
            {
                "status": "opened",
                "deal_id": deal_id,
                "level": open_level,
                "quantity": quantity,
                "stop_distance": stop_distance,
            }
        )

    except Exception as exc:
        logger.error("Manual position open failed for %s: %s", epic, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/positions/close/{position_id}")
async def close_position_manual(request: Request, position_id: int) -> JSONResponse:
    """Close an open position manually from the dashboard."""
    api_queue = getattr(request.app.state, "api_queue", None)
    session_factory = request.app.state.session_factory

    if not api_queue or not session_factory:
        return JSONResponse({"error": "Trading not available"}, status_code=503)

    async with session_factory() as session:
        position = await session.get(Position, position_id)
        if not position:
            return JSONResponse({"error": "Position not found"}, status_code=404)
        if position.state != PositionState.OPEN:
            return JSONResponse({"error": "Position is not open"}, status_code=400)

        deal_id = position.deal_id
        if not deal_id:
            try:
                positions_data = await api_queue.get(
                    "/positions",
                    version=2,
                    priority=Priority.URGENT,
                    label=f"manual close {position.epic}: resolve deal_id",
                )
                for entry in positions_data.get("positions", []):
                    if entry.get("market", {}).get("epic") == position.epic:
                        deal_id = entry.get("position", {}).get("dealId")
                        if deal_id:
                            position.deal_id = deal_id
                            await session.commit()
                        break
            except Exception as exc:
                logger.warning(
                    "Could not resolve dealId for %s: %s", position.epic, exc
                )

        if not deal_id:
            return JSONResponse(
                {"error": "No deal ID found for this position"}, status_code=400
            )

        try:
            market_data = await api_queue.get(
                f"/markets/{position.epic}",
                version=3,
                priority=Priority.URGENT,
                label=f"manual close {position.epic}: market",
            )
            close_level = float(market_data.get("snapshot", {}).get("bid", 0))
        except Exception as exc:
            return JSONResponse(
                {"error": f"Failed to fetch market price: {exc}"}, status_code=500
            )

        close_payload = {
            "dealId": deal_id,
            "direction": "SELL",
            "size": position.quantity or 1,
            "orderType": "MARKET",
            "timeInForce": "EXECUTE_AND_ELIMINATE",
            "forceOpen": False,
        }
        try:
            close_result = await api_queue.delete(
                "/positions/otc",
                close_payload,
                version=1,
                priority=Priority.URGENT,
                label=f"manual close {position.epic}: order",
            )
        except Exception as exc:
            logger.error("Manual close failed for %s: %s", position.epic, exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

        # Prefer IG's authoritative fill level + realized profit from the close
        # confirmation; fall back to the observed bid and euro_per_point.
        deal_reference = close_result.get("dealReference")
        ig_profit: float | None = None
        if deal_reference:
            try:
                confirm = await api_queue.get(
                    f"/confirms/{deal_reference}",
                    version=1,
                    priority=Priority.URGENT,
                    label=f"manual close {position.epic}: confirm",
                )
                if confirm.get("level") is not None:
                    close_level = float(confirm["level"])
                if confirm.get("profit") is not None and confirm.get(
                    "profitCurrency"
                ) in (None, "", "EUR", "E", "€"):
                    ig_profit = float(confirm["profit"])
            except Exception as exc:
                logger.debug("Close confirm unavailable for %s: %s", position.epic, exc)

        now = datetime.now(UTC)
        move = close_level - float(position.level_open or 0)
        if position.euro_per_point is not None and float(position.euro_per_point) != 0:
            euro_pnl = move * float(position.euro_per_point)
        else:
            euro_per_pip = (
                float(position.euro_stop or 1)
                / float(position.size or 1)
                / float(position.quantity or 1)
            )
            euro_pnl = move * (position.quantity or 1) * euro_per_pip
        if ig_profit is not None:
            euro_pnl = ig_profit

        position.state = PositionState.CLOSE
        position.time_close = now.time()
        position.level_close = Decimal(str(round(close_level, 5)))
        position.reason_close = "manual"
        position.euro = Decimal(str(round(euro_pnl, 3)))
        position.win = 1 if euro_pnl > 0 else 0
        await session.commit()

        logger.info(
            "Manual position closed: %s level=%.3f P&L=%.2f€",
            position.epic,
            close_level,
            euro_pnl,
        )
        return JSONResponse(
            {
                "status": "closed",
                "epic": position.epic,
                "level": close_level,
                "pnl": round(euro_pnl, 2),
            }
        )


@router.get("/api/ig-errors")
async def api_ig_errors(request: Request) -> JSONResponse:
    """JSON API: last 20 IG API errors."""
    error_log = request.app.state.error_log
    if error_log is None:
        return JSONResponse({"errors": []})
    entries = error_log.get_all()
    return JSONResponse(
        {
            "count": len(entries),
            "errors": [
                {
                    "timestamp": e.timestamp.isoformat(),
                    "method": e.method,
                    "endpoint": e.endpoint,
                    "http_status": e.http_status,
                    "ig_error_code": e.ig_error_code,
                    "hint": e.hint,
                }
                for e in entries
            ],
        }
    )


@router.post("/api/ig-errors/clear")
async def api_ig_errors_clear(request: Request) -> JSONResponse:
    """Clear the in-memory error log."""
    error_log = request.app.state.error_log
    if error_log is not None:
        error_log.clear()
    return JSONResponse({"cleared": True})


@router.get("/api/logs")
async def api_logs(request: Request) -> JSONResponse:
    """JSON API: last 30 server log entries (INFO+) since startup."""
    log_buffer = getattr(request.app.state, "log_buffer", None)
    if log_buffer is None:
        return JSONResponse({"entries": []})
    return JSONResponse({"entries": log_buffer.get_all()})


def _render_logs_section(entries: list[dict]) -> str:
    """Render the log section fragment: filter toolbar + log rows (newest first)."""
    _level_class = {"INFO": "info", "WARNING": "warning", "ERROR": "error", "DEBUG": "debug"}
    _level_short = {"INFO": "INF", "WARNING": "WRN", "ERROR": "ERR", "DEBUG": "DBG"}

    info_count = sum(1 for e in entries if e["level"] == "INFO")
    warn_count = sum(1 for e in entries if e["level"] == "WARNING")
    err_count = sum(1 for e in entries if e["level"] == "ERROR")
    total = len(entries)

    rows = ""
    for e in reversed(entries):
        level = e["level"]
        cls = _level_class.get(level, "debug")
        short = _level_short.get(level, level[:3])
        name_short = e["name"].split(".")[-1]
        rows += (
            f'<div class="log-row" data-level="{html.escape(level)}">'
            f'<span class="log-ts">{html.escape(e["ts"])}</span>'
            f'<span class="log-badge {cls}">{short}</span>'
            f'<span class="log-name" title="{html.escape(e["name"])}">{html.escape(name_short)}</span>'
            f'<span class="log-msg">{html.escape(e["msg"])}</span>'
            f"</div>\n"
        )
    if not rows:
        rows = '<div class="log-empty">No log entries yet — logs appear here once the bot runs.</div>'

    return f"""<div class="logs-toolbar">
            <button class="log-filter-btn active" data-level="all" onclick="filterLogs('all',this)">All&nbsp;({total})</button>
            <button class="log-filter-btn" data-level="INFO" onclick="filterLogs('INFO',this)">Info&nbsp;({info_count})</button>
            <button class="log-filter-btn" data-level="WARNING" onclick="filterLogs('WARNING',this)">Warning&nbsp;({warn_count})</button>
            <button class="log-filter-btn" data-level="ERROR" onclick="filterLogs('ERROR',this)">Error&nbsp;({err_count})</button>
        </div>
        <div class="logs-list">
            {rows}
        </div>"""


def _render_epic_list_page(
    epics: list[str],
    db_epics: dict,
    last_refresh: datetime | None,
) -> str:
    """Render the full epic list page."""
    if last_refresh:
        local_refresh = last_refresh.astimezone(_PARIS)
        if local_refresh.date() == date.today():
            refresh_color = "#4ade80"
            refresh_text = f"Today at {local_refresh.strftime('%H:%M:%S')}"
        else:
            refresh_color = "#f59e0b"
            refresh_text = local_refresh.strftime("%d/%m/%Y %H:%M:%S")
    else:
        refresh_color = "#ef4444"
        refresh_text = "Never refreshed this session"

    count = len(epics)
    count_color = "#4ade80" if count > 0 else "#ef4444"

    rows = ""
    for i, name in enumerate(sorted(epics), 1):
        e = db_epics.get(name)
        description = (e.description or "—") if e else "—"
        etype = (e.type or "—") if e else "—"
        deposit = f"{e.deposit:.3f}" if (e and e.deposit) else "—"
        rows += f"""
            <tr>
                <td class="number dim">{i}</td>
                <td class="epic-col">{html.escape(name)}</td>
                <td class="desc-col">{html.escape(description)}</td>
                <td class="type-col">{html.escape(etype)}</td>
                <td class="dep-col number">{deposit}</td>
            </tr>"""

    if not rows:
        rows = '<tr><td colspan="5" style="text-align:center;color:#475569;padding:2rem;">No epics — run Refresh Epic List from the dashboard.</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Epic List</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
<div class="container">
    <nav>
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/">Dashboard</a></li>
            <li><a href="/epics" class="active">Epic List</a></li>
            <li><a href="/epics/tradable">Tradable</a></li>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/positions" target="_blank">Positions<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/positions/summary" target="_blank">Daily Summary<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
        </ul>
    </nav>

    <div class="header-bar">
        <h1><i data-lucide="globe" class="lc-icon"></i> Epic List</h1>
        <div class="stat-badge">
            <span class="stat-label">Total epics</span>
            <span class="stat-value" style="color:{count_color};">{count}</span>
        </div>
        <div class="stat-badge">
            <span class="stat-label">Last refresh</span>
            <span class="stat-value" style="color:{refresh_color}; font-size:0.9rem;">{refresh_text}</span>
        </div>
        <div class="filter-wrap">
            <input id="filter-input" type="text" placeholder="Filter epics…" oninput="filterTable(this.value)">
            <span id="filter-count">{count} shown</span>
        </div>
    </div>

    <div class="section">
        <table>
            <thead>
                <tr>
                    <th style="width:3rem;">#</th>
                    <th>Epic</th>
                    <th>Description</th>
                    <th>Type</th>
                    <th>Deposit</th>
                </tr>
            </thead>
            <tbody id="epic-tbody">
                {rows}
            </tbody>
        </table>
    </div>

    <footer>Navigation tree crawl result — refreshes daily at 07:30 and on bot startup</footer>
</div>
<script>
const totalRows = {count};
function filterTable(q) {{
    const tbody = document.getElementById('epic-tbody');
    const rows  = tbody.querySelectorAll('tr');
    const ql    = q.toLowerCase();
    let shown   = 0;
    rows.forEach(tr => {{
        const text = tr.textContent.toLowerCase();
        const hide = ql && !text.includes(ql);
        tr.classList.toggle('hidden', hide);
        if (!hide) shown++;
    }});
    document.getElementById('filter-count').textContent = shown + ' shown';
}}
lucide.createIcons();
</script>
</body>
</html>"""


def _render_tradable_list_page(
    markets: list[MarketInfo],
    last_refresh: datetime | None,
) -> str:
    """Render the tradable epic list page (current open/TRADEABLE markets)."""
    if last_refresh:
        local_refresh = last_refresh.astimezone(_PARIS)
        if local_refresh.date() == date.today():
            refresh_color = "#4ade80"
            refresh_text = f"Today at {local_refresh.strftime('%H:%M:%S')}"
        else:
            refresh_color = "#f59e0b"
            refresh_text = local_refresh.strftime("%d/%m/%Y %H:%M:%S")
    else:
        refresh_color = "#ef4444"
        refresh_text = "Not refreshed this session"

    count = len(markets)
    count_color = "#4ade80" if count > 0 else "#ef4444"

    rows = ""
    for i, m in enumerate(sorted(markets, key=lambda x: x.epic), 1):
        spread_pct = m.spread_ratio * 100
        rows += f"""
            <tr>
                <td class="number dim">{i}</td>
                <td class="epic-col">{html.escape(m.epic)}</td>
                <td class="desc-col">{html.escape(m.name)}</td>
                <td class="type-col">{html.escape(m.status)}</td>
                <td class="number">{m.bid:.2f}</td>
                <td class="number">{m.offer:.2f}</td>
                <td class="number">{spread_pct:.3f}%</td>
            </tr>"""

    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#475569;padding:2rem;">No tradable epics — run Refresh Tradable Epics from the dashboard.</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Tradable Epics</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
<div class="container">
    <nav>
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/">Dashboard</a></li>
            <li><a href="/epics">Epic List</a></li>
            <li><a href="/epics/tradable" class="active">Tradable</a></li>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/positions" target="_blank">Positions<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
        </ul>
    </nav>

    <div class="header-bar">
        <h1><i data-lucide="zap" class="lc-icon"></i> Tradable Epics</h1>
        <div class="stat-badge">
            <span class="stat-label">Tradable now</span>
            <span class="stat-value" style="color:{count_color};">{count}</span>
        </div>
        <div class="stat-badge">
            <span class="stat-label">Last refresh</span>
            <span class="stat-value" style="color:{refresh_color}; font-size:0.9rem;">{refresh_text}</span>
        </div>
        <div class="filter-wrap">
            <input id="filter-input" type="text" placeholder="Filter epics…" oninput="filterTable(this.value)">
            <span id="filter-count">{count} shown</span>
        </div>
    </div>

    <div class="section">
        <table>
            <thead>
                <tr>
                    <th style="width:3rem;">#</th>
                    <th>Epic</th>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Bid</th>
                    <th>Offer</th>
                    <th>Spread</th>
                </tr>
            </thead>
            <tbody id="epic-tbody">
                {rows}
            </tbody>
        </table>
    </div>

    <footer>Open/TRADEABLE subset of the epic list — refreshes hourly during market hours. Spread is applied later at analysis time.</footer>
</div>
<script>
const totalRows = {count};
function filterTable(q) {{
    const tbody = document.getElementById('epic-tbody');
    const rows  = tbody.querySelectorAll('tr');
    const ql    = q.toLowerCase();
    let shown   = 0;
    rows.forEach(tr => {{
        const text = tr.textContent.toLowerCase();
        const hide = ql && !text.includes(ql);
        tr.classList.toggle('hidden', hide);
        if (!hide) shown++;
    }});
    document.getElementById('filter-count').textContent = shown + ' shown';
}}
lucide.createIcons();
</script>
</body>
</html>"""


def _bid_pct(bid: float, low: float, high: float) -> float:
    """Return bid position as % within known [low, high] range."""
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (bid - low) / (high - low) * 100))


def _render_action_cards(jobs: list[dict]) -> str:
    """Render the job cards for the Actions section.

    Each card carries an auto/manual switch and a Run button (visible only in
    manual mode). ``danger`` selects the Run-button colour and whether a
    confirmation prompt is shown before triggering.
    """
    if not jobs:
        return '<div class="action-empty">Scheduler not available.</div>'

    cards = ""
    for j in jobs:
        action = html.escape(str(j["action"]))
        name = html.escape(str(j["name"]))
        desc = html.escape(str(j["description"]))
        schedule = html.escape(str(j["schedule"]))
        danger = j["danger"] if j["danger"] in ("safe", "warn", "danger") else "safe"
        auto = bool(j["auto"])
        needs_confirm = "true" if danger in ("warn", "danger") else "false"
        run_style = "display:none;" if auto else ""
        mode_cls = "auto" if auto else "manual"
        mode_txt = "Automatic" if auto else "Manual"
        checked = "checked" if auto else ""
        auto_cls = " is-auto" if auto else ""
        cards += f"""
                <div class="action-card{auto_cls}" data-action="{action}">
                    <div class="action-card-head">
                        <span class="action-card-name">{name}</span>
                        <label class="switch" title="Automatic / Manual">
                            <input type="checkbox" {checked} onchange="toggleJobMode('{action}', this)">
                            <span class="switch-slider"></span>
                        </label>
                    </div>
                    <div class="action-card-desc">{desc}</div>
                    <div class="action-card-schedule"><i data-lucide="clock" class="lc-icon"></i> {schedule}</div>
                    <div class="action-card-mode {mode_cls}">{mode_txt}</div>
                    <button class="action-btn {danger} run-btn" style="{run_style}" onclick="runAction('{action}', this, {needs_confirm})">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>"""
    return cards


def _render_config_grid(settings) -> str:
    """Render the strategy configuration grid.

    Shared by the Configuration section and the Win-rate modal so both always
    show the same settings from a single source of truth.
    """
    return f"""<div class="config-grid">
                    <div class="config-item">
                        <div class="config-key">Environment</div>
                        <div class="config-value">{settings.ig_env.value.upper()}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Trading Hours</div>
                        <div class="config-value">{settings.strategy_hour_start}:00 – {settings.strategy_hour_end}:00 (close {settings.strategy_hour_close}:00)</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Max Positions</div>
                        <div class="config-value">{settings.strategy_max_positions}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Max Trades/Day</div>
                        <div class="config-value">{settings.strategy_max_trades_day}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Daily Loss Limit</div>
                        <div class="config-value">€{settings.strategy_daily_loss_limit:.0f}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Daily Win Target</div>
                        <div class="config-value">€{settings.strategy_daily_win_target:.0f}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Min R²</div>
                        <div class="config-value">{settings.strategy_min_r2}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Min Score</div>
                        <div class="config-value">{settings.strategy_min_score}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Stop Multiplier</div>
                        <div class="config-value">{settings.strategy_stop_multiplier}×</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Target Multiplier</div>
                        <div class="config-value">{settings.strategy_target_multiplier}×</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Max Spread Ratio</div>
                        <div class="config-value">{settings.strategy_max_spread_ratio}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Close Target</div>
                        <div class="config-value">{settings.strategy_close_target}</div>
                    </div>
                </div>"""


def _render_week_summary_section(resume_records: list, today: date) -> str:
    """Render the weekly direction summary fragment for the current week."""
    week_str = today.strftime("%Y-W%W")
    if not resume_records:
        return (
            f'<div style="color:#64748b;padding:1.5rem;text-align:center;font-size:0.85rem;">'
            f"No weekly summary for {week_str} — run Weekly Summary job on Friday after market close."
            f"</div>"
        )
    buy_count = sum(1 for r in resume_records if r.direction == "BUY")
    sell_count = sum(1 for r in resume_records if r.direction == "SELL")
    rows = ""
    for r in resume_records:
        direction = r.direction or "—"
        dir_color = "#4ade80" if direction == "BUY" else "#ef4444" if direction == "SELL" else "#94a3b8"
        day_str = r.day.strftime("%d/%m") if r.day else "—"
        rows += f"""
                <tr>
                    <td class="epic-col">{html.escape(r.epic)}</td>
                    <td style="color:{dir_color};font-weight:600;">{html.escape(direction)}</td>
                    <td class="err-ts">{html.escape(day_str)}</td>
                </tr>"""
    return f"""<div style="display:flex;align-items:center;gap:1.5rem;margin-bottom:0.8rem;font-size:0.82rem;">
            <span style="color:#64748b;">Week {html.escape(week_str)}</span>
            <span style="color:#4ade80;"><strong>{buy_count}</strong> BUY</span>
            <span style="color:#ef4444;"><strong>{sell_count}</strong> SELL</span>
            <span style="color:#94a3b8;">{len(resume_records)} epic{"s" if len(resume_records) != 1 else ""}</span>
        </div>
        <div style="overflow-x:auto;">
            <table class="err-table" style="max-width:480px;">
                <thead>
                    <tr>
                        <th>Epic</th>
                        <th>Direction</th>
                        <th>Updated</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>"""


def _render_day_history_section(day_records: list, today: date) -> str:
    """Render the daily P&L history table fragment."""
    if not day_records:
        return (
            '<div style="color:#64748b;padding:1.5rem;text-align:center;font-size:0.85rem;">'
            "No daily summaries yet — run the Daily Summary job after market close."
            "</div>"
        )
    rows = ""
    for d in day_records:
        trade_count = len([e for e in (d.euro_list or "").split(",") if e.strip()])
        pnl = float(d.euro_total) if d.euro_total is not None else 0.0
        pnl_color = "#4ade80" if pnl > 0 else "#ef4444" if pnl < 0 else "#94a3b8"
        pnl_str = f"€{pnl:+.2f}" if d.euro_total is not None else "—"
        is_today = d.date == today
        date_str = d.date.strftime("%a %d/%m/%Y") if d.date else "—"
        row_style = ' style="background:#2a1f16;"' if is_today else ""
        today_badge = (
            ' <span style="color:#E07B39;font-size:0.68rem;font-weight:700;letter-spacing:0.5px;">TODAY</span>'
            if is_today
            else ""
        )
        state_val = d.state.value if d.state else "—"
        state_color = "#4ade80" if state_val == "close" else "#f59e0b"
        rows += f"""
                <tr{row_style}>
                    <td class="err-ts">{html.escape(date_str)}{today_badge}</td>
                    <td class="number">{trade_count}</td>
                    <td class="number" style="color:{pnl_color};font-weight:600;">{pnl_str}</td>
                    <td style="color:{state_color};font-size:0.75rem;">{state_val.upper()}</td>
                </tr>"""
    total_pnl = sum(float(d.euro_total) for d in day_records if d.euro_total is not None)
    total_color = "#4ade80" if total_pnl > 0 else "#ef4444" if total_pnl < 0 else "#94a3b8"
    return f"""<div style="display:flex;align-items:center;gap:1.5rem;margin-bottom:0.8rem;font-size:0.82rem;">
            <span style="color:#64748b;">{len(day_records)} day{"s" if len(day_records) != 1 else ""}</span>
            <span style="color:{total_color};">30-day total <strong>€{total_pnl:+.2f}</strong></span>
        </div>
        <div style="overflow-x:auto;">
            <table class="err-table" style="max-width:600px;">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Trades</th>
                        <th>P&amp;L</th>
                        <th>State</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>"""


def _build_fragments(state: dict) -> dict[str, str]:
    """Build the HTML fragments for every dynamically refreshed dashboard region.

    Returns a mapping ``{fragment_id: html}`` consumed both by the initial
    full-page render (``_render_dashboard``) and by the live polling endpoint
    (``/api/dashboard-fragments``). Keeping a single source of truth here means
    the page and the incremental updates can never drift apart.

    Fragment ids: ``kpi_bar``, ``market_rows``, ``queue_modal``, ``api_modal``,
    ``positions_modal``.
    """
    market_summary: list[dict] = state["market_summary"]
    kpis: dict = state["kpis"]
    guard_stats = state.get("guard_stats")
    error_entries: list[APIErrorEntry] = state.get("error_entries") or []
    queue_stats = state.get("queue_stats")
    queue_recent = state.get("queue_recent") or []
    queue_pending_tasks = state.get("queue_pending_tasks") or []
    open_positions = state.get("open_positions") or []
    closed_positions = state.get("closed_positions") or []

    # ── Open positions modal rows ──────────────────────────────────────────────
    if open_positions:
        pos_rows_html = ""
        for p in open_positions:
            t_open = p.time_open.strftime("%H:%M:%S") if p.time_open else "—"
            lvl_open = f"{p.level_open:.3f}" if p.level_open else "—"
            lvl_win = f"{p.level_win:.3f}" if p.level_win else "—"
            lvl_stop = f"{p.level_stop:.3f}" if p.level_stop else "—"
            qty = p.quantity or "—"
            pnl_val = float(p.euro or 0)
            pnl_color = "#4ade80" if pnl_val >= 0 else "#ef4444"
            pnl_str = f"€{pnl_val:+.2f}" if p.euro is not None else "—"
            strategy_str = (p.strategy.value if p.strategy else "—").upper()
            epic_esc = html.escape(p.epic)
            pos_rows_html += f"""
                    <tr>
                        <td class="err-ts">{html.escape(t_open)}</td>
                        <td class="epic-col">{epic_esc}</td>
                        <td class="desc-col">{html.escape(p.epic_name)}</td>
                        <td class="number">{lvl_open}</td>
                        <td class="number">{lvl_win}</td>
                        <td class="number">{lvl_stop}</td>
                        <td class="number">{qty}</td>
                        <td class="number" style="color:{pnl_color};">{pnl_str}</td>
                        <td class="err-ts">{html.escape(strategy_str)}</td>
                        <td style="text-align:center;">
                            <button class="close-pos-btn" onclick="closePosition({p.id}, '{epic_esc}', this)" title="Close this position manually">Close</button>
                        </td>
                    </tr>"""
    else:
        pos_rows_html = '<tr><td colspan="10" class="err-empty">No open positions right now.</td></tr>'

    # ── Closed positions modal rows ────────────────────────────────────────────
    if closed_positions:
        closed_rows_html = ""
        for p in closed_positions:
            d_str = p.date.strftime("%d/%m/%Y") if p.date else "—"
            t_open = p.time_open.strftime("%H:%M:%S") if p.time_open else "—"
            t_close = p.time_close.strftime("%H:%M:%S") if p.time_close else "—"
            lvl_open = f"{p.level_open:.3f}" if p.level_open else "—"
            lvl_close = f"{p.level_close:.3f}" if p.level_close else "—"
            qty = p.quantity or "—"
            pnl_val = _display_pnl(p)
            pnl_color = "#4ade80" if pnl_val >= 0 else "#ef4444"
            pnl_str = f"€{pnl_val:+.2f}"
            open_label, open_color = _open_reason_label(p.reason_open)
            close_label, close_color = _close_reason_label(p.reason_close)
            closed_rows_html += f"""
                    <tr>
                        <td class="err-ts">{html.escape(d_str)}</td>
                        <td class="err-ts">{html.escape(t_open)}</td>
                        <td class="err-ts">{html.escape(d_str)}</td>
                        <td class="err-ts">{html.escape(t_close)}</td>
                        <td class="epic-col">{html.escape(p.epic)}</td>
                        <td class="desc-col">{html.escape(p.epic_name)}</td>
                        <td class="number">{lvl_open}</td>
                        <td class="number">{lvl_close}</td>
                        <td class="number">{qty}</td>
                        <td class="number" style="color:{pnl_color};">{pnl_str}</td>
                        <td style="color:{open_color};">{html.escape(open_label)}</td>
                        <td style="color:{close_color};">{html.escape(close_label)}</td>
                    </tr>"""
    else:
        closed_rows_html = '<tr><td colspan="12" class="err-empty">No closed positions today.</td></tr>'

    market_rows = ""
    for s in market_summary:
        pct = _bid_pct(s["bid"], s["low"], s["high"])
        pct_color = "#4ade80" if pct >= 50 else "#f59e0b" if pct >= 25 else "#ef4444"
        epic_esc = html.escape(str(s["epic"]))
        market_rows += f"""
        <tr class="clickable-row" onclick="openChartModal('{epic_esc}')">
            <td class="epic-col">{epic_esc}</td>
            <td class="number">{s['bid']:.1f}</td>
            <td class="number">{s['offer']:.1f}</td>
            <td class="number">{s['spread']:.3f}</td>
            <td class="number">{s['high']:.1f} / {s['low']:.1f}</td>
            <td>
                <div class="range-bar-wrap">
                    <div class="range-bar-bg">
                        <div class="range-bar-fill" style="width:{pct:.1f}%; background:{pct_color};"></div>
                        <div class="range-bar-cursor" style="left:calc({pct:.1f}% - 1px);"></div>
                    </div>
                    <span class="range-pct" style="color:{pct_color};">{pct:.0f}%</span>
                </div>
            </td>
            <td class="number">{s['candles']}</td>
            <td style="text-align:center;">
                <button class="buy-btn" onclick="event.stopPropagation(); openPosition('{epic_esc}', this)" title="Open BUY position at minimum size">Buy</button>
            </td>
        </tr>"""

    pnl_color = "#4ade80" if kpis["daily_pnl"] >= 0 else "#ef4444"
    open_pnl_color = "#4ade80" if kpis["open_pnl"] >= 0 else "#ef4444"

    # API Guard KPI tile
    if guard_stats is None:
        api_status_color = "#475569"
        api_status_label = "N/A"
        api_status_sub = "Guard not configured"
        api_border_color = "#475569"
    elif guard_stats.is_blocked:
        api_status_color = "#ef4444"
        api_status_label = "BLOCKED"
        since_str = (
            guard_stats.blocked_since.astimezone(_PARIS).strftime("%H:%M")
            if guard_stats.blocked_since
            else "?"
        )
        until_str = (
            guard_stats.blocked_until.astimezone(_PARIS).strftime("%H:%M")
            if guard_stats.blocked_until
            else "?"
        )
        api_status_sub = f"Since {since_str} — auto-unblocks ~{until_str}"
        api_border_color = "#ef4444"
    else:
        used_pct = (
            guard_stats.calls_last_minute / guard_stats.max_per_minute
            if guard_stats.max_per_minute
            else 0
        )
        if used_pct >= 0.80:
            api_status_color = "#f59e0b"
            api_border_color = "#f59e0b"
        else:
            api_status_color = "#4ade80"
            api_border_color = "#4ade80"
        api_status_label = "OK"
        api_status_sub = (
            f"{guard_stats.calls_last_minute}/{guard_stats.max_per_minute} calls/min"
        )

    # Error log section HTML
    if error_entries:
        error_rows_html = ""
        for e in error_entries:
            ts = e.timestamp.astimezone(_PARIS).strftime("%H:%M:%S")
            code_display = e.ig_error_code or "—"
            hint_display = e.hint or "—"
            status_color = "#ef4444" if e.http_status >= 500 else "#f59e0b"
            # IG/proxy errors may carry raw HTML bodies — escape every field so a
            # single error can never corrupt the page or inject markup.
            error_rows_html += f"""
                    <tr>
                        <td class="err-ts">{html.escape(ts)}</td>
                        <td class="err-method">{html.escape(str(e.method))}</td>
                        <td class="err-endpoint">{html.escape(str(e.endpoint))}</td>
                        <td class="err-status" style="color:{status_color};">{e.http_status}</td>
                        <td class="err-code">{html.escape(str(code_display))}</td>
                        <td class="err-hint">{html.escape(str(hint_display))}</td>
                    </tr>"""
        error_count = len(error_entries)
    else:
        error_rows_html = '<tr><td colspan="6" class="err-empty">No API errors recorded this session.</td></tr>'
        error_count = 0

    error_section_label = (
        f'<i data-lucide="circle-x" class="lc-icon" style="color:#ef4444;"></i> API Errors ({error_count})'
        if error_count
        else '<i data-lucide="circle-check" class="lc-icon" style="color:#4ade80;"></i> API Errors (none)'
    )

    # ── API queue section ──────────────────────────────────────────────────────
    if queue_stats is None:
        queue_todo = queue_running = queue_succeeded = "—"
        queue_failed = queue_retried = queue_rate_limited = "—"
        queue_failed_color = queue_rl_color = "#94a3b8"
        queue_todo_color = "#94a3b8"
        queue_kpi_border = "#475569"
    else:
        todo_count = queue_stats.pending
        queue_todo = todo_count
        queue_running = queue_stats.running
        queue_succeeded = queue_stats.succeeded
        queue_failed = queue_stats.failed
        queue_retried = queue_stats.retried
        queue_rate_limited = queue_stats.rate_limited
        queue_failed_color = "#ef4444" if queue_stats.failed else "#4ade80"
        queue_rl_color = "#f59e0b" if queue_stats.rate_limited else "#94a3b8"
        queue_todo_color = "#f59e0b" if todo_count else "#94a3b8"
        queue_kpi_border = (
            "#ef4444"
            if queue_stats.failed
            else ("#f59e0b" if todo_count else "#4ade80")
        )

    _status_colors = {
        "done": "#4ade80",
        "error": "#ef4444",
        "running": "#60a5fa",
        "pending": "#94a3b8",
    }

    def _truncate(text: str, limit: int = 60) -> tuple[str, str]:
        """Return (display_text, full_text). display_text is truncated if needed."""
        s = str(text)
        if len(s) <= limit:
            return html.escape(s), ""
        return html.escape(s[:limit]) + "…", html.escape(s)

    if queue_recent:
        queue_rows_html = ""
        for t in queue_recent:
            ts = (
                t.finished_at.astimezone(_PARIS).strftime("%H:%M:%S")
                if t.finished_at
                else "—"
            )
            sc = _status_colors.get(t.status, "#94a3b8")
            err_raw = t.last_error or "—"

            label_short, label_full = _truncate(t.label, 60)
            err_short, err_full = _truncate(err_raw, 60)

            # Tries cell: show total_attempts / attempts if they differ (reveals retries)
            total = getattr(t, "total_attempts", t.attempts)
            tries_display = (
                f'<span title="Total executions: {total} (including rate-limit retries)">{total}</span>'
                if total != t.attempts
                else str(t.attempts)
            )

            label_cell = (
                f'<span class="truncated" onclick="showModal(this)" data-full="{label_full}">{label_short}</span>'
                if label_full
                else label_short
            )
            err_cell = (
                f'<span class="truncated" onclick="showModal(this)" data-full="{err_full}">{err_short}</span>'
                if err_full
                else err_short
            )

            queue_rows_html += f"""
                    <tr>
                        <td class="err-ts">{html.escape(ts)}</td>
                        <td class="err-method">{html.escape(str(t.method))}</td>
                        <td class="err-endpoint">{label_cell}</td>
                        <td class="err-status" style="color:{sc};">{html.escape(str(t.status))}</td>
                        <td class="err-status">{tries_display}</td>
                        <td class="err-hint">{err_cell}</td>
                    </tr>"""
    else:
        queue_rows_html = '<tr><td colspan="6" class="err-empty">No tasks processed yet this session.</td></tr>'

    # ── Pending (TODO) tasks table ─────────────────────────────────────────────
    _priority_labels = {0: "URGENT", 5: "HIGH", 10: "NORMAL"}
    if queue_pending_tasks:
        todo_rows_html = ""
        for t in queue_pending_tasks:
            ts = t.created_at.astimezone(_PARIS).strftime("%H:%M:%S")
            label_short, label_full = _truncate(t.label, 60)
            prio_label = _priority_labels.get(t.priority, str(t.priority))
            prio_color = (
                "#ef4444"
                if t.priority == 0
                else "#f59e0b" if t.priority == 5 else "#94a3b8"
            )
            label_cell = (
                f'<span class="truncated" onclick="showModal(this)" data-full="{label_full}">{label_short}</span>'
                if label_full
                else label_short
            )
            retry_display = (
                f' <span style="color:#f59e0b;"><i data-lucide="undo-2" class="lc-icon"></i>{t.attempts}</span>'
                if t.attempts
                else ""
            )
            todo_rows_html += f"""
                    <tr>
                        <td class="err-ts">{html.escape(ts)}</td>
                        <td class="err-method">{html.escape(str(t.method))}</td>
                        <td class="err-endpoint">{label_cell}{retry_display}</td>
                        <td class="err-status" style="color:{prio_color};">{prio_label}</td>
                    </tr>"""
        todo_table_html = f"""
            <table class="err-table queue-todo-table" style="margin-top:0.6rem;margin-bottom:0.8rem;">
                <thead>
                    <tr>
                        <th>Created</th>
                        <th>Method</th>
                        <th>Task</th>
                        <th>Priority</th>
                    </tr>
                </thead>
                <tbody id="todo-tbody">
                    {todo_rows_html}
                </tbody>
            </table>"""
    else:
        todo_table_html = ""

    # ── KPI bar fragment (the tiles, inner HTML of .kpi-bar) ────────────────────
    kpi_bar = f"""
        <div class="kpi-tile clickable" style="border-left-color:{queue_kpi_border}; position:relative;" onclick="openQueueModal()">
            <div class="kpi-label">Queue</div>
            <div class="kpi-value" style="color:{queue_todo_color};">{queue_todo}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{queue_succeeded} done</span>&nbsp;/&nbsp;<span style="color:{queue_failed_color};">{queue_failed} err</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{kpis['epic_kpi_color']}; position:relative;" onclick="location.href='/epics'">
            <div class="kpi-label">Epic list</div>
            <div class="kpi-value" style="color:{kpis['epic_kpi_color']};">{kpis['all_epics_count']}</div>
            <div class="kpi-sub">{kpis['refresh_label']}</div>
            <button class="kpi-refresh-btn" onclick="event.stopPropagation(); runKpiAction('refresh_epic_list', this)" title="Refresh epic list"><i data-lucide="refresh-cw" class="lc-icon"></i></button>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{kpis['tradable_kpi_color']}; position:relative;" onclick="location.href='/epics/tradable'">
            <div class="kpi-label">Epic tradable</div>
            <div class="kpi-value" style="color:{kpis['tradable_kpi_color']};">{kpis['tradable_count']}</div>
            <div class="kpi-sub">{kpis['tradable_refresh_label']}</div>
            <button class="kpi-refresh-btn" onclick="event.stopPropagation(); runKpiAction('refresh_tradable_epics', this)" title="Refresh tradable epics"><i data-lucide="refresh-cw" class="lc-icon"></i></button>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{open_pnl_color};" onclick="openPositionsModal()">
            <div class="kpi-label">OPEN</div>
            <div class="kpi-value" style="color:{open_pnl_color};">{f"{kpis['open_pnl']:+.2f} €" if kpis['open_trades'] else "—"}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['open_trades']} position{'s' if kpis['open_trades'] != 1 else ''}</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{pnl_color};" onclick="openClosedModal()">
            <div class="kpi-label">CLOSED</div>
            <div class="kpi-value" style="color:{pnl_color};">{f"{kpis['daily_pnl']:+.2f} €" if kpis['closed_trades'] else "—"}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['closed_trades']} trade{'s' if kpis['closed_trades'] != 1 else ''}</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{'#4ade80' if kpis['win_rate'] >= 0.5 else '#ef4444'};" onclick="openWinRateModal()">
            <div class="kpi-label">Win rate</div>
            <div class="kpi-value" style="color:{'#4ade80' if kpis['win_rate'] >= 0.5 else '#ef4444'};">{kpis['win_rate']:.1%}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['total_wins']} win</span>&nbsp;/&nbsp;<span style="color:#ef4444;">{kpis['total_losses']} Loose</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{api_border_color};" onclick="openApiModal()">
            <div class="kpi-label">IG API</div>
            <div class="kpi-value" style="color:{api_status_color};">{api_status_label}</div>
            <div class="kpi-sub">{api_status_sub}</div>
        </div>"""

    # ── Queue modal fragment (inner content) ────────────────────────────────────
    queue_modal = f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;">
            <div class="guard-stat"><span class="guard-stat-label">TODO</span><span class="guard-stat-value" style="color:{queue_todo_color};">{queue_todo}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Running</span><span class="guard-stat-value">{queue_running}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Succeeded</span><span class="guard-stat-value" style="color:#4ade80;">{queue_succeeded}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Failed</span><span class="guard-stat-value" style="color:{queue_failed_color};">{queue_failed}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Retried</span><span class="guard-stat-value">{queue_retried}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Rate-limited</span><span class="guard-stat-value" style="color:{queue_rl_color};">{queue_rate_limited}</span></div>
        </div>
        {todo_table_html}
        <table class="err-table" style="margin-top:0.6rem;">
            <thead>
                <tr>
                    <th>Finished</th>
                    <th>Method</th>
                    <th>Task</th>
                    <th>Status</th>
                    <th>Tries</th>
                    <th>Last error</th>
                </tr>
            </thead>
            <tbody>
                {queue_rows_html}
            </tbody>
        </table>"""

    # ── IG API modal fragment (availability + error log) ────────────────────────
    api_modal = f"""
        <h3 style="color:#94a3b8;font-size:0.78rem;text-transform:uppercase;letter-spacing:1px;margin:0 0 0.7rem;">Availability</h3>
        <div class="guard-stat-row">
            <div class="guard-stat">
                <span class="guard-stat-label">Status</span>
                <span class="guard-stat-value" style="color:{api_status_color};">{api_status_label}</span>
            </div>
            <div class="guard-stat">
                <span class="guard-stat-label">Total calls</span>
                <span class="guard-stat-value">{guard_stats.total_calls if guard_stats else "—"}</span>
            </div>
            <div class="guard-stat">
                <span class="guard-stat-label">Last minute</span>
                <span class="guard-stat-value">{guard_stats.calls_last_minute if guard_stats else "—"} / {guard_stats.max_per_minute if guard_stats else "—"}</span>
            </div>
            <div class="guard-stat">
                <span class="guard-stat-label">Last second</span>
                <span class="guard-stat-value">{guard_stats.calls_last_second if guard_stats else "—"} / {guard_stats.max_per_second if guard_stats else "—"}</span>
            </div>
        </div>
        <div class="guard-bar-wrap" style="margin-bottom:0.8rem;">
            <span style="font-size:0.7rem;color:#64748b;white-space:nowrap;">Calls/min</span>
            <div class="guard-bar-bg">
                <div class="guard-bar-fill" style="width:{min(100, (guard_stats.calls_last_minute / guard_stats.max_per_minute * 100) if guard_stats and guard_stats.max_per_minute else 0):.1f}%; background:{api_border_color};"></div>
            </div>
            <span style="font-size:0.72rem;color:#64748b;">
                {f"{guard_stats.calls_last_minute / guard_stats.max_per_minute:.0%}" if guard_stats and guard_stats.max_per_minute else "—"}
            </span>
        </div>
        {f'''<div class="guard-block-info" style="margin-bottom:0.8rem;">
            <div class="guard-block-since">Blocked since {guard_stats.blocked_since.astimezone(_PARIS).strftime("%Y-%m-%d %H:%M:%S") if guard_stats.blocked_since else "?"} — {html.escape(str(guard_stats.blocked_reason))}</div>
            <div class="guard-block-until"><i data-lucide="hourglass" class="lc-icon"></i> Auto-unblocks ~{guard_stats.blocked_until.astimezone(_PARIS).strftime("%H:%M:%S") if guard_stats.blocked_until else "?"}</div>
        </div>''' if guard_stats and guard_stats.is_blocked else ""}
        <h3 style="color:#94a3b8;font-size:0.78rem;text-transform:uppercase;letter-spacing:1px;margin:1rem 0 0.4rem;">{error_section_label}</h3>
        <div style="display:flex;justify-content:flex-end;padding:0.3rem 0 0.4rem;">
            <button class="err-clear-btn" onclick="clearErrors()" style="display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Clear</button>
        </div>
        <div style="overflow-x:auto;">
            <table class="err-table">
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Method</th>
                        <th>Endpoint</th>
                        <th>HTTP</th>
                        <th>IG Error Code</th>
                        <th>Translation</th>
                    </tr>
                </thead>
                <tbody id="err-tbody">
                    {error_rows_html}
                </tbody>
            </table>
        </div>"""

    # ── Open positions modal fragment ───────────────────────────────────────────
    positions_modal = f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;">
            <div class="guard-stat"><span class="guard-stat-label">Count</span><span class="guard-stat-value" style="color:#4ade80;">{kpis['open_trades']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Total P&amp;L</span><span class="guard-stat-value" style="color:{open_pnl_color};">€{kpis['open_pnl']:.2f}</span></div>
        </div>
        <div style="overflow-x:auto;">
            <table class="err-table">
                <thead>
                    <tr>
                        <th>Opened</th>
                        <th>Epic</th>
                        <th>Name</th>
                        <th>Level open</th>
                        <th>Target</th>
                        <th>Stop</th>
                        <th>Qty</th>
                        <th>P&amp;L</th>
                        <th>Strategy</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {pos_rows_html}
                </tbody>
            </table>
        </div>"""

    # ── Closed positions modal fragment ─────────────────────────────────────────
    # This modal is scoped to today, so its win rate uses today's split.
    win_rate_pct = kpis["win_rate_today"] * 100
    closed_positions_modal = f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;">
            <div class="guard-stat"><span class="guard-stat-label">Trades</span><span class="guard-stat-value">{kpis['closed_trades']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Total P&amp;L</span><span class="guard-stat-value" style="color:{pnl_color};">€{kpis['daily_pnl']:+.2f}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Wins</span><span class="guard-stat-value" style="color:#4ade80;">{kpis['wins']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Losses</span><span class="guard-stat-value" style="color:#ef4444;">{kpis['losses']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Win rate</span><span class="guard-stat-value" style="color:{'#4ade80' if kpis['win_rate_today'] >= 0.5 else '#ef4444'};">{win_rate_pct:.1f}%</span></div>
        </div>
        <div style="overflow-x:auto;">
            <table class="err-table">
                <thead>
                    <tr>
                        <th>Date open</th>
                        <th>Opened</th>
                        <th>Date close</th>
                        <th>Closed</th>
                        <th>Epic</th>
                        <th>Name</th>
                        <th>Level open</th>
                        <th>Level close</th>
                        <th>Qty</th>
                        <th>P&amp;L</th>
                        <th>Open reason</th>
                        <th>Close reason</th>
                    </tr>
                </thead>
                <tbody>
                    {closed_rows_html}
                </tbody>
            </table>
        </div>"""

    _today = date.today()
    return {
        "kpi_bar": kpi_bar,
        "market_rows": market_rows,
        "week_summary": _render_week_summary_section(
            state.get("resume_records") or [], _today
        ),
        "day_history": _render_day_history_section(
            state.get("day_records") or [], _today
        ),
        "queue_modal": queue_modal,
        "api_modal": api_modal,
        "positions_modal": positions_modal,
        "closed_positions_modal": closed_positions_modal,
        "actions": _render_action_cards(state.get("jobs", [])),
        "logs_section": _render_logs_section(state.get("log_entries") or []),
    }


def _render_dashboard(settings, state: dict) -> str:
    """Render the full dashboard shell with nav, config, commands and the
    dynamically refreshed fragments (KPI bar, market data, modals).

    The dynamic regions are produced by :func:`_build_fragments` and embedded in
    containers (``id="frag-*"``) that the client refreshes in place every two
    seconds via ``/api/dashboard-fragments`` — no full-page reload.
    """
    frags = _build_fragments(state)

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Dashboard</title>
    <link rel="stylesheet" href="/static/style.css">
    <style>
        /* Table overrides — inlined to bypass browser CSS cache */
        .err-table     {{ border: 1px solid #4a3a30; border-radius: 4px; overflow: hidden; }}
        .err-table td,
        .err-table th  {{ text-align: left !important; }}
        .err-table td  {{ border-right: 1px solid #4a3a30; border-bottom: 1px solid #4a3a30; }}
        .err-table th  {{ border-right: 1px solid #4a3a30; }}
        .err-table td:last-child,
        .err-table th:last-child {{ border-right: none; }}
        .err-table tbody tr:last-child td {{ border-bottom: none; }}
        .err-table tbody tr:nth-child(odd)  {{ background: #1c1714; }}
        .err-table tbody tr:nth-child(even) {{ background: #251e19; }}
        .err-table tbody tr:hover           {{ background: #2e261f; }}
        /* KPI tiles — clickable variant and refresh icon */
        .kpi-tile.clickable {{ cursor: pointer; transition: background 0.15s; }}
        .kpi-tile.clickable:hover {{ background: #2a201a; }}
        .kpi-refresh-btn {{
            position: absolute; top: 0.4rem; right: 0.5rem;
            background: none; border: none; color: #5f5248;
            cursor: pointer; font-size: 1rem; line-height: 1;
            padding: 0.3rem 0.4rem; margin: 0.1rem; border-radius: 4px;
            transition: color 0.15s, background 0.15s;
        }}
        .kpi-refresh-btn:hover {{ color: #E07B39; background: rgba(224,123,57,0.12); }}
        .kpi-refresh-btn:disabled {{ opacity: 0.35; cursor: default; }}
        .clickable-row {{ cursor: pointer; transition: background 0.1s; }}
        .clickable-row:hover {{ background: #2e261f !important; }}
        /* Buy button in market table */
        .buy-btn {{
            background: #1a3a1a; border: 1px solid #2d5a2d; color: #4ade80;
            cursor: pointer; font-size: 0.75rem; font-weight: 600;
            padding: 0.2rem 0.6rem; border-radius: 4px;
            transition: background 0.15s, color 0.15s;
            white-space: nowrap;
        }}
        .buy-btn:hover {{ background: #16803c; color: #f0fdf4; border-color: #16803c; }}
        .buy-btn:disabled {{ opacity: 0.4; cursor: default; }}
        /* Close position button in open positions modal */
        .close-pos-btn {{
            background: #3a1a1a; border: 1px solid #5a2d2d; color: #ef4444;
            cursor: pointer; font-size: 0.75rem; font-weight: 600;
            padding: 0.2rem 0.6rem; border-radius: 4px;
            transition: background 0.15s, color 0.15s;
            white-space: nowrap;
        }}
        .close-pos-btn:hover {{ background: #991b1b; color: #fef2f2; border-color: #991b1b; }}
        .close-pos-btn:disabled {{ opacity: 0.4; cursor: default; }}
        /* Toast notifications */
        #toast-container {{
            position: fixed; bottom: 1.5rem; right: 1.5rem;
            z-index: 9999; display: flex; flex-direction: column-reverse;
            gap: 0.5rem; pointer-events: none;
        }}
        .toast {{
            display: flex; align-items: flex-start; gap: 0.65rem;
            padding: 0.7rem 1rem; border-radius: 6px; border-left: 3px solid;
            background: #1e293b; color: #e2e8f0; font-size: 0.82rem;
            line-height: 1.4; min-width: 220px; max-width: 340px;
            pointer-events: all; cursor: pointer;
            box-shadow: 0 4px 14px rgba(0,0,0,0.45);
            animation: toast-in 0.22s ease forwards;
        }}
        .toast.toast-out {{ animation: toast-out 0.28s ease forwards; pointer-events: none; }}
        .toast-success {{ border-left-color: #4ade80; }}
        .toast-success .toast-icon {{ color: #4ade80; }}
        .toast-error   {{ border-left-color: #ef4444; }}
        .toast-error   .toast-icon {{ color: #ef4444; }}
        .toast-warning {{ border-left-color: #f59e0b; }}
        .toast-warning .toast-icon {{ color: #f59e0b; }}
        .toast-info    {{ border-left-color: #60a5fa; }}
        .toast-info    .toast-icon {{ color: #60a5fa; }}
        .toast-icon {{ flex-shrink: 0; margin-top: 0.1rem; display: flex; }}
        .toast-icon svg {{ width: 14px; height: 14px; }}
        .toast-body {{ flex: 1; min-width: 0; }}
        .toast-title {{ font-weight: 600; margin-bottom: 0.1rem; }}
        .toast-msg   {{ color: #94a3b8; word-break: break-word; }}
        @keyframes toast-in {{
            from {{ opacity: 0; transform: translateX(1.5rem); }}
            to   {{ opacity: 1; transform: translateX(0); }}
        }}
        @keyframes toast-out {{
            from {{ opacity: 1; transform: translateX(0); max-height: 120px; }}
            to   {{ opacity: 0; transform: translateX(1.5rem); max-height: 0; padding-top: 0; padding-bottom: 0; }}
        }}
        /* Log section */
        .logs-toolbar {{ display: flex; gap: 0.5rem; margin-bottom: 0.6rem; align-items: center; flex-wrap: wrap; }}
        .log-filter-btn {{
            background: #1c1714; border: 1px solid #4a3a30; color: #94a3b8;
            cursor: pointer; font-size: 0.75rem; font-weight: 600;
            padding: 0.25rem 0.75rem; border-radius: 4px;
            transition: background 0.15s, color 0.15s, border-color 0.15s;
        }}
        .log-filter-btn:hover {{ border-color: #94a3b8; color: #e2e8f0; }}
        .log-filter-btn.active[data-level="all"]     {{ background: #252525; color: #e2e8f0; border-color: #94a3b8; }}
        .log-filter-btn.active[data-level="INFO"]    {{ background: #0f2233; color: #60a5fa; border-color: #60a5fa; }}
        .log-filter-btn.active[data-level="WARNING"] {{ background: #2a1c08; color: #f59e0b; border-color: #f59e0b; }}
        .log-filter-btn.active[data-level="ERROR"]   {{ background: #2a0f0f; color: #ef4444; border-color: #ef4444; }}
        .logs-list {{ display: flex; flex-direction: column; gap: 0.1rem; font-family: monospace; font-size: 0.78rem; }}
        .log-row {{ display: flex; align-items: baseline; gap: 0.5rem; padding: 0.18rem 0.4rem; border-radius: 3px; }}
        .log-row:hover {{ background: #2e261f; }}
        .log-ts {{ color: #4b5563; white-space: nowrap; flex-shrink: 0; min-width: 5rem; }}
        .log-badge {{ font-size: 0.67rem; font-weight: 700; padding: 0.05rem 0.3rem; border-radius: 3px; flex-shrink: 0; min-width: 2.5rem; text-align: center; }}
        .log-badge.info    {{ background: #0f2233; color: #60a5fa; }}
        .log-badge.warning {{ background: #2a1c08; color: #f59e0b; }}
        .log-badge.error   {{ background: #2a0f0f; color: #ef4444; }}
        .log-badge.debug   {{ background: #1a1a2a; color: #64748b; }}
        .log-name {{ color: #4b5563; flex-shrink: 0; max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .log-msg  {{ color: #cbd5e1; word-break: break-all; flex: 1; }}
        .log-empty {{ color: #475569; padding: 1rem; text-align: center; }}
    </style>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
<!-- Queue Modal -->
<div id="queue-modal" onclick="if(event.target===this)closeQueueModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:960px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 style="margin:0;color:#E07B39;font-size:1.1rem;display:flex;align-items:center;gap:0.4rem;"><i data-lucide="inbox" class="lc-icon"></i> API Queue</h2>
            <button onclick="closeQueueModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
        <div class="modal-refresh-row">Live · updated <span id="refresh-queue">—</span></div>
        <div id="frag-queue_modal">{frags['queue_modal']}</div>
    </div>
</div>
<!-- Chart Modal -->
<div id="chart-modal" onclick="if(event.target===this)closeChartModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:960px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 id="chart-modal-title" style="margin:0;color:#E07B39;font-size:1.1rem;">Chart</h2>
            <button onclick="closeChartModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
        <div id="chart-container" style="height:420px;"></div>
    </div>
</div>
<!-- IG API Modal -->
<div id="ig-api-modal" onclick="if(event.target===this)closeApiModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:860px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 style="margin:0;color:#E07B39;font-size:1.1rem;display:flex;align-items:center;gap:0.4rem;"><i data-lucide="plug" class="lc-icon"></i> IG API</h2>
            <button onclick="closeApiModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
        <div class="modal-refresh-row">Live · updated <span id="refresh-api">—</span></div>
        <div id="frag-api_modal">{frags['api_modal']}</div>
    </div>
</div>
<!-- Buy Confirmation Modal -->
<div id="buy-confirm-modal" onclick="if(event.target===this)closeBuyConfirmModal(false)" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9000;align-items:center;justify-content:center;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:420px;width:90%;padding:1.5rem;">
        <h2 style="margin:0 0 0.8rem;color:#E07B39;font-size:1.1rem;">Confirm BUY Order</h2>
        <p style="color:#cbd5e1;margin:0 0 0.4rem;">Open BUY on <strong id="buy-confirm-epic" style="color:#f0fdf4;"></strong>?</p>
        <p style="color:#94a3b8;font-size:0.83rem;margin:0 0 1.4rem;">This places a real market order at minimum deal size.</p>
        <div style="display:flex;gap:0.7rem;justify-content:flex-end;">
            <button onclick="closeBuyConfirmModal(false)" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.4rem 1rem;border-radius:4px;">Cancel</button>
            <button onclick="closeBuyConfirmModal(true)" style="background:#16803c;border:1px solid #16803c;color:#f0fdf4;cursor:pointer;font-size:0.85rem;font-weight:600;padding:0.4rem 1rem;border-radius:4px;">Confirm BUY</button>
        </div>
    </div>
</div>
<!-- Close Position Confirmation Modal -->
<div id="close-confirm-modal" onclick="if(event.target===this)closeCloseConfirmModal(false)" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9000;align-items:center;justify-content:center;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:420px;width:90%;padding:1.5rem;">
        <h2 style="margin:0 0 0.8rem;color:#ef4444;font-size:1.1rem;">Confirm Close Position</h2>
        <p style="color:#cbd5e1;margin:0 0 0.4rem;">Close position on <strong id="close-confirm-epic" style="color:#f0fdf4;"></strong>?</p>
        <p style="color:#94a3b8;font-size:0.83rem;margin:0 0 1.4rem;">This closes the position at current market price.</p>
        <div style="display:flex;gap:0.7rem;justify-content:flex-end;">
            <button onclick="closeCloseConfirmModal(false)" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.4rem 1rem;border-radius:4px;">Cancel</button>
            <button onclick="closeCloseConfirmModal(true)" style="background:#991b1b;border:1px solid #991b1b;color:#fef2f2;cursor:pointer;font-size:0.85rem;font-weight:600;padding:0.4rem 1rem;border-radius:4px;">Confirm Close</button>
        </div>
    </div>
</div>
<!-- Open Positions Modal -->
<div id="positions-modal" onclick="if(event.target===this)closePositionsModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:960px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 style="margin:0;color:#E07B39;font-size:1.1rem;display:flex;align-items:center;gap:0.4rem;"><i data-lucide="activity" class="lc-icon"></i> Open Positions</h2>
            <button onclick="closePositionsModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
        <div class="modal-refresh-row">Live · updated <span id="refresh-positions">—</span></div>
        <div id="frag-positions_modal">{frags['positions_modal']}</div>
    </div>
</div>
<!-- Closed Positions Modal -->
<div id="closed-modal" onclick="if(event.target===this)closeClosedModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:1100px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 style="margin:0;color:#E07B39;font-size:1.1rem;display:flex;align-items:center;gap:0.4rem;"><i data-lucide="check-circle" class="lc-icon"></i> Closed Positions — Today</h2>
            <button onclick="closeClosedModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
        <div class="modal-refresh-row">Live · updated <span id="refresh-closed">—</span></div>
        <div id="frag-closed_positions_modal">{frags['closed_positions_modal']}</div>
    </div>
</div>
<!-- Win Rate / Configuration Modal -->
<div id="winrate-modal" onclick="if(event.target===this)closeWinRateModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:860px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 style="margin:0;color:#E07B39;font-size:1.1rem;display:flex;align-items:center;gap:0.4rem;"><i data-lucide="settings" class="lc-icon"></i> Configuration</h2>
            <button onclick="closeWinRateModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
        {_render_config_grid(settings)}
    </div>
</div>
<div class="container">

    <!-- KPI Bar -->
    <div class="kpi-updated">Live data · updated <span id="refresh-kpi">—</span></div>
    <div class="kpi-bar" id="frag-kpi_bar">{frags['kpi_bar']}</div>

    <!-- Market Data -->
    <div class="section">
        <div class="section-header" data-sid="market">
            <span class="section-title"><i data-lucide="trending-up" class="lc-icon"></i> Market Data — Real-time Prices</span>
            <span class="section-refresh">updated <span id="refresh-market">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <table class="market-table">
                <thead>
                    <tr>
                        <th>Epic</th>
                        <th>Bid</th>
                        <th>Offer</th>
                        <th>Spread</th>
                        <th>High / Low</th>
                        <th>Bid % range</th>
                        <th>Candles</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody id="frag-market_rows">{frags['market_rows']}</tbody>
            </table>
        </div>
    </div>

    <!-- Weekly Summary -->
    <div class="section">
        <div class="section-header" data-sid="week">
            <span class="section-title"><i data-lucide="calendar" class="lc-icon"></i> Weekly Summary</span>
            <span class="section-refresh">updated <span id="refresh-week">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <div id="frag-week_summary">{frags['week_summary']}</div>
        </div>
    </div>

    <!-- Daily History -->
    <div class="section">
        <div class="section-header" data-sid="day">
            <span class="section-title"><i data-lucide="calendar-days" class="lc-icon"></i> Daily History</span>
            <span class="section-refresh">updated <span id="refresh-day">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <div id="frag-day_history">{frags['day_history']}</div>
        </div>
    </div>

    <!-- Actions -->
    <div class="section">
        <div class="section-header" data-sid="actions">
            <span class="section-title"><i data-lucide="zap" class="lc-icon"></i> Actions</span>
            <span class="section-refresh">updated <span id="refresh-actions">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <div class="actions-toolbar">
                <span class="actions-hint">Switch each job between automatic and manual. Manual jobs expose a Run button.</span>
                <div class="actions-bulk">
                    <button class="bulk-btn enable" onclick="setAllJobs(true, this)"><i data-lucide="play" class="lc-icon"></i> Enable all</button>
                    <button class="bulk-btn disable" onclick="setAllJobs(false, this)"><i data-lucide="pause" class="lc-icon"></i> Pause all</button>
                </div>
            </div>
            <div class="actions-grid" id="frag-actions">{frags['actions']}
            </div>
        </div>
    </div>

    <!-- Python Commands (bottom) -->
    <div class="section">
        <div class="section-header" data-sid="commands">
            <span class="section-title"><i data-lucide="clipboard-list" class="lc-icon"></i> Python Commands</span>
            <span class="section-refresh">updated <span id="refresh-commands">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <div class="command-list">
                <div class="command">
                    <div class="command-name">python -m src.main</div>
                    <div class="command-desc">Start the bot (scheduler only, no web UI)</div>
                </div>
                <div class="command">
                    <div class="command-name">python -m src.main --web</div>
                    <div class="command-desc">Start the bot + this web dashboard on port {settings.web_port}</div>
                </div>
                <div class="command">
                    <div class="command-name">python -m src.main --analyze-only</div>
                    <div class="command-desc">Single analysis pass, print signals, no trading</div>
                </div>
                <div class="command">
                    <div class="command-name">python -m src.main --web --log-level DEBUG</div>
                    <div class="command-desc">Bot + web + verbose debug logging</div>
                </div>
                <div class="command">
                    <div class="command-name">python -m src.main --analyze-only --epics IX.D.DAX.IFMM.IP</div>
                    <div class="command-desc">Analyze a specific epic only</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Server Logs -->
    <div class="section">
        <div class="section-header" data-sid="logs">
            <span class="section-title"><i data-lucide="terminal" class="lc-icon"></i> Server Logs</span>
            <span class="section-refresh">updated <span id="refresh-logs">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <div id="frag-logs_section">{frags['logs_section']}</div>
        </div>
    </div>

    <!-- Navigation (bottom) -->
    <nav style="margin-bottom:0; margin-top:1.5rem;">
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/positions" target="_blank">Positions<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/positions/summary" target="_blank">Daily Summary<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/api/status" target="_blank">API Status<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/api/prices/IX.D.DAX.IFMM.IP" target="_blank">Prices (DAX)<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
        </ul>
        <button id="btn-pause" class="nav-btn" onclick="togglePause()" style="display:inline-flex;align-items:center;gap:0.4rem;"><i data-lucide="pause" class="lc-icon"></i> Pause</button>
    </nav>

    <footer id="footer-refresh">Live — updating every 2 s</footer>
</div>

<script>
// ── Toast notifications ─────────────────────────────────────────────────────
const _TOAST_ICONS = {{ success:'circle-check', error:'circle-x', warning:'alert-circle', info:'info' }};
const _TOAST_SVG = {{
    'circle-check':  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>',
    'circle-x':      '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></svg>',
    'alert-circle':  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    'info':          '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
}};

function showToast(title, msg, type) {{
    type = type || 'info';
    let container = document.getElementById('toast-container');
    if (!container) {{
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }}
    const icon = _TOAST_SVG[_TOAST_ICONS[type]] || _TOAST_SVG['info'];
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML =
        '<span class="toast-icon">' + icon + '</span>'
        + '<div class="toast-body">'
        + (title ? '<div class="toast-title">' + title + '</div>' : '')
        + (msg   ? '<div class="toast-msg">'   + msg   + '</div>' : '')
        + '</div>';
    container.appendChild(toast);
    const timer = setTimeout(function() {{ _dismissToast(toast); }}, 4500);
    toast.addEventListener('click', function() {{ clearTimeout(timer); _dismissToast(toast); }});
}}

function _dismissToast(toast) {{
    toast.classList.add('toast-out');
    toast.addEventListener('animationend', function() {{ toast.remove(); }}, {{ once: true }});
}}

// ── Collapse state persistence ──────────────────────────────────────────────
const STORAGE_KEY = 'ig_sections_v1';

function _loadState() {{
    try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}'); }}
    catch {{ return {{}}; }}
}}

function _saveState(sid, collapsed) {{
    const s = _loadState();
    s[sid] = collapsed;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
}}

function _toggleSection(header) {{
    const body = header.nextElementSibling;
    const btn  = header.querySelector('.section-toggle');
    if (!body || !btn) return;
    const collapsed = body.classList.toggle('collapsed');
    btn.textContent = collapsed ? '+' : '−';
    if (header.dataset.sid) _saveState(header.dataset.sid, collapsed);
}}

// Event delegation — handles both existing and dynamically added headers
document.addEventListener('click', function(e) {{
    const header = e.target.closest('.section-header');
    if (header) _toggleSection(header);
}});

// Restore collapse state from localStorage before first paint
document.addEventListener('DOMContentLoaded', function() {{
    const saved = _loadState();
    document.querySelectorAll('.section-header[data-sid]').forEach(function(header) {{
        const sid = header.dataset.sid;
        if (saved[sid]) {{
            const body = header.nextElementSibling;
            const btn  = header.querySelector('.section-toggle');
            if (body) body.classList.add('collapsed');
            if (btn)  btn.textContent = '+';
        }}
    }});
}});

// ── Job mode switches (Actions section) ─────────────────────────────────────
// Each job toggles between automatic (scheduled) and manual (paused, Run-only).
// State changes only on user action, so the switches are not touched by the
// 2 s live poll — that would fight with mid-toggle interaction.
function _applyJobModeUI(card, auto) {{
    const runBtn    = card.querySelector('.run-btn');
    const modeLabel = card.querySelector('.action-card-mode');
    if (runBtn) runBtn.style.display = auto ? 'none' : '';
    if (modeLabel) {{
        modeLabel.textContent = auto ? 'Automatic' : 'Manual';
        modeLabel.className   = 'action-card-mode ' + (auto ? 'auto' : 'manual');
    }}
    card.classList.toggle('is-auto', auto);
}}

async function toggleJobMode(action, cb) {{
    const auto = cb.checked;
    const card = cb.closest('.action-card');
    cb.disabled = true;
    try {{
        const res = await fetch('/api/jobs/' + action + '/' + (auto ? 'auto' : 'manual'), {{ method: 'POST' }});
        if (res.ok) {{
            _applyJobModeUI(card, auto);
            showToast(action.replace(/_/g, ' '), auto ? 'Now automatic' : 'Now manual', auto ? 'success' : 'info');
        }} else {{
            cb.checked = !auto;  // revert on failure
            showToast(action.replace(/_/g, ' '), 'Failed to change mode', 'error');
        }}
    }} catch (e) {{
        cb.checked = !auto;
        showToast(action.replace(/_/g, ' '), 'Network error', 'error');
    }} finally {{
        cb.disabled = false;
    }}
}}

// General pause-all / resume-all — flips every job at once.
async function setAllJobs(auto, btn) {{
    btn.disabled = true;
    try {{
        const res = await fetch(auto ? '/api/bot/resume' : '/api/bot/pause', {{ method: 'POST' }});
        if (res.ok) {{
            document.querySelectorAll('.action-card[data-action]').forEach(function(card) {{
                const cb = card.querySelector('.switch input');
                if (cb) cb.checked = auto;
                _applyJobModeUI(card, auto);
            }});
            showToast(auto ? 'All jobs automatic' : 'All jobs manual', null, auto ? 'success' : 'warning');
        }} else {{
            showToast('Error', 'Failed to switch all jobs', 'error');
        }}
    }} catch (e) {{
        showToast('Error', 'Network error', 'error');
    }} finally {{
        btn.disabled = false;
    }}
}}

// ── Live fragment polling (single request → in-place section updates) ────────
// One request every POLL_INTERVAL returns the HTML for every dynamic region.
// Only the fragments whose markup actually changed are swapped into the DOM, so
// there is no full-page reload, no scroll jump and no lost UI state.
const PAUSE_KEY     = 'ig_refresh_paused';
const POLL_INTERVAL = 2000; // ms
const LIVE_STAMPS   = ['refresh-kpi', 'refresh-market', 'refresh-week', 'refresh-day', 'refresh-queue', 'refresh-api', 'refresh-positions', 'refresh-closed', 'refresh-actions', 'refresh-logs'];
const btnPause  = document.getElementById('btn-pause');
const footer    = document.getElementById('footer-refresh');

let _paused      = localStorage.getItem(PAUSE_KEY) === 'true';
let _timeoutId   = null;
let _polling     = false;
const _lastFrags = {{}};

function _stamp(id, value) {{
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}}

function _applyPauseUI() {{
    if (_paused) {{
        btnPause.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Resume';
        btnPause.classList.add('paused');
        footer.textContent   = 'Live updates paused';
    }} else {{
        btnPause.innerHTML = '<i data-lucide="pause" class="lc-icon"></i> Pause';
        btnPause.classList.remove('paused');
        footer.textContent   = 'Live — updating every 2 s';
    }}
    lucide.createIcons();
}}

function togglePause() {{
    _paused = !_paused;
    localStorage.setItem(PAUSE_KEY, _paused ? 'true' : 'false');
    if (_paused) {{
        if (_timeoutId) clearTimeout(_timeoutId);
        _applyPauseUI();
    }} else {{
        _applyPauseUI();
        _poll();  // resume immediately
    }}
}}

// Swap only the fragments whose HTML actually changed since the last poll.
function _applyFragments(frags) {{
    let changed = false;
    for (const id in frags) {{
        const el = document.getElementById('frag-' + id);
        if (!el) continue;
        if (_lastFrags[id] !== frags[id]) {{
            el.innerHTML   = frags[id];
            _lastFrags[id] = frags[id];
            changed = true;
        }}
    }}
    if (changed) lucide.createIcons();
    _reapplyLogFilter();
}}

function _scheduleNextPoll() {{
    if (_timeoutId) clearTimeout(_timeoutId);
    _timeoutId = setTimeout(_poll, POLL_INTERVAL);
}}

async function _poll() {{
    if (_paused || _polling) return;
    _polling = true;
    try {{
        const ctrl = new AbortController();
        const t    = setTimeout(() => ctrl.abort(), 5000);
        const res  = await fetch('/api/dashboard-fragments', {{ signal: ctrl.signal }});
        clearTimeout(t);
        if (res.ok) {{
            const data = await res.json();
            _applyFragments(data.fragments || {{}});
            const st = data.server_time || '';
            LIVE_STAMPS.forEach(function(id) {{ _stamp(id, st); }});
        }}
    }} catch (_) {{}}
    finally {{
        _polling = false;
        if (!_paused) _scheduleNextPoll();
    }}
}}

// Static sections (config, actions, commands) never change between polls — stamp
// them once with the page-load time; the live sections are stamped on each poll.
(function _initStamps() {{
    const t = new Date().toLocaleTimeString('fr-FR', {{ hour12: false }});
    ['refresh-commands'].forEach(function(id) {{ _stamp(id, t); }});
    LIVE_STAMPS.forEach(function(id) {{ _stamp(id, t); }});
}})();

_applyPauseUI();
if (!_paused) {{
    _scheduleNextPoll();
}}

async function clearErrors() {{
    try {{
        await fetch('/api/ig-errors/clear', {{ method: 'POST' }});
        document.getElementById('err-tbody').innerHTML =
            '<tr><td colspan="6" class="err-empty">No API errors recorded this session.</td></tr>';
        showToast('Error log cleared', null, 'info');
    }} catch (e) {{
        console.error('Clear errors failed', e);
        showToast('Error', 'Failed to clear error log', 'error');
    }}
}}

// ── KPI refresh buttons ─────────────────────────────────────────────────────
async function runKpiAction(action, btn) {{
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle" class="lc-icon lc-spin"></i>';
    lucide.createIcons();
    try {{
        const res = await fetch('/api/actions/' + action, {{ method: 'POST' }});
        if (res.ok) {{
            btn.textContent = '✓';
            showToast(action.replace(/_/g, ' '), 'Task triggered successfully', 'success');
        }} else {{
            btn.textContent = '✗';
            showToast(action.replace(/_/g, ' '), 'Task failed (HTTP ' + res.status + ')', 'error');
        }}
    }} catch (e) {{
        btn.textContent = '✗';
        showToast(action.replace(/_/g, ' '), 'Network error', 'error');
    }} finally {{
        setTimeout(() => {{
            btn.innerHTML = '<i data-lucide="refresh-cw" class="lc-icon"></i>';
            lucide.createIcons();
            btn.disabled = false;
        }}, 3000);
    }}
}}

// ── Manual actions ──────────────────────────────────────────────────────────
async function runAction(action, btn, needsConfirm) {{
    if (needsConfirm && !confirm('Run "' + action + '"? This action may affect live positions or data.')) return;
    const card   = btn.closest('.action-card');
    const status = card.querySelector('.action-status');
    btn.disabled = true;
    status.className = 'action-status running';
    status.innerHTML = '<i data-lucide="loader-circle" class="lc-icon lc-spin"></i> running…';
    lucide.createIcons();
    try {{
        const res  = await fetch('/api/actions/' + action, {{ method: 'POST' }});
        const data = await res.json();
        if (res.ok) {{
            status.className = 'action-status ok';
            status.textContent = '✓ triggered';
            showToast(action.replace(/_/g, ' '), 'Task triggered successfully', 'success');
        }} else {{
            const errMsg = data.error || 'error';
            status.className = 'action-status err';
            status.textContent = '✗ ' + errMsg;
            showToast(action.replace(/_/g, ' '), errMsg, 'error');
        }}
    }} catch (e) {{
        status.className = 'action-status err';
        status.textContent = '✗ network error';
        showToast(action.replace(/_/g, ' '), 'Network error', 'error');
    }} finally {{
        btn.disabled = false;
        setTimeout(() => {{
            status.className = 'action-status';
            status.textContent = '';
        }}, 6000);
    }}
}}

// ── Truncated text modal ────────────────────────────────────────────────────
function showModal(el) {{
    const full = el.dataset.full;
    if (!full) return;
    let modal = document.getElementById('text-modal');
    if (!modal) {{
        modal = document.createElement('div');
        modal.id = 'text-modal';
        modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9000;display:flex;align-items:center;justify-content:center;';
        modal.innerHTML = '<div style="background:#1e293b;border:1px solid #334155;border-radius:6px;padding:1.2rem 1.5rem;max-width:680px;width:90%;max-height:60vh;overflow:auto;">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem;">'
            + '<span style="color:#94a3b8;font-size:0.75rem;text-transform:uppercase;letter-spacing:1px;">Full value</span>'
            + '<button onclick="document.getElementById(\\'text-modal\\').remove()" style="background:none;border:none;color:#94a3b8;cursor:pointer;font-size:1.1rem;">✕</button>'
            + '</div>'
            + '<pre id="text-modal-body" style="white-space:pre-wrap;word-break:break-all;color:#e2e8f0;font-family:monospace;font-size:0.82rem;margin:0;"></pre>'
            + '</div>';
        modal.addEventListener('click', function(e) {{
            if (e.target === modal) modal.remove();
        }});
        document.body.appendChild(modal);
    }}
    document.getElementById('text-modal-body').textContent = full;
    document.getElementById('text-modal').style.display = 'flex';
}}

// ── Queue modal ──────────────────────────────────────────────────────────────
function openQueueModal() {{
    document.getElementById('queue-modal').style.display = 'block';
}}
function closeQueueModal() {{
    document.getElementById('queue-modal').style.display = 'none';
}}

// ── IG API modal ──────────────────────────────────────────────────────────────
function openApiModal() {{
    document.getElementById('ig-api-modal').style.display = 'block';
}}
function closeApiModal() {{
    document.getElementById('ig-api-modal').style.display = 'none';
}}

// ── Open Positions modal ──────────────────────────────────────────────────────
function openPositionsModal() {{
    document.getElementById('positions-modal').style.display = 'block';
}}
function closePositionsModal() {{
    document.getElementById('positions-modal').style.display = 'none';
}}

// ── Closed Positions modal ────────────────────────────────────────────────────
function openClosedModal() {{
    document.getElementById('closed-modal').style.display = 'block';
}}
function closeClosedModal() {{
    document.getElementById('closed-modal').style.display = 'none';
}}

// ── Win Rate / Configuration modal ────────────────────────────────────────────
function openWinRateModal() {{
    document.getElementById('winrate-modal').style.display = 'block';
}}
function closeWinRateModal() {{
    document.getElementById('winrate-modal').style.display = 'none';
}}

// ── Buy Confirmation Modal ────────────────────────────────────────────────────
let _buyConfirmResolve = null;

function openBuyConfirmModal(epic) {{
    return new Promise(function(resolve) {{
        _buyConfirmResolve = resolve;
        document.getElementById('buy-confirm-epic').textContent = epic;
        document.getElementById('buy-confirm-modal').style.display = 'flex';
    }});
}}

function closeBuyConfirmModal(confirmed) {{
    document.getElementById('buy-confirm-modal').style.display = 'none';
    if (_buyConfirmResolve) {{
        _buyConfirmResolve(confirmed);
        _buyConfirmResolve = null;
    }}
}}

// ── Close Position Confirmation Modal ────────────────────────────────────────
let _closeConfirmResolve = null;

function openCloseConfirmModal(epic) {{
    return new Promise(function(resolve) {{
        _closeConfirmResolve = resolve;
        document.getElementById('close-confirm-epic').textContent = epic;
        document.getElementById('close-confirm-modal').style.display = 'flex';
    }});
}}

function closeCloseConfirmModal(confirmed) {{
    document.getElementById('close-confirm-modal').style.display = 'none';
    if (_closeConfirmResolve) {{
        _closeConfirmResolve(confirmed);
        _closeConfirmResolve = null;
    }}
}}

document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') {{
        if (document.getElementById('buy-confirm-modal').style.display !== 'none') closeBuyConfirmModal(false);
        if (document.getElementById('close-confirm-modal').style.display !== 'none') closeCloseConfirmModal(false);
        if (document.getElementById('positions-modal').style.display !== 'none') closePositionsModal();
        if (document.getElementById('closed-modal').style.display !== 'none') closeClosedModal();
        if (document.getElementById('winrate-modal').style.display !== 'none') closeWinRateModal();
    }}
}});

// ── Open position (manual BUY from dashboard) ─────────────────────────────────
async function openPosition(epic, btn) {{
    const confirmed = await openBuyConfirmModal(epic);
    if (!confirmed) return;
    const origText  = btn.textContent;
    const origBg    = btn.style.background;
    const origColor = btn.style.color;
    btn.disabled = true;
    btn.textContent = '…';
    try {{
        const res  = await fetch('/api/positions/open/' + encodeURIComponent(epic), {{ method: 'POST' }});
        const data = await res.json();
        if (res.ok) {{
            btn.textContent      = '✓';
            btn.style.background = '#16803c';
            btn.style.color      = '#f0fdf4';
            btn.title = 'Opened @ ' + data.level + ' qty=' + data.quantity;
            showToast('Position opened', epic + ' @ ' + data.level + ' — qty ' + data.quantity, 'success');
        }} else {{
            btn.textContent      = '✗';
            btn.style.background = '#991b1b';
            btn.style.color      = '#fef2f2';
            btn.title = data.error || 'Error';
            showToast('Order failed', data.error || 'Could not open position', 'error');
        }}
    }} catch(e) {{
        btn.textContent      = '✗';
        btn.style.background = '#991b1b';
        btn.style.color      = '#fef2f2';
        btn.title = e.message;
        showToast('Order failed', e.message, 'error');
    }} finally {{
        setTimeout(function() {{
            btn.textContent      = origText;
            btn.style.background = origBg;
            btn.style.color      = origColor;
            btn.title            = 'Open BUY position at minimum size';
            btn.disabled         = false;
        }}, 5000);
    }}
}}

// ── Close position (manual SELL from positions modal) ────────────────────────
async function closePosition(positionId, epic, btn) {{
    const confirmed = await openCloseConfirmModal(epic);
    if (!confirmed) return;
    const origText  = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {{
        const res  = await fetch('/api/positions/close/' + positionId, {{ method: 'POST' }});
        const data = await res.json();
        if (res.ok) {{
            btn.textContent      = '✓';
            btn.style.background = '#16803c';
            btn.style.color      = '#f0fdf4';
            const pnlStr = data.pnl !== undefined ? ' P&L €' + (data.pnl >= 0 ? '+' : '') + data.pnl.toFixed(2) : '';
            showToast('Position closed', epic + ' @ ' + data.level + pnlStr, 'success');
            // Disable the row to prevent double-close
            btn.closest('tr').style.opacity = '0.45';
        }} else {{
            btn.textContent      = '✗';
            btn.style.background = '#991b1b';
            btn.style.color      = '#fef2f2';
            showToast('Close failed', data.error || 'Could not close position', 'error');
            setTimeout(function() {{
                btn.textContent      = origText;
                btn.style.background = '';
                btn.style.color      = '';
                btn.disabled         = false;
            }}, 5000);
        }}
    }} catch(e) {{
        btn.textContent      = '✗';
        btn.style.background = '#991b1b';
        btn.style.color      = '#fef2f2';
        showToast('Close failed', e.message, 'error');
        setTimeout(function() {{
            btn.textContent      = origText;
            btn.style.background = '';
            btn.style.color      = '';
            btn.disabled         = false;
        }}, 5000);
    }}
}}

// ── Chart modal ──────────────────────────────────────────────────────────────

// Convert a UTC ISO string (e.g. "2026-06-08T08:30:00+00:00") to a naive local
// datetime string in Europe/Paris (e.g. "2026-06-08T10:30:00") so that Plotly
// displays the correct French hour without applying any extra offset.
function _toParisNaive(utcISOStr) {{
    try {{
        const d = new Date(utcISOStr);
        const parts = new Intl.DateTimeFormat('en-CA', {{
            timeZone: 'Europe/Paris',
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false,
        }}).formatToParts(d);
        const p = {{}};
        parts.forEach(function({{type, value}}) {{ p[type] = value; }});
        return p.year + '-' + p.month + '-' + p.day + 'T' + p.hour + ':' + p.minute + ':' + p.second;
    }} catch (_) {{
        return utcISOStr;
    }}
}}

async function openChartModal(epic) {{
    const modal     = document.getElementById('chart-modal');
    const titleEl   = document.getElementById('chart-modal-title');
    const container = document.getElementById('chart-container');
    titleEl.innerHTML = '<i data-lucide="trending-up" class="lc-icon"></i> ' + epic;
    lucide.createIcons();
    container.innerHTML = '<div style="color:#64748b;padding:3rem;text-align:center;">Loading…</div>';
    modal.style.display = 'block';
    try {{
        const res = await fetch('/api/prices/' + encodeURIComponent(epic));
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (!data.bid_closes || !data.bid_closes.length) {{
            container.innerHTML = '<div style="color:#64748b;padding:3rem;text-align:center;">No price data available yet.</div>';
            return;
        }}
        const rawBids = data.bid_closes;
        const utcTimestamps = data.timestamps && data.timestamps.length ? data.timestamps : null;
        // Convert UTC → Paris naive strings so Plotly displays the correct local hour.
        const timestamps = utcTimestamps
            ? utcTimestamps.map(_toParisNaive)
            : rawBids.map(function(_, i) {{ return i + 1; }});
        const minBid = Math.min.apply(null, rawBids);
        const maxBid = Math.max.apply(null, rawBids);
        const range = maxBid - minBid;
        const pctY = rawBids.map(function(v) {{ return range === 0 ? 50 : (v - minBid) / range * 100; }});
        const xIsDate = utcTimestamps !== null;
        Plotly.newPlot(container, [{{
            x: timestamps,
            y: pctY,
            customdata: rawBids,
            type: 'scatter',
            mode: 'lines',
            line: {{ color: '#E07B39', width: 1.5 }},
            name: 'Bid close',
            hovertemplate: 'Bid: %{{customdata:.4f}}<br>%{{y:.1f}}<extra></extra>'
        }}], {{
            paper_bgcolor: '#1c1714',
            plot_bgcolor: '#1c1714',
            font: {{ color: '#94a3b8', size: 11 }},
            margin: {{ l: 55, r: 20, t: 10, b: 50 }},
            xaxis: {{
                gridcolor: '#2d2319',
                zerolinecolor: '#2d2319',
                type: xIsDate ? 'date' : 'linear',
                tickformat: xIsDate ? '%H:%M' : '',
                title: ''
            }},
            yaxis: {{
                gridcolor: '#2d2319',
                zerolinecolor: '#2d2319',
                ticksuffix: '%',
                range: [-3, 103],
                title: ''
            }},
        }}, {{ responsive: true, displayModeBar: false }});
    }} catch(e) {{
        container.innerHTML = '<div style="color:#ef4444;padding:3rem;text-align:center;">Failed to load data: ' + e.message + '</div>';
    }}
}}

function closeChartModal() {{
    document.getElementById('chart-modal').style.display = 'none';
    Plotly.purge(document.getElementById('chart-container'));
}}

// ── Server log filter ─────────────────────────────────────────────────────────
let _currentLogFilter = 'all';

function filterLogs(level, _btn) {{
    _currentLogFilter = level;
    _reapplyLogFilter();
}}

function _reapplyLogFilter() {{
    document.querySelectorAll('.log-filter-btn').forEach(function(btn) {{
        btn.classList.toggle('active', btn.dataset.level === _currentLogFilter);
    }});
    document.querySelectorAll('.log-row').forEach(function(row) {{
        const show = _currentLogFilter === 'all' || row.dataset.level === _currentLogFilter;
        row.style.display = show ? '' : 'none';
    }});
}}

lucide.createIcons();
</script>
</body>
</html>"""
