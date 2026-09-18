"""Dashboard HTML fragment builders (dynamically refreshed regions)."""

import html
from datetime import UTC, date, datetime, time

from src.core.api_error_log import APIErrorEntry
from src.web.routes.dashboard.components import (
    render_button,
    render_card,
    render_table,
)
from src.web.routes.dashboard.state import (
    _PARIS,
    _PHASE_RANK,
    _bid_pct,
    _close_reason_label,
    _display_pnl,
    _loss_explanation,
    _open_reason_label,
    _phase_label,
    _pnl_color,
    _regime_label,
    _unrealized_pnl_color,
)


def _fmt_paris_time(d: date | None, t: time | None) -> str:
    """Format a stored (UTC date, UTC time) pair as Paris-local ``HH:MM:SS``.

    Position times are persisted timezone-naive in UTC (``datetime.now(UTC)`` at
    open, IG's ``createdDateUTC`` for adopted rows). The dashboard must show them
    on the French (Europe/Paris) wall clock, so combine the date+time as UTC and
    convert — mirroring what the chart modal already does client-side. Falls back
    to a naive format when the date is missing (cannot localise without it).
    """
    if t is None:
        return "—"
    if d is None:
        return t.strftime("%H:%M:%S")
    return datetime.combine(d, t, tzinfo=UTC).astimezone(_PARIS).strftime("%H:%M:%S")


