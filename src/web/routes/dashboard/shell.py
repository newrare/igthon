"""Full-page dashboard shell (skeleton embedding the dynamic fragments)."""

import html

from src.web.routes.dashboard.components import (
    render_button,
    render_confirm_modal,
    render_modal,
)
from src.web.routes.dashboard.fragments import (
    _build_fragments,
    _render_config_grid,
)


# The open / stop / close selection is chosen exclusively in ``.env`` (the single
# source of truth) — the dashboard only *displays* the active names, read-only.
def _render_selection_name(name: str, *, icon: str, cls: str, title: str) -> str:
    """Render one active selection name as a read-only title-bar chip."""
    return (
        f'<span class="{cls}" title="{title}">'
        f'<i data-lucide="{icon}" class="lc-icon"></i>'
        f'<span class="strategy-name">{html.escape(name or "—")}</span>'
        "</span>"
    )


def _render_strategy_name(current: str) -> str:
    """Read-only title-bar chip for the active entry (open) strategy."""
    return _render_selection_name(
        current,
        icon="cpu",
        cls="dashboard-title-strategy",
        title="Active entry (open) strategy — set OPEN_STRATEGY in .env",
    )


def _render_stop_distance_name(current: str) -> str:
    """Read-only title-bar chip for the active stop policy."""
    return _render_selection_name(
        current,
        icon="ruler",
        cls="dashboard-title-stop",
        title="Active stop policy (initial stop) — set STOP_STRATEGY in .env",
    )


def _render_close_profile_name(current: str) -> str:
    """Read-only title-bar chip for the active close zones (start/margin/profit)."""
    return _render_selection_name(
        current,
        icon="shield",
        cls="dashboard-title-exit",
        title=(
            "Active close zones (exit) — set CLOSE_ZONESTART / CLOSE_ZONEMARGE / "
            "CLOSE_ZONEPROFIT in .env"
        ),
    )


def _render_modals(settings, frags: dict[str, str]) -> str:
    """Build every overlay modal from the shared component helpers."""
    return "".join(
        [
            render_modal(
                modal_id="queue-modal",
                close_fn="closeQueueModal",
                title='<i data-lucide="inbox" class="lc-icon"></i> API Queue',
                refresh_id="refresh-queue",
                body=f'<div id="frag-queue_modal">{frags["queue_modal"]}</div>',
            ),
            render_modal(
                modal_id="chart-modal",
                close_fn="closeChartModal",
                title="Chart",
                title_id="chart-modal-title",
                # Above the other live-data modals (default 8500) so the chart
                # opens on top when launched from an open/closed positions row,
                # rather than behind their dark overlay (which hid the indicators).
                z_index=8700,
                # A Buy button next to the title opens a BUY on the epic
                # currently displayed (tracked client-side in
                # ``_chartModalEpic``), reusing the same confirm + funds-tooltip
                # flow as the market-data table rows.
                header_actions=render_button(
                    "Buy",
                    cls="buy-btn",
                    onclick="openPosition(_chartModalEpic, this)",
                    title="Open BUY position at minimum size",
                    attrs=(
                        'onmouseenter="showFundsTooltip(event, _chartModalEpic)" '
                        'onmouseleave="hideFundsTooltip()"'
                    ),
                ),
                # The chart sits in a relative wrapper so the carousel arrows
                # (previous/next epic in the source table) and the "paused"
                # zoom badge can be overlaid on its edges. The arrows are hidden
                # by JS when the source list holds a single epic.
                body=(
                    '<div style="position:relative;height:100%;min-height:420px;">'
                    '<button id="chart-nav-prev" class="chart-nav-btn chart-nav-prev" '
                    'onclick="chartNavPrev()" title="Previous epic (←)" '
                    'style="display:none;">‹</button>'
                    '<div id="chart-paused-badge" style="display:none;">'
                    "⏸ Paused (zoomed) — double-click to resume</div>"
                    # Running/realised P&L for the epic's trade(s), overlaid
                    # top-left of the chart. Filled by _loadChart from the
                    # trade's stored euro P&L + position state.
                    '<div id="chart-modal-pnl" style="position:absolute;'
                    "top:6px;left:10px;z-index:5;font-size:0.82rem;"
                    "background:rgba(28,23,20,0.78);padding:3px 9px;"
                    'border-radius:4px;display:none;"></div>'
                    '<div id="chart-container" style="height:100%;'
                    'min-height:420px;"></div>'
                    '<button id="chart-nav-next" class="chart-nav-btn chart-nav-next" '
                    'onclick="chartNavNext()" title="Next epic (→)" '
                    'style="display:none;">›</button>'
                    "</div>"
                ),
            ),
            render_modal(
                modal_id="epics-modal",
                close_fn="closeEpicsModal",
                title='<i data-lucide="globe" class="lc-icon"></i> Epic List',
                refresh_id="refresh-epics",
                body=f'<div id="frag-epic_list_modal">{frags["epic_list_modal"]}</div>',
            ),
            render_confirm_modal(
                modal_id="buy-confirm-modal",
                close_fn="closeBuyConfirmModal",
                title="Confirm BUY Order",
                title_color="#E07B39",
                lead_html=(
                    '<p style="color:#cbd5e1;margin:0 0 0.4rem;">Open BUY on '
                    '<strong id="buy-confirm-epic" style="color:#f0fdf4;">'
                    "</strong>?</p>"
                ),
                note="This places a real market order at minimum deal size.",
                confirm_label="Confirm BUY",
                confirm_style=(
                    "background:#16803c;border:1px solid #16803c;color:#f0fdf4;"
                    "cursor:pointer;font-size:0.85rem;font-weight:600;"
                    "padding:0.4rem 1rem;border-radius:4px;"
                ),
            ),
            render_confirm_modal(
                modal_id="close-confirm-modal",
                close_fn="closeCloseConfirmModal",
                title="Confirm Close Position",
                title_color="#ef4444",
                lead_html=(
                    '<p style="color:#cbd5e1;margin:0 0 0.4rem;">Close position on '
                    '<strong id="close-confirm-epic" style="color:#f0fdf4;">'
                    "</strong>?</p>"
                ),
                note="This closes the position at current market price.",
                confirm_label="Confirm Close",
                confirm_style=(
                    "background:#991b1b;border:1px solid #991b1b;color:#fef2f2;"
                    "cursor:pointer;font-size:0.85rem;font-weight:600;"
                    "padding:0.4rem 1rem;border-radius:4px;"
                ),
            ),
            render_modal(
                modal_id="positions-modal",
                close_fn="closePositionsModal",
                title='<i data-lucide="activity" class="lc-icon"></i> Open Positions',
                refresh_id="refresh-positions",
                body=f'<div id="frag-positions_modal">{frags["positions_modal"]}</div>',
            ),
            render_modal(
                modal_id="closed-modal",
                close_fn="closeClosedModal",
                title=(
                    '<i data-lucide="check-circle" class="lc-icon"></i> '
                    "Closed Positions — Today"
                ),
                refresh_id="refresh-closed",
                body=(
                    '<div id="frag-closed_positions_modal">'
                    f'{frags["closed_positions_modal"]}</div>'
                ),
            ),
            render_modal(
                modal_id="winrate-modal",
                close_fn="closeWinRateModal",
                title='<i data-lucide="settings" class="lc-icon"></i> Configuration',
                body=_render_config_grid(settings),
            ),
        ]
    )


