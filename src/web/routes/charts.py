"""Charts route — visualise an epic's price curve with trade markers.

Renders the persisted candle history (see :class:`CandleStore`) as an
interactive Plotly chart, overlaid with the open/close points of every position
taken on that epic. This is the tool to eyeball whether the strategy rules fired
where they should: where a position opened, where it closed, and against which
levels (win / stop).

All data comes from the database — opening these pages costs **zero** IG API
calls.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from src.models.candle import CandleRecord
from src.models.position import Position

_PARIS = ZoneInfo("Europe/Paris")

router = APIRouter()


def _nav(active: str) -> str:
    """Render the shared navigation bar with ``active`` highlighted."""

    def cls(name: str) -> str:
        return ' class="active"' if name == active else ""

    return f"""
    <nav>
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/"{cls("dashboard")}>Dashboard</a></li>
            <li><a href="/epics"{cls("epics")}>Epic List</a></li>
            <li><a href="/epics/tradable"{cls("tradable")}>Tradable</a></li>
            <li><a href="/charts"{cls("charts")}>Charts</a></li>
            <li><a href="/positions" target="_blank">Positions</a></li>
        </ul>
    </nav>"""


@router.get("/charts", response_class=HTMLResponse)
async def charts_index(request: Request) -> HTMLResponse:
    """List every epic that has stored candle history."""
    store = request.app.state.candle_store
    if store is None:
        return HTMLResponse("<p>Candle store not configured.</p>", status_code=503)

    stats = await store.epics_with_data()

    rows = ""
    for s in stats:
        first = s.first.astimezone(_PARIS).strftime("%d/%m %H:%M") if s.first else "—"
        last = s.last.astimezone(_PARIS).strftime("%d/%m %H:%M") if s.last else "—"
        rows += f"""
            <tr>
                <td class="epic-col"><a href="/charts/{html.escape(s.epic)}">{html.escape(s.epic)}</a></td>
                <td class="number">{s.count}</td>
                <td>{first}</td>
                <td>{last}</td>
            </tr>"""

    if not rows:
        rows = (
            '<tr><td colspan="4" style="text-align:center;color:#475569;padding:2rem;">'
            "No candle history yet — run Collect &amp; Analyze (or wait for the "
            "scheduled loop) to start recording.</td></tr>"
        )

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Charts</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="container">
    {_nav("charts")}
    <div class="header-bar">
        <h1>&#128200; Charts</h1>
        <div class="stat-badge">
            <span class="stat-label">Epics with data</span>
            <span class="stat-value" style="color:#4ade80;">{len(stats)}</span>
        </div>
    </div>
    <div class="section">
        <table>
            <thead>
                <tr><th>Epic</th><th>Candles</th><th>First</th><th>Last</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    <footer>Price history is recorded passively during Collect &amp; Analyze — no extra API calls. Old candles are dumped to disk and purged nightly.</footer>
</div>
</body>
</html>""")


@router.get("/charts/{epic}", response_class=HTMLResponse)
async def chart_epic(
    request: Request,
    epic: str,
    day: str | None = Query(None, description="Day to display (YYYY-MM-DD)"),
) -> HTMLResponse:
    """Render the price curve for ``epic`` on a given day with trade markers."""
    store = request.app.state.candle_store
    session_factory = request.app.state.session_factory
    if store is None:
        return HTMLResponse("<p>Candle store not configured.</p>", status_code=503)

    candles = await store.fetch(epic)
    if not candles:
        return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
            <link rel="stylesheet" href="/static/style.css"></head><body>
            <div class="container">{_nav("charts")}
            <div class="header-bar"><h1>&#128200; {html.escape(epic)}</h1></div>
            <div class="section"><p style="padding:2rem;text-align:center;color:#475569;">
            No candle history for this epic yet.</p></div></div></body></html>""")

    # Group available days (in Paris local time) for the day selector.
    available_days = sorted(
        {c.timestamp.astimezone(_PARIS).date() for c in candles}, reverse=True
    )
    selected_day = _parse_day(day) or available_days[0]

    day_candles = [
        c for c in candles if c.timestamp.astimezone(_PARIS).date() == selected_day
    ]

    # Positions taken on this epic that day (combine UTC date+time, show in Paris).
    positions: list[Position] = []
    if session_factory is not None:
        async with session_factory() as session:
            positions = list(
                (
                    await session.scalars(
                        select(Position)
                        .where(Position.epic == epic, Position.date == selected_day)
                        .order_by(Position.id)
                    )
                ).all()
            )

    chart_html = _build_chart(epic, selected_day, day_candles, positions)
    day_options = _build_day_selector(epic, available_days, selected_day)

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — {html.escape(epic)}</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="container">
    {_nav("charts")}
    <div class="header-bar">
        <h1>&#128200; {html.escape(epic)}</h1>
        <div class="stat-badge">
            <span class="stat-label">Candles</span>
            <span class="stat-value" style="color:#4ade80;">{len(day_candles)}</span>
        </div>
        <div class="stat-badge">
            <span class="stat-label">Trades</span>
            <span class="stat-value" style="color:#60a5fa;">{len(positions)}</span>
        </div>
        {day_options}
    </div>
    <div class="section">
        {chart_html}
    </div>
    <footer>Green &#9650; = position opened, red &#10005; = closed. Dashed lines = win target / stop level. Times shown in Europe/Paris.</footer>
</div>
</body>
</html>""")


