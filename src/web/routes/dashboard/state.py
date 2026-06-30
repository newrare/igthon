"""Dashboard data gathering and pure display helpers (no HTML markup)."""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Request
from sqlalchemy import select

from src.core.api_queue import Priority
from src.execution.risk import daily_risk_block
from src.execution.trading import TradeConfig
from src.models.day import Day
from src.models.epic import Epic
from src.models.position import Position, PositionState
from src.models.resume import Resume
from src.utils.tools import euro_per_point

logger = logging.getLogger(__name__)

_PARIS = ZoneInfo("Europe/Paris")

# How long a fetched account balance stays fresh. The dashboard polls every 1 s;
# caching the balance bounds the background ``GET /accounts`` refresh to once per
# TTL (the figure barely moves between polls).
_BALANCE_TTL = timedelta(seconds=15)

# How long a fetched spread cost stays fresh. Unlike contract size or currency,
# the dealing spread does move intraday (it widens around the open/close and on
# news), so we refresh it on a short cadence rather than memoizing it for long —
# while still keeping it off the 1 s poll's critical path.
_SPREAD_COST_TTL = timedelta(seconds=60)
# IG caps ``GET /markets?epics=`` at 50 epics per call.
_MARKET_BATCH_SIZE = 50


def _to_float_or_none(value: object) -> float | None:
    """Best-effort float conversion; ``None`` when the value is missing/invalid."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_account_balance(request: Request) -> dict | None:
    """Return the cached account balance, refreshing it in the background.

    Shape (from IG ``GET /accounts`` v1): ``{"balance", "deposit", "profitLoss",
    "available"}``. This is a NON-BLOCKING read: the dashboard poll must never
    ``await`` an external IG call (it would stall the whole fragments endpoint —
    and the queue view it renders — whenever the queue is busy or rate-limited).
    When the cached value is stale, a fire-and-forget refresh is scheduled and
    the last known value is returned immediately; the fresh figure lands on a
    later poll.
    """
    app_state = request.app.state
    cached = getattr(app_state, "account_balance", None)
    fetched_at = getattr(app_state, "account_balance_at", None)
    now = datetime.now(UTC)
    fresh = (
        cached is not None
        and fetched_at is not None
        and now - fetched_at < _BALANCE_TTL
    )
    if not fresh:
        _schedule_account_balance_refresh(app_state)
    return cached


def _schedule_account_balance_refresh(app_state) -> None:
    """Spawn a background ``GET /accounts`` refresh, deduplicated.

    A single in-flight refresh is allowed at a time (guarded by the
    ``account_balance_refreshing`` flag) so the 1 s poll cadence can't pile up
    duplicate balance fetches in the queue.
    """
    api_queue = getattr(app_state, "api_queue", None)
    if api_queue is None or getattr(app_state, "account_balance_refreshing", False):
        return
    app_state.account_balance_refreshing = True
    asyncio.create_task(_refresh_account_balance(app_state, api_queue))


async def _refresh_account_balance(app_state, api_queue) -> None:
    """Fetch the balance via the queue and update the in-memory cache."""
    try:
        data = await api_queue.get(
            "/accounts",
            version=1,
            suppress_error_logging=True,
            label="dashboard: account balance",
        )
        account_id = getattr(app_state.settings, "ig_account_id", None)
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        balance: dict | None = None
        for account in accounts:
            if account.get("accountId") == account_id:
                balance = account.get("balance")
                break
        if balance is None and accounts:
            # Configured id absent from the response — fall back to first account.
            balance = accounts[0].get("balance")
        app_state.account_balance = balance
        app_state.account_balance_at = datetime.now(UTC)
    except Exception as exc:
        logger.debug("Background balance refresh failed: %s", exc)
    finally:
        app_state.account_balance_refreshing = False


def _spread_cost_map(request: Request, epics: list[str]) -> dict[str, float]:
    """Return cached ``{epic: euro cost of crossing the spread once at min size}``.

    The figure is ``(offer - bid) * euro_per_point(minDealSize, currency)`` — what
    a single minimum-size round trip pays away to the broker's spread, the simplest
    gauge of how expensive a market is to trade. The bid/offer used is the live
    dealing spread from the market ``snapshot``, NOT the historical-candle close
    (whose bid and offer are often equal, which is where the misleading ``0`` came
    from).

    The dealing spread and the contract details live in IG market details, not in
    the price buffer. Like the balance, this is a NON-BLOCKING read: stale/missing
    epics trigger a fire-and-forget batched ``GET /markets`` refresh and the poll
    returns whatever is already cached.
    """
    app_state = request.app.state
    cache: dict[str, tuple[datetime, float]] = getattr(
        app_state, "spread_cost_cache", {}
    )
    app_state.spread_cost_cache = cache
    now = datetime.now(UTC)
    stale = [
        e for e in epics if e not in cache or now - cache[e][0] >= _SPREAD_COST_TTL
    ]
    if stale:
        _schedule_spread_cost_refresh(app_state, stale)
    return {e: cache[e][1] for e in epics if e in cache}


def _schedule_spread_cost_refresh(app_state, stale: list[str]) -> None:
    """Spawn a background batched ``GET /markets`` refresh for stale epics.

    Epics already being fetched are tracked in ``spread_cost_inflight`` so
    repeated polls don't enqueue duplicate batches for the same markets while a
    refresh is pending.
    """
    api_queue = getattr(app_state, "api_queue", None)
    if api_queue is None:
        return
    inflight: set[str] = getattr(app_state, "spread_cost_inflight", set())
    app_state.spread_cost_inflight = inflight
    todo = [e for e in stale if e not in inflight]
    if not todo:
        return
    inflight.update(todo)
    asyncio.create_task(_refresh_spread_cost(app_state, api_queue, todo))


async def _refresh_spread_cost(app_state, api_queue, epics: list[str]) -> None:
    """Fetch market details for ``epics`` and update the spread-cost cache."""
    cache: dict[str, tuple[datetime, float]] = app_state.spread_cost_cache
    inflight: set[str] = app_state.spread_cost_inflight
    try:
        for start in range(0, len(epics), _MARKET_BATCH_SIZE):
            batch = epics[start : start + _MARKET_BATCH_SIZE]
            try:
                data = await api_queue.get(
                    f"/markets?epics={','.join(batch)}",
                    version=1,
                    suppress_error_logging=True,
                    priority=Priority.NORMAL,
                    label="dashboard: spread cost",
                )
            except Exception as exc:
                logger.debug("Background spread-cost refresh failed: %s", exc)
                continue
            now = datetime.now(UTC)
            for detail in data.get("marketDetails", []):
                instrument = detail.get("instrument", {})
                epic = instrument.get("epic")
                if not epic:
                    continue
                snapshot = detail.get("snapshot", {})
                bid = _to_float_or_none(snapshot.get("bid"))
                offer = _to_float_or_none(snapshot.get("offer"))
                if bid is None or offer is None:
                    continue
                rules = detail.get("dealingRules", {})
                min_size = float(rules.get("minDealSize", {}).get("value", 1) or 1)
                currency = (instrument.get("currencies") or [{}])[0].get("code", "EUR")
                epp = euro_per_point(detail, min_size, currency)
                cache[epic] = (now, (offer - bid) * epp)
    finally:
        inflight.difference_update(epics)


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
        "never_opened": ("Never opened", "#64748b"),
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
                    "dots": len(buf),
                    "high": max(buf.bid_closes) if buf.bid_closes else 0,
                    "low": min(buf.bid_closes) if buf.bid_closes else 0,
                }
            )

    # Euro cost of crossing the spread once at minimum deal size on each tracked
    # epic. Built from the live dealing spread and contract details, neither of
    # which is in the price buffer — read from cache (refreshed in the background,
    # never awaited here so the poll can't stall on the IG queue).
    spread_cost = _spread_cost_map(request, epics)
    for entry in market_summary:
        entry["spread_cost"] = spread_cost.get(entry["epic"])

    # Fetch database statistics
    kpis: dict = {}
    open_positions: list[Position] = []
    closed_positions: list[Position] = []
    day_records: list[Day] = []
    resume_records: list[Resume] = []
    epic_db_map: dict[str, Epic] = {}
    if session_factory:
        async with session_factory() as session:
            # Available epics for trading (tracked ones)
            kpis["available_epics"] = len(epics)

            # Epic list (for the Epic List modal): enrich the in-memory names
            # from the scheduler with their persisted DB rows where available.
            if all_epics:
                epic_rows = await session.scalars(
                    select(Epic).where(Epic.name.in_(all_epics))
                )
                epic_db_map = {e.name: e for e in epic_rows}

            # Open positions — ALL of them, regardless of date. A position that
            # stays open past midnight (or one adopted from IG) keeps tying up
            # margin, so it must remain visible; scoping this to ``today`` is what
            # let live positions vanish from the dashboard while "in use" stayed
            # non-zero. Closed positions below stay scoped to today.
            open_pos = await session.scalars(
                select(Position).where(Position.state == PositionState.OPEN)
            )
            open_positions = list(open_pos)

            closed_pos = await session.scalars(
                select(Position)
                .where(Position.date == today, Position.state == PositionState.CLOSE)
                .order_by(Position.time_close.desc())
            )
            # Drop "never_opened" phantoms: provisional rows whose order never
            # confirmed at IG (deal_id never bound). They are not real trades, so
            # they must not appear in the closed table nor count toward the day's
            # P&L / wins / losses / trade count. Filtered in Python so a NULL
            # reason_close (legacy rows) is kept (SQL ``!=`` would drop NULLs).
            closed_positions = [
                p for p in closed_pos if p.reason_close != "never_opened"
            ]

            kpis["open_trades"] = len(open_positions)
            # Aggregate open P&L from the stored ``euro`` — IG's last recorded
            # unrealized figure, refreshed by the sync_positions job (~every 20s).
            # This is deliberately the SAME source the per-epic table row uses
            # (``Position.euro``), so the "OPEN" tile and the open-positions table
            # can never disagree. We do NOT recompute from the live streaming bid
            # here: that estimate (move * euro_per_point) drifted away from IG's
            # figure between syncs and made the tile and the table show different
            # P&L for the same epic. The trade-off — the figure lags the market by
            # up to one sync interval — is surfaced via the "as of" timestamp below.
            kpis["open_pnl"] = sum(
                float(p.euro) for p in open_positions if p.euro is not None
            )

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
                # Exclude never_opened phantoms (unconfirmed orders) from the
                # whole-history track record, same as the daily figures above.
                if p.reason_close == "never_opened":
                    continue
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

    # "As of" time of the open P&L figures — the last successful IG position
    # sync. The OPEN tile shows IG's last recorded value (not a live estimate),
    # so it carries the time that value was relevé to make the lag explicit.
    positions_synced_at: datetime | None = (
        scheduler.positions_synced_at if scheduler else None
    )
    if positions_synced_at is not None:
        kpis["open_pnl_as_of"] = positions_synced_at.astimezone(_PARIS).strftime(
            "%H:%M:%S"
        )
    else:
        kpis["open_pnl_as_of"] = None

    kpis["all_epics_count"] = len(all_epics)
    kpis["epic_kpi_color"] = epic_kpi_color
    kpis["refresh_label"] = refresh_label
    kpis["tradable_count"] = len(tradable_epics)
    kpis["tradable_kpi_color"] = tradable_kpi_color
    kpis["tradable_refresh_label"] = tradable_refresh_label

    # Wallet KPI — cash available to open vs. margin tied up by open positions.
    # Cached read; a stale value triggers a background refresh (never awaited).
    balance = _fetch_account_balance(request)
    if balance:
        kpis["wallet_available"] = _to_float_or_none(balance.get("available"))
        kpis["wallet_used"] = _to_float_or_none(balance.get("deposit"))
    else:
        kpis["wallet_available"] = None
        kpis["wallet_used"] = None

    # Daily-risk opening indicator: surface *why* the bot has stopped opening when
    # a day-scope circuit-breaker has tripped (daily loss/target, max trades,
    # win-rate floor). Computed from the same inputs the live gate uses — today's
    # CLOSE rows, stored ``euro`` for P&L and the ``win`` flag for the rate — so
    # the badge and ``evaluate_open_gates`` never disagree. ``None`` = not blocked.
    open_block_reason: str | None = None
    daily_risk_enabled = True
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        try:
            cfg = TradeConfig.from_settings(settings)
            daily_risk_enabled = cfg.daily_risk_enabled
            daily_count = len(closed_positions)
            daily_euro = sum(float(p.euro or 0) for p in closed_positions)
            daily_wins = sum(1 for p in closed_positions if (p.win or 0) > 0)
            daily_win_rate = daily_wins / daily_count if daily_count else 1.0
            # Live figures + thresholds for the risk modal (one row per breaker).
            kpis["risk_daily_pnl"] = daily_euro
            kpis["risk_trade_count"] = daily_count
            kpis["risk_win_rate"] = daily_win_rate
            kpis["risk_loss_limit"] = cfg.day_euro_finish_loose
            kpis["risk_win_target"] = cfg.day_euro_finish_win
            kpis["risk_max_trades"] = cfg.max_trades_day
            kpis["risk_min_win_rate"] = cfg.min_win_rate
            # Only report a block reason while the gates are armed; disarmed, the
            # bot opens regardless so there is nothing to block on.
            if daily_risk_enabled:
                open_block_reason = daily_risk_block(
                    daily_pnl=daily_euro,
                    trade_count=daily_count,
                    win_rate=daily_win_rate,
                    config=cfg,
                )
        except Exception:  # pragma: no cover - defensive: never break the 2s poll
            logger.debug("Could not compute daily-risk open block", exc_info=True)
    kpis["daily_risk_enabled"] = daily_risk_enabled
    kpis["open_block_reason"] = open_block_reason

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
    queue_errors = api_queue.errors() if api_queue else []

    # Server log buffer
    log_buffer = getattr(request.app.state, "log_buffer", None)
    log_entries = log_buffer.get_all() if log_buffer else []

    # Human-readable instrument name (IG description), mirrored from the Epic
    # List. Sourced from the DB epic rows keyed by epic identifier; a dash when
    # the epic has not been enriched (discovered) yet.
    for entry in market_summary:
        epic_row = epic_db_map.get(entry["epic"])
        entry["name"] = (epic_row.description or "—") if epic_row else "—"

    return {
        "market_summary": market_summary,
        "kpis": kpis,
        "all_epics": all_epics,
        "epic_db_map": epic_db_map,
        "guard_stats": guard_stats,
        "error_entries": error_entries,
        "queue_stats": queue_stats,
        "queue_recent": queue_recent,
        "queue_pending_tasks": queue_pending_tasks,
        "queue_errors": queue_errors,
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "day_records": day_records,
        "resume_records": resume_records,
        "bot_paused": scheduler.is_paused if scheduler else None,
        "scheduler_available": scheduler is not None,
        "jobs": scheduler.jobs_status() if scheduler else [],
        "log_entries": log_entries,
        # IG connection state for the degraded-mode banner: the web server runs
        # even when login fails, so the dashboard explains the situation rather
        # than crashing. Set by run_bot's connection loop on app.state.
        "startup_error": getattr(request.app.state, "startup_error", None),
        "connecting": getattr(request.app.state, "connecting", False),
    }


def _bid_pct(bid: float, low: float, high: float) -> float:
    """Return bid position as % within known [low, high] range."""
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (bid - low) / (high - low) * 100))


def _pnl_color(value: float) -> str:
    """Green for a non-negative P&L, red otherwise (dashboard convention)."""
    return "#4ade80" if value >= 0 else "#ef4444"
