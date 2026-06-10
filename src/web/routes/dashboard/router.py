"""Dashboard HTTP routes (FastAPI endpoint handlers)."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.models.position import Position, PositionState, PositionStrategy
from src.services.api_queue import Priority
from src.utils.tools import _to_float, euro_per_point, margin_factor_pct
from src.web.routes.dashboard.fragments import _build_fragments
from src.web.routes.dashboard.pages import _render_tradable_list_page
from src.web.routes.dashboard.shell import _render_dashboard
from src.web.routes.dashboard.state import _PARIS, _gather_dashboard_state

logger = logging.getLogger(__name__)

router = APIRouter()


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
    """Switch a job between automatic (``mode=auto``) and manual
    (``mode=manual``)."""
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


@router.get("/api/positions/funds/{epic}")
async def position_funds_required(request: Request, epic: str) -> JSONResponse:
    """Estimate the margin required to open a minimum-size BUY on ``epic``.

    Powers the BUY-button hover so the user can tell, *before* clicking, whether
    the account has enough free cash — avoiding a wasted open call that IG would
    reject for insufficient funds. The ``/markets`` payload is cached per epic
    (margin factors barely change) so repeated hovers don't hit IG. ``available``
    reuses the dashboard's cached account balance (no extra call).
    """
    api_queue = getattr(request.app.state, "api_queue", None)
    buffer = request.app.state.buffer
    if not api_queue:
        return JSONResponse({"error": "Trading not available"}, status_code=503)

    buf = buffer.get(epic)
    if not buf or not buf.last:
        return JSONResponse({"error": "No price data for this epic"}, status_code=400)
    price = buf.last.bid_close

    # Per-epic /markets cache (10 min TTL) — margin factor / contract size are
    # near-static, so a hover storm resolves to one fetch per epic.
    cache = getattr(request.app.state, "margin_cache", None)
    if cache is None:
        cache = {}
        request.app.state.margin_cache = cache
    now = datetime.now(UTC)
    entry = cache.get(epic)
    market_data = None
    if entry and (now - entry[1]) < timedelta(minutes=10):
        market_data = entry[0]
    if market_data is None:
        try:
            market_data = await api_queue.get(
                f"/markets/{epic}",
                version=3,
                suppress_error_logging=True,
                label=f"funds {epic}: market",
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        cache[epic] = (market_data, now)

    instrument = market_data.get("instrument", {})
    dealing_rules = market_data.get("dealingRules", {})
    currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
    min_deal = _to_float(dealing_rules.get("minDealSize", {}).get("value", 1))
    quantity = max(int(min_deal), 1)

    # euro_per_point = size × contractSize × quote->EUR rate; times the price gives
    # the position's notional in EUR, times the margin factor gives the margin.
    epp = euro_per_point(market_data, quantity, currency)
    margin_pct = margin_factor_pct(instrument)
    margin_eur: float | None = None
    if epp and margin_pct is not None:
        margin_eur = epp * price * margin_pct / 100

    balance = getattr(request.app.state, "account_balance", None)
    available = None
    if isinstance(balance, dict) and balance.get("available") is not None:
        available = _to_float(balance.get("available"))

    sufficient = (
        available is not None and margin_eur is not None and available >= margin_eur
    )
    return JSONResponse(
        {
            "epic": epic,
            "quantity": quantity,
            "margin_eur": round(margin_eur, 2) if margin_eur is not None else None,
            "available_eur": round(available, 2) if available is not None else None,
            "sufficient": sufficient,
        }
    )


@router.post("/api/positions/open/{epic}")
async def open_position_manual(request: Request, epic: str) -> JSONResponse:
    """Open a BUY position at minimum deal size for the given epic.

    Triggered manually from the dashboard.
    """
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
