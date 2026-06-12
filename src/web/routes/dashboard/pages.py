"""Standalone full-page render: tradable epic list."""

import html
from datetime import date, datetime

from src.services.market_scanner import MarketInfo
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
            <li><a href="/epics/tradable" class="active">Tradable</a></li>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/simulator">Simulator</a></li>
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