def _render_connection_banner(state: dict) -> str:
    """Banner shown when the bot is not connected to IG.

    The web server starts before (and independently of) the IG login, so the
    dashboard is reachable even when the broker connection fails. This explains
    the degraded state instead of leaving the user with a blank or broken page.
    """
    error = state.get("startup_error")
    connecting = state.get("connecting")
    if error:
        return (
            '<div class="conn-banner conn-banner-error" role="alert">'
            '<i data-lucide="alert-triangle" class="lc-icon"></i>'
            "<span><strong>Not connected to IG.</strong> "
            f"The dashboard is running in read-only mode. Reason: {error}. "
            "Check <code>IG_API_KEY</code> and credentials in <code>.env</code> — "
            "the bot retries automatically and goes live once it connects.</span>"
            "</div>"
        )
    if connecting:
        return (
            '<div class="conn-banner conn-banner-info" role="status">'
            '<i data-lucide="loader" class="lc-icon"></i>'
            "<span>Connecting to IG… the dashboard will go live automatically."
            "</span></div>"
        )
    return ""


def _render_dashboard(settings, state: dict) -> str:
    """Render the full dashboard shell with nav, config, commands and the
    dynamically refreshed fragments (KPI bar, market data, modals).

    The dynamic regions are produced by :func:`_build_fragments` and embedded in
    containers (``id="frag-*"``) that the client refreshes in place every
    second via ``/api/dashboard-fragments`` — no full-page reload.
    """
    frags = _build_fragments(state)
    modals = _render_modals(settings, frags)

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Dashboard</title>
    <link rel="stylesheet" href="/static/style.css?v=7">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
{modals}
<div class="container">

    {_render_connection_banner(state)}

    <!-- Main title — entry (open) → stop distance (initial stop) → close (exit) -->
    <div class="dashboard-title">
        <i data-lucide="bot" class="lc-icon"></i>
        <h1>IG Trading Bot</h1>
        <span class="dashboard-title-sep">·</span>
        {_render_strategy_name(settings.open_strategy)}
        <i data-lucide="arrow-right" class="lc-icon dashboard-title-arrow"></i>
        {_render_stop_distance_name(settings.stop_strategy)}
        <i data-lucide="arrow-right" class="lc-icon dashboard-title-arrow"></i>
        {_render_close_profile_name(
            f"{settings.close_zonestart}/{settings.close_zonemarge}/"
            f"{settings.close_zoneprofit}"
        )}
    </div>

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
            <div style="overflow-x:auto;">
                <table class="err-table">
                    <thead>
                        <tr>
                            <th>Epic</th>
                            <th>Name</th>
                            <th title="Euro cost of crossing the spread once at minimum deal size: (offer − bid) × € per point">Spread (€)</th>
                            <th>High / Low</th>
                            <th>Bid % range</th>
                            <th title="Number of price points (readings) currently buffered for this epic">Dots</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="frag-market_rows">{frags['market_rows']}</tbody>
                </table>
            </div>
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
            <li><a href="/simulator">Simulator</a></li>
            <li><a href="/backtest">Backtest</a></li>
            <li><a href="/positions" target="_blank">Positions<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/positions/summary" target="_blank">Daily Summary<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/api/status" target="_blank">API Status<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
            <li><a href="/api/prices/IX.D.DAX.IFMM.IP" target="_blank">Prices (DAX)<svg class="ext-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4.5 3H3a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.5M7.5 1.5H10.5M10.5 1.5V4.5M10.5 1.5L5.5 6.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></a></li>
        </ul>
        <button id="btn-pause" class="nav-btn" onclick="togglePause()" style="display:inline-flex;align-items:center;gap:0.4rem;"><i data-lucide="pause" class="lc-icon"></i> Pause</button>
    </nav>

    <footer id="footer-refresh">Live — updating every 1 s</footer>
</div>

<script src="/static/dashboard.js?v=26"></script>
</body>
</html>"""