def _render_logs_section(entries: list[dict]) -> str:
    """Render the log section fragment: filter toolbar + log rows (newest first)."""
    _level_class = {
        "INFO": "info",
        "WARNING": "warning",
        "ERROR": "error",
        "DEBUG": "debug",
    }
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
            <button id="btn-logs-pause" class="log-pause-btn" onclick="toggleLogsPause(event)"><i data-lucide="pause" class="lc-icon"></i> Pause</button>
        </div>
        <div class="logs-list">
            {rows}
        </div>"""


# The open / stop / close selection is chosen exclusively in ``.env`` (the single
# source of truth) — the dashboard only *displays* the active names, read-only.
# Rendered as flat inline labels (no pill, no border, no hover) precisely so they
# never read as buttons the user could click to change the selection.
def _render_selection_name(name: str, *, icon: str, cls: str, title: str) -> str:
    """Render one active selection name as a read-only title-bar label."""
    return (
        f'<span class="{cls}" title="{title}">'
        f'<i data-lucide="{icon}" class="lc-icon"></i>'
        f'<span class="strategy-name">{html.escape(name or "—")}</span>'
        "</span>"
    )


def _render_selection_chips(selection: dict) -> str:
    """Render the entry → stop → close labels shown next to the bot title."""
    return "".join(
        [
            _render_selection_name(
                selection.get("open_strategy") or "—",
                icon="cpu",
                cls="dashboard-title-strategy",
                title="Active entry (open) strategy — set OPEN_STRATEGY in .env",
            ),
            '<i data-lucide="arrow-right" class="lc-icon dashboard-title-arrow"></i>',
            _render_selection_name(
                selection.get("stop_strategy") or "—",
                icon="ruler",
                cls="dashboard-title-stop",
                title="Active stop policy (initial stop) — set STOP_STRATEGY in .env",
            ),
            '<i data-lucide="arrow-right" class="lc-icon dashboard-title-arrow"></i>',
            _render_selection_name(
                selection.get("close_zones") or "—",
                icon="shield",
                cls="dashboard-title-exit",
                title=(
                    "Active close zones (exit) — set CLOSE_ZONESTART / "
                    "CLOSE_ZONEMARGE / CLOSE_ZONESECURE / CLOSE_ZONEPROFIT in .env"
                ),
            ),
        ]
    )


# ``OPEN_MODE`` decides *who places the order* — ``auto`` lets the bot fire, while
# ``notify`` only proposes the validated intent and waits for the user. It is the
# single most consequential switch in ``.env``, so the bar spells it out instead
# of leaving it to be inferred from an empty signals list.
def _render_open_mode_chip(open_mode: str) -> str:
    """Render the active OPEN_MODE as a read-only badge for the title bar."""
    mode = (open_mode or "").strip().lower() or "auto"
    if mode == "notify":
        cls, icon = "notify", "bell"
        detail = (
            "the bot proposes every validated open as a signal and waits for you "
            "to place it"
        )
    else:
        cls, icon = "auto", "zap"
        detail = "the bot places its own orders"
    title = f"OPEN_MODE={mode} — {detail} (set OPEN_MODE in .env)"
    return (
        f'<span class="dashboard-title-mode mode-{cls}" title="{html.escape(title)}">'
        f'<i data-lucide="{icon}" class="lc-icon"></i>'
        f'<span class="mode-name">OPEN_MODE={html.escape(mode)}</span>'
        "</span>"
    )


def _render_title_bar(enabled: bool, selection: dict, open_mode: str) -> str:
    """Render the merged title bar: bot identity + the auto-open switch.

    One single-line bar carries the bot name, the active OPEN_MODE, the active
    entry/stop/close selection and the two day controls (block openings / end the
    day). Its colour *is* the auto-open state — green while the bot may still
    take a trade, red (pulsing) the moment openings are blocked — so the state
    needs no wording of its own: the lock icon (whose tooltip spells it out) plus the bar colour say
    it, and the row stays one line high. Rebuilt from the server state on every
    poll, so what is displayed can never drift from the scheduler's.
    """
    if enabled:
        state_cls, icon, state_label = "on", "unlock", "Openings allowed"
        btn_label, btn_icon = "Block for today", "hand"
    else:
        state_cls, icon, state_label = "off", "lock", "Openings blocked"
        btn_label, btn_icon = "Allow openings", "play"
    return f"""
        <div class="dashboard-title autoopen-{state_cls}" id="autoopen-banner">
            <i data-lucide="bot" class="lc-icon dashboard-title-bot"></i>
            <h1>IG Trading Bot</h1>
            {_render_open_mode_chip(open_mode)}
            <span class="dashboard-title-sep">·</span>
            {_render_selection_chips(selection)}
            <div class="dashboard-title-actions">
                <span class="autoopen-state" id="autoopen-state" title="{state_label}">
                    <i data-lucide="{icon}" class="lc-icon autoopen-icon"></i>
                </span>
                <button class="autoopen-btn" id="autoopen-btn" onclick="toggleAutoOpen(this)"
                        title="Authorise or block every automatic opening for the current day — open positions keep running and manual Buy / Sell still work">
                    <i data-lucide="{btn_icon}" class="lc-icon"></i> {btn_label}
                </button>
                <button class="autoopen-btn endday-btn" id="endday-btn" onclick="endDay(this)"
                        title="End the day now: close EVERY open position immediately and block any further automatic opening">
                    <i data-lucide="power" class="lc-icon"></i> End day
                </button>
            </div>
        </div>"""


def _render_signals_section(signals: list[dict], open_mode: str) -> str:
    """Render the pending entry signals — the opens the bot proposed to the user.

    Only ``OPEN_MODE=notify`` ever fills this list: each card is an intent that
    cleared every pre-open gate and was handed over instead of being ordered. The
    card carries what is needed to decide without leaving the dashboard — the
    direction, the strategy and its score, the close-out price, how old the
    proposal is — plus the two ways to look at the market (chart, market reading)
    and the two ways to answer it (Buy/Sell, Dismiss).

    In ``auto`` mode the section states that the bot is opening by itself, so an
    empty list never reads as "the strategy found nothing".
    """
    if open_mode != "notify":
        return (
            '<div class="signal-empty">'
            "OPEN_MODE=auto — the bot opens its own positions. "
            "Set <code>OPEN_MODE=notify</code> in <code>.env</code> to be "
            "notified instead and decide yourself."
            "</div>"
        )
    if not signals:
        return (
            '<div class="signal-empty">'
            "No pending signal — nothing worth opening right now."
            "</div>"
        )

    cards = ""
    for sig in signals:
        epic = html.escape(str(sig["epic"]))
        name = html.escape(str(sig.get("name") or sig["epic"]))
        direction = "SELL" if str(sig["direction"]).upper() == "SELL" else "BUY"
        dir_cls = "sell" if direction == "SELL" else "buy"
        strategy = html.escape(str(sig.get("strategy") or "—"))
        score = float(sig.get("score") or 0.0)
        price = float(sig.get("price") or 0.0)
        price_txt = f"{price:.2f}" if price else "—"
        age = int(sig.get("age_seconds") or 0)
        age_txt = f"{age}s" if age < 60 else f"{age // 60}min"
        sid = int(sig["id"])
        open_btn = render_button(
            direction.title(),
            cls=f"{dir_cls}-btn",
            onclick=f"openPosition('{epic}', this, '{direction}')",
            title=f"Open {direction} on {epic} at minimum size",
            attrs=(
                f"onmouseenter=\"showFundsTooltip(event, '{epic}')\" "
                'onmouseleave="hideFundsTooltip()"'
            ),
        )
        cards += f"""
        <div class="signal-card signal-{dir_cls}" data-signal-id="{sid}">
            <div class="signal-head">
                <span class="signal-dir signal-dir-{dir_cls}">{direction}</span>
                <span class="signal-epic">{epic}</span>
                <span class="signal-age">{age_txt} ago</span>
            </div>
            <div class="signal-name">{name}</div>
            <div class="signal-meta">
                <span title="Entry strategy that proposed this open">{strategy}</span>
                <span title="Strategy score (diagnostic only)">score {score:.2f}</span>
                <span title="Close-out price when the signal was raised">@ {price_txt}</span>
            </div>
            <div class="signal-actions">
                {render_button('<i data-lucide="line-chart" class="lc-icon"></i> Chart', cls="signal-btn", onclick=f"openSignalChart('{epic}', {sid}, event)", title="Look at the curve — this drops the proposal from the list")}
                {render_button('<i data-lucide="gauge" class="lc-icon"></i> Read', cls="signal-btn", onclick=f"openReadingModal('{epic}', event)", title="Read this market: regime, trends, noise, levels")}
                {open_btn}
                {render_button('<i data-lucide="x" class="lc-icon"></i>', cls="signal-btn signal-dismiss", onclick=f"dismissSignal({sid}, this)", title="Pass — drop this proposal")}
            </div>
        </div>"""
    return f'<div class="signals-grid">{cards}</div>'


def _render_action_cards(jobs: list[dict]) -> str:
    """Render the job cards for the Actions section.

    Each card carries a Run button (visible only in manual mode) and, for jobs
    backed by a cron trigger, an auto/manual switch. Jobs flagged
    ``toggleable=False`` (manual-only or startup-only work) get no switch: there
    is no schedule to turn on or off, so the toggle would only ever fail.
    ``danger`` selects the Run-button colour and whether a confirmation prompt is
    shown before triggering.
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
        toggleable = bool(j.get("toggleable", True))
        needs_confirm = "true" if danger in ("warn", "danger") else "false"
        run_style = "display:none;" if auto else ""
        mode_cls = "auto" if auto else "manual"
        mode_txt = "Automatic" if auto else "Manual"
        checked = "checked" if auto else ""
        auto_cls = " is-auto" if auto else ""
        switch = (
            f"""<label class="switch" title="Automatic / Manual">
                            <input type="checkbox" {checked} onchange="toggleJobMode('{action}', this)">
                            <span class="switch-slider"></span>
                        </label>"""
            if toggleable
            else ""
        )
        mode_row = (
            f'<div class="action-card-mode {mode_cls}">{mode_txt}</div>'
            if toggleable
            else ""
        )
        run_btn = render_button(
            "&#9654; Run",
            cls=f"action-btn {danger} run-btn",
            style=run_style,
            onclick=f"runAction('{action}', this, {needs_confirm})",
        )
        inner = f"""
                    <div class="action-card-head">
                        <span class="action-card-name">{name}</span>
                        {switch}
                    </div>
                    <div class="action-card-desc">{desc}</div>
                    <div class="action-card-schedule"><i data-lucide="clock" class="lc-icon"></i> {schedule}</div>
                    {mode_row}
                    {run_btn}
                    <div class="action-status"></div>
                """
        cards += render_card(
            inner, cls=f"action-card{auto_cls}", attrs=f'data-action="{action}"'
        )
    return cards


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
        pnl_str = f"{pnl:+.2f}€" if d.euro_total is not None else "—"
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
    total_pnl = sum(
        float(d.euro_total) for d in day_records if d.euro_total is not None
    )
    total_color = (
        "#4ade80" if total_pnl > 0 else "#ef4444" if total_pnl < 0 else "#94a3b8"
    )
    table = render_table(
        ["Date", "Trades", "P&amp;L", "State"], rows, style="max-width:600px;"
    )
    return f"""<div style="display:flex;align-items:center;gap:1.5rem;margin-bottom:0.8rem;font-size:0.82rem;">
            <span style="color:#64748b;">{len(day_records)} day{"s" if len(day_records) != 1 else ""}</span>
            <span style="color:{total_color};">30-day total <strong>{total_pnl:+.2f}€</strong></span>
        </div>
        {table}"""


