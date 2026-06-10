"""Dashboard data gathering and pure display helpers (no HTML markup)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Request
from sqlalchemy import select

from src.models.day import Day
from src.models.epic import Epic
from src.models.position import Position, PositionState
from src.models.resume import Resume
from src.services.api_queue import Priority
from src.utils.tools import euro_per_point

_PARIS = ZoneInfo("Europe/Paris")

# How long a fetched account balance stays fresh. The dashboard polls every 2 s;
# caching the balance avoids one ``GET /accounts`` per poll (which would burn the
# rate-limit guard for a figure that barely moves between polls).
_BALANCE_TTL = timedelta(seconds=15)

# How long a fetched per-point value stays fresh. Contract size, currency and
# min deal size are effectively static intraday, so we memoize them well past
# the 2 s poll to avoid re-hitting ``GET /markets`` for figures that never move.
_VALUE_PER_POINT_TTL = timedelta(minutes=5)
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


async def _fetch_account_balance(request: Request) -> dict | None:
    """Return the active account's balance dict, cached for a short TTL.

    Shape (from IG ``GET /accounts`` v1): ``{"balance", "deposit", "profitLoss",
    "available"}`` where ``available`` is the cash free to open new positions and
    ``deposit`` is the margin currently tied up by open positions. Returns the last
    good value on a fetch failure, or ``None`` when never successfully fetched.
    """
    app_state = request.app.state
    api_queue = getattr(app_state, "api_queue", None)
    cached = getattr(app_state, "account_balance", None)
    fetched_at = getattr(app_state, "account_balance_at", None)
    now = datetime.now(UTC)
    if (
        cached is not None
        and fetched_at is not None
        and now - fetched_at < _BALANCE_TTL
    ):
        return cached
    if api_queue is None:
        return cached
    try:
        data = await api_queue.get(
            "/accounts",
            version=1,
            suppress_error_logging=True,
            label="dashboard: account balance",
        )
    except Exception:
        return cached
    account_id = getattr(app_state.settings, "ig_account_id", None)
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    balance: dict | None = None
    for account in accounts:
        if account.get("accountId") == account_id:
            balance = account.get("balance")
            break
    if balance is None and accounts:
        # Configured id absent from the response — fall back to the first account.
        balance = accounts[0].get("balance")
    app_state.account_balance = balance
    app_state.account_balance_at = now
    return balance


async def _value_per_point_map(request: Request, epics: list[str]) -> dict[str, float]:
    """Return ``{epic: euros per point for a minimum-size buy}``.

    The figure is ``euro_per_point(market, minDealSize, currency)`` — the euro
    value of one point of movement for the smallest buy the dealing rules allow,
    i.e. what a single click on the row's Buy button is exposed to per point.

    Contract size, currency and min deal size live in IG market details, not in
    the price buffer, so they are fetched live via a batched ``GET /markets``.
    Results are memoized per epic for :data:`_VALUE_PER_POINT_TTL` because those
    fields are static intraday; only epics whose cache entry is missing or stale
    trigger a fetch, so the 2 s fragment poll stays cheap.
    """
    app_state = request.app.state
    api_queue = getattr(app_state, "api_queue", None)
    cache: dict[str, tuple[datetime, float]] = getattr(
        app_state, "value_per_point_cache", {}
    )
    app_state.value_per_point_cache = cache
    now = datetime.now(UTC)
    stale = [
        e for e in epics if e not in cache or now - cache[e][0] >= _VALUE_PER_POINT_TTL
    ]
    if api_queue is not None and stale:
        for start in range(0, len(stale), _MARKET_BATCH_SIZE):
            batch = stale[start : start + _MARKET_BATCH_SIZE]
            try:
                data = await api_queue.get(
                    f"/markets?epics={','.join(batch)}",
                    version=1,
                    suppress_error_logging=True,
                    priority=Priority.NORMAL,
                    label="dashboard: value per point",
                )
            except Exception:
                continue
            for detail in data.get("marketDetails", []):
                instrument = detail.get("instrument", {})
                epic = instrument.get("epic")
                if not epic:
                    continue
                rules = detail.get("dealingRules", {})
                min_size = float(rules.get("minDealSize", {}).get("value", 1) or 1)
                currency = (instrument.get("currencies") or [{}])[0].get("code", "EUR")
                cache[epic] = (now, euro_per_point(detail, min_size, currency))
    return {e: cache[e][1] for e in epics if e in cache}


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

    # Euro value of one point for a minimum-size buy on each tracked epic. Not in
    # the price buffer — fetched live (cached) from IG market details.
    value_per_point = await _value_per_point_map(request, epics)
    for entry in market_summary:
        entry["value_per_point"] = value_per_point.get(entry["epic"], 0.0)

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

    # Wallet KPI — cash available to open vs. margin tied up by open positions.
    balance = await _fetch_account_balance(request)
    if balance:
        kpis["wallet_available"] = _to_float_or_none(balance.get("available"))
        kpis["wallet_used"] = _to_float_or_none(balance.get("deposit"))
    else:
        kpis["wallet_available"] = None
        kpis["wallet_used"] = None

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
        "all_epics": all_epics,
        "epic_db_map": epic_db_map,
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


def _bid_pct(bid: float, low: float, high: float) -> float:
    """Return bid position as % within known [low, high] range."""
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (bid - low) / (high - low) * 100))


def _pnl_color(value: float) -> str:
    """Green for a non-negative P&L, red otherwise (dashboard convention)."""
    return "#4ade80" if value >= 0 else "#ef4444"
