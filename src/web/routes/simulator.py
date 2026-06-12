"""Simulator route — test the strategy rules on fictional pseudo-random curves.

Two tools on one page:

- a curve generator preview: draw a synthetic market curve (the generation
  internals live in :mod:`src.services.curve_generator` and stay opaque here);
- a strategy simulation: replay the project's real open/close rules over many
  generated days until ~100 positions have closed, then report the win/loss
  count and a euro P&L estimate.

Everything is in-memory and synthetic — zero IG API calls, zero DB writes.
"""

from __future__ import annotations

import asyncio
import random

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src.services.curve_generator import PROFILES, generate_curve
from src.services.simulator import SimulationConfig, run_simulation

router = APIRouter()

_MAX_SEED = 2**31


def _nav() -> str:
    """Shared navigation bar with Simulator highlighted."""
    return """
    <nav>
        <span class="nav-label">Nav</span>
        <ul>
            <li><a href="/">Dashboard</a></li>
            <li><a href="/epics/tradable">Tradable</a></li>
            <li><a href="/charts">Charts</a></li>
            <li><a href="/simulator" class="active">Simulator</a></li>
        </ul>
    </nav>"""


class SimulationRequest(BaseModel):
    """Validated parameters for a simulation run."""

    profile: str = Field("random", pattern="^[a-z_]+$")
    seed: int | None = Field(None, ge=0, lt=_MAX_SEED)
    target_trades: int = Field(100, ge=1, le=1000)
    epics_per_day: int = Field(3, ge=1, le=10)
    candles_per_day: int = Field(600, ge=100, le=2000)
    base_price: float = Field(8000.0, gt=0)
    euro_per_point: float = Field(1.0, gt=0, le=1000)
    breakeven_lock: bool = False
    breakeven_buffer_mult: float = Field(1.0, ge=0, le=10)
    breakeven_margin_mult: float = Field(2.0, ge=0.5, le=20)


@router.get("/api/simulator/curve")
async def api_simulator_curve(
    profile: str = Query("random"),
    seed: int | None = Query(None, ge=0, lt=_MAX_SEED),
    candles: int = Query(600, ge=50, le=2000),
    base_price: float = Query(8000.0, gt=0),
) -> JSONResponse:
    """JSON API: one synthetic curve for the chart preview."""
    if profile not in PROFILES:
        return JSONResponse({"error": f"Unknown profile: {profile}"}, status_code=400)
    if seed is None:
        seed = random.randrange(_MAX_SEED)

    series = generate_curve(
        profile, seed=seed, num_candles=candles, base_price=base_price
    )
    return JSONResponse(
        {
            "profile": profile,
            "seed": seed,
            "timestamps": [c.timestamp.strftime("%H:%M") for c in series],
            "bid_closes": [c.bid_close for c in series],
            "offer_closes": [c.offer_close for c in series],
        }
    )