def _render_epic_list_modal(
    epics: list[str],
    db_epics: dict,
    refresh_color: str,
    refresh_label: str,
) -> str:
    """Render the Epic List modal body: header stats, filter and table.

    Mirrors the columns of the former standalone ``/epics`` page, but as a
    fragment embedded in an overlay modal. The whole body lives inside the
    polled ``frag-epic_list_modal`` container, so it stays current with the
    daily epic discovery without a page reload.
    """
    count = len(epics)
    count_color = "#4ade80" if count > 0 else "#ef4444"

    rows = ""
    for name in sorted(epics):
        e = db_epics.get(name)
        epic_id = str(e.id) if e else "—"
        instrument_name = (e.description or "—") if e else "—"
        funds = f"{e.deposit:.2f}€" if (e and e.deposit) else "—"
        stop_loss = f"-{e.stop_loss:.2f}€" if (e and e.stop_loss) else "—"
        if e and e.is_tradable:
            tradable = '<span style="color:#4ade80;">✓</span>'
        elif e and e.not_tradable_reason:
            reason = html.escape(e.not_tradable_reason)
            tradable = (
                f'<span style="color:#475569;">✗</span>'
                f'<span style="color:#64748b;font-size:0.7rem;margin-left:0.3rem;">{reason}</span>'
            )
        else:
            tradable = '<span style="color:#475569;">✗</span>'
        rows += f"""
            <tr>
                <td class="number dim">{html.escape(epic_id)}</td>
                <td class="epic-col">{html.escape(name)}</td>
                <td class="desc-col">{html.escape(instrument_name)}</td>
                <td class="dep-col number">{funds}</td>
                <td class="dep-col number" style="color:#f87171;">{stop_loss}</td>
                <td style="text-align:center;">{tradable}</td>
            </tr>"""
    if not rows:
        rows = (
            '<tr><td colspan="6" style="text-align:center;color:#475569;'
            'padding:2rem;">No epics — run Refresh Epic List from the '
            "dashboard.</td></tr>"
        )

    return f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;align-items:center;">
            <div class="guard-stat"><span class="guard-stat-label">Total epics</span><span class="guard-stat-value" style="color:{count_color};">{count}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Last refresh</span><span class="guard-stat-value" style="color:{refresh_color};font-size:0.85rem;">{html.escape(refresh_label)}</span></div>
            <div class="filter-wrap" style="margin-left:auto;">
                <input id="epic-modal-filter" type="text" placeholder="Filter epics…" oninput="filterEpicsModal(this.value)">
                <span id="epic-modal-count">{count} shown</span>
            </div>
        </div>
        {render_table(
            ["Id", "Epic", "Name", "Funds (1 Buy)", "Loss @ Stop", "Tradable"],
            rows,
            tbody_id="epic-modal-tbody",
        )}"""


def _render_epic_result_modal(closed_positions: list, kpis: dict) -> str:
    """Render the per-epic result modal body (today's closed trades).

    One row per open/close cycle — an epic traded twice in the day shows twice,
    each line with its own result and its own diagnosis. Ordered worst first so
    the day reads top-down as "what cost money, then what paid"; the last row is
    the biggest gain. Every losing line carries a one-sentence explanation (see
    ``_loss_explanation``), which is the whole point of the list: a red number is
    only useful once you know whether the stop was too tight or the market simply
    turned.
    """
    trades = sorted(
        ((p, _display_pnl(p)) for p in closed_positions), key=lambda t: t[1]
    )

    rows = ""
    for position, pnl in trades:
        color = _pnl_color(pnl)
        explanation = _loss_explanation(position, pnl)
        if explanation:
            why = (
                '<span style="color:#fca5a5;font-size:0.78rem;">'
                f"{html.escape(explanation)}</span>"
            )
        else:
            why = '<span style="color:#475569;">—</span>'
        epic_esc = html.escape(position.epic)
        rows += f"""
                    <tr class="clickable-row" onclick="openChartModal('{epic_esc}', event)">
                        <td class="epic-col">{epic_esc}</td>
                        <td class="desc-col">{html.escape(position.epic_name)}</td>
                        <td class="err-ts">{html.escape(_fmt_paris_time(position.date, position.time_open))}</td>
                        <td class="err-ts">{html.escape(_fmt_paris_time(position.date, position.time_close))}</td>
                        <td class="number" style="color:{color};font-weight:600;">{pnl:+.2f}€</td>
                        <td>{why}</td>
                    </tr>"""
    if not rows:
        rows = (
            '<tr><td colspan="6" class="err-empty">No epic closed today — the '
            "per-epic result appears once a position is closed.</td></tr>"
        )

    closed_epics = kpis.get("closed_epics", 0)
    avg_pnl_per_epic = kpis.get("avg_pnl_per_epic", 0.0)
    worst = trades[0] if trades else None
    best = trades[-1] if trades else None
    return f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;">
            <div class="guard-stat"><span class="guard-stat-label">Epics closed</span><span class="guard-stat-value">{closed_epics}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Avg / epic</span><span class="guard-stat-value" style="color:{_pnl_color(avg_pnl_per_epic)};">{avg_pnl_per_epic:+.2f}€</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Avg winner</span><span class="guard-stat-value" style="color:#4ade80;">{kpis.get('avg_epic_win', 0.0):+.2f}€</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Avg loser</span><span class="guard-stat-value" style="color:#ef4444;">{kpis.get('avg_epic_loss', 0.0):+.2f}€</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Worst trade</span><span class="guard-stat-value" style="color:#ef4444;">{f"{html.escape(worst[0].epic_name)} {worst[1]:+.2f}€" if worst else "—"}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Best trade</span><span class="guard-stat-value" style="color:#4ade80;">{f"{html.escape(best[0].epic_name)} {best[1]:+.2f}€" if best else "—"}</span></div>
        </div>
        {render_table(
            ["Epic", "Name", "Opened", "Closed", "P&amp;L", "Why"],
            rows,
        )}"""


def _build_fragments(state: dict) -> dict[str, str]:
    """Build the HTML fragments for every dynamically refreshed dashboard region.

    Returns a mapping ``{fragment_id: html}`` consumed both by the initial
    full-page render (``_render_dashboard``) and by the live polling endpoint
    (``/api/dashboard-fragments``). Keeping a single source of truth here means
    the page and the incremental updates can never drift apart.

    Fragment ids: ``title_bar``, ``kpi_bar``, ``market_rows``,
    ``queue_modal`` (which now also carries the IG API guard detail),
    ``positions_modal``.
    """
    market_summary: list[dict] = state["market_summary"]
    kpis: dict = state["kpis"]
    guard_stats = state.get("guard_stats")
    error_entries: list[APIErrorEntry] = state.get("error_entries") or []
    queue_stats = state.get("queue_stats")
    queue_recent = state.get("queue_recent") or []
    queue_pending_tasks = state.get("queue_pending_tasks") or []
    queue_errors = state.get("queue_errors") or []
    open_positions = state.get("open_positions") or []
    closed_positions = state.get("closed_positions") or []

    # ── Open positions modal rows ──────────────────────────────────────────────
    if open_positions:
        pos_rows_html = ""
        for p in open_positions:
            t_open = _fmt_paris_time(p.date, p.time_open)
            lvl_open = f"{p.level_open:.3f}" if p.level_open else "—"
            lvl_win = f"{p.level_win:.3f}" if p.level_win else "—"
            lvl_stop = f"{p.level_stop:.3f}" if p.level_stop else "—"
            qty = p.quantity or "—"
            pnl_val = float(p.euro or 0)
            pnl_color = _pnl_color(pnl_val)
            pnl_str = f"{pnl_val:+.2f}€" if p.euro is not None else "—"
            strategy_str = (p.strategy.value if p.strategy else "—").upper()
            epic_esc = html.escape(p.epic)
            close_btn = render_button(
                "Close",
                cls="close-pos-btn",
                onclick=f"event.stopPropagation(); closePosition({p.id}, '{epic_esc}', this)",
                title="Close this position manually",
            )
            read_btn = render_button(
                '<i data-lucide="gauge" class="lc-icon"></i>',
                cls="reading-btn",
                onclick=f"openReadingModal('{epic_esc}', event)",
                title="Read this position: gain, excursions, stop room, market state",
            )
            pos_rows_html += f"""
                    <tr class="clickable-row" onclick="openChartModal('{epic_esc}', event)">
                        <td class="err-ts">{html.escape(t_open)}</td>
                        <td class="epic-col">{epic_esc}</td>
                        <td class="desc-col">{html.escape(p.epic_name)}</td>
                        <td class="number">{lvl_open}</td>
                        <td class="number">{lvl_win}</td>
                        <td class="number">{lvl_stop}</td>
                        <td class="number">{qty}</td>
                        <td class="number" style="color:{pnl_color};">{pnl_str}</td>
                        <td class="err-ts">{html.escape(strategy_str)}</td>
                        <td style="text-align:center;white-space:nowrap;">
                            {close_btn} {read_btn}
                        </td>
                    </tr>"""
    else:
        pos_rows_html = '<tr><td colspan="10" class="err-empty">No open positions right now.</td></tr>'

    # ── Closed positions modal rows ────────────────────────────────────────────
    if closed_positions:
        closed_rows_html = ""
        for p in closed_positions:
            d_str = p.date.strftime("%d/%m/%Y") if p.date else "—"
            t_open = _fmt_paris_time(p.date, p.time_open)
            t_close = _fmt_paris_time(p.date, p.time_close)
            lvl_open = f"{p.level_open:.3f}" if p.level_open else "—"
            lvl_close = f"{p.level_close:.3f}" if p.level_close else "—"
            qty = p.quantity or "—"
            pnl_val = _display_pnl(p)
            pnl_color = _pnl_color(pnl_val)
            pnl_str = f"{pnl_val:+.2f}€"
            open_label, open_color = _open_reason_label(p.reason_open)
            close_label, close_color = _close_reason_label(p.reason_close)
            epic_esc = html.escape(p.epic)
            closed_rows_html += f"""
                    <tr class="clickable-row" onclick="openChartModal('{epic_esc}', event)">
                        <td class="err-ts" title="{html.escape(d_str)}">{html.escape(t_open)}</td>
                        <td class="err-ts" title="{html.escape(d_str)}">{html.escape(t_close)}</td>
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
        closed_rows_html = '<tr><td colspan="10" class="err-empty">No closed positions today.</td></tr>'

    market_rows = ""
    for s in market_summary:
        pct = _bid_pct(s["bid"], s["low"], s["high"])
        pct_color = "#4ade80" if pct >= 50 else "#f59e0b" if pct >= 25 else "#ef4444"
        epic_esc = html.escape(str(s["epic"]))
        # Where the price sits inside its current move (a three-segment gauge:
        # start / middle / end) and the regime it sits in — the two reads that
        # replaced the raw high/low pair.
        phase_gauge, phase_label, phase_color, phase_why = _phase_label(
            s.get("phase"), s.get("phase_direction")
        )
        phase_title = html.escape(f"Momentum — {phase_why}")
        regime_label, regime_color, regime_why = _regime_label(s.get("regime"))
        regime_title = html.escape(f"Regime — {regime_why}")
        spread_cost = s.get("spread_cost")
        spread_str = f"{spread_cost:.2f}€" if spread_cost else "—"
        # A market traded and already closed today stays in the watched list so
        # its curve after the exit keeps being recorded — the badge says so, and
        # carries what the epic banked.
        followed_badge = ""
        if s.get("followed"):
            banked = s.get("followed_pnl")
            banked_str = f" {banked:+.2f}€" if banked is not None else ""
            followed_badge = (
                ' <span style="color:#38bdf8;font-size:0.68rem;font-weight:700;'
                'letter-spacing:0.5px;" title="Already traded today — kept live to '
                'follow the market after the exit">TRADED'
                f"{html.escape(banked_str)}</span>"
            )
        buy_btn = render_button(
            "Buy",
            cls="buy-btn",
            onclick=f"event.stopPropagation(); openPosition('{epic_esc}', this, 'BUY')",
            title="Open BUY position at minimum size",
            attrs=(
                f"onmouseenter=\"showFundsTooltip(event, '{epic_esc}')\" "
                'onmouseleave="hideFundsTooltip()"'
            ),
        )
        sell_btn = render_button(
            "Sell",
            cls="sell-btn",
            onclick=f"event.stopPropagation(); openPosition('{epic_esc}', this, 'SELL')",
            title="Open SELL (short) position at minimum size",
            attrs=(
                f"onmouseenter=\"showFundsTooltip(event, '{epic_esc}')\" "
                'onmouseleave="hideFundsTooltip()"'
            ),
        )
        # Opens the market-reading panel (static/reading.js) instead of the chart:
        # the numbers behind the curve — regime, trends per horizon, candle
        # pressure, noise, readable levels — for this epic.
        read_btn = render_button(
            '<i data-lucide="gauge" class="lc-icon"></i>',
            cls="reading-btn",
            onclick=f"openReadingModal('{epic_esc}', event)",
            title="Read this market: regime, trends, noise, levels",
        )
        market_rows += f"""
        <tr class="clickable-row" onclick="openChartModal('{epic_esc}', event)">
            <td class="epic-col">{epic_esc}{followed_badge}</td>
            <td class="desc-col">{html.escape(str(s.get('name', '—')))}</td>
            <td class="number">{spread_str}</td>
            <td class="number" data-sort="{_PHASE_RANK.get(s.get('phase'), 0)}" title="{phase_title}"><span style="color:{phase_color};letter-spacing:0.06em;">{phase_gauge}</span> <span style="color:{phase_color};">{phase_label}</span></td>
            <td title="{regime_title}" style="color:{regime_color};white-space:nowrap;">{regime_label}</td>
            <td>
                <div class="range-bar-wrap">
                    <div class="range-bar-bg">
                        <div class="range-bar-fill" style="width:{pct:.1f}%; background:{pct_color};"></div>
                        <div class="range-bar-cursor" style="left:calc({pct:.1f}% - 1px);"></div>
                    </div>
                    <span class="range-pct" style="color:{pct_color};">{pct:.0f}%</span>
                </div>
            </td>
            <td class="number">{s['dots']}</td>
            <td style="text-align:center;white-space:nowrap;">
                {buy_btn} {sell_btn} {read_btn}
            </td>
        </tr>"""

    pnl_color = _pnl_color(kpis["daily_pnl"])
    # The OPEN tile shows money the market has not paid yet, so it uses the muted
    # "potential" palette (dark green / orange) instead of the realized one.
    open_pnl_color = _unrealized_pnl_color(kpis["open_pnl"])

    # Second sub-line of the OPEN tile: the whole book valued at the stops the
    # group pre-pass plans — the floor under the live figure above. Hidden when no
    # group-aware close profile ran (or the book could not be valued this tick).
    group_book_euro = kpis.get("group_book_euro")
    if kpis["open_trades"] and group_book_euro is not None:
        open_book_sub = (
            '<div class="kpi-sub kpi-sub-alt" title="What the book would bank if '
            "every open position closed at its planned stop — net of the assumed "
            'execution slip and reconciliation margin">'
            f'at planned stops <span style="color:{_unrealized_pnl_color(group_book_euro)};">'
            f"{group_book_euro:+.2f}€</span></div>"
        )
    else:
        open_book_sub = ""

    # "Avg / epic" tile — the day's realized P&L divided by the number of epics
    # that actually closed today. Unlike a win rate it is size-aware: a day made
    # of many small wins and one large loss reads negative here, as it should.
    # The sub-line contrasts the average winning epic with the average losing one,
    # which is the ratio the modal then breaks down instrument by instrument.
    closed_epics = kpis.get("closed_epics", 0)
    avg_pnl_per_epic = kpis.get("avg_pnl_per_epic", 0.0)
    if not closed_epics:
        avg_epic_color = "#475569"
        avg_epic_value = "—"
        avg_epic_sub = '<div class="kpi-sub">No epic closed today</div>'
    else:
        avg_epic_color = _pnl_color(avg_pnl_per_epic)
        avg_epic_value = f"{avg_pnl_per_epic:+.2f}€"
        avg_win = kpis.get("avg_epic_win", 0.0)
        avg_loss = kpis.get("avg_epic_loss", 0.0)
        # Winners and losers on two separate lines: side by side the two euro
        # figures read as a single expression, stacked they read as the two sides
        # of the day they actually are.
        avg_epic_sub = (
            f'<div class="kpi-sub"><span style="color:#4ade80;">'
            f'{kpis.get("winning_epics", 0)} up {avg_win:+.2f}€</span></div>'
            f'<div class="kpi-sub"><span style="color:#ef4444;">'
            f'{kpis.get("losing_epics", 0)} down {avg_loss:+.2f}€</span></div>'
        )

    # Wallet KPI tile — funds available to open a position, with margin in use
    # below. Grey when the balance could not be fetched (no API / startup), red
    # when nothing is left to open, green otherwise.
    wallet_available = kpis.get("wallet_available")
    wallet_used = kpis.get("wallet_used")
    if wallet_available is None:
        wallet_color = "#475569"
        wallet_value = "—"
    else:
        wallet_color = "#4ade80" if wallet_available > 0 else "#ef4444"
        wallet_value = f"{wallet_available:,.2f}€"
    wallet_sub = (
        f"In use: {wallet_used:,.2f}€" if wallet_used is not None else "In use: —"
    )
    wallet_tile = f"""
        <div class="kpi-tile" style="border-left-color:{wallet_color};">
            <div class="kpi-label">
                <span><i data-lucide="wallet" class="lc-icon"></i> Wallet</span>
                <button class="kpi-resync" onclick="resyncWallet(this)" title="Resync balance">
                    <i data-lucide="refresh-cw" class="lc-icon"></i>
                </button>
            </div>
            <div class="kpi-value" style="color:{wallet_color};">{wallet_value}</div>
            <div class="kpi-sub">{wallet_sub}</div>
        </div>"""

    # API Guard status (rendered inside the Queue modal, no longer its own tile).
    if guard_stats is None:
        api_status_color = "#475569"
        api_status_label = "N/A"
        api_border_color = "#475569"
    elif guard_stats.is_blocked:
        api_status_color = "#ef4444"
        api_status_label = "BLOCKED"
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
        todo_count = queue_stats.pending + queue_stats.running
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

    # The Queue tile now also surfaces the IG guard state: a blocked API turns
    # the tile border red regardless of the queue's own counters.
    if guard_stats is not None and guard_stats.is_blocked:
        queue_kpi_border = "#ef4444"

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
        todo_table_html = render_table(
            ["Created", "Method", "Task", "Priority"],
            todo_rows_html,
            cls="err-table queue-todo-table",
            style="margin-top:0.6rem;margin-bottom:0.8rem;",
            tbody_id="todo-tbody",
            wrap_scroll=False,
        )
    else:
        todo_table_html = ""

    # ── Errors table ───────────────────────────────────────────────────────────
    # A dedicated, persistent log of abandoned calls (kept in its own larger ring
    # buffer than `queue_recent`, so a failure survives a burst of later success).
    # The full error and the exact API route/version are shown to ease debugging.
    if queue_errors:
        err_rows = ""
        for e in queue_errors:
            ts = e.failed_at.astimezone(_PARIS).strftime("%H:%M:%S")
            route = f"{e.method} {e.endpoint} (v{e.version})"
            http_display = e.http_status if e.http_status is not None else "—"
            code_display = e.ig_error_code or "—"
            full_err = html.escape(e.error or "—")
            err_rows += f"""
                    <tr>
                        <td class="err-ts">{html.escape(ts)}</td>
                        <td class="err-method">{html.escape(str(e.method))}</td>
                        <td class="err-endpoint" style="white-space:nowrap;">{html.escape(str(e.endpoint))} <span style="color:#64748b;">v{e.version}</span></td>
                        <td class="err-status">{html.escape(str(http_display))}</td>
                        <td class="err-code">{html.escape(str(code_display))}</td>
                        <td class="err-status">{e.attempts}</td>
                        <td class="err-hint" style="white-space:pre-wrap;word-break:break-word;max-width:520px;" title="{html.escape(route)}">{full_err}</td>
                    </tr>"""
        queue_errors_count = len(queue_errors)
        queue_errors_table = render_table(
            ["Time", "Method", "Route", "HTTP", "IG Code", "Tries", "Full error"],
            err_rows,
            tbody_id="queue-err-tbody",
        )
    else:
        queue_errors_count = 0
        queue_errors_table = (
            '<table class="err-table"><tbody id="queue-err-tbody">'
            '<tr><td colspan="7" class="err-empty">No queue errors recorded this session.</td></tr>'
            "</tbody></table>"
        )

    queue_errors_label = (
        f'<i data-lucide="circle-x" class="lc-icon" style="color:#ef4444;"></i> Queue errors ({queue_errors_count})'
        if queue_errors_count
        else '<i data-lucide="circle-check" class="lc-icon" style="color:#4ade80;"></i> Queue errors (none)'
    )

    # ── KPI bar fragment (the tiles, inner HTML of .kpi-bar) ────────────────────
    kpi_bar = f"""
        <div class="kpi-tile clickable" style="border-left-color:{queue_kpi_border}; position:relative;" onclick="openQueueModal()">
            <div class="kpi-label">Queue</div>
            <div class="kpi-value" style="color:{queue_todo_color};">{queue_todo}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{queue_succeeded} done</span>&nbsp;/&nbsp;<span style="color:{queue_failed_color};">{queue_failed} err</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{kpis['epic_kpi_color']}; position:relative;" onclick="openEpicsModal()">
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
            <div class="kpi-value" style="color:{open_pnl_color};">{f"{kpis['open_pnl']:+.2f}€" if kpis['open_trades'] else "—"}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['open_trades']} position{'s' if kpis['open_trades'] != 1 else ''}</span>{f"<span style='color:#64748b;'> {kpis['open_pnl_as_of']}</span>" if kpis['open_trades'] and kpis.get('open_pnl_as_of') else ""}</div>
            {open_book_sub}
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{pnl_color};" onclick="openClosedModal()">
            <div class="kpi-label">CLOSED</div>
            <div class="kpi-value" style="color:{pnl_color};">{f"{kpis['daily_pnl']:+.2f}€" if kpis['closed_trades'] else "—"}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['closed_trades']} trade{'s' if kpis['closed_trades'] != 1 else ''}</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{avg_epic_color};" onclick="openEpicResultModal()" title="Average euro result per epic closed today — the day's realized P&amp;L split over the instruments that actually traded">
            <div class="kpi-label">Avg / epic</div>
            <div class="kpi-value" style="color:{avg_epic_color};">{avg_epic_value}</div>
            {avg_epic_sub}
        </div>
        {wallet_tile}"""

    # ── IG API guard detail (availability + error log) ──────────────────────────
    # Folded into the bottom of the Queue modal — there is no standalone IG API
    # card/modal anymore; the guard state lives alongside the queue it throttles.
    error_log_table = render_table(
        ["Time", "Method", "Endpoint", "HTTP", "IG Error Code", "Translation"],
        error_rows_html,
        tbody_id="err-tbody",
    )
    api_detail = f"""
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
        {error_log_table}"""

    # ── Queue modal fragment (inner content) ────────────────────────────────────
    # The IG API guard detail (api_detail) is appended at the bottom so the queue
    # and the guard that throttles it are inspected from a single modal.
    queue_finished_table = render_table(
        ["Finished", "Method", "Task", "Status", "Tries", "Last error"],
        queue_rows_html,
        style="margin-top:0.6rem;",
        wrap_scroll=False,
    )
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
        {queue_finished_table}
        <h3 style="color:#94a3b8;font-size:0.78rem;text-transform:uppercase;letter-spacing:1px;margin:1.6rem 0 0.4rem;">{queue_errors_label}</h3>
        <div style="display:flex;justify-content:flex-end;padding:0.3rem 0 0.4rem;">
            <button class="err-clear-btn" onclick="clearQueueErrors()" style="display:inline-flex;align-items:center;gap:0.35rem;"><i data-lucide="x" class="lc-icon"></i> Clear</button>
        </div>
        {queue_errors_table}
        <h3 style="color:#e2e8f0;font-size:0.9rem;font-weight:600;margin:1.6rem 0 0.9rem;padding-top:1rem;border-top:1px solid #1e293b;"><i data-lucide="plug" class="lc-icon"></i> IG API</h3>
        {api_detail}"""

    # ── Open positions modal fragment ───────────────────────────────────────────
    positions_table = render_table(
        [
            "Opened",
            "Epic",
            "Name",
            "Level open",
            "Target",
            "Stop",
            "Qty",
            "P&amp;L",
            "Strategy",
            "Action",
        ],
        pos_rows_html,
    )
    positions_modal = f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;">
            <div class="guard-stat"><span class="guard-stat-label">Count</span><span class="guard-stat-value" style="color:#4ade80;">{kpis['open_trades']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Total P&amp;L</span><span class="guard-stat-value" style="color:{open_pnl_color};">{kpis['open_pnl']:.2f}€</span></div>
        </div>
        {positions_table}"""

    # ── Closed positions modal fragment ─────────────────────────────────────────
    # This modal is scoped to today, so its win rate uses today's split.
    win_rate_pct = kpis["win_rate_today"] * 100
    closed_table = render_table(
        [
            "Opened",
            "Closed",
            "Epic",
            "Name",
            "Level open",
            "Level close",
            "Qty",
            "P&amp;L",
            "Open reason",
            "Close reason",
        ],
        closed_rows_html,
    )
    closed_positions_modal = f"""
        <div class="guard-stat-row" style="margin-bottom:1rem;">
            <div class="guard-stat"><span class="guard-stat-label">Trades</span><span class="guard-stat-value">{kpis['closed_trades']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Total P&amp;L</span><span class="guard-stat-value" style="color:{pnl_color};">{kpis['daily_pnl']:+.2f}€</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Wins</span><span class="guard-stat-value" style="color:#4ade80;">{kpis['wins']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Losses</span><span class="guard-stat-value" style="color:#ef4444;">{kpis['losses']}</span></div>
            <div class="guard-stat"><span class="guard-stat-label">Win rate</span><span class="guard-stat-value" style="color:{'#4ade80' if kpis['win_rate_today'] >= 0.5 else '#ef4444'};">{win_rate_pct:.1f}%</span></div>
        </div>
        {closed_table}"""

    _today = date.today()
    return {
        "title_bar": _render_title_bar(
            bool(state.get("auto_open_enabled", True)),
            state.get("selection") or {},
            str(state.get("open_mode") or "auto"),
        ),
        "kpi_bar": kpi_bar,
        "market_rows": market_rows,
        "day_history": _render_day_history_section(
            state.get("day_records") or [], _today
        ),
        "queue_modal": queue_modal,
        "epic_list_modal": _render_epic_list_modal(
            state.get("all_epics") or [],
            state.get("epic_db_map") or {},
            kpis["epic_kpi_color"],
            kpis["refresh_label"],
        ),
        "positions_modal": positions_modal,
        "closed_positions_modal": closed_positions_modal,
        "epic_result_modal": _render_epic_result_modal(closed_positions, kpis),
        "actions": _render_action_cards(state.get("jobs", [])),
        "signals_section": _render_signals_section(
            state.get("signals") or [],
            str(state.get("open_mode") or "auto"),
        ),
        "logs_section": _render_logs_section(state.get("log_entries") or []),
    }
