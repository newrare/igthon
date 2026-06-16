"""Backtest route — replay a strategy on archived real-market candles.

Companion to ``/simulator`` (which uses synthetic curves). This page lists the
weeks of real candles available in the on-disk archive (produced by the candle
retention dump), lets the user pick a week / strategy, and replays the project's
real open/close rules over that data — reporting win/loss counts and a euro P&L
estimate.

The whole path reads only archive files (no DB, no IG API), so a backtest can be
run while the main process keeps recording the current week.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.services.backtest_archive import BacktestArchive
from src.services.backtester import (
    BacktestConfig,
    dedupe_correlated_epics,
    percentage_summary,
    run_backtest,
    trade_return_pct,
)
from src.strategies import STRATEGIES

router = APIRouter()


def _nav() -> str:
    """Shared navigation bar with Backtest highlighted."""
    return """
    <nav>
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/">Dashboard</a></li>
            <li><a href="/epics/tradable">Tradable</a></li>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/simulator">Simulator</a></li>
            <li><a href="/backtest" class="active">Backtest</a></li>
        </ul>
    </nav>"""


def _archive(request: Request) -> BacktestArchive:
    """Build an archive reader rooted at the configured dump directory."""
    return BacktestArchive(request.app.state.settings.candle_dump_dir)


class BacktestRequest(BaseModel):
    """Validated parameters for a backtest run."""

    weeks: list[str] = Field(default_factory=list, max_length=104)
    epics: list[str] = Field(default_factory=list, max_length=200)
    strategy: str | None = Field(None, pattern="^[a-z_]+$")
    target_trades: int = Field(100, ge=1, le=2000)


@router.get("/api/backtest/datasets")
async def api_backtest_datasets(request: Request) -> JSONResponse:
    """JSON API: weeks of archived candles available for backtesting."""
    archive = _archive(request)
    datasets = await asyncio.to_thread(archive.datasets)
    return JSONResponse(
        {
            "weeks": [
                {
                    "week": d.week,
                    "total_candles": d.total_candles,
                    "first": d.first.isoformat(),
                    "last": d.last.isoformat(),
                    "epics": [
                        {
                            "epic": e.epic,
                            "count": e.count,
                            "first": e.first.isoformat(),
                            "last": e.last.isoformat(),
                        }
                        for e in d.epics
                    ],
                }
                for d in datasets
            ]
        }
    )


@router.post("/api/backtest/export")
async def api_backtest_export(request: Request) -> JSONResponse:
    """JSON API: snapshot the live candle table into the archive (no deletion).

    Lets a backtest use recent data that has not yet aged past the retention
    window — the candles stay in the database, they are merely copied to the
    per-week archive files the backtester reads.
    """
    store = getattr(request.app.state, "candle_store", None)
    if store is None:
        return JSONResponse(
            {"error": "Candle store is not available in this process."},
            status_code=503,
        )
    written, paths = await store.export_to_archive()
    return JSONResponse({"rows_written": written, "files": [p.name for p in paths]})


@router.post("/api/backtest/run")
async def api_backtest_run(request: Request, body: BacktestRequest) -> JSONResponse:
    """JSON API: run a backtest over the selected archive data.

    The load + replay is pure CPU/IO work pushed to a worker thread so the event
    loop (and the 1 s dashboard poll) stays responsive.
    """
    if body.strategy is not None and body.strategy not in STRATEGIES:
        return JSONResponse(
            {"error": f"Unknown strategy: {body.strategy}"}, status_code=400
        )

    settings = request.app.state.settings
    strategy_name = body.strategy or settings.strategy_name
    archive = _archive(request)
    config = BacktestConfig(target_trades=body.target_trades)

    def _load_and_run():
        candles = archive.load(weeks=body.weeks or None, epics=body.epics or None)
        # Collapse correlated duplicate contracts (3× DAX, 3× FTSE, …) so the
        # same bet is not counted several times; run_backtest dedupes too, this
        # mirror is only to report what was kept/dropped to the UI.
        kept, dropped = dedupe_correlated_epics(candles)
        result = run_backtest(settings, candles, config, strategy_name)
        candles_loaded = sum(len(c) for c in kept.values())
        return result, len(kept), dropped, candles_loaded

    result, epics_loaded, epics_dropped, candles_loaded = await asyncio.to_thread(
        _load_and_run
    )

    if candles_loaded == 0:
        return JSONResponse(
            {"error": "No archived candles match the selection."}, status_code=400
        )

    # Report P&L as percentage return computed from the real fill prices —
    # comparable across instruments, no fabricated euro-per-point. Keep only the
    # contract-agnostic structural stats from the euro summary (counts, win rate,
    # gate/close reason breakdowns) and merge in the percentage magnitudes.
    base = result.summary()
    summary = {
        "trades": base["trades"],
        "wins": base["wins"],
        "losses": base["losses"],
        "win_rate": base["win_rate"],
        "days_simulated": base["days_simulated"],
        "buy_signals": base["buy_signals"],
        "rejections": base["rejections"],
        "close_reasons": base["close_reasons"],
        **percentage_summary(result.trades),
    }
    return JSONResponse(
        {
            "strategy": strategy_name,
            "epics_loaded": epics_loaded,
            "epics_dropped": epics_dropped,
            "candles_loaded": candles_loaded,
            "summary": summary,
            "trades": [
                {
                    "epic": t.epic,
                    "day": t.day,
                    "open_time": t.open_time,
                    "close_time": t.close_time,
                    "level_open": t.level_open,
                    "level_close": t.level_close,
                    "reason": t.reason_close,
                    "return_pct": round(trade_return_pct(t), 4),
                    "win": t.win,
                    "stop_updates": t.stop_updates,
                }
                for t in result.trades
            ],
        }
    )


def _stepper(
    label: str,
    field_id: str,
    *,
    value: str = "",
    minimum: str = "",
    maximum: str = "",
    step: str = "1",
) -> str:
    """A numeric input flanked by −/+ stepper buttons (shares ``.stepper`` CSS)."""
    attrs = f'id="{field_id}" step="{step}"'
    if value:
        attrs += f' value="{value}"'
    if minimum != "":
        attrs += f' min="{minimum}"'
    if maximum != "":
        attrs += f' max="{maximum}"'
    return f"""<label>{label}
        <div class="stepper">
            <button type="button" class="step-btn" tabindex="-1"
                onclick="stepField('{field_id}', -1)">&minus;</button>
            <input type="number" {attrs}>
            <button type="button" class="step-btn" tabindex="-1"
                onclick="stepField('{field_id}', 1)">+</button>
        </div>
    </label>"""


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request) -> HTMLResponse:
    """Render the backtest page (archive picker + strategy replay)."""
    live_strategy = request.app.state.settings.strategy_name
    strategy_options = "".join(
        f'<option value="{name}"{" selected" if name == live_strategy else ""}>'
        f"{name}{' (live)' if name == live_strategy else ''}</option>"
        for name in sorted(STRATEGIES)
    )
    bt_target = _stepper(
        "Trades target",
        "bt-target",
        value="100",
        minimum="1",
        maximum="2000",
        step="10",
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Backtest</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
<div class="container">
    {_nav()}
    <div class="header-bar">
        <h1><i data-lucide="history" class="lc-icon"></i> Strategy Backtest</h1>
        <div class="stat-badge">
            <span class="stat-label">Data</span>
            <span class="stat-value" style="color:#4ade80;">archived real market</span>
        </div>
    </div>

    <!-- Archive picker -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="database" class="lc-icon"></i> Archived data</span>
            <div style="display:flex; gap:0.5rem;">
                <button class="nav-btn" id="bt-export-btn" onclick="exportData()">
                    <i data-lucide="download" class="lc-icon"></i> Snapshot DB now
                </button>
                <button class="nav-btn" onclick="loadDatasets()">
                    <i data-lucide="refresh-cw" class="lc-icon"></i> Reload
                </button>
            </div>
        </div>
        <div class="section-body">
            <p class="bt-hint">Pick a week of archived candles, then (optionally)
            narrow it to specific epics — leave them all unchecked (or use
            <em>All epics</em>) to backtest the whole week. The archive is built by
            the candle retention dump; <strong>Snapshot DB now</strong> also copies
            the current database candles (including the last 7 days not yet purged)
            into the archive so you can backtest recent data.</p>
            <div class="bt-controls">
                <label>Week
                    <select id="bt-week" onchange="renderEpics()"></select>
                </label>
            </div>
            <div id="bt-week-meta" class="bt-meta"></div>
            <div id="bt-export-meta" class="bt-meta"></div>
            <div id="bt-epics" class="bt-epics"></div>
        </div>
    </div>

    <!-- Backtest run -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="play-circle" class="lc-icon"></i> Run backtest</span>
        </div>
        <div class="section-body">
            <p class="bt-hint">Replays the bot's real opening/closing rules
            (signal, pre-open gates, win/stop levels, ATR trailing stop) over the
            selected week until the trade target is reached. Correlated duplicate
            contracts (e.g. the three DAX contracts) are collapsed to one.</p>
            <div class="bt-controls">
                <label>Strategy
                    <select id="bt-strategy">{strategy_options}</select>
                </label>
                {bt_target}
                <button class="nav-btn" id="bt-run-btn" onclick="runBacktest()">
                    <i data-lucide="play" class="lc-icon"></i> Run backtest
                </button>
            </div>
            <div id="bt-status" class="bt-meta"></div>
            <div id="bt-results" style="display:none;">
                <div class="kpi-bar" id="bt-kpis"></div>
                <div id="bt-equity-chart" style="min-height:300px;margin-top:1rem;"></div>
                <div class="bt-tables">
                    <div>
                        <h3>Close reasons</h3>
                        <table><thead><tr><th>Reason</th><th>Count</th></tr></thead>
                        <tbody id="bt-reasons"></tbody></table>
                        <h3 style="margin-top:1rem;">Open rejections</h3>
                        <table><thead><tr><th>Gate</th><th>Count</th></tr></thead>
                        <tbody id="bt-rejections"></tbody></table>
                    </div>
                    <div>
                        <h3>Trades</h3>
                        <div class="bt-trades-wrap">
                        <table><thead><tr>
                            <th>#</th><th>Epic</th><th>Open</th><th>Close</th>
                            <th>Reason</th><th>Return %</th>
                        </tr></thead>
                        <tbody id="bt-trades"></tbody></table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <footer>Backtests replay archived real candles through the live open/close
    rules — a coherence check on past data, not a guarantee of future results.</footer>
</div>

<style>
.bt-controls {{ display:flex; flex-wrap:wrap; gap:0.9rem; align-items:flex-end;
    padding:0.8rem 0; }}
.bt-controls label {{ display:flex; flex-direction:column; gap:0.3rem;
    font-size:0.68rem; color:var(--text-muted); text-transform:uppercase;
    letter-spacing:0.6px; font-weight:600; }}
.bt-controls input, .bt-controls select {{
    background:var(--bg); border:1px solid var(--border); color:var(--text);
    padding:0.4rem 0.6rem; border-radius:var(--radius-sm); font-size:0.82rem;
    font-family:var(--mono); width:13rem; transition:border-color var(--transition);
}}
.bt-controls input:focus, .bt-controls select:focus {{
    outline:none; border-color:var(--primary); }}
.bt-controls select {{ cursor:pointer; }}
.stepper {{ display:flex; align-items:stretch; width:9rem; }}
.stepper input {{ width:100%; text-align:center; border-radius:0 !important;
    border-left:none !important; border-right:none !important; }}
.stepper input::-webkit-outer-spin-button,
.stepper input::-webkit-inner-spin-button {{ -webkit-appearance:none; margin:0; }}
.stepper input[type=number] {{ -moz-appearance:textfield; appearance:textfield; }}
.step-btn {{ background:var(--surface-2); border:1px solid var(--border);
    color:var(--text-muted); width:1.9rem; flex-shrink:0; cursor:pointer;
    font-size:1rem; line-height:1; font-family:var(--mono);
    transition:background var(--transition), color var(--transition); }}
.step-btn:first-child {{ border-radius:var(--radius-sm) 0 0 var(--radius-sm); }}
.step-btn:last-child {{ border-radius:0 var(--radius-sm) var(--radius-sm) 0; }}
.step-btn:hover {{ background:var(--primary); color:#120e0c; border-color:var(--primary); }}
.bt-controls .nav-btn {{ min-width:11rem; justify-content:center; }}
.bt-controls .nav-btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
.bt-meta {{ font-size:0.75rem; color:var(--text-muted); padding:0.2rem 0 0.6rem;
    display:flex; align-items:center; gap:0.5rem; }}
.bt-hint {{ font-size:0.78rem; color:var(--text-muted); margin:0.4rem 0 0; }}
.bt-epics {{ display:flex; flex-wrap:wrap; gap:0.5rem; margin-top:0.4rem; }}
.bt-epics label {{ display:flex; align-items:center; gap:0.4rem; font-size:0.72rem;
    font-family:var(--mono); color:var(--text-muted); background:var(--surface-2);
    border:1px solid var(--border); border-radius:var(--radius-sm);
    padding:0.25rem 0.55rem; cursor:pointer; }}
.bt-epics input {{ accent-color:var(--primary); cursor:pointer; }}
.bt-tables {{ display:grid; grid-template-columns:1fr 2fr; gap:1.2rem;
    margin-top:1.2rem; }}
.bt-tables h3 {{ font-size:0.8rem; color:var(--primary); margin:0 0 0.4rem;
    text-transform:uppercase; letter-spacing:0.8px; }}
.bt-trades-wrap {{ max-height:24rem; overflow-y:auto; }}
@media (max-width:900px) {{ .bt-tables {{ grid-template-columns:1fr; }} }}
.spinner {{ width:14px; height:14px; border:2px solid var(--border);
    border-top-color:var(--primary); border-radius:50%;
    display:inline-block; animation:spin 0.7s linear infinite;
    vertical-align:-2px; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
</style>

<script>
const PLOTLY_LAYOUT = {{
    template: "plotly_dark",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(15,23,42,0.6)",
    margin: {{l: 55, r: 20, t: 20, b: 40}},
    font: {{color: "#94a3b8"}},
}};

let DATASETS = [];

// Format a percentage return with the sign glued on (e.g. +0.34%, -0.07%).
function pct(v, signed) {{
    const n = (signed && v > 0 ? "+" : "") + v.toFixed(3);
    return n + "%";
}}

function stepField(id, dir) {{
    const el = document.getElementById(id);
    const step = parseFloat(el.step) || 1;
    const min = el.min !== "" ? parseFloat(el.min) : -Infinity;
    const max = el.max !== "" ? parseFloat(el.max) : Infinity;
    let v = parseFloat(el.value);
    if (isNaN(v)) v = min !== -Infinity ? min : 0;
    v = Math.min(max, Math.max(min, v + dir * step));
    el.value = Number.isInteger(step) ? v : parseFloat(v.toFixed(2));
}}

function fmtDate(iso) {{ return iso ? iso.replace("T", " ").slice(0, 16) : "—"; }}

async function exportData() {{
    const btn = document.getElementById("bt-export-btn");
    const meta = document.getElementById("bt-export-meta");
    btn.disabled = true;
    meta.innerHTML = '<span class="spinner"></span> Snapshotting database…';
    try {{
        const resp = await fetch("/api/backtest/export", {{method: "POST"}});
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "export failed");
        meta.textContent = data.rows_written > 0
            ? `Snapshot done: ${{data.rows_written}} new candles archived ` +
              `(${{data.files.join(", ")}}).`
            : "Snapshot done: archive already up to date with the database.";
    }} catch (err) {{
        meta.textContent = "Snapshot failed: " + err.message;
        btn.disabled = false;
        return;
    }}
    btn.disabled = false;
    await loadDatasets();  // refresh the week list with the freshly-exported data
}}

async function loadDatasets() {{
    const weekSel = document.getElementById("bt-week");
    const meta = document.getElementById("bt-week-meta");
    meta.innerHTML = '<span class="spinner"></span> Loading archive…';
    let data;
    try {{
        const resp = await fetch("/api/backtest/datasets");
        data = await resp.json();
    }} catch (err) {{
        meta.textContent = "Failed to load archive: " + err.message;
        return;
    }}
    DATASETS = data.weeks || [];
    if (!DATASETS.length) {{
        weekSel.innerHTML = "";
        meta.textContent = "No archived weeks yet — the retention dump has not " +
            "produced any candle files.";
        document.getElementById("bt-epics").innerHTML = "";
        return;
    }}
    weekSel.innerHTML = DATASETS.map(d =>
        `<option value="${{d.week}}">${{d.week}} — ${{d.epics.length}} epics, ` +
        `${{d.total_candles}} candles</option>`).join("");
    renderEpics();
}}

function renderEpics() {{
    const week = document.getElementById("bt-week").value;
    const d = DATASETS.find(x => x.week === week);
    const meta = document.getElementById("bt-week-meta");
    const box = document.getElementById("bt-epics");
    if (!d) {{ meta.textContent = ""; box.innerHTML = ""; return; }}
    meta.innerHTML = `<strong>${{d.week}}</strong>: ${{fmtDate(d.first)}} → ` +
        `${{fmtDate(d.last)}}, ${{d.epics.length}} epics, ${{d.total_candles}} candles ` +
        `(leave epics unchecked to backtest them all)`;
    const allToggle =
        `<label style="border-color:var(--primary);color:var(--text);">` +
        `<input type="checkbox" id="bt-epic-all" onchange="toggleAllEpics(this)"> ` +
        `<strong>All epics</strong></label>`;
    box.innerHTML = allToggle + d.epics.map(e =>
        `<label><input type="checkbox" class="bt-epic" value="${{e.epic}}"> ` +
        `${{e.epic}} <span style="color:var(--text-dim);">(${{e.count}})</span></label>`
    ).join("");
}}

function toggleAllEpics(master) {{
    document.querySelectorAll(".bt-epic").forEach(el => {{ el.checked = master.checked; }});
}}

function kpi(label, value, color) {{
    return `<div class="kpi-tile"><div class="kpi-label">${{label}}</div>` +
        `<div class="kpi-value" style="color:${{color}};">${{value}}</div></div>`;
}}

async function runBacktest() {{
    const btn = document.getElementById("bt-run-btn");
    const status = document.getElementById("bt-status");
    const results = document.getElementById("bt-results");

    results.style.display = "none";
    document.getElementById("bt-kpis").innerHTML = "";
    document.getElementById("bt-reasons").innerHTML = "";
    document.getElementById("bt-rejections").innerHTML = "";
    document.getElementById("bt-trades").innerHTML = "";
    Plotly.purge("bt-equity-chart");

    const week = document.getElementById("bt-week").value;
    if (!week) {{ status.textContent = "No archived week selected."; return; }}
    const epics = Array.from(document.querySelectorAll(".bt-epic:checked"))
        .map(el => el.value);

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Backtesting…';
    status.innerHTML = '<span class="spinner"></span> Running backtest…';

    const restoreButton = () => {{
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Run backtest';
        lucide.createIcons();
    }};

    const body = {{
        weeks: [week],
        epics: epics,
        strategy: document.getElementById("bt-strategy").value,
        target_trades: parseInt(document.getElementById("bt-target").value) || 100,
    }};

    let data;
    try {{
        const resp = await fetch("/api/backtest/run", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(body),
        }});
        data = await resp.json();
        if (!resp.ok) throw new Error(data.error || JSON.stringify(data.detail));
    }} catch (err) {{
        status.textContent = `Backtest failed: ${{err.message}}`;
        restoreButton();
        return;
    }}
    restoreButton();

    const s = data.summary;
    const dropped = (data.epics_dropped && data.epics_dropped.length)
        ? ` — deduped ${{data.epics_dropped.length}} correlated contract(s)` : "";
    status.innerHTML = `strategy=<strong>${{data.strategy}}</strong> — ` +
        `${{data.epics_loaded}} epics, ${{data.candles_loaded}} candles, ` +
        `${{s.days_simulated}} days, ${{s.buy_signals}} BUY signals${{dropped}}`;
    results.style.display = "block";

    const pnlColor = s.total_return_pct >= 0 ? "#4ade80" : "#f87171";
    document.getElementById("bt-kpis").innerHTML =
        kpi("Trades", s.trades, "#e2e8f0") +
        kpi("Wins", s.wins, "#4ade80") +
        kpi("Losses", s.losses, "#f87171") +
        kpi("Win rate", (s.win_rate * 100).toFixed(1) + "%",
            s.win_rate >= 0.5 ? "#4ade80" : "#fbbf24") +
        kpi("Total return", pct(s.total_return_pct, true), pnlColor) +
        kpi("Avg win", pct(s.avg_win_pct, true), "#4ade80") +
        kpi("Avg loss", pct(s.avg_loss_pct, true), "#f87171") +
        kpi("Max drawdown", pct(s.max_drawdown_pct), "#fbbf24");

    Plotly.newPlot("bt-equity-chart", [{{
        y: s.equity_pct, mode: "lines", name: "Cumulative return (%)",
        line: {{color: s.total_return_pct >= 0 ? "#4ade80" : "#f87171", width: 1.8}},
        fill: "tozeroy", fillcolor: "rgba(96,165,250,0.08)",
    }}], {{...PLOTLY_LAYOUT, height: 300,
        xaxis: {{title: "Closed trades"}},
        yaxis: {{title: "Cumulative return (%)"}}}},
        {{displayModeBar: false, responsive: true}});

    const fill = (id, obj) => {{
        document.getElementById(id).innerHTML = Object.entries(obj)
            .sort((a, b) => b[1] - a[1])
            .map(([k, v]) => `<tr><td>${{k}}</td><td class="number">${{v}}</td></tr>`)
            .join("") || '<tr><td colspan="2">—</td></tr>';
    }};
    fill("bt-reasons", s.close_reasons);
    fill("bt-rejections", s.rejections);

    document.getElementById("bt-trades").innerHTML = data.trades.map((t, i) => {{
        const c = t.return_pct >= 0 ? "#4ade80" : "#f87171";
        return `<tr><td class="number">${{i + 1}}</td>` +
            `<td>${{t.epic}}</td>` +
            `<td>${{t.open_time}} @ ${{t.level_open}}</td>` +
            `<td>${{t.close_time}} @ ${{t.level_close}}</td>` +
            `<td>${{t.reason}}</td>` +
            `<td class="number" style="color:${{c}};">${{pct(t.return_pct, true)}}</td></tr>`;
    }}).join("");
    lucide.createIcons();
}}

lucide.createIcons();
loadDatasets();
</script>
</body>
</html>""")