@router.get("/api/candles/{epic}")
async def api_candles(
    request: Request,
    epic: str,
    limit: int = Query(2000, ge=1, le=20000),
) -> JSONResponse:
    """JSON API: raw stored candles for an epic (oldest to newest)."""
    store = request.app.state.candle_store
    if store is None:
        return JSONResponse({"error": "Candle store not configured"}, status_code=503)

    candles = await store.fetch(epic)
    candles = candles[-limit:]
    return JSONResponse(
        {
            "epic": epic,
            "count": len(candles),
            "candles": [
                {
                    "timestamp": c.timestamp.isoformat(),
                    "bid_close": c.bid_close,
                    "offer_close": c.offer_close,
                    "spread": round(c.offer_close - c.bid_close, 5),
                }
                for c in candles
            ],
        }
    )


def _parse_day(day: str | None) -> date | None:
    """Parse a ``YYYY-MM-DD`` query parameter, returning None when invalid."""
    if not day:
        return None
    try:
        return date.fromisoformat(day)
    except ValueError:
        return None


def _build_day_selector(epic: str, days: list[date], selected: date) -> str:
    """Render a day <select> that reloads the page on change."""
    options = ""
    for d in days:
        sel = " selected" if d == selected else ""
        options += (
            f'<option value="{d.isoformat()}"{sel}>{d.strftime("%d/%m/%Y")}</option>'
        )
    return f"""
        <div class="filter-wrap">
            <label style="font-size:0.75rem;color:#64748b;margin-right:0.4rem;">Day</label>
            <select onchange="location.href='/charts/{html.escape(epic)}?day=' + this.value">
                {options}
            </select>
        </div>"""


def _build_chart(
    epic: str,
    day: date,
    candles: list[CandleRecord],
    positions: list[Position],
) -> str:
    """Build the Plotly figure HTML fragment for one epic/day."""
    if not candles:
        return '<p style="padding:2rem;text-align:center;color:#475569;">No candles for this day.</p>'

    times = [c.timestamp.astimezone(_PARIS) for c in candles]
    bid = [c.bid_close for c in candles]
    offer = [c.offer_close for c in candles]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=times,
            y=bid,
            name="Bid close",
            mode="lines",
            line={"color": "#60a5fa", "width": 1.6},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=times,
            y=offer,
            name="Offer close",
            mode="lines",
            line={"color": "#475569", "width": 1, "dash": "dot"},
        )
    )

    for p in positions:
        _add_position_markers(fig, p, day)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(15,23,42,0.6)",
        margin={"l": 50, "r": 20, "t": 30, "b": 40},
        height=560,
        legend={"orientation": "h", "y": 1.08},
        xaxis={"title": "Time (Europe/Paris)"},
        yaxis={"title": "Price"},
        hovermode="x unified",
    )
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


def _add_position_markers(fig: go.Figure, p: Position, day: date) -> None:
    """Overlay open/close markers and win/stop levels for one position."""
    open_dt = (
        datetime.combine(day, p.time_open, tzinfo=UTC).astimezone(_PARIS)
        if p.time_open
        else None
    )
    close_dt = (
        datetime.combine(day, p.time_close, tzinfo=UTC).astimezone(_PARIS)
        if p.time_close
        else None
    )
    level_open = float(p.level_open) if p.level_open is not None else None
    level_close = float(p.level_close) if p.level_close is not None else None
    pnl = float(p.euro) if p.euro is not None else None

    if open_dt and level_open is not None:
        fig.add_trace(
            go.Scatter(
                x=[open_dt],
                y=[level_open],
                mode="markers",
                marker={"symbol": "triangle-up", "size": 13, "color": "#22c55e"},
                name="Open",
                showlegend=False,
                hovertemplate=(
                    f"<b>OPEN</b> #{p.id}<br>%{{x|%H:%M:%S}}<br>"
                    f"level={level_open}<extra></extra>"
                ),
            )
        )
    if close_dt and level_close is not None:
        reason = html.escape(p.reason_close or "")
        pnl_txt = f"<br>P&L={pnl}€" if pnl is not None else ""
        fig.add_trace(
            go.Scatter(
                x=[close_dt],
                y=[level_close],
                mode="markers",
                marker={"symbol": "x", "size": 12, "color": "#ef4444"},
                name="Close",
                showlegend=False,
                hovertemplate=(
                    f"<b>CLOSE</b> #{p.id}<br>%{{x|%H:%M:%S}}<br>"
                    f"level={level_close}<br>reason={reason}{pnl_txt}<extra></extra>"
                ),
            )
        )

    # Win target / stop level as faint horizontal references.
    if p.level_win is not None:
        fig.add_hline(
            y=float(p.level_win),
            line={"color": "#22c55e", "width": 1, "dash": "dash"},
            opacity=0.35,
        )
    if p.level_stop is not None:
        fig.add_hline(
            y=float(p.level_stop),
            line={"color": "#ef4444", "width": 1, "dash": "dash"},
            opacity=0.35,
        )
