"""Standalone full-page render: tradable epic list."""

import html
from datetime import date, datetime

from src.markets.market_scanner import MarketInfo
from src.web.routes.dashboard.state import _PARIS


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
        # Worst-case euro loss if a minimum-size BUY is stopped out at the initial
        # protective stop (precomputed at scan time). ``None`` when the contract /
        # price / stop-rule data needed to size it was missing.
        if m.stop_loss_eur is not None:
            risk_sort = f"{m.stop_loss_eur:.6f}"
            risk_txt = f"{m.stop_loss_eur:.2f} €"
        else:
            # Sort unknown risk to the bottom in both directions.
            risk_sort = ""
            risk_txt = "—"
        rows += f"""
            <tr>
                <td class="number dim" data-sort="{i}">{i}</td>
                <td class="epic-col" data-sort="{html.escape(m.epic)}">{html.escape(m.epic)}</td>
                <td class="desc-col" data-sort="{html.escape(m.name)}">{html.escape(m.name)}</td>
                <td class="type-col" data-sort="{html.escape(m.status)}">{html.escape(m.status)}</td>
                <td class="number" data-sort="{m.bid:.6f}">{m.bid:.2f}</td>
                <td class="number" data-sort="{m.offer:.6f}">{m.offer:.2f}</td>
                <td class="number" data-sort="{spread_pct:.6f}">{spread_pct:.3f}%</td>
                <td class="number" data-sort="{risk_sort}">{risk_txt}</td>
            </tr>"""

    if not rows:
        rows = '<tr><td colspan="8" style="text-align:center;color:#475569;padding:2rem;">No tradable epics — run Refresh Tradable Epics from the dashboard.</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Tradable Epics</title>
    <link rel="stylesheet" href="/static/style.css?v=13">
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
    <script src="/static/tables.js?v=1"></script>
</head>
<body>
<div class="container">
    <nav>
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/">Dashboard</a></li>
            <li><a href="/epics/tradable" class="active">Tradable</a></li>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/simulator">Simulator</a></li>
            <li><a href="/backtest">Backtest</a></li>
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
        <table id="epic-table">
            <thead>
                <tr>
                    <th style="width:3rem;" data-type="num">#</th>
                    <th data-type="text">Epic</th>
                    <th data-type="text">Name</th>
                    <th data-type="text">Status</th>
                    <th data-type="num">Bid</th>
                    <th data-type="num">Offer</th>
                    <th data-type="num">Spread</th>
                    <th data-type="num" title="Worst-case euro loss if a min-size BUY is stopped out at the initial stop">Risk</th>
                </tr>
            </thead>
            <tbody id="epic-tbody">
                {rows}
            </tbody>
        </table>
    </div>

    <footer>Open/TRADEABLE subset of the epic list — refreshes hourly during market hours. Spread is applied later at analysis time. Risk = worst-case euro loss if a minimum-size BUY is stopped out at the initial protective stop.</footer>
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

// Click-to-sort is provided globally by /static/tables.js (honours the
// per-header data-type and per-cell data-sort attributes emitted above).
lucide.createIcons();
</script>
</body>
</html>"""
