"""Simulator route — test the strategy rules on fictional pseudo-random curves.

Two tools on one page:

- a curve generator preview: draw a synthetic market curve (the generation
  internals live in :mod:`src.backtest.curve_generator` and stay opaque here);
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

from src.backtest.curve_generator import PROFILES, generate_curve
from src.backtest.simulator import (
    SimulationConfig,
    run_close_visual,
    run_open_visual,
    run_simulation,
)
from src.entry import ENTRY_STRATEGIES
from src.exit import CloseZoneProfit

# The exit is a single composer profile; its behaviour is set by the three
# per-zone selectors (CLOSE_ZONESTART / CLOSE_ZONEMARGE / CLOSE_ZONEPROFIT) read
# from settings. The simulator's "close profile" selector is thus a single fixed
# entry — the zone selection is not overridable per sim run.
_CLOSE_PROFILE_NAMES = frozenset({CloseZoneProfit.name})

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
            <li><a href="/backtest">Backtest</a></li>
        </ul>
    </nav>"""


class SimulationRequest(BaseModel):
    """Validated parameters for a simulation run."""

    profile: str = Field("random", pattern="^[a-z_]+$")
    strategy: str | None = Field(None, pattern="^[a-z_]+$")
    close_profile: str | None = Field(None, pattern="^[a-z_]+$")
    seed: int | None = Field(None, ge=0, lt=_MAX_SEED)
    target_trades: int = Field(100, ge=1, le=1000)
    epics_per_day: int = Field(3, ge=1, le=40)
    candles_per_day: int = Field(600, ge=100, le=2000)
    base_price: float = Field(8000.0, gt=0)
    euro_per_point: float = Field(1.0, gt=0, le=1000)
    # Spread malus charged at each open, as a % of the entry bid (percentage lens).
    spread_malus_pct: float = Field(0.0, ge=0, le=10)


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


@router.get("/api/simulator/close-profile")
async def api_simulator_close_profile(
    request: Request,
    curve_profile: str = Query("random"),
    close_profile: str | None = Query(None),
    seed: int | None = Query(None, ge=0, lt=_MAX_SEED),
    candles: int = Query(600, ge=100, le=2000),
    base_price: float = Query(8000.0, gt=0),
    euro_per_point: float = Query(1.0, gt=0, le=1000),
    open_index: int | None = Query(None, ge=0),
) -> JSONResponse:
    """JSON API: one open→close cycle so the close profile can be eyeballed.

    Opens a single BUY at a random (or pinned) moment on a synthetic curve and
    walks the chosen close profile forward tick by tick, returning the price
    curve, the entry, the trailing-stop track and the exit.
    """
    if curve_profile not in PROFILES:
        return JSONResponse(
            {"error": f"Unknown profile: {curve_profile}"}, status_code=400
        )
    if close_profile is not None and close_profile not in _CLOSE_PROFILE_NAMES:
        return JSONResponse(
            {"error": f"Unknown close profile: {close_profile}"}, status_code=400
        )
    settings = request.app.state.settings
    result = await asyncio.to_thread(
        run_close_visual,
        settings,
        curve_profile=curve_profile,
        close_profile_name=close_profile,
        seed=seed,
        num_candles=candles,
        base_price=base_price,
        euro_per_point=euro_per_point,
        open_index=open_index,
    )
    return JSONResponse(result)