@router.post("/api/simulator/run")
async def api_simulator_run(request: Request, body: SimulationRequest) -> JSONResponse:
    """JSON API: run a full simulation and return stats + trade list.

    The run is pure CPU work (a few seconds), so it is pushed to a worker
    thread to keep the event loop (and the 1 s dashboard poll) responsive.
    """
    if body.profile not in PROFILES:
        return JSONResponse(
            {"error": f"Unknown profile: {body.profile}"}, status_code=400
        )
    seed = body.seed if body.seed is not None else random.randrange(_MAX_SEED)

    sim_config = SimulationConfig(
        target_trades=body.target_trades,
        epics_per_day=body.epics_per_day,
        candles_per_day=body.candles_per_day,
        profile=body.profile,
        seed=seed,
        base_price=body.base_price,
        euro_per_point=body.euro_per_point,
        breakeven_lock=body.breakeven_lock,
        breakeven_buffer_mult=body.breakeven_buffer_mult,
        breakeven_margin_mult=body.breakeven_margin_mult,
    )
    settings = request.app.state.settings
    result = await asyncio.to_thread(run_simulation, settings, sim_config)

    return JSONResponse(
        {
            "seed": seed,
            "summary": result.summary(),
            "trades": [
                {
                    "epic": t.epic,
                    "day": t.day,
                    "open_time": t.open_time,
                    "close_time": t.close_time,
                    "level_open": t.level_open,
                    "level_close": t.level_close,
                    "reason": t.reason_close,
                    "euro": t.euro,
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
    """A numeric input flanked by −/+ stepper buttons (see ``.stepper`` CSS)."""
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


@router.get("/simulator", response_class=HTMLResponse)
async def simulator_page(request: Request) -> HTMLResponse:
    """Render the simulator page (curve preview + strategy simulation)."""
    profile_options = "".join(
        f'<option value="{p}"{" selected" if p == "random" else ""}>{p}</option>'
        for p in PROFILES
    )
    curve_candles = _stepper(
        "Candles", "curve-candles", value="600", minimum="50", maximum="2000", step="50"
    )
    curve_base = _stepper(
        "Base price", "curve-base", value="8000", minimum="1", step="100"
    )
    sim_target = _stepper(
        "Trades target",
        "sim-target",
        value="100",
        minimum="1",
        maximum="1000",
        step="10",
    )
    sim_epics = _stepper(
        "Epics / day", "sim-epics", value="3", minimum="1", maximum="10", step="1"
    )
    sim_epp = _stepper("&euro; / point", "sim-epp", value="1", minimum="0.01", step="1")
    sim_margin = _stepper(
        "BE margin (spreads)",
        "sim-margin",
        value="2",
        minimum="0.5",
        maximum="20",
        step="0.5",
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>IG Trading Bot — Simulator</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
</head>
<body>
<div class="container">
    {_nav()}
    <div class="header-bar">
        <h1><i data-lucide="flask-conical" class="lc-icon"></i> Strategy Simulator</h1>
        <div class="stat-badge">
            <span class="stat-label">Market</span>
            <span class="stat-value" style="color:#fbbf24;">100% fictional</span>
        </div>
    </div>

    <!-- Curve generator preview -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="activity" class="lc-icon"></i> Curve Generator</span>
        </div>
        <div class="section-body">
            <div class="sim-controls">
                <label>Profile
                    <select id="curve-profile">{profile_options}</select>
                </label>
                <label>Seed <input type="number" id="curve-seed" min="0" placeholder="random"></label>
                {curve_candles}
                {curve_base}
                <button class="nav-btn" onclick="generateCurve()">
                    <i data-lucide="refresh-cw" class="lc-icon"></i> Generate
                </button>
            </div>
            <div id="curve-meta" class="sim-meta"></div>
            <div id="curve-chart" style="min-height:360px;"></div>
        </div>
    </div>

    <!-- Strategy simulation -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="play-circle" class="lc-icon"></i> Strategy Simulation</span>
        </div>
        <div class="section-body">
            <p class="sim-hint">Replays the bot's real opening/closing rules
            (signal score, pre-open gates, win/stop levels, ATR trailing stop)
            over generated days until the trade target is reached.</p>
            <div class="sim-controls">
                <label>Profile
                    <select id="sim-profile">{profile_options}</select>
                </label>
                <label>Seed <input type="number" id="sim-seed" min="0" placeholder="random"></label>
                {sim_target}
                {sim_epics}
                {sim_epp}
                <label class="sim-check">Break-even lock
                    <input type="checkbox" id="sim-breakeven" checked>
                </label>
                {sim_margin}
                <button class="nav-btn" id="sim-run-btn" onclick="runSimulation()">
                    <i data-lucide="play" class="lc-icon"></i> Run simulation
                </button>
            </div>
            <div id="sim-status" class="sim-meta"></div>
            <div id="sim-results" style="display:none;">
                <div class="kpi-bar" id="sim-kpis"></div>
                <div id="equity-chart" style="min-height:300px;margin-top:1rem;"></div>
                <div class="sim-tables">
                    <div>
                        <h3>Close reasons</h3>
                        <table><thead><tr><th>Reason</th><th>Count</th></tr></thead>
                        <tbody id="sim-reasons"></tbody></table>
                        <h3 style="margin-top:1rem;">Open rejections</h3>
                        <table><thead><tr><th>Gate</th><th>Count</th></tr></thead>
                        <tbody id="sim-rejections"></tbody></table>
                    </div>
                    <div>
                        <h3>Trades</h3>
                        <div class="sim-trades-wrap">
                        <table><thead><tr>
                            <th>#</th><th>Day</th><th>Open</th><th>Close</th>
                            <th>Reason</th><th>P&amp;L €</th>
                        </tr></thead>
                        <tbody id="sim-trades"></tbody></table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <footer>Curves are pseudo-random and fully fictional — results are a
    coherence check of the strategy rules, not a market prediction.</footer>
</div>

<style>
.sim-controls {{ display:flex; flex-wrap:wrap; gap:0.9rem; align-items:flex-end;
    padding:0.8rem 0; }}
.sim-controls label {{ display:flex; flex-direction:column; gap:0.3rem;
    font-size:0.68rem; color:var(--text-muted); text-transform:uppercase;
    letter-spacing:0.6px; font-weight:600; }}
/* Inputs/selects share the app's field look (cf. #filter-input). */
.sim-controls input, .sim-controls select {{
    background:var(--bg); border:1px solid var(--border); color:var(--text);
    padding:0.4rem 0.6rem; border-radius:var(--radius-sm); font-size:0.82rem;
    font-family:var(--mono); width:9rem; transition:border-color var(--transition);
}}
.sim-controls input:focus, .sim-controls select:focus {{
    outline:none; border-color:var(--primary); }}
.sim-controls input::placeholder {{ color:var(--text-dim); }}
.sim-controls select {{ cursor:pointer; }}
/* Numeric stepper: input flanked by −/+ buttons, drawn as one control. */
.stepper {{ display:flex; align-items:stretch; width:9rem; }}
.stepper input {{
    width:100%; text-align:center; border-radius:0 !important;
    border-left:none !important; border-right:none !important; }}
/* Hide the browser's native number spinners — our buttons replace them. */
.stepper input::-webkit-outer-spin-button,
.stepper input::-webkit-inner-spin-button {{ -webkit-appearance:none; margin:0; }}
.stepper input[type=number] {{ -moz-appearance:textfield; appearance:textfield; }}
.step-btn {{
    background:var(--surface-2); border:1px solid var(--border); color:var(--text-muted);
    width:1.9rem; flex-shrink:0; cursor:pointer; font-size:1rem; line-height:1;
    font-family:var(--mono); transition:background var(--transition),
    color var(--transition); }}
.step-btn:first-child {{ border-radius:var(--radius-sm) 0 0 var(--radius-sm); }}
.step-btn:last-child {{ border-radius:0 var(--radius-sm) var(--radius-sm) 0; }}
.step-btn:hover {{ background:var(--primary); color:#120e0c; border-color:var(--primary); }}
.step-btn:active {{ background:var(--primary-dark); }}
/* Break-even toggle: keep the checkbox inline under its label. */
.sim-check {{ justify-content:center; }}
.sim-check input {{ width:auto !important; align-self:center; cursor:pointer;
    accent-color:var(--primary); transform:scale(1.3); margin-top:0.3rem; }}
.sim-controls .nav-btn {{ min-width:11rem; justify-content:center; }}
.sim-controls .nav-btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
.sim-meta {{ font-size:0.75rem; color:var(--text-muted); padding:0.2rem 0 0.6rem;
    display:flex; align-items:center; gap:0.5rem; }}
.sim-hint {{ font-size:0.78rem; color:var(--text-muted); margin:0.4rem 0 0; }}
.sim-tables {{ display:grid; grid-template-columns:1fr 2fr; gap:1.2rem;
    margin-top:1.2rem; }}
.sim-tables h3 {{ font-size:0.8rem; color:var(--primary); margin:0 0 0.4rem;
    text-transform:uppercase; letter-spacing:0.8px; }}
.sim-trades-wrap {{ max-height:24rem; overflow-y:auto; }}
@media (max-width:900px) {{ .sim-tables {{ grid-template-columns:1fr; }} }}
/* Inline spinner shown while a simulation is running. */
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

// Format a euro amount with the symbol glued to the number (e.g. 88.01€).
function eur(v, signed) {{
    const n = (signed && v > 0 ? "+" : "") + v.toFixed(2);
    return n + "€";
}}

// −/+ stepper: nudge a numeric field by its step, clamped to min/max.
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

async function generateCurve() {{
    const profile = document.getElementById("curve-profile").value;
    const seed = document.getElementById("curve-seed").value;
    const candles = document.getElementById("curve-candles").value;
    const base = document.getElementById("curve-base").value;
    const params = new URLSearchParams({{profile, candles, base_price: base}});
    if (seed !== "") params.set("seed", seed);

    const meta = document.getElementById("curve-meta");
    meta.textContent = "Generating…";
    const resp = await fetch(`/api/simulator/curve?${{params}}`);
    const data = await resp.json();
    if (data.error) {{ meta.textContent = data.error; return; }}

    meta.innerHTML = `profile=${{data.profile}} seed=<strong>${{data.seed}}</strong> ` +
        `(reuse this seed to replay the exact same curve)`;
    Plotly.newPlot("curve-chart", [
        {{x: data.timestamps, y: data.bid_closes, name: "Bid",
          mode: "lines", line: {{color: "#60a5fa", width: 1.6}}}},
        {{x: data.timestamps, y: data.offer_closes, name: "Offer",
          mode: "lines", line: {{color: "#475569", width: 1, dash: "dot"}}}},
    ], {{...PLOTLY_LAYOUT, height: 360,
        xaxis: {{title: "Time", nticks: 12}}, yaxis: {{title: "Price"}}}},
        {{displayModeBar: false, responsive: true}});
}}

function kpi(label, value, color) {{
    // Label on top, value below — same markup as the dashboard KPI tiles.
    return `<div class="kpi-tile"><div class="kpi-label">${{label}}</div>` +
        `<div class="kpi-value" style="color:${{color}};">${{value}}</div></div>`;
}}

async function runSimulation() {{
    const btn = document.getElementById("sim-run-btn");
    const status = document.getElementById("sim-status");
    const results = document.getElementById("sim-results");

    // Clear any previous run so stale numbers/charts never linger on screen.
    results.style.display = "none";
    document.getElementById("sim-kpis").innerHTML = "";
    document.getElementById("sim-reasons").innerHTML = "";
    document.getElementById("sim-rejections").innerHTML = "";
    document.getElementById("sim-trades").innerHTML = "";
    Plotly.purge("equity-chart");

    // Loading state: spinner in the button + status line.
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Simulating…';
    status.innerHTML = '<span class="spinner"></span> Running simulation…';

    const restoreButton = () => {{
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Run simulation';
        lucide.createIcons();
    }};

    const seedVal = document.getElementById("sim-seed").value;
    const body = {{
        profile: document.getElementById("sim-profile").value,
        target_trades: parseInt(document.getElementById("sim-target").value) || 100,
        epics_per_day: parseInt(document.getElementById("sim-epics").value) || 3,
        euro_per_point: parseFloat(document.getElementById("sim-epp").value) || 1,
        breakeven_lock: document.getElementById("sim-breakeven").checked,
        breakeven_margin_mult:
            parseFloat(document.getElementById("sim-margin").value) || 2,
    }};
    if (seedVal !== "") body.seed = parseInt(seedVal);

    let data;
    try {{
        const resp = await fetch("/api/simulator/run", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(body),
        }});
        data = await resp.json();
        if (!resp.ok) throw new Error(data.error || JSON.stringify(data.detail));
    }} catch (err) {{
        status.textContent = `Simulation failed: ${{err.message}}`;
        restoreButton();
        return;
    }}
    restoreButton();

    const s = data.summary;
    status.innerHTML = `seed=<strong>${{data.seed}}</strong> — ` +
        `${{s.days_simulated}} fictional days, ${{s.buy_signals}} BUY signals ` +
        `(reuse the seed to replay)`;
    document.getElementById("sim-results").style.display = "block";

    const pnlColor = s.total_pnl >= 0 ? "#4ade80" : "#f87171";
    document.getElementById("sim-kpis").innerHTML =
        kpi("Trades", s.trades, "#e2e8f0") +
        kpi("Wins", s.wins, "#4ade80") +
        kpi("Losses", s.losses, "#f87171") +
        kpi("Win rate", (s.win_rate * 100).toFixed(1) + "%",
            s.win_rate >= 0.5 ? "#4ade80" : "#fbbf24") +
        kpi("Total P&L", eur(s.total_pnl), pnlColor) +
        kpi("Avg win", eur(s.avg_win), "#4ade80") +
        kpi("Avg loss", eur(s.avg_loss), "#f87171") +
        kpi("Max drawdown", eur(s.max_drawdown), "#fbbf24");

    Plotly.newPlot("equity-chart", [{{
        y: s.equity, mode: "lines", name: "Cumulative P&L (€)",
        line: {{color: s.total_pnl >= 0 ? "#4ade80" : "#f87171", width: 1.8}},
        fill: "tozeroy", fillcolor: "rgba(96,165,250,0.08)",
    }}], {{...PLOTLY_LAYOUT, height: 300,
        xaxis: {{title: "Closed trades"}}, yaxis: {{title: "Cumulative P&L (€)"}}}},
        {{displayModeBar: false, responsive: true}});

    const fill = (id, obj) => {{
        document.getElementById(id).innerHTML = Object.entries(obj)
            .sort((a, b) => b[1] - a[1])
            .map(([k, v]) => `<tr><td>${{k}}</td><td class="number">${{v}}</td></tr>`)
            .join("") || '<tr><td colspan="2">—</td></tr>';
    }};
    fill("sim-reasons", s.close_reasons);
    fill("sim-rejections", s.rejections);

    document.getElementById("sim-trades").innerHTML = data.trades.map((t, i) => {{
        const c = t.euro >= 0 ? "#4ade80" : "#f87171";
        return `<tr><td class="number">${{i + 1}}</td>` +
            `<td class="number">${{t.day + 1}}</td>` +
            `<td>${{t.open_time}} @ ${{t.level_open}}</td>` +
            `<td>${{t.close_time}} @ ${{t.level_close}}</td>` +
            `<td>${{t.reason}}</td>` +
            `<td class="number" style="color:${{c}};">${{eur(t.euro, true)}}</td></tr>`;
    }}).join("");
    lucide.createIcons();
}}

lucide.createIcons();
generateCurve();
</script>
</body>
</html>""")
