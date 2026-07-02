"""Dashboard HTML fragment builders (dynamically refreshed regions)."""

import html
from datetime import UTC, date, datetime, time

from src.core.api_error_log import APIErrorEntry
from src.markets.market_scanner import MarketScanner
from src.web.routes.dashboard.components import (
    render_button,
    render_card,
    render_table,
)
from src.web.routes.dashboard.state import (
    _PARIS,
    _bid_pct,
    _close_reason_label,
    _display_pnl,
    _open_reason_label,
    _pnl_color,
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
        run_btn = render_button(
            "&#9654; Run",
            cls=f"action-btn {danger} run-btn",
            style=run_style,
            onclick=f"runAction('{action}', this, {needs_confirm})",
        )
        inner = f"""
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
                    {run_btn}
                    <div class="action-status"></div>
                """
        cards += render_card(
            inner, cls=f"action-card{auto_cls}", attrs=f'data-action="{action}"'
        )
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
                        <div class="config-key">Open Strategy</div>
                        <div class="config-value">{settings.open_strategy}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Stop Strategy</div>
                        <div class="config-value">{settings.stop_strategy}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Close · Zone Start</div>
                        <div class="config-value">{settings.close_zonestart}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Close · Zone Margin</div>
                        <div class="config-value">{settings.close_zonemarge}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Close · Zone Profit</div>
                        <div class="config-value">{settings.close_zoneprofit}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-key">Max Spread Ratio</div>
                        <div class="config-value">{MarketScanner.DEFAULT_MAX_SPREAD_RATIO}</div>
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
        dir_color = (
            "#4ade80"
            if direction == "BUY"
            else "#ef4444" if direction == "SELL" else "#94a3b8"
        )
        day_str = r.day.strftime("%d/%m") if r.day else "—"
        rows += f"""
                <tr>
                    <td class="epic-col">{html.escape(r.epic)}</td>
                    <td style="color:{dir_color};font-weight:600;">{html.escape(direction)}</td>
                    <td class="err-ts">{html.escape(day_str)}</td>
                </tr>"""
    table = render_table(
        ["Epic", "Direction", "Updated"], rows, style="max-width:480px;"
    )
    return f"""<div style="display:flex;align-items:center;gap:1.5rem;margin-bottom:0.8rem;font-size:0.82rem;">
            <span style="color:#64748b;">Week {html.escape(week_str)}</span>
            <span style="color:#4ade80;"><strong>{buy_count}</strong> BUY</span>
            <span style="color:#ef4444;"><strong>{sell_count}</strong> SELL</span>
            <span style="color:#94a3b8;">{len(resume_records)} epic{"s" if len(resume_records) != 1 else ""}</span>
        </div>
        {table}"""


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


def _build_fragments(state: dict) -> dict[str, str]:
    """Build the HTML fragments for every dynamically refreshed dashboard region.

    Returns a mapping ``{fragment_id: html}`` consumed both by the initial
    full-page render (``_render_dashboard``) and by the live polling endpoint
    (``/api/dashboard-fragments``). Keeping a single source of truth here means
    the page and the incremental updates can never drift apart.

    Fragment ids: ``kpi_bar``, ``market_rows``, ``queue_modal`` (which now also
    carries the IG API guard detail), ``positions_modal``.
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
                        <td style="text-align:center;">
                            {close_btn}
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
        spread_cost = s.get("spread_cost")
        spread_str = f"{spread_cost:.2f}€" if spread_cost else "—"
        buy_btn = render_button(
            "Buy",
            cls="buy-btn",
            onclick=f"event.stopPropagation(); openPosition('{epic_esc}', this)",
            title="Open BUY position at minimum size",
            attrs=(
                f"onmouseenter=\"showFundsTooltip(event, '{epic_esc}')\" "
                'onmouseleave="hideFundsTooltip()"'
            ),
        )
        market_rows += f"""
        <tr class="clickable-row" onclick="openChartModal('{epic_esc}', event)">
            <td class="epic-col">{epic_esc}</td>
            <td class="desc-col">{html.escape(str(s.get('name', '—')))}</td>
            <td class="number">{spread_str}</td>
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
            <td class="number">{s['dots']}</td>
            <td style="text-align:center;">
                {buy_btn}
            </td>
        </tr>"""

    pnl_color = _pnl_color(kpis["daily_pnl"])
    open_pnl_color = _pnl_color(kpis["open_pnl"])

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
            <div class="kpi-label"><i data-lucide="wallet" class="lc-icon"></i> Wallet</div>
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
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['open_trades']} position{'s' if kpis['open_trades'] != 1 else ''}</span>{f"<span style='color:#64748b;'> · IG {kpis['open_pnl_as_of']}</span>" if kpis['open_trades'] and kpis.get('open_pnl_as_of') else ""}</div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{pnl_color};" onclick="openClosedModal()">
            <div class="kpi-label">CLOSED</div>
            <div class="kpi-value" style="color:{pnl_color};">{f"{kpis['daily_pnl']:+.2f}€" if kpis['closed_trades'] else "—"}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['closed_trades']} trade{'s' if kpis['closed_trades'] != 1 else ''}</span></div>
        </div>
        <div class="kpi-tile clickable" style="border-left-color:{'#4ade80' if kpis['win_rate'] >= 0.5 else '#ef4444'};" onclick="openWinRateModal()">
            <div class="kpi-label">Win rate</div>
            <div class="kpi-value" style="color:{'#4ade80' if kpis['win_rate'] >= 0.5 else '#ef4444'};">{kpis['win_rate']:.1%}</div>
            <div class="kpi-sub"><span style="color:#4ade80;">{kpis['total_wins']} win</span>&nbsp;/&nbsp;<span style="color:#ef4444;">{kpis['total_losses']} Loose</span></div>
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
        "kpi_bar": kpi_bar,
        "market_rows": market_rows,
        "week_summary": _render_week_summary_section(
            state.get("resume_records") or [], _today
        ),
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
        "actions": _render_action_cards(state.get("jobs", [])),
        "logs_section": _render_logs_section(state.get("log_entries") or []),
    }