@router.get("/api/simulator/open-strategy")
async def api_simulator_open_strategy(
    request: Request,
    curve_profile: str = Query("random"),
    strategy: str | None = Query(None),
    seed: int | None = Query(None, ge=0, lt=_MAX_SEED),
    candles: int = Query(600, ge=100, le=2000),
    base_price: float = Query(8000.0, gt=0),
) -> JSONResponse:
    """JSON API: walk the entry strategy until its first BUY so the open can be
    eyeballed.

    Replays the chosen entry strategy tick by tick on a synthetic curve and
    stops at the first BUY signal. The returned curve is truncated at the open
    tick — the future is withheld so judging *whether the trigger fires
    correctly* stays free of hindsight bias.
    """
    if curve_profile not in PROFILES:
        return JSONResponse(
            {"error": f"Unknown profile: {curve_profile}"}, status_code=400
        )
    if strategy is not None and strategy not in ENTRY_STRATEGIES:
        return JSONResponse({"error": f"Unknown strategy: {strategy}"}, status_code=400)
    settings = request.app.state.settings
    result = await asyncio.to_thread(
        run_open_visual,
        settings,
        curve_profile=curve_profile,
        strategy_name=strategy,
        seed=seed,
        num_candles=candles,
        base_price=base_price,
    )
    return JSONResponse(result)


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
    if body.strategy is not None and body.strategy not in ENTRY_STRATEGIES:
        return JSONResponse(
            {"error": f"Unknown strategy: {body.strategy}"}, status_code=400
        )
    if (
        body.close_profile is not None
        and body.close_profile not in _CLOSE_PROFILE_NAMES
    ):
        return JSONResponse(
            {"error": f"Unknown close profile: {body.close_profile}"}, status_code=400
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
        spread_malus_pct=body.spread_malus_pct,
    )
    settings = request.app.state.settings
    strategy_name = body.strategy or settings.open_strategy
    close_profile_name = body.close_profile or CloseZoneProfit.name
    result = await asyncio.to_thread(
        run_simulation, settings, sim_config, strategy_name, close_profile_name
    )

    return JSONResponse(
        {
            "seed": seed,
            "strategy": strategy_name,
            "close_profile": close_profile_name,
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
    # Entry-strategy dropdown defaults to the live ENTRY_STRATEGY_NAME so the
    # simulation replays exactly what the bot would do; other entries compare.
    live_strategy = request.app.state.settings.open_strategy
    strategy_options = "".join(
        f'<option value="{name}"{" selected" if name == live_strategy else ""}>'
        f"{name}{' (live)' if name == live_strategy else ''}</option>"
        for name in sorted(ENTRY_STRATEGIES)
    )
    # Cross-epic rankers (open_ranking, …) select the best of many markets,
    # so the UI bumps "Epics / day" to the live pool size (~40) when one is chosen.
    ranker_names = [
        name
        for name, cls in ENTRY_STRATEGIES.items()
        if getattr(cls, "cross_epic_selection", False)
    ]
    ranker_json = "[" + ",".join(f'"{n}"' for n in ranker_names) + "]"
    # Close-profile dropdown: the single composer profile (its per-zone behaviour
    # is set in .env and not overridable per sim run).
    live_close = CloseZoneProfit.name
    close_options = "".join(
        f'<option value="{name}"{" selected" if name == live_close else ""}>'
        f"{name}{' (live)' if name == live_close else ''}</option>"
        for name in sorted(_CLOSE_PROFILE_NAMES)
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
        "Epics / day", "sim-epics", value="3", minimum="1", maximum="40", step="1"
    )
    sim_epp = _stepper("&euro; / point", "sim-epp", value="1", minimum="0.01", step="1")
    sim_malus = _stepper(
        "Spread malus %",
        "sim-malus",
        value="0.02",
        minimum="0",
        maximum="10",
        step="0.01",
    )
    op_base = _stepper("Base price", "op-base", value="8000", minimum="1", step="100")
    cp_base = _stepper("Base price", "cp-base", value="8000", minimum="1", step="100")
    cp_epp = _stepper("&euro; / point", "cp-epp", value="1", minimum="0.01", step="1")
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

    <!-- Entry-strategy visual simulation -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="target" class="lc-icon"></i> Entry Strategy — Open Trigger Test</span>
        </div>
        <div class="section-body">
            <p class="sim-hint">Walks the chosen entry strategy forward tick by
            tick on a synthetic curve and stops at the <strong>first BUY
            signal</strong> — the moment the bot would open. To keep your read on
            <em>whether the trigger fires at the right spot</em> honest, the curve
            is cut at the open: the future is hidden, so the outcome can't bias
            your judgement.</p>
            <div class="sim-controls">
                <label>Strategy
                    <select id="op-strategy">{strategy_options}</select>
                </label>
                <label>Curve profile
                    <select id="op-profile">{profile_options}</select>
                </label>
                <label>Seed <input type="number" id="op-seed" min="0" placeholder="random"></label>
                {op_base}
                <button class="nav-btn" id="op-run-btn" onclick="runOpenStrategy()">
                    <i data-lucide="dice-5" class="lc-icon"></i> New curve
                </button>
            </div>
            <div id="op-meta" class="sim-meta"></div>
            <div class="kpi-bar" id="op-kpis"></div>
            <div id="op-chart" style="min-height:420px;"></div>
        </div>
    </div>

    <!-- Close-profile visual simulation -->
    <div class="section">
        <div class="section-header">
            <span class="section-title"><i data-lucide="shield" class="lc-icon"></i> Close Profile — Visual Test</span>
        </div>
        <div class="section-body">
            <p class="sim-hint">Opens one BUY at a random moment on a synthetic
            curve and walks the chosen close profile forward tick by tick. Watch
            the protective stop (orange) trail the price with a safety gap and
            ratchet up as the curve climbs — and where it finally closes.</p>
            <div class="sim-controls">
                <label>Curve profile
                    <select id="cp-profile">{profile_options}</select>
                </label>
                <label>Close profile
                    <select id="cp-close">{close_options}</select>
                </label>
                <label>Seed <input type="number" id="cp-seed" min="0" placeholder="random"></label>
                {cp_base}
                {cp_epp}
                <button class="nav-btn" id="cp-run-btn" onclick="runCloseProfile()">
                    <i data-lucide="dice-5" class="lc-icon"></i> New trade
                </button>
            </div>
            <div id="cp-meta" class="sim-meta"></div>
            <div class="kpi-bar" id="cp-kpis"></div>
            <div id="cp-chart" style="min-height:420px;"></div>
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
            over generated days until the trade target is reached. The
            <strong>% lens</strong> charges the spread malus at every open and
            tallies win%/loss% in fictional points (bid&rarr;bid, % of entry).</p>
            <div class="sim-controls">
                <label>Entry strategy
                    <select id="sim-strategy" onchange="onSimStrategyChange()">{strategy_options}</select>
                </label>
                <label>Close profile
                    <select id="sim-close">{close_options}</select>
                </label>
                <label>Profile
                    <select id="sim-profile">{profile_options}</select>
                </label>
                <label>Seed <input type="number" id="sim-seed" min="0" placeholder="random"></label>
                {sim_target}
                {sim_epics}
                {sim_malus}
                {sim_epp}
                <button class="nav-btn" id="sim-run-btn" onclick="runSimulation()">
                    <i data-lucide="play" class="lc-icon"></i> Run simulation
                </button>
            </div>
            <p class="sim-hint" id="sim-ranker-hint" style="display:none;">
            <strong>Ranker mode:</strong> scores all <strong>Epics / day</strong>
            markets each tick, holds one rolling position (best of the pool), and
            re-ranks on close — skipping epics already used that day, exactly like
            the live scheduler.</p>
            <div id="sim-status" class="sim-meta"></div>
            <div id="sim-results" style="display:none;">
                <h3 class="sim-block-title">% lens — fictional points (spread malus applied)</h3>
                <div class="kpi-bar" id="sim-kpis-pct"></div>
                <div id="equity-chart-pct" style="min-height:300px;margin-top:1rem;"></div>
                <h3 class="sim-block-title">&euro; lens — engine P&amp;L (offer fill, curve spread)</h3>
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
                            <th>Reason</th><th>Net %</th><th>P&amp;L €</th>
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
.sim-controls .nav-btn {{ min-width:11rem; justify-content:center; }}
.sim-controls .nav-btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
.sim-meta {{ font-size:0.75rem; color:var(--text-muted); padding:0.2rem 0 0.6rem;
    display:flex; align-items:center; gap:0.5rem; }}
.sim-hint {{ font-size:0.78rem; color:var(--text-muted); margin:0.4rem 0 0; }}
.sim-tables {{ display:grid; grid-template-columns:1fr 2fr; gap:1.2rem;
    margin-top:1.2rem; }}
.sim-tables h3 {{ font-size:0.8rem; color:var(--primary); margin:0 0 0.4rem;
    text-transform:uppercase; letter-spacing:0.8px; }}
.sim-block-title {{ font-size:0.78rem; color:var(--primary); margin:1.2rem 0 0.2rem;
    text-transform:uppercase; letter-spacing:0.8px; font-weight:600; }}
.sim-block-title:first-child {{ margin-top:0; }}
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

// Cross-epic rankers pick the best of ~40 streamed markets, so the aggregate
// simulation must feed that whole pool — selecting one bumps Epics/day to 40.
const RANKER_STRATEGIES = new Set({ranker_json});

function onSimStrategyChange() {{
    const strat = document.getElementById("sim-strategy").value;
    const epics = document.getElementById("sim-epics");
    const hint = document.getElementById("sim-ranker-hint");
    if (RANKER_STRATEGIES.has(strat)) {{
        epics.value = 40;  // live pool size — score all, hold one rolling position
        hint.style.display = "";
    }} else {{
        hint.style.display = "none";
    }}
}}

// Format a euro amount with the symbol glued to the number (e.g. 88.01€).
function eur(v, signed) {{
    const n = (signed && v > 0 ? "+" : "") + v.toFixed(2);
    return n + "€";
}}

// Format a percentage (fictional points), e.g. +0.34% / -0.12%.
function pct(v, signed) {{
    const n = (signed && v > 0 ? "+" : "") + v.toFixed(2);
    return n + "%";
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
    document.getElementById("sim-kpis-pct").innerHTML = "";
    document.getElementById("sim-reasons").innerHTML = "";
    document.getElementById("sim-rejections").innerHTML = "";
    document.getElementById("sim-trades").innerHTML = "";
    Plotly.purge("equity-chart");
    Plotly.purge("equity-chart-pct");

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
        strategy: document.getElementById("sim-strategy").value,
        close_profile: document.getElementById("sim-close").value,
        target_trades: parseInt(document.getElementById("sim-target").value) || 100,
        epics_per_day: parseInt(document.getElementById("sim-epics").value) || 3,
        euro_per_point: parseFloat(document.getElementById("sim-epp").value) || 1,
        spread_malus_pct: parseFloat(document.getElementById("sim-malus").value) || 0,
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
    status.innerHTML = `entry=<strong>${{data.strategy}}</strong> · ` +
        `close=<strong>${{data.close_profile}}</strong> · ` +
        `spread malus=<strong>${{s.spread_malus_pct}}%</strong> · ` +
        `seed=<strong>${{data.seed}}</strong> — ` +
        `${{s.days_simulated}} fictional days, ${{s.buy_signals}} BUY signals ` +
        `(reuse the seed to replay)`;
    document.getElementById("sim-results").style.display = "block";

    // ---- % lens (fictional points, spread malus applied) ----
    const pctColor = s.total_pct >= 0 ? "#4ade80" : "#f87171";
    document.getElementById("sim-kpis-pct").innerHTML =
        kpi("Trades", s.trades, "#e2e8f0") +
        kpi("Wins", s.wins_pct, "#4ade80") +
        kpi("Losses", s.losses_pct, "#f87171") +
        kpi("Win rate", (s.win_rate_pct * 100).toFixed(1) + "%",
            s.win_rate_pct >= 0.5 ? "#4ade80" : "#fbbf24") +
        kpi("Cumulative", pct(s.total_pct, true), pctColor) +
        kpi("Avg win", pct(s.avg_win_pct, true), "#4ade80") +
        kpi("Avg loss", pct(s.avg_loss_pct, true), "#f87171") +
        kpi("Best / Worst", pct(s.best_pct, true) + " / " + pct(s.worst_pct, true),
            "#e2e8f0") +
        kpi("Max drawdown", pct(s.max_drawdown_pct), "#fbbf24");

    Plotly.newPlot("equity-chart-pct", [{{
        y: s.equity_pct, mode: "lines", name: "Cumulative % (fictional points)",
        line: {{color: pctColor, width: 1.8}},
        fill: "tozeroy", fillcolor: "rgba(96,165,250,0.08)",
    }}], {{...PLOTLY_LAYOUT, height: 300,
        xaxis: {{title: "Closed trades"}}, yaxis: {{title: "Cumulative % (fictional points)"}}}},
        {{displayModeBar: false, responsive: true}});

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

    const netPcts = s.net_pcts || [];
    document.getElementById("sim-trades").innerHTML = data.trades.map((t, i) => {{
        const c = t.euro >= 0 ? "#4ade80" : "#f87171";
        const np = netPcts[i];
        const npCol = np >= 0 ? "#4ade80" : "#f87171";
        const npCell = np === undefined ? "—" : pct(np, true);
        return `<tr><td class="number">${{i + 1}}</td>` +
            `<td class="number">${{t.day + 1}}</td>` +
            `<td>${{t.open_time}} @ ${{t.level_open}}</td>` +
            `<td>${{t.close_time}} @ ${{t.level_close}}</td>` +
            `<td>${{t.reason}}</td>` +
            `<td class="number" style="color:${{npCol}};">${{npCell}}</td>` +
            `<td class="number" style="color:${{c}};">${{eur(t.euro, true)}}</td></tr>`;
    }}).join("");
    lucide.createIcons();
}}

// Entry-strategy open trigger test: walk forward to the first BUY, then cut the
// curve at the open so the post-open price action stays hidden.
async function runOpenStrategy() {{
    const btn = document.getElementById("op-run-btn");
    const meta = document.getElementById("op-meta");
    const strategy = document.getElementById("op-strategy").value;
    const profile = document.getElementById("op-profile").value;
    const seed = document.getElementById("op-seed").value;
    const base = document.getElementById("op-base").value;

    const params = new URLSearchParams({{
        curve_profile: profile, strategy, base_price: base,
    }});
    if (seed !== "") params.set("seed", seed);

    btn.disabled = true;
    meta.innerHTML = '<span class="spinner"></span> Looking for an open…';

    let data;
    try {{
        const resp = await fetch(`/api/simulator/open-strategy?${{params}}`);
        data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "request failed");
    }} catch (err) {{
        meta.textContent = `Failed: ${{err.message}}`;
        btn.disabled = false;
        return;
    }}
    btn.disabled = false;

    const ts = data.timestamps;
    const traces = [
        {{x: ts, y: data.bids, name: "Bid", mode: "lines",
          line: {{color: "#60a5fa", width: 1.6}}}},
        {{x: ts, y: data.offers, name: "Offer", mode: "lines",
          line: {{color: "#475569", width: 1, dash: "dot"}}}},
    ];

    if (data.opened) {{
        const o = data.open;
        meta.innerHTML =
            `strategy=<strong>${{data.strategy}}</strong> · curve=${{data.curve_profile}} · ` +
            `seed=<strong>${{data.seed}}</strong> · ` +
            `<span style="color:#4ade80;">BUY</span> at ${{o.time}} ` +
            `(candle ${{o.index + 1}}/${{data.candles_total}}) @ bid ${{o.bid}} · ` +
            `future hidden`;
        document.getElementById("op-kpis").innerHTML =
            kpi("Signal", "BUY", "#4ade80") +
            kpi("Open time", o.time, "#e2e8f0") +
            kpi("Open bid", o.bid, "#60a5fa") +
            kpi("Score", o.score, "#a78bfa") +
            kpi("Candle", (o.index + 1) + " / " + data.candles_total, "#94a3b8");
        // Mark the open with a triangle on the bid at the truncation point.
        traces.push({{x: [ts[o.index]], y: [o.bid], name: "Open (BUY)",
            mode: "markers",
            marker: {{color: "#4ade80", size: 14, symbol: "triangle-up"}}}});
    }} else {{
        meta.innerHTML =
            `strategy=<strong>${{data.strategy}}</strong> · curve=${{data.curve_profile}} · ` +
            `seed=<strong>${{data.seed}}</strong> · ` +
            `<span style="color:#fbbf24;">no open triggered</span> over ` +
            `${{data.candles_total}} candles`;
        document.getElementById("op-kpis").innerHTML =
            kpi("Signal", "none", "#fbbf24") +
            kpi("Candles", data.candles_total, "#94a3b8") +
            kpi("Warmup", data.warmup, "#94a3b8");
    }}

    Plotly.newPlot("op-chart", traces, {{...PLOTLY_LAYOUT, height: 420,
        xaxis: {{title: "Time", nticks: 12}}, yaxis: {{title: "Price"}},
        legend: {{orientation: "h", y: 1.08}}}},
        {{displayModeBar: false, responsive: true}});
}}

// Close-profile visual test: one open→close cycle, stop trailing the curve.
async function runCloseProfile() {{
    const btn = document.getElementById("cp-run-btn");
    const meta = document.getElementById("cp-meta");
    const profile = document.getElementById("cp-profile").value;
    const close = document.getElementById("cp-close").value;
    const seed = document.getElementById("cp-seed").value;
    const base = document.getElementById("cp-base").value;
    const epp = document.getElementById("cp-epp").value;

    const params = new URLSearchParams({{
        curve_profile: profile, close_profile: close,
        base_price: base, euro_per_point: epp,
    }});
    if (seed !== "") params.set("seed", seed);

    btn.disabled = true;
    meta.innerHTML = '<span class="spinner"></span> Simulating one trade…';

    let data;
    try {{
        const resp = await fetch(`/api/simulator/close-profile?${{params}}`);
        data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "request failed");
    }} catch (err) {{
        meta.textContent = `Failed: ${{err.message}}`;
        btn.disabled = false;
        return;
    }}
    btn.disabled = false;

    // Map the stop track (sparse indices) onto x = timestamps for plotting.
    const ts = data.timestamps;
    const stopX = data.stops.map(s => ts[s.index]);
    const stopY = data.stops.map(s => s.level);

    const reasonColor = data.win ? "#4ade80" : "#f87171";
    meta.innerHTML =
        `curve=${{data.curve_profile}} · profile=<strong>${{data.close_profile}}</strong> · ` +
        `seed=<strong>${{data.seed}}</strong> · ` +
        `open ${{data.open.time}} @ ${{data.open.level}} → ` +
        `close ${{data.close.time}} @ ${{data.close.level}} ` +
        `(<span style="color:${{reasonColor}};">${{data.close.reason}}</span>) · ` +
        `${{data.stop_updates}} stop moves`;

    document.getElementById("cp-kpis").innerHTML =
        kpi("Result", eur(data.euro, true), reasonColor) +
        kpi("Close reason", data.close.reason, "#e2e8f0") +
        kpi("Stop moves", data.stop_updates, "#60a5fa") +
        kpi("Level zero", data.level_zero, "#a78bfa") +
        kpi("Level margin", data.level_margin, "#22d3ee") +
        kpi("Close", data.close.time + " @ " + data.close.level, "#94a3b8");

    // Horizontal reference spanning open → end of curve, drawn at a constant y.
    const span = [ts[data.open.index], ts[ts.length - 1]];
    const hline = (y, name, color) => ({{
        x: span, y: [y, y], name, mode: "lines",
        line: {{color, width: 1.2, dash: "dash"}},
    }});

    Plotly.newPlot("cp-chart", [
        // Intra-candle bid range (low–high) as a faint band behind the lines, so
        // a stop filled on a wick is visible even when the close stays above it.
        {{x: ts, y: data.bid_highs, mode: "lines", line: {{width: 0}},
          hoverinfo: "skip", showlegend: false}},
        {{x: ts, y: data.bid_lows, name: "Bid range (low–high)", mode: "lines",
          line: {{width: 0}}, fill: "tonexty",
          fillcolor: "rgba(96,165,250,0.18)", hoverinfo: "skip"}},
        {{x: ts, y: data.bids, name: "Bid", mode: "lines",
          line: {{color: "#60a5fa", width: 1.6}}}},
        {{x: stopX, y: stopY, name: "Protective stop", mode: "lines",
          line: {{color: "#fb923c", width: 1.8, shape: "hv"}}}},
        // Break-even (offer paid) and the margin above it (positive beyond noise).
        hline(data.level_zero, "Level zero (break-even)", "#a78bfa"),
        hline(data.level_margin, "Level margin (zero + noise)", "#22d3ee"),
        {{x: [ts[data.open.index]], y: [data.open.bid], name: "Open",
          mode: "markers", marker: {{color: "#4ade80", size: 12, symbol: "triangle-up"}}}},
        {{x: [ts[data.close.index]], y: [data.close.level], name: "Close",
          mode: "markers", marker: {{color: "#f87171", size: 12, symbol: "x"}}}},
    ], {{...PLOTLY_LAYOUT, height: 420,
        xaxis: {{title: "Time", nticks: 12}}, yaxis: {{title: "Price"}},
        legend: {{orientation: "h", y: 1.08}}}},
        {{displayModeBar: false, responsive: true}});
}}

lucide.createIcons();
onSimStrategyChange();  // honour the live default if it is a ranker
generateCurve();
runOpenStrategy();
runCloseProfile();
</script>
</body>
</html>""")
