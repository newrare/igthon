"""Full-page dashboard shell (skeleton embedding the dynamic fragments)."""

from src.web.routes.dashboard.components import (
    render_button,
    render_confirm_modal,
    render_modal,
)
from src.web.routes.dashboard.fragments import _build_fragments


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
                # Buy/Sell buttons next to the title open a BUY or SELL (short) on
                # the epic currently displayed (tracked client-side in
                # ``_chartModalEpic``), reusing the same confirm + funds-tooltip
                # flow as the market-data table rows.
                header_actions=(
                    render_button(
                        "Buy",
                        cls="buy-btn",
                        onclick="openPosition(_chartModalEpic, this, 'BUY')",
                        title="Open BUY position at minimum size",
                        attrs=(
                            'onmouseenter="showFundsTooltip(event, _chartModalEpic)" '
                            'onmouseleave="hideFundsTooltip()"'
                        ),
                    )
                    + " "
                    + render_button(
                        "Sell",
                        cls="sell-btn",
                        onclick="openPosition(_chartModalEpic, this, 'SELL')",
                        title="Open SELL (short) position at minimum size",
                        attrs=(
                            'onmouseenter="showFundsTooltip(event, _chartModalEpic)" '
                            'onmouseleave="hideFundsTooltip()"'
                        ),
                    )
                    + " "
                    # Manual exit for the position shown on the chart. Hidden by
                    # JS unless the epic has an OPEN trade, whatever the sign of
                    # its running P&L: banking a gain and cutting a loss are both
                    # legitimate manual calls. The label carries the P&L that
                    # clicking would realise.
                    + render_button(
                        "Close",
                        cls="close-pos-btn",
                        style="display:none;",
                        onclick="closeChartPosition(this)",
                        title="Close this position now at market price",
                        attrs='id="chart-close-btn"',
                    )
                    + " "
                    # Queued exits: instead of closing now, arm a standing order
                    # the monitor honours on the tick the running P&L clears a
                    # euro threshold — 0 € ("as soon as it is positive") or a
                    # figure the user types (e.g. -20 € to cap a loss). Both are
                    # the same order with a different threshold, persisted on the
                    # position so a restart cannot drop it.
                    + render_button(
                        "⏳ ≥ 0 €",
                        cls="close-pos-btn",
                        # Same geometry as Close, blue instead of red: a queued
                        # order must not read as "close now".
                        style=(
                            "display:none;background:#172554;"
                            "border-color:#1e3a8a;color:#60a5fa;"
                        ),
                        onclick="armChartEuroTarget(this, 0)",
                        title=(
                            "Queue a close: exit as soon as the running P&amp;L "
                            "turns positive (checked on every monitor tick)"
                        ),
                        attrs='id="chart-arm-zero-btn"',
                    )
                    + " "
                    + render_button(
                        "⏳ ≥ x €",
                        cls="close-pos-btn",
                        # Same geometry as Close, blue instead of red: a queued
                        # order must not read as "close now".
                        style=(
                            "display:none;background:#172554;"
                            "border-color:#1e3a8a;color:#60a5fa;"
                        ),
                        onclick="armChartEuroTargetPrompt(this)",
                        title=(
                            "Queue a close: exit as soon as the running P&amp;L is "
                            "above a euro threshold you choose (e.g. -20 €)"
                        ),
                        attrs='id="chart-arm-custom-btn"',
                    )
                    + " "
                    # Free stop: move the stop to the price where the position
                    # would realise a euro P&L the user types (e.g. -30 €). The
                    # right-edge stop buttons stop at the scale's edges, so with
                    # the price at the top (a short) or the bottom (a long) there
                    # is no button left to widen the stop with.
                    + render_button(
                        "⛔ Stop @ x €",
                        cls="close-pos-btn",
                        style=(
                            "display:none;background:#2a1f0a;"
                            "border-color:#5a4210;color:#f59e0b;"
                        ),
                        onclick="setChartStopPrompt(this)",
                        title=(
                            "Move the stop to where the position would realise "
                            "a euro P&amp;L you choose (e.g. -30 €)"
                        ),
                        attrs='id="chart-stop-custom-btn"',
                    )
                    + " "
                    # Opens the market-reading panel (regime, trends, noise,
                    # levels, and the since-open measures when a trade is on) for
                    # the epic currently displayed. Rendered by static/reading.js
                    # on top of this modal.
                    + render_button(
                        '<i data-lucide="gauge" class="lc-icon"></i> Reading',
                        cls="reading-btn",
                        onclick="openReadingModal(_chartModalEpic, event)",
                        title="Read this market: regime, trends, noise, levels",
                    )
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
                    # Info badges overlaid top-left of the chart: the running /
                    # realised P&L for the epic's trade(s) (filled by _loadChart
                    # from the trade's stored euro P&L + position state), the live
                    # dealing spread of the market, and its shape — the regime
                    # plus one trend arrow per horizon (5/15/30/60 min).
                    '<div style="position:absolute;top:6px;left:10px;z-index:5;'
                    'display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
                    '<div id="chart-modal-pnl" style="font-size:0.82rem;'
                    "background:rgba(28,23,20,0.78);padding:3px 9px;"
                    'border-radius:4px;display:none;"></div>'
                    '<div id="chart-modal-spread" style="font-size:0.82rem;'
                    "background:rgba(28,23,20,0.78);padding:3px 9px;"
                    'border-radius:4px;display:none;"></div>'
                    '<div id="chart-modal-shape" style="font-size:0.82rem;'
                    "background:rgba(28,23,20,0.78);padding:3px 9px;"
                    'border-radius:4px;display:none;"></div>'
                    # Noise vs spread: two concentric discs sized by the market's
                    # own tick-noise band and by the dealing spread, so it is
                    # visible at a glance whether the noise a stop has to survive
                    # is bigger than what a round trip costs.
                    '<div id="chart-modal-noise" style="font-size:0.82rem;'
                    "background:rgba(28,23,20,0.78);padding:3px 9px;"
                    "border-radius:4px;display:none;align-items:center;"
                    'gap:6px;"></div>'
                    # The armed queued close, when one is set: what it waits for,
                    # and a click to cancel it.
                    '<div id="chart-modal-armed" style="font-size:0.82rem;'
                    "background:rgba(28,23,20,0.78);padding:3px 9px;"
                    'border-radius:4px;display:none;"></div>'
                    "</div>"
                    '<div id="chart-container" style="height:100%;'
                    'min-height:420px;"></div>'
                    # The modal title now names the instrument, so the epic
                    # identifier lives here: a discreet footnote under the curve
                    # (left-aligned on the plot area, past Plotly's y-axis
                    # margin) for whoever needs the exact IG code.
                    '<div id="chart-modal-epic" style="position:absolute;'
                    "bottom:2px;left:58px;z-index:5;font-size:0.66rem;"
                    "color:#64748b;letter-spacing:0.05em;"
                    'pointer-events:none;"></div>'
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
                modal_id="sell-confirm-modal",
                close_fn="closeSellConfirmModal",
                title="Confirm SELL (short) Order",
                title_color="#ef4444",
                lead_html=(
                    '<p style="color:#cbd5e1;margin:0 0 0.4rem;">Open SELL (short) on '
                    '<strong id="sell-confirm-epic" style="color:#fef2f2;">'
                    "</strong>?</p>"
                ),
                note=(
                    "This places a real short market order at minimum deal size — "
                    "the stop sits ABOVE the entry and everything is mirrored."
                ),
                confirm_label="Confirm SELL",
                confirm_style=(
                    "background:#991b1b;border:1px solid #991b1b;color:#fef2f2;"
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
                modal_id="epic-result-modal",
                close_fn="closeEpicResultModal",
                title=(
                    '<i data-lucide="bar-chart-3" class="lc-icon"></i> '
                    "Result per epic — Today"
                ),
                refresh_id="refresh-epic-result",
                body=(
                    '<div id="frag-epic_result_modal">'
                    f'{frags["epic_result_modal"]}</div>'
                ),
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
    <link rel="stylesheet" href="/static/style.css?v=21">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
{modals}
<div class="container">

    {_render_connection_banner(state)}

    <!-- Title bar — bot identity, entry (open) → stop → close (exit) selection,
         and the day controls (block openings / end day). Its colour is the
         auto-open state, so it doubles as the auto-open switch. -->
    <div id="frag-title_bar">{frags['title_bar']}</div>

    <!-- KPI Bar -->
    <div class="kpi-updated">Live data · updated <span id="refresh-kpi">—</span></div>
    <div class="kpi-bar" id="frag-kpi_bar">{frags['kpi_bar']}</div>

    <!-- Entry signals — opens the bot proposes instead of taking (OPEN_MODE=notify).
         Each card is a validated intent waiting for the user's decision; the bell
         button asks the browser for permission to notify when a new one lands. -->
    <div class="section">
        <div class="section-header" data-sid="signals">
            <span class="section-title"><i data-lucide="bell-ring" class="lc-icon"></i> Entry Signals — Your Call</span>
            <button class="section-action" id="notify-permission-btn" title="Allow desktop notifications for new entry signals" onclick="event.stopPropagation(); requestSignalNotifications(this)"><i data-lucide="bell" class="lc-icon"></i> Notifications</button>
            <button class="section-action" id="signal-sound-btn" title="A chime plays on every new entry signal (click to mute)" onclick="event.stopPropagation(); toggleSignalSound(this)"><i data-lucide="volume-2" class="lc-icon"></i> Sound on</button>
            <button class="section-action" id="signal-test-btn" title="Fire a fake signal to check the chime, the desktop notification and the tab badge" onclick="event.stopPropagation(); testSignalAlert()"><i data-lucide="flask-conical" class="lc-icon"></i> Test</button>
            <span class="section-refresh">updated <span id="refresh-signals">—</span></span>
            <button class="section-toggle">&#8722;</button>
        </div>
        <div class="section-body">
            <!-- What is actually armed right now (permission, sound, live
                 stream): a silent dashboard must never be mistaken for a quiet
                 market. Filled in by dashboard.js. -->
            <div id="signal-alert-status" class="alert-status"></div>
            <div id="frag-signals_section">{frags['signals_section']}</div>
        </div>
    </div>

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
                            <th title="How far along its current directional move the market is: start (young), mid (established) or end (stretched / already giving back). Blank when there is no move to be inside of.">Momentum</th>
                            <th title="Market state over the last 30 min: trend up/down, range (contained, mean-reverting) or volatile (big swings going nowhere)">Regime</th>
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

    <!-- Daily History -->
    <div class="section">
        <div class="section-header" data-sid="day">
            <span class="section-title"><i data-lucide="calendar-days" class="lc-icon"></i> Daily History</span>
            <button class="section-action" title="Resync from IG realized P&amp;L (last 30 days)" onclick="event.stopPropagation(); resyncDayHistory(this)"><i data-lucide="refresh-cw" class="lc-icon"></i> Resync</button>
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

<script src="/static/tables.js?v=2"></script>
<script src="/static/dashboard.js?v=53"></script>
<script src="/static/reading.js?v=1"></script>
</body>
</html>"""
