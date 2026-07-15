"""Dashboard HTTP routes (FastAPI endpoint handlers)."""

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from src.core.api_queue import Priority
from src.core.indicators import adverse_tick_noise
from src.entry.base import EntryIntent
from src.execution.trading import TradeConfig, TradingService
from src.models.position import Position, PositionState
from src.utils.tools import _to_float, euro_per_point, margin_factor_pct
from src.web.routes.dashboard.fragments import _build_fragments
from src.web.routes.dashboard.pages import _render_tradable_list_page
from src.web.routes.dashboard.shell import _render_dashboard
from src.web.routes.dashboard.state import (
    _PARIS,
    _gather_dashboard_state,
    _to_float_or_none,
    force_account_balance_refresh,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Noise band drawn under the bid on the position chart. Mirrors the defaults on
# the zone updaters (``BreakevenLockStop`` / ``TrailingRatchetStop`` in
# ``src/exit/zones``): the same ``adverse_tick_noise`` window scaled by
# ``noise_mult``, so the chart's noise-adjusted bid (``bid − band``) matches the
# band those gates actually test against — making the moment it clears
# break-even / margin (the zone the updaters see) visible on the chart.
_CHART_NOISE_WINDOW = 20
_CHART_NOISE_STD_K = 2.0
_CHART_NOISE_MULT = 2.0


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard page (initial full render)."""
    settings = request.app.state.settings
    state = await _gather_dashboard_state(request)
    return HTMLResponse(content=_render_dashboard(settings, state))


@router.get("/api/dashboard-fragments")
async def api_dashboard_fragments(request: Request) -> JSONResponse:
    """JSON API: HTML fragments for every live dashboard region.

    Polled by the dashboard every second. The client swaps only the
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
    "trend_select",
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


def _iso_utc(ts: datetime) -> str:
    """ISO-8601 string with an explicit UTC offset (naive values are tagged UTC)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def _trade_overlay(p: Position) -> dict:
    """Serialise one position's chart levels/markers for the chart modal.

    ``openTime``/``closeTime`` are UTC ISO strings (date + stored UTC time); the
    browser converts them to Europe/Paris for display.
    """

    def _num(attr: str) -> float | None:
        value = getattr(p, attr, None)
        return float(value) if value is not None else None

    def _level(attr: str) -> float | None:
        """Price level, or None when unset. A stored 0 means "no level" (e.g.
        strategies without a fixed win target); it must not reach the chart, where
        it would be folded into the normalisation bounds and flatten the bid curve
        against the top of the view."""
        value = _num(attr)
        return value if value and value > 0 else None

    def _ts(t: time | None) -> str | None:
        if p.date is None or t is None:
            return None
        return datetime.combine(p.date, t, tzinfo=UTC).isoformat()

    def _stops() -> list[dict] | None:
        """Sanitised stop trajectory points, or None when unavailable.

        Drops malformed/zero entries so a stored 0 level (treated as "unset"
        elsewhere) cannot flatten the chart's normalisation bounds.
        """
        raw = getattr(p, "stop_history", None)
        if not raw:
            return None
        points: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            when = entry.get("t")
            level = entry.get("level")
            if when is None or not level:
                continue
            point = {"t": when, "level": float(level)}
            # Level actually posted at the broker for this ratchet — one spread
            # below the software follower (see TradingService._broker_stop_level).
            # Kept internal here; the follower line uses ``level`` and the broker
            # ("Loose") line reads ``broker`` (falling back to ``level`` for legacy
            # points recorded before the two stops were decoupled).
            broker = entry.get("broker")
            if broker:
                point["broker"] = float(broker)
            points.append(point)
        return points or None

    # Entry/break-even/margin/exit are drawn on the BID curve the chart plots, so
    # they differ by direction:
    #   - LONG: filled on the offer, so the open bid is ``level_zero - spread`` and
    #     break-even is ``level_zero`` itself (one spread above the bid — the bid
    #     must climb back through the spread to reach flat).
    #   - SHORT: sold at the bid, so the entry marker is ``level_open`` (the fill).
    #     The close engine watches the offer (buy-to-close), so break-even, margin
    #     and the exit fill sit one spread BELOW their stored (offer) levels when
    #     mapped onto the bid curve.
    zero = _level("level_zero")
    spread = _num("pip_spread")
    level_open_v = _level("level_open")
    close_v = _level("level_close")
    margin_v = _level("level_margin")
    direction = getattr(p, "direction", "BUY") or "BUY"

    have_spread = spread is not None
    if direction == "SELL":
        open_bid: float | None = level_open_v
        zero_line = (zero - spread) if (zero is not None and have_spread) else zero
        margin_line = (
            (margin_v - spread)
            if (margin_v is not None and spread is not None)
            else margin_v
        )
        close_line = (
            (close_v - spread)
            if (close_v is not None and spread is not None)
            else close_v
        )
    else:
        if zero is not None and spread is not None:
            open_bid = zero - spread
        else:
            open_bid = level_open_v
        zero_line = zero
        margin_line = margin_v
        close_line = close_v

    # Two distinct protective-stop lines for the chart:
    #   - the FOLLOWER stop (``level_follower`` + its ratchet history): the
    #     application-side trailing stop that ratchets up with the market once
    #     break-even + margin is cleared. It is the level a close is decided on
    #     app-side, between two bid polls.
    #   - the LOOSE stop (``level_stop`` + its pushed updates): the protective
    #     stop actually resting at the broker, the safety net that secures a close
    #     if price gaps between two bid readings.
    # They diverge because the broker stop is deliberately posted one spread below
    # the software follower (see TradingService._broker_stop_level), so the app
    # closes first and the broker order is a pure safety net — from the open
    # onward (the offset is now applied at open too, not only on ratchets) and
    # also whenever IG's minimum-stop-distance rule widened the broker stop. The
    # loose trajectory is the broker's initial level (index 0, ``level_stop``,
    # already a spread below the follower) followed by the level actually pushed
    # to IG at each ratchet (the per-point ``broker``); legacy points without it
    # fall back to the follower level.
    raw_stops = _stops()
    # Follower (application-side) line: strip the internal per-point broker level
    # so the software-stop trajectory stays {t, level}.
    follower_stops = (
        [{"t": s["t"], "level": s["level"]} for s in raw_stops] if raw_stops else None
    )
    loose_initial = _level("level_stop")
    if not raw_stops:
        loose_stops = None
    else:
        loose_stops = []
        for i, s in enumerate(raw_stops):
            if i == 0 and loose_initial is not None:
                level = loose_initial
            else:
                level = s.get("broker", s["level"])
            loose_stops.append({"t": s["t"], "level": level})

    # ``profit10``: the bid level at which the trade's running P&L is +10 €,
    # derived from the stored ``euro_per_point`` (10 € / €-per-point in price
    # points away from break-even, in the profitable direction). Mapped onto the
    # bid curve like the other levels. (The +3 % line is a purely visual offset
    # from break-even and is computed client-side from the chart's range.)
    epp = _num("euro_per_point")
    profit10: float | None = None
    if zero_line is not None and epp:
        sign = -1.0 if direction == "SELL" else 1.0
        profit10 = zero_line + sign * (10.0 / epp)

    # Profit trigger — one further noise margin above the margin line (a second
    # symmetric band): the boundary above which the stop trails progressively
    # (CLOSE_ZONEPROFIT). Below it, but above break-even, the stop is parked on a
    # support inside the break-even→margin band (CLOSE_ZONEMARGE). Derived from the
    # two frozen reference lines (2 × margin − break-even), so it needs no extra
    # stored level and maps onto the bid curve exactly like them.
    profit_trigger: float | None = None
    if margin_line is not None and zero_line is not None:
        profit_trigger = margin_line + (margin_line - zero_line)

    return {
        "id": p.id,
        "direction": direction,
        "open": level_open_v,
        "openBid": open_bid,
        "zero": zero_line,
        # Margin level frozen at open (break-even + noise margin): the ceiling of
        # where the raised stop is parked (the top of the break-even→margin band),
        # NOT the zone boundary. Drawn as its own reference line.
        "margin": margin_line,
        # Profit trigger frozen at open (one further noise margin above margin):
        # the boundary between the break-even band (zone 2, CLOSE_ZONEMARGE) and
        # real profit (zone 3, CLOSE_ZONEPROFIT). Above it the stop trails up.
        "profitTrigger": profit_trigger,
        # Loose stop (resting at the broker): the initial clamped level (never
        # lowered) and, when a ratchet history exists, the stepped path of every
        # level later pushed to IG.
        "stopLoose": loose_initial,
        "stopsLoose": loose_stops,
        # Follower stop (application-side trail): the ``level_follower`` the close
        # profile enforces, plus its stepped ratchet path. It can sit tighter than
        # the loose stop above. Empty/absent for positions opened before the
        # history was captured — the chart then falls back to the flat
        # ``stopFollower`` / ``stopLoose`` scalars.
        "stopFollower": _level("level_follower"),
        "stopsFollower": follower_stops,
        "target": _level("level_win"),
        # Profit reference line above break-even: +10 € (euro-based), on the bid
        # curve. The +3 % line is derived client-side from the chart range.
        "profit10": profit10,
        "close": close_line,
        # Close reason — lets the chart flag an *estimated* exit (the position
        # vanished from IG and the close level/time were derived, not a captured
        # fill) so it is not read as a real stop/limit execution.
        "closeReason": getattr(p, "reason_close", None),
        # Prefer the broker's exact UTC execution time (captured from IG's
        # transaction history) over the bot's loop/detection clock; fall back to
        # the latter when reconciliation has not stamped the broker time yet.
        "openTime": _ts(getattr(p, "time_open_broker", None) or p.time_open),
        "closeTime": _ts(getattr(p, "time_close_broker", None) or p.time_close),
        # Position state ("open"/"close") so the chart labels the P&L as running
        # ("en cours") for an open trade or realised ("validé") once closed.
        "state": getattr(getattr(p, "state", None), "value", None),
        "pnl": _num("euro"),
    }


@router.get("/api/chart/{epic}")
async def api_chart(request: Request, epic: str) -> JSONResponse:
    """JSON API: whole-day price curve for an epic plus every trade taken on it.

    Candles come from the durable candle store (the full Paris trading day, not
    just the in-memory window), falling back to the in-memory ``PriceBuffer``
    when the store is unavailable or has nothing yet. ``trades`` lists *every*
    open/close cycle for the epic today so the chart can mark each entry/exit —
    not only the most recent one.
    """
    today = date.today()
    candles: list[dict] = []

    store = getattr(request.app.state, "candle_store", None)
    if store is not None:
        start = datetime.combine(today, time.min, tzinfo=_PARIS).astimezone(UTC)
        end = datetime.combine(today, time.max, tzinfo=_PARIS).astimezone(UTC)
        records = await store.fetch(epic, since=start, until=end)
        candles = [
            {"t": _iso_utc(c.timestamp), "bid": c.bid_close, "offer": c.offer_close}
            for c in records
        ]

    # Fall back to the live buffer when the store has no rows for today yet.
    if not candles:
        buf = request.app.state.buffer.get(epic)
        if buf:
            candles = [
                {"t": _iso_utc(c.timestamp), "bid": c.bid_close, "offer": c.offer_close}
                for c in buf.candles
            ]

    # Per-candle adverse tick-noise band (points). Computed on the rolling window
    # of bid closes up to each candle, mirroring the updaters' gate/floor. The
    # chart draws ``bid − band`` under the bid so the noise-adjusted trace the
    # zone gates test is visible: where it clears break-even / margin is where the
    # updaters treat the bid as truly in the band / profit zone.
    bids = [c["bid"] for c in candles]
    for i, candle in enumerate(candles):
        candle["noise"] = _CHART_NOISE_MULT * adverse_tick_noise(
            bids[: i + 1], _CHART_NOISE_WINDOW, _CHART_NOISE_STD_K
        )

    # Every trade on this epic today (all open/close cycles, oldest first).
    trades: list[dict] = []
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is not None:
        async with session_factory() as session:
            rows = await session.scalars(
                select(Position)
                .where(Position.epic == epic, Position.date == today)
                .order_by(Position.id)
            )
            trades = [_trade_overlay(p) for p in rows]

    return JSONResponse({"epic": epic, "candles": candles, "trades": trades})


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
            "errors": [
                {
                    "label": e.label,
                    "method": e.method,
                    "endpoint": e.endpoint,
                    "version": e.version,
                    "http_status": e.http_status,
                    "ig_error_code": e.ig_error_code,
                    "error": e.error,
                    "attempts": e.attempts,
                    "total_attempts": e.total_attempts,
                    "priority": e.priority,
                    "failed_at": e.failed_at.isoformat(),
                }
                for e in api_queue.errors()
            ],
        }
    )


@router.post("/api/queue/errors/clear")
async def api_queue_errors_clear(request: Request) -> JSONResponse:
    """Clear the persistent queue-error log."""
    api_queue = getattr(request.app.state, "api_queue", None)
    if api_queue is not None:
        api_queue.clear_errors()
    return JSONResponse({"cleared": True})


@router.post("/api/wallet/resync")
async def wallet_resync(request: Request) -> JSONResponse:
    """Force an immediate account-balance refresh (wallet KPI resync button).

    The balance shown on the dashboard is otherwise refreshed only in the
    background, at most once every 15 s. This endpoint awaits a fresh
    ``GET /accounts`` so the user can pull the current figure on demand (e.g.
    after topping up a demo account from the IG web platform — IG exposes no
    REST endpoint to reset the demo balance, so the reset stays a manual step
    there and this button just re-reads the new value).
    """
    api_queue = getattr(request.app.state, "api_queue", None)
    if api_queue is None:
        return JSONResponse({"error": "API not available"}, status_code=503)
    try:
        balance = await force_account_balance_refresh(request.app.state)
    except Exception as exc:
        logger.warning("Wallet resync failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
    if not balance:
        return JSONResponse({"error": "Balance unavailable"}, status_code=502)
    return JSONResponse(
        {
            "available": _to_float_or_none(balance.get("available")),
            "used": _to_float_or_none(balance.get("deposit")),
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
    """Open a BUY position for the given epic, manually from the dashboard.

    This forces the *open decision only*: the entry strategy is bypassed
    (direction is hard-coded BUY), but everything else goes through the bot's
    normal open path. The active close profile chooses the protective stop via
    :meth:`CloseProfile.initial_plan`, and
    :meth:`TradingService.open_from_intent` reuses the same sizing, risk caps,
    dealing-rule validation, confirmation and DB record as an automatic open —
    so the stop is the strategy's stop, not IG's bare minimum (which sat
    inside the spread and closed the trade on the first monitor tick).

    The portfolio risk gates (max positions, duplicate epic, daily P&L limits,
    win-rate) still apply: a manual open that violates them is refused.
    """
    api_queue = getattr(request.app.state, "api_queue", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    scheduler = getattr(request.app.state, "scheduler", None)
    settings = request.app.state.settings
    buffer = request.app.state.buffer

    if not api_queue or not session_factory or scheduler is None:
        return JSONResponse({"error": "Trading not available"}, status_code=503)

    buf = buffer.get(epic)
    if not buf or not buf.last:
        return JSONResponse({"error": "No price data for this epic"}, status_code=400)

    try:
        config = TradeConfig.from_settings(settings)
        intent = EntryIntent(epic=epic, direction="BUY")
        async with session_factory() as session:
            trading = TradingService(
                api_queue, session, config, close_profile=scheduler.close_profile
            )

            # Open under the scheduler's per-epic lock (single-flight): a
            # double-click or a second browser tab firing the same manual open —
            # or the analysis tick opening this epic at the same instant — can no
            # longer slip two orders through the duplicate-epic gate before the
            # first commits its row. The lock spans the gate re-check + order.
            position, reason = await scheduler.open_epic_guarded(trading, intent, buf)
            if reason:
                return JSONResponse({"error": reason}, status_code=400)
            if position is None:
                # open_position returned None: market not TRADEABLE or IG
                # rejected the deal.
                return JSONResponse(
                    {"error": "Open rejected (market closed or deal rejected)"},
                    status_code=400,
                )

            # Tag the manual origin (open_from_intent records reason_open="auto").
            position.reason_open = "manual"
            await session.commit()

            level = float(position.level_open)
            quantity = int(position.quantity)
            deal_id = position.deal_id or ""

        logger.info(
            "Manual position opened: %s qty=%d level=%.5f", epic, quantity, level
        )
        return JSONResponse(
            {
                "status": "opened",
                "deal_id": deal_id,
                "level": level,
                "quantity": quantity,
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
    scheduler = getattr(request.app.state, "scheduler", None)
    settings = request.app.state.settings

    if not api_queue or not session_factory:
        return JSONResponse({"error": "Trading not available"}, status_code=503)

    async with session_factory() as session:
        position = await session.get(Position, position_id)
        if not position:
            return JSONResponse({"error": "Position not found"}, status_code=404)
        if position.state != PositionState.OPEN:
            return JSONResponse({"error": "Position is not open"}, status_code=400)

        # Delegate the whole close to TradingService rather than reimplementing it.
        # The old inline code hard-coded ``direction="SELL"`` and a long-only
        # ``move = close - open``, so it could neither close a recovery SELL (IG
        # rejects a SELL to close a SELL) nor record a short's P&L with the right
        # sign. ``_close_position`` (via ``close_manually``) mirrors the direction
        # (BUY to close a SELL), mirrors the P&L, resolves a missing dealId,
        # prefers IG's authoritative confirm (fill level + realized profit) and
        # handles the phantom case — all shared with the automated close path.
        try:
            market_data = await api_queue.get(
                f"/markets/{position.epic}",
                version=3,
                priority=Priority.URGENT,
                label=f"manual close {position.epic}: market",
            )
        except Exception as exc:
            return JSONResponse(
                {"error": f"Failed to fetch market price: {exc}"}, status_code=500
            )

        # Fallback close level only (the IG confirm inside _close_position
        # overrides it with the real fill when available): a short is bought back
        # at the offer, a long sold at the bid.
        snapshot = market_data.get("snapshot", {})
        price_field = "offer" if position.direction == "SELL" else "bid"
        close_level = float(snapshot.get(price_field, 0) or 0)

        config = TradeConfig.from_settings(settings)
        close_profile = scheduler.close_profile if scheduler is not None else None
        trading = TradingService(
            api_queue, session, config, close_profile=close_profile
        )
        try:
            closed = await trading.close_manually(position, close_level)
        except Exception as exc:
            logger.error("Manual close failed for %s: %s", position.epic, exc)
            return JSONResponse({"error": str(exc)}, status_code=500)
        if not closed:
            return JSONResponse({"error": "Close rejected by IG"}, status_code=500)

        euro_pnl = float(position.euro or 0)
        close_level = float(position.level_close or close_level)
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
