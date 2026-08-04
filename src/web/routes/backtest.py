"""Backtest route — replay a strategy on archived real-market candles.

Companion to ``/simulator`` (which uses synthetic curves). This page lists the
weeks of real candles available in the on-disk archive (produced by the candle
retention dump), lets the user pick a week and a **full open/stop/close
selection**, and replays the project's real rules over that data.

The page exposes the same six decoupled selectors as ``.env`` —
``OPEN_STRATEGY``, ``STOP_STRATEGY`` and the four ``CLOSE_ZONE*`` zones — because
comparing those combinations against one another is the entire point of a
backtest. Anything left on *live* keeps the configured value.

A run always covers **every epic** of the selected week and **all** of its days:
there is no epic filter and no trade cap in the UI, so two runs of the same week
differ only by the selection under test.

The whole path reads only archive files (no DB, no IG API), so a backtest can be
run while the main process keeps recording the current week.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.backtest.backtest_archive import BacktestArchive
from src.backtest.backtester import (
    SELECTION_REGISTRIES,
    BacktestConfig,
    StrategySelection,
    backtestable_names,
    dedupe_correlated_epics,
    euro_summary,
    percentage_summary,
    run_backtest,
    trade_euro,
    trade_euro_breakeven,
    trade_return_pct,
    untestable_reason,
)
from src.backtest.contract_values import ContractTable

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


def _contract_table(request: Request) -> ContractTable:
    """Load the epic → € / point table the euro figures are priced with.

    An unset path yields an empty table (no euro figures, counts and percentages
    unaffected) — the same graceful degradation as a table that has never been
    generated.
    """
    path = getattr(request.app.state.settings, "backtest_contract_file", None)
    return ContractTable.load(path)


#: Label shown above each selector, in the order the price zones are crossed.
SELECTOR_LABELS: dict[str, str] = {
    "open_strategy": "Open strategy",
    "stop_strategy": "Stop distance",
    "close_zonestart": "Zone start (→ break-even)",
    "close_zonemarge": "Zone marge (BE → margin)",
    "close_zonesecure": "Zone secure (→ profit)",
    "close_zoneprofit": "Zone profit (beyond)",
}


class BacktestRequest(BaseModel):
    """Validated parameters for a backtest run.

    The six selectors mirror the ``.env`` names one-for-one; each is optional and
    falls back to the live configuration. ``strategy`` is the legacy alias of
    ``open_strategy`` and is still accepted.

    ``epics`` narrows the run to specific epics; the web page never sets it (a
    run always covers the whole week) but it stays available for programmatic
    callers. There is no trade cap: the whole selection is replayed.
    """

    weeks: list[str] = Field(default_factory=list, max_length=104)
    epics: list[str] = Field(default_factory=list, max_length=200)
    strategy: str | None = Field(None, pattern="^[a-z_]+$")
    open_strategy: str | None = Field(None, pattern="^[a-z_]+$")
    stop_strategy: str | None = Field(None, pattern="^[a-z_]+$")
    close_zonestart: str | None = Field(None, pattern="^[a-z_]+$")
    close_zonemarge: str | None = Field(None, pattern="^[a-z_]+$")
    close_zonesecure: str | None = Field(None, pattern="^[a-z_]+$")
    close_zoneprofit: str | None = Field(None, pattern="^[a-z_]+$")

    def selection(self) -> StrategySelection:
        """The run's selector overrides, ``None`` where the live value applies."""
        return StrategySelection(
            open_strategy=self.open_strategy or self.strategy,
            stop_strategy=self.stop_strategy,
            close_zonestart=self.close_zonestart,
            close_zonemarge=self.close_zonemarge,
            close_zonesecure=self.close_zonesecure,
            close_zoneprofit=self.close_zoneprofit,
        )


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
    settings = request.app.state.settings
    selection = body.selection()
    # Validated on the resolved names, so an untestable *live* value fails here
    # too instead of being replayed as a degraded look-alike.
    problems = selection.problems(settings)
    if problems:
        detail = "; ".join(f"{k}: {v}" for k, v in sorted(problems.items()))
        return JSONResponse(
            {"error": f"Unusable selection — {detail}"}, status_code=400
        )

    archive = _archive(request)
    table = _contract_table(request)
    # No trade cap: replay every day of the selected week(s).
    config = BacktestConfig()

    def _load_and_run():
        candles = archive.load(weeks=body.weeks or None, epics=body.epics or None)
        # Collapse correlated duplicate contracts (3× DAX, 3× FTSE, …) so the
        # same bet is not counted several times; run_backtest dedupes too, this
        # mirror is only to report what was kept/dropped to the UI.
        kept, dropped = dedupe_correlated_epics(candles)
        result = run_backtest(settings, candles, config, selection)
        candles_loaded = sum(len(c) for c in kept.values())
        return result, len(kept), dropped, candles_loaded

    result, epics_loaded, epics_dropped, candles_loaded = await asyncio.to_thread(
        _load_and_run
    )

    if candles_loaded == 0:
        return JSONResponse(
            {"error": "No archived candles match the selection."}, status_code=400
        )

    # Three lenses on the same replay, in one summary:
    #   - structural counts (trades / wins / losses / win rate) — no contract
    #     value needed, so they always cover every trade;
    #   - euros, priced per epic from the contract table (see euro_summary) —
    #     both the real result and the break-even-exit scenario;
    #   - percentage returns, kept as the instrument-agnostic fallback for the
    #     epics the contract table cannot price.
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
        **euro_summary(result.trades, table),
    }
    resolved = selection.resolve(settings)
    return JSONResponse(
        {
            "strategy": resolved["open_strategy"],
            "selection": resolved,
            "epics_loaded": epics_loaded,
            "epics_dropped": epics_dropped,
            "candles_loaded": candles_loaded,
            "summary": summary,
            "trades": [
                {
                    "epic": t.epic,
                    "direction": t.direction,
                    "day": t.day,
                    "open_time": t.open_time,
                    "close_time": t.close_time,
                    "level_open": t.level_open,
                    "level_close": t.level_close,
                    "reason": t.reason_close,
                    "return_pct": round(trade_return_pct(t), 4),
                    "euro": _rounded(trade_euro(t, table.euro_per_point(t.epic))),
                    "euro_breakeven": _rounded(
                        trade_euro_breakeven(t, table.euro_per_point(t.epic))
                    ),
                    "breakeven_time": t.time_breakeven_exit,
                    "win": t.win,
                    "stop_updates": t.stop_updates,
                }
                for t in result.trades
            ],
        }
    )


