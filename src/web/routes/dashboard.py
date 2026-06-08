"""Dashboard route — main overview page."""

import asyncio
import html
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from src.models.epic import Epic
from src.models.position import Position, PositionState, PositionStrategy
from src.services.api_error_log import APIErrorEntry
from src.services.api_queue import Priority
from src.services.market_scanner import MarketInfo

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


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard page."""
    buffer = request.app.state.buffer
    settings = request.app.state.settings
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
                select(Position).where(
                    Position.date == today, Position.state == PositionState.CLOSE
                )
            )
            closed_positions = list(closed_pos)

            kpis["open_trades"] = len(open_positions)
            kpis["open_pnl"] = sum(float(p.euro or 0) for p in open_positions)
            kpis["closed_trades"] = len(closed_positions)
            kpis["daily_pnl"] = sum(float(p.euro or 0) for p in closed_positions)
            kpis["win_rate"] = (
                sum(1 for p in closed_positions if (p.win or 0) > 0)
                / len(closed_positions)
                if closed_positions
                else 0.0
            )
    else:
        kpis = {
            "available_epics": len(epics),
            "open_trades": 0,
            "open_pnl": 0.0,
            "closed_trades": 0,
            "daily_pnl": 0.0,
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

    html = _render_dashboard(
        market_summary,
        settings,
        kpis,
        guard_stats,
        error_entries,
        queue_stats,
        queue_recent,
        queue_pending_tasks,
        open_positions,
    )
    return HTMLResponse(content=html)


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
    """Suspend all scheduled bot jobs (API calls stop)."""
    scheduler = request.app.state.scheduler
    if scheduler:
        scheduler.pause_bot()
    return JSONResponse({"bot_paused": True})


@router.post("/api/bot/resume")
async def bot_resume(request: Request) -> JSONResponse:
    """Resume all suspended bot jobs."""
    scheduler = request.app.state.scheduler
    if scheduler:
        scheduler.resume_bot()
    return JSONResponse({"bot_paused": False})


_ACTIONS = {
    "refresh_epic_list",
    "refresh_tradable_epics",
    "collect_and_analyze",
    "monitor_positions",
    "end_of_day",
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
        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
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
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            level_open=Decimal(str(round(open_level, 3))),
            level_win=Decimal(str(round(open_level + stop_distance * 2, 3))),
            level_zero=Decimal(str(round(open_level, 3))),
            level_follower=Decimal(str(round(open_level - stop_distance * 0.5, 3))),
            level_loose=Decimal(str(round(open_level - stop_distance, 3))),
            level_security=Decimal(str(round(open_level - stop_distance * 0.8, 3))),
            level_stop=Decimal(str(round(open_level - stop_distance, 3))),
            pip_spread=Decimal(str(round(current_spread, 3))),
            quantity=quantity,
            size=int(stop_distance),
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


def _render_epic_list_page(
    epics: list[str],
    db_epics: dict,
    last_refresh: datetime | None,
) -> str:
    """Render the full epic list page."""
    today = date.today()
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


def _render_dashboard(
    market_summary: list[dict],
    settings,
    kpis: dict,
    guard_stats=None,
    error_entries: list[APIErrorEntry] | None = None,
    queue_stats=None,
    queue_recent=None,
    queue_pending_tasks=None,
    open_positions=None,
) -> str:
    """Render the dashboard with nav, KPIs, commands, config and market data."""
    if error_entries is None:
        error_entries = []
    if queue_recent is None:
        queue_recent = []
    if queue_pending_tasks is None:
        queue_pending_tasks = []
    if open_positions is None:
        open_positions = []

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
            pos_rows_html += f"""
                    <tr>
                        <td class="err-ts">{html.escape(t_open)}</td>
                        <td class="epic-col">{html.escape(p.epic)}</td>
                        <td class="desc-col">{html.escape(p.epic_name)}</td>
                        <td class="number">{lvl_open}</td>
                        <td class="number">{lvl_win}</td>
                        <td class="number">{lvl_stop}</td>
                        <td class="number">{qty}</td>
                        <td class="number" style="color:{pnl_color};">{pnl_str}</td>
                        <td class="err-ts">{html.escape(strategy_str)}</td>
                    </tr>"""
    else:
        pos_rows_html = '<tr><td colspan="9" class="err-empty">No open positions right now.</td></tr>'

    market_rows = ""
    for s in market_summary:
        pct = _bid_pct(s["bid"], s["low"], s["high"])
        pct_color = "#4ade80" if pct >= 50 else "#f59e0b" if pct >= 25 else "#ef4444"
        epic_esc = html.escape(str(s['epic']))
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
        queue_section_label = '<i data-lucide="inbox" class="lc-icon"></i> API Queue (off)'
        queue_todo = queue_running = queue_succeeded = "—"
        queue_failed = queue_retried = queue_rate_limited = "—"
        queue_failed_color = queue_rl_color = "#94a3b8"
        queue_todo_color = "#94a3b8"
        queue_kpi_border = "#475569"
    else:
        todo_count = queue_stats.pending
        queue_section_label = f'<i data-lucide="inbox" class="lc-icon"></i> API Queue ({todo_count} todo)'
        queue_todo = todo_count
        queue_running = queue_stats.running
        queue_succeeded = queue_stats.succeeded
        queue_failed = queue_stats.failed
        queue_retried = queue_stats.retried
        queue_rate_limited = queue_stats.rate_limited
        queue_failed_color = "#ef4444" if queue_stats.failed else "#4ade80"
        queue_rl_color = "#f59e0b" if queue_stats.rate_limited else "#94a3b8"
        queue_todo_color = "#f59e0b" if todo_count else "#94a3b8"
        queue_kpi_border = "#ef4444" if queue_stats.failed else ("#f59e0b" if todo_count else "#4ade80")

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
        </table>
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
        </div>
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
<!-- Open Positions Modal -->
<div id="positions-modal" onclick="if(event.target===this)closePositionsModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:8500;overflow-y:auto;padding:2rem 1rem;">
    <div style="background:#1c1714;border:1px solid #4a3a30;border-radius:8px;max-width:960px;width:100%;margin:0 auto;padding:1.5rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1.2rem;">
            <h2 style="margin:0;color:#E07B39;font-size:1.1rem;display:flex;align-items:center;gap:0.4rem;"><i data-lucide="activity" class="lc-icon"></i> Open Positions</h2>
            <button onclick="closePositionsModal()" style="background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Close</button>
        </div>
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
                    </tr>
                </thead>
                <tbody>
                    {pos_rows_html}
                </tbody>
            </table>
        </div>
    </div>
</div>
<div id="refresh-bar-track"><div id="refresh-bar"></div></div>
<div class="container">

    <!-- KPI Bar -->
    <div class="kpi-bar">
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
            <div class="kpi-value" style="color:{open_pnl_color};">€{kpis['open_pnl']:.2f}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['open_trades']} position{'s' if kpis['open_trades'] != 1 else ''}</span></div>
        </div>
        <div class="kpi-tile" style="border-left-color:{pnl_color};">
            <div class="kpi-label">CLOSED {kpis['closed_trades']}</div>
            <div class="kpi-value" style="color:{pnl_color};">€{kpis['daily_pnl']:.2f}</div>
        </div>
        <div class="kpi-tile">
            <div class="kpi-label">Win rate</div>
            <div class="kpi-value">{kpis['win_rate']:.1%}</div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{api_border_color};" onclick="openApiModal()">
            <div class="kpi-label">IG API</div>
            <div class="kpi-value" style="color:{api_status_color};">{api_status_label}</div>
            <div class="kpi-sub">{api_status_sub}</div>
        </div>
        <div class="kpi-tile clickable" id="kpi-bot" style="position:relative; border-left-color:#94a3b8;" onclick="toggleBotPause()">
            <div class="kpi-label">Bot</div>
            <div class="kpi-value" id="kpi-bot-value" style="color:#94a3b8;">…</div>
            <div class="kpi-sub" id="kpi-bot-sub">Loading</div>
        </div>
    </div>

    <!-- Market Data -->
    <div class="section">
        <div class="section-header" data-sid="market">
            <span class="section-title"><i data-lucide="trending-up" class="lc-icon"></i> Market Data — Real-time Prices</span>
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
                <tbody>
                    {market_rows}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Configuration -->
    <div class="section">
        <div class="section-header" data-sid="config">
            <span class="section-title"><i data-lucide="settings" class="lc-icon"></i> Configuration</span>
            <button class="section-toggle">−</button>
        </div>
            <div class="section-body">
                <div class="config-grid">
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
                </div>
            </div>
        </div>

    <!-- Manual Actions -->
    <div class="section">
        <div class="section-header" data-sid="actions">
            <span class="section-title"><i data-lucide="zap" class="lc-icon"></i> Manual Actions</span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <div class="actions-grid">
                <div class="action-card">
                    <div class="action-card-name">Collect &amp; Analyze</div>
                    <div class="action-card-desc">Fetch latest prices, compute signals, open positions on BUY.</div>
                    <button class="action-btn safe" onclick="runAction('collect_and_analyze', this)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
                <div class="action-card">
                    <div class="action-card-name">Monitor Positions</div>
                    <div class="action-card-desc">Check all open positions and apply close strategy.</div>
                    <button class="action-btn safe" onclick="runAction('monitor_positions', this)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
                <div class="action-card">
                    <div class="action-card-name">Daily Summary</div>
                    <div class="action-card-desc">Generate or update today&apos;s P&amp;L record in the database.</div>
                    <button class="action-btn safe" onclick="runAction('daily_summary', this)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
                <div class="action-card">
                    <div class="action-card-name">Weekly Summary</div>
                    <div class="action-card-desc">Generate per-epic direction summaries for the current week.</div>
                    <button class="action-btn safe" onclick="runAction('weekly_summary', this)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
                <div class="action-card">
                    <div class="action-card-name">Daily Reset</div>
                    <div class="action-card-desc">Clear price buffer &mdash; all in-memory candle history is lost.</div>
                    <button class="action-btn warn" onclick="runAction('daily_reset', this, true)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
                <div class="action-card">
                    <div class="action-card-name">Dump &amp; Purge Candles</div>
                    <div class="action-card-desc">Export candles past the retention window to a CSV dump, then delete them from the table.</div>
                    <button class="action-btn warn" onclick="runAction('dump_and_purge_candles', this, true)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
                <div class="action-card">
                    <div class="action-card-name">End of Day</div>
                    <div class="action-card-desc">Force close ALL open positions immediately.</div>
                    <button class="action-btn danger" onclick="runAction('end_of_day', this, true)">&#9654; Run</button>
                    <div class="action-status"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Python Commands (bottom) -->
    <div class="section">
        <div class="section-header" data-sid="commands">
            <span class="section-title"><i data-lucide="clipboard-list" class="lc-icon"></i> Python Commands</span>
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

    <footer id="footer-refresh">Auto-refresh every 30 s</footer>
</div>

<script>
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

// ── Bot pause / resume (KPI card) ──────────────────────────────────────────
const kpiBotTile  = document.getElementById('kpi-bot');
const kpiBotValue = document.getElementById('kpi-bot-value');
const kpiBotSub   = document.getElementById('kpi-bot-sub');
let _botAvailable = false;
let _botPaused    = false;

function _applyBotState(paused, available) {{
    _botAvailable = available;
    _botPaused    = paused;
    if (!available) {{
        kpiBotValue.textContent          = '—';
        kpiBotValue.style.color          = '#94a3b8';
        kpiBotSub.textContent            = 'Unavailable';
        kpiBotTile.style.borderLeftColor = '#94a3b8';
        return;
    }}
    if (paused) {{
        kpiBotValue.textContent          = 'PAUSED';
        kpiBotValue.style.color          = '#ef4444';
        kpiBotSub.textContent            = 'Click to resume';
        kpiBotTile.style.borderLeftColor = '#ef4444';
    }} else {{
        kpiBotValue.textContent          = 'RUNNING';
        kpiBotValue.style.color          = '#4ade80';
        kpiBotSub.textContent            = 'Click to pause';
        kpiBotTile.style.borderLeftColor = '#4ade80';
    }}
}}

async function toggleBotPause() {{
    if (!_botAvailable) return;
    try {{
        const res  = await fetch(_botPaused ? '/api/bot/resume' : '/api/bot/pause', {{ method: 'POST' }});
        const data = await res.json();
        _applyBotState(data.bot_paused, true);
    }} catch (e) {{
        console.error('Bot toggle failed', e);
    }}
}}

(async function initBotState() {{
    try {{
        const res  = await fetch('/api/status');
        const data = await res.json();
        _applyBotState(data.bot_paused, data.scheduler_available);
    }} catch (e) {{
        _applyBotState(false, false);
    }}
}})();

// ── Refresh countdown bar & pause ──────────────────────────────────────────
const PAUSE_KEY = 'ig_refresh_paused';
const TOTAL     = 30000; // ms
const bar       = document.getElementById('refresh-bar');
const btnPause  = document.getElementById('btn-pause');
const footer    = document.getElementById('footer-refresh');


let _paused    = localStorage.getItem(PAUSE_KEY) === 'true';
let _rafId     = null;
let _timeoutId = null;

function _applyPauseUI() {{
    if (_paused) {{
        btnPause.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Resume';
        btnPause.classList.add('paused');
        bar.style.width      = '100%';
        bar.style.background = '#f59e0b';
        footer.textContent   = 'Auto-refresh paused';
    }} else {{
        btnPause.innerHTML = '<i data-lucide="pause" class="lc-icon"></i> Pause';
        btnPause.classList.remove('paused');
        footer.textContent   = 'Auto-refresh every 30 s';
    }}
    lucide.createIcons();
}}

function togglePause() {{
    _paused = !_paused;
    localStorage.setItem(PAUSE_KEY, _paused ? 'true' : 'false');
    if (_paused) {{
        if (_rafId)     cancelAnimationFrame(_rafId);
        if (_timeoutId) clearTimeout(_timeoutId);
        _applyPauseUI();
    }} else {{
        // Immediate refresh — page reload resets bar and timer
        location.reload();
    }}
}}

function _startRefreshBar() {{
    const start = performance.now();

    function frame(now) {{
        if (_paused) return;
        const elapsed  = now - start;
        const fraction = Math.max(0, 1 - elapsed / TOTAL);
        bar.style.width = (fraction * 100) + '%';
        if (fraction > 1/6) {{
            bar.style.background = '#E07B39';
        }} else {{
            const t = 1 - fraction * 6;
            const r = Math.round(0xe0 + t * (0xf5 - 0xe0));
            const g = Math.round(0x7b + t * (0x9e - 0x7b));
            const b = Math.round(0x39 + t * (0x0b - 0x39));
            bar.style.background = `rgb(${{r}},${{g}},${{b}})`;
        }}
        if (elapsed < TOTAL) {{
            _rafId = requestAnimationFrame(frame);
        }}
    }}

    _rafId     = requestAnimationFrame(frame);
    _timeoutId = setTimeout(_tryReload, TOTAL);
}}

async function _tryReload() {{
    if (_paused) return;
    try {{
        const ctrl = new AbortController();
        const t    = setTimeout(() => ctrl.abort(), 5000);
        const res  = await fetch('/api/status', {{ signal: ctrl.signal }});
        clearTimeout(t);
        if (!_paused && res.ok) {{ location.reload(); return; }}
    }} catch (_) {{}}
    if (!_paused) _startRefreshBar();
}}

window.addEventListener('beforeunload', function() {{
    const overlay = document.createElement('div');
    overlay.id = 'refresh-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(18,14,12,0.88);z-index:9999;display:flex;align-items:center;justify-content:center;color:#E07B39;font-size:1rem;letter-spacing:2px;pointer-events:all;';
    overlay.textContent = 'Refreshing…';
    document.body.appendChild(overlay);
    // Safety net: if the navigation stalls (slow/hung server response) the new
    // page never commits and this overlay would trap the UI forever. Remove it
    // and resume the refresh cycle so the dashboard stays usable.
    setTimeout(function() {{
        const o = document.getElementById('refresh-overlay');
        if (o) {{
            o.remove();
            if (!_paused) _startRefreshBar();
        }}
    }}, 12000);
}});

// Drop any stale overlay restored from the bfcache (back/forward navigation).
window.addEventListener('pageshow', function() {{
    const o = document.getElementById('refresh-overlay');
    if (o) o.remove();
}});

_applyPauseUI();
if (!_paused) {{
    _startRefreshBar();
}}

async function clearErrors() {{
    try {{
        await fetch('/api/ig-errors/clear', {{ method: 'POST' }});
        document.getElementById('err-tbody').innerHTML =
            '<tr><td colspan="6" class="err-empty">No API errors recorded this session.</td></tr>';
    }} catch (e) {{ console.error('Clear errors failed', e); }}
}}

// ── KPI refresh buttons ─────────────────────────────────────────────────────
async function runKpiAction(action, btn) {{
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle" class="lc-icon lc-spin"></i>';
    lucide.createIcons();
    try {{
        const res = await fetch('/api/actions/' + action, {{ method: 'POST' }});
        btn.textContent = res.ok ? '✓' : '✗';
    }} catch (e) {{
        btn.textContent = '✗';
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
        }} else {{
            status.className = 'action-status err';
            status.textContent = '✗ ' + (data.error || 'error');
        }}
    }} catch (e) {{
        status.className = 'action-status err';
        status.textContent = '✗ network error';
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

document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') {{
        if (document.getElementById('buy-confirm-modal').style.display !== 'none') closeBuyConfirmModal(false);
        if (document.getElementById('positions-modal').style.display !== 'none') closePositionsModal();
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
        }} else {{
            btn.textContent      = '✗';
            btn.style.background = '#991b1b';
            btn.style.color      = '#fef2f2';
            btn.title = data.error || 'Error';
        }}
    }} catch(e) {{
        btn.textContent      = '✗';
        btn.style.background = '#991b1b';
        btn.style.color      = '#fef2f2';
        btn.title = e.message;
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

// ── Chart modal ──────────────────────────────────────────────────────────────
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
        const timestamps = data.timestamps && data.timestamps.length ? data.timestamps : rawBids.map(function(_, i) {{ return i + 1; }});
        const minBid = Math.min.apply(null, rawBids);
        const maxBid = Math.max.apply(null, rawBids);
        const range = maxBid - minBid;
        const pctY = rawBids.map(function(v) {{ return range === 0 ? 50 : (v - minBid) / range * 100; }});
        const xIsDate = data.timestamps && data.timestamps.length > 0;
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

lucide.createIcons();
</script>
</body>
</html>"""