def _rounded(value: float | None) -> float | None:
    """Round a euro figure for the JSON payload, preserving ``None`` (unpriced)."""
    return None if value is None else round(value, 2)


def _selector_field(selector: str, live_value: str) -> str:
    """One ``<select>`` for a selector, pre-set to the live ``.env`` value.

    The live name is marked so a run's baseline is obvious. When the live value is
    one the offline engine cannot reproduce (``UNTESTABLE_NAMES``, e.g.
    ``smartgroup``) it is still listed — hiding it would leave the operator
    wondering why the page disagrees with ``.env`` — but as a **disabled** option,
    so the browser falls back to a valid name and the run states what it replayed.
    """
    choices = backtestable_names(selector)
    reason = untestable_reason(selector, live_value)
    options = ""
    if live_value and (reason or live_value not in choices):
        note = "not backtestable" if reason else "unknown name"
        options += (
            f'<option value="{live_value}" disabled title="{reason or ""}">'
            f"{live_value} (live — {note})</option>"
        )
    options += "".join(
        f'<option value="{name}"{" selected" if name == live_value else ""}>'
        f"{name}{' (live)' if name == live_value else ''}</option>"
        for name in choices
    )
    return f"""<label>{SELECTOR_LABELS[selector]}
            <select id="bt-{selector}" class="bt-selector"
                data-selector="{selector}">{options}</select>
        </label>"""


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request) -> HTMLResponse:
    """Render the backtest page (archive picker + full selection replay)."""
    settings = request.app.state.settings
    selector_fields = "\n        ".join(
        _selector_field(selector, getattr(settings, selector, "") or "")
        for selector in SELECTION_REGISTRIES
    )
    table = _contract_table(request)
    contract_note = (
        f"{len(table)} epic(s) priced in the contract table"
        + (f" (captured {table.generated_at[:16]})" if table.generated_at else "")
        if len(table)
        else "No € / point table yet — run <code>python -m "
        "src.scripts.dump_euro_per_point</code> to get euro figures"
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Backtest</title>
    <link rel="stylesheet" href="/static/style.css?v=13">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
    <script src="/static/tables.js?v=1"></script>
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
            <p class="bt-hint">Pick a week of archived candles — a run always
            covers <strong>every epic</strong> of that week. The archive is built by
            the candle retention dump; <strong>Snapshot DB now</strong> also copies
            the current database candles (including the last 7 days not yet purged)
            into the archive so you can backtest recent data.</p>
            <div class="bt-controls">
                <label>Week
                    <select id="bt-week" onchange="renderWeekMeta()"></select>
                </label>
            </div>
            <div id="bt-week-meta" class="bt-meta"></div>
            <div id="bt-export-meta" class="bt-meta"></div>
        </div>
    </div>

    <!-- Backtest run -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="play-circle" class="lc-icon"></i> Run backtest</span>
        </div>
        <div class="section-body">
            <p class="bt-hint">Replays the bot's real opening/closing rules
            (signal, pre-open gates, win/stop levels, per-zone stop updates) over
            <strong>every day</strong> of the selected week. Correlated duplicate
            contracts (e.g. the three DAX contracts) are collapsed to one. The six
            selectors below are the same ones <code>.env</code> holds — each starts
            on the live value, change any of them to test a combination.</p>
            <div class="bt-controls">
                {selector_fields}
            </div>
            <div class="bt-controls" style="padding-top:0;">
                <button class="nav-btn" id="bt-run-btn" onclick="runBacktest()">
                    <i data-lucide="play" class="lc-icon"></i> Run backtest
                </button>
                <button class="nav-btn" onclick="resetSelectors()">
                    <i data-lucide="rotate-ccw" class="lc-icon"></i> Back to live
                </button>
            </div>
            <div class="bt-meta">{contract_note}</div>
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
                            <th>#</th><th>Epic</th><th>Way</th><th>Open</th>
                            <th>Close</th><th>Reason</th><th>Return %</th>
                            <th>Euro</th><th>Euro @BE</th>
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
.bt-controls .nav-btn {{ min-width:11rem; justify-content:center; }}
.bt-controls .nav-btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
.bt-meta {{ font-size:0.75rem; color:var(--text-muted); padding:0.2rem 0 0.6rem;
    display:flex; align-items:center; gap:0.5rem; }}
.bt-meta code {{ font-family:var(--mono); color:var(--text); }}
/* Multi-line meta (run summary + warnings): one flex child that stacks inside. */
.bt-meta .bt-lines {{ display:flex; flex-direction:column; gap:0.25rem; }}
.bt-hint {{ font-size:0.78rem; color:var(--text-muted); margin:0.4rem 0 0; }}
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

// Format a euro figure; null means "this epic has no € / point in the table".
function eur(v, signed) {{
    if (v === null || v === undefined) return "—";
    return (signed && v > 0 ? "+" : "") + v.toFixed(2) + " €";
}}

function fmtDate(iso) {{ return iso ? iso.replace("T", " ").slice(0, 16) : "—"; }}

// Put every selector back on the live .env value the page was rendered with.
// A live value that is not backtestable is a disabled option, so fall back to
// the first selectable name instead of leaving an unusable selection in place.
function resetSelectors() {{
    document.querySelectorAll(".bt-selector").forEach(sel => {{
        const options = Array.from(sel.options);
        const target = options.find(o => o.text.endsWith("(live)"))
            || options.find(o => !o.disabled);
        if (target) sel.value = target.value;
    }});
}}

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
        return;
    }}
    weekSel.innerHTML = DATASETS.map(d =>
        `<option value="${{d.week}}">${{d.week}} — ${{d.epics.length}} epics, ` +
        `${{d.total_candles}} candles</option>`).join("");
    renderWeekMeta();
}}

// Coverage line for the selected week. Every epic is always replayed, so this
// is informational only — there is no epic picker.
function renderWeekMeta() {{
    const week = document.getElementById("bt-week").value;
    const d = DATASETS.find(x => x.week === week);
    const meta = document.getElementById("bt-week-meta");
    if (!d) {{ meta.textContent = ""; return; }}
    meta.innerHTML = `<strong>${{d.week}}</strong>: ${{fmtDate(d.first)}} → ` +
        `${{fmtDate(d.last)}}, ${{d.epics.length}} epics, ${{d.total_candles}} candles ` +
        `(all epics are backtested)`;
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

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Backtesting…';
    status.innerHTML = '<span class="spinner"></span> Running backtest…';

    const restoreButton = () => {{
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Run backtest';
        lucide.createIcons();
    }};

    // No epics / no trade cap: the whole week, every epic, every day. Every
    // selector is sent explicitly, so the run is reproducible from the payload
    // alone even if .env changes afterwards.
    const body = {{weeks: [week]}};
    document.querySelectorAll(".bt-selector").forEach(sel => {{
        body[sel.dataset.selector] = sel.value;
    }});

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
    const sel = data.selection || {{}};
    const chain = ["open_strategy", "stop_strategy", "close_zonestart",
        "close_zonemarge", "close_zonesecure", "close_zoneprofit"]
        .map(k => sel[k]).filter(Boolean).join(" › ");
    // An unpriced epic is silence in the euro totals — say so rather than letting
    // a partial figure read as the whole result.
    const unpriced = s.unpriced_trades
        ? ` — <span style="color:#fbbf24;">${{s.unpriced_trades}} trade(s) have no ` +
          `€ / point (${{s.unpriced_epics.join(", ")}}) and are excluded from the ` +
          `euro totals</span>`
        : "";
    status.innerHTML = `<div class="bt-lines"><div><strong>${{chain}}</strong> — ` +
        `${{data.epics_loaded}} epics, ${{data.candles_loaded}} candles, ` +
        `${{s.days_simulated}} days, ${{s.buy_signals}} entry signals${{dropped}}` +
        `${{unpriced}}</div></div>`;
    results.style.display = "block";

    const euroColor = s.total_euro >= 0 ? "#4ade80" : "#f87171";
    const beColor = s.total_euro_breakeven >= 0 ? "#4ade80" : "#f87171";
    const beRate = s.trades ? s.wins_breakeven / s.trades : 0;
    document.getElementById("bt-kpis").innerHTML =
        kpi("Trades", s.trades, "#e2e8f0") +
        kpi("Wins", s.wins, "#4ade80") +
        kpi("Losses", s.losses, "#f87171") +
        kpi("Win rate", (s.win_rate * 100).toFixed(1) + "%",
            s.win_rate >= 0.5 ? "#4ade80" : "#fbbf24") +
        kpi("Total euro", eur(s.total_euro, true), euroColor) +
        kpi("Total euro @BE", eur(s.total_euro_breakeven, true), beColor) +
        kpi("Wins @BE", s.wins_breakeven, "#4ade80") +
        kpi("Losses @BE", s.losses_breakeven, "#f87171") +
        kpi("Win rate @BE", (beRate * 100).toFixed(1) + "%",
            beRate >= 0.5 ? "#4ade80" : "#fbbf24");

    // Both euro curves on one axis: the real exit against the "close the moment
    // it turns green" scenario, so the cost/benefit of holding is visible.
    Plotly.newPlot("bt-equity-chart", [
        {{
            y: s.equity_euro, mode: "lines", name: "Real exit (€)",
            line: {{color: euroColor, width: 1.8}},
            fill: "tozeroy", fillcolor: "rgba(96,165,250,0.08)",
        }},
        {{
            y: s.equity_euro_breakeven, mode: "lines",
            name: "Break-even exit (€)",
            line: {{color: "#60a5fa", width: 1.4, dash: "dot"}},
        }},
    ], {{...PLOTLY_LAYOUT, height: 300,
        showlegend: true,
        legend: {{orientation: "h", y: 1.12, x: 0}},
        xaxis: {{title: "Closed trades (priced)"}},
        yaxis: {{title: "Cumulative P&L (€)"}}}},
        {{displayModeBar: false, responsive: true}});

    const fill = (id, obj) => {{
        document.getElementById(id).innerHTML = Object.entries(obj)
            .sort((a, b) => b[1] - a[1])
            .map(([k, v]) => `<tr><td>${{k}}</td><td class="number">${{v}}</td></tr>`)
            .join("") || '<tr><td colspan="2">—</td></tr>';
    }};
    fill("bt-reasons", s.close_reasons);
    fill("bt-rejections", s.rejections);

    const sign = v => (v === null || v === undefined)
        ? "#94a3b8" : (v >= 0 ? "#4ade80" : "#f87171");
    document.getElementById("bt-trades").innerHTML = data.trades.map((t, i) => {{
        const way = t.direction === "SELL"
            ? '<span style="color:#f87171;">SELL</span>'
            : '<span style="color:#4ade80;">BUY</span>';
        return `<tr><td class="number">${{i + 1}}</td>` +
            `<td>${{t.epic}}</td>` +
            `<td>${{way}}</td>` +
            `<td>${{t.open_time}} @ ${{t.level_open}}</td>` +
            `<td>${{t.close_time}} @ ${{t.level_close}}</td>` +
            `<td>${{t.reason}}</td>` +
            `<td class="number" style="color:${{sign(t.return_pct)}};">` +
            `${{pct(t.return_pct, true)}}</td>` +
            `<td class="number" style="color:${{sign(t.euro)}};">` +
            `${{eur(t.euro, true)}}</td>` +
            `<td class="number" style="color:${{sign(t.euro_breakeven)}};" ` +
            `title="${{t.breakeven_time || "never crossed break-even"}}">` +
            `${{eur(t.euro_breakeven, true)}}</td></tr>`;
    }}).join("");
    lucide.createIcons();
}}

lucide.createIcons();
loadDatasets();
</script>
</body>
</html>""")
