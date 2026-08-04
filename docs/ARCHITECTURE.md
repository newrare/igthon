# Architecture

## Directory layout

The code is organised **by domain**, mirroring the bot's responsibilities. The
two trading decisions — **opening** and **closing** — are fully decoupled (see
"Open/close decoupling"); everything else is shared, purely-functional
infrastructure that can be tested regardless of the market.

```
igthon/
├── .env.example            # Configuration template (copy to .env)
├── alembic/                # DB migrations (env.py + versions/)
├── src/
│   ├── main.py             # CLI entry point (argparse + asyncio runner)
│   ├── core/               # INFRA — plumbing shared by every domain
│   │   ├── config.py       # pydantic-settings — loads .env
│   │   ├── scheduler.py    # APScheduler jobs (thin: orchestration, no decisions)
│   │   ├── indicators.py   # Technical indicators (regression, SMA, ROC, ATR, ER)
│   │   ├── similarity.py   # Curve-shape signature + "same bet?" redundancy
│   │   ├── recorder.py     # Structured logging + alerts
│   │   ├── api_queue.py    # Single-worker queue — serialises all IG calls
│   │   ├── api_guard.py    # Rate-limit tracker (per-second + per-minute)
│   │   ├── api_error_log.py# Ring buffer of recent API errors
│   │   └── api/            # IG HTTP client: client.py, session.py, endpoints/
│   ├── feed/               # FLUX — live prices, buffer, persistence
│   │   ├── streaming.py    # Lightstreamer client — live candle feed
│   │   ├── market_data.py  # Fetches /prices → feeds PriceBuffer
│   │   ├── price_buffer.py # In-memory rolling candle buffer per epic
│   │   └── candle_store.py # Persist + prune candles; per-week CSV archive
│   ├── markets/            # MARCHÉS — build the tradeable epic list
│   │   └── market_scanner.py
│   ├── entry/              # OUVERTURE — entry strategies (open decision only)
│   │   ├── base.py            # EntryStrategy → EntryIntent (direction, NO exit levels)
│   │   ├── open_donchian.py   # Donchian breakout + Efficiency-Ratio regime gate
│   │   ├── open_projection.py # Breakout confirmed by a multi-model projection
│   │   └── open_ranking.py    # Cross-epic ranker, one rolling position
│   ├── stops/              # ARRÊT — initial-stop placement (drives sizing)
│   │   ├── base.py            # StopDistance → initial_stop()
│   │   ├── stop_support.py    # Stop below a recency-weighted support
│   │   ├── stop_atr.py        # Flat stop_atr_k × ATR from the entry
│   │   ├── stop_regression.py # Choppiness-scaled residual-noise band
│   │   └── stop_linearspeed.py# Tight when the last 10 min accelerate, else structure
│   ├── exit/               # FERMETURE — close profiles (own the whole exit)
│   │   ├── base.py            # CloseProfile → OpenPlan / CloseDecision
│   │   ├── trailing.py        # Pure close maths (decide_close_reason, trailing stop)
│   │   ├── close_zoneprofit.py# Reference profile: per-zone stop management
│   │   └── zones/             # Per-zone stop updaters (underwater / band / secure / profit)
│   ├── execution/          # MAINS — turn decisions into broker/DB effects
│   │   ├── gates.py        # Pure pre-open gates
│   │   └── trading.py      # TradingService: order placement, close, reconcile
│   ├── backtest/           # OUTILS — offline evaluation (no DB, no IG API)
│   │   ├── simulator.py    # Replay entry+close over synthetic curves
│   │   ├── backtester.py   # Replay entry+close over archived real candles
│   │   ├── backtest_archive.py
│   │   └── curve_generator.py
│   ├── models/             # DATA — SQLAlchemy ORM (database.py + tables)
│   ├── utils/              # tools.py — formatting, misc helpers
│   └── web/                # VUES — FastAPI app + dashboard/routes
└── tests/                  # One test file per module + isolated entry/exit tests
```

______________________________________________________________________

## Open/close decoupling

The single most important rule: **the code that opens a position and the code
that closes it are independent and never reference each other.**

- An **`EntryStrategy`** (`src/entry/`) turns the price buffer into an
  `EntryIntent` carrying only a **direction** (and an optional sizing hint). It
  produces **no exit levels** at all.
- A **`CloseProfile`** (`src/exit/`), chosen *independently* by config, owns the
  whole exit: `initial_plan()` picks the protective stop at open (which also
  drives sizing), and `evaluate()` decides every monitor tick — hold, close
  (with a reason), or ratchet the stop.
- They are composed at runtime by the execution layer and linked only through
  the persisted `Position` (its `close_profile` column records which profile
  manages it for life).

Consequences: a new opening idea is a new `entry/` module; a new exit scenario
is a new `exit/` module; either can be swapped or unit-tested in isolation, and
a close profile can be measured on synthetic price paths with no entry involved.

Selection is one line each in `.env` — the single source of truth, all required
(no default, no DB persistence, no runtime switching): `OPEN_STRATEGY` /
`STOP_STRATEGY` and the four per-zone close selectors `CLOSE_ZONESTART` /
`CLOSE_ZONEMARGE` / `CLOSE_ZONESECURE` / `CLOSE_ZONEPROFIT` (the close side is
split into four independently-tuned zones — follower→break-even,
break-even→margin, margin→profit trigger, above the profit trigger).
The same file carries `ALLOW_SAME_DAY_REOPEN`, a global open policy (required
too): whether an epic may be opened more than once in the same day, in either
direction. It is enforced by the shared pre-open gate and the rolling selector,
so every entry strategy obeys it. `ALLOW_RECOVERY_REVERT` (required as well) is
the second global open policy: when a position is stopped out at a loss on the
stop it was **opened with** *and* the curve it walked since its open shows a real
move (at least one candle carrying a visible chunk of the risk, price still past
the stop), the bot immediately opens the opposite side on the same epic to follow
the reversal. Both rules are pure
(`execution/gates.should_revert_after_stop_loss` and
`execution/gates.curve_supports_revert`) and composed in
`core/scheduler.BotScheduler._revert_after_stop_loss`, so it belongs to neither
the entry nor the exit domain — it only reacts to a close and asks the shared open
path for the reverse side.

______________________________________________________________________

## Layers

```
┌──────────────────────────────────────────────┐
│  CLI (src/main.py)  │  Web (src/web/) — VUES   │  ← entry points
├──────────────────────────────────────────────┤
│  entry/  ·  exit/  ·  execution/  ·  backtest/ │  ← trading logic (open/close)
├──────────────────────────────────────────────┤
│  feed/  ·  markets/  ·  core/ (infra)          │  ← data + infrastructure
├──────────────────────────────────────────────┤
│  core/api/ (IG REST/stream)  │  models/ (DB)   │  ← external I/O + persistence
└──────────────────────────────────────────────┘
```

**Rules:**

- Open and close logic live in separate domains and never import each other.
- The scheduler (`core/scheduler.py`) only *orchestrates*: it composes the
  entry strategy with the close profile and calls the execution layer — it holds
  no trading decision of its own.
- No global state — pass dependencies via constructor or FastAPI `Depends`.
- All IG API calls go through `APIQueue` to enforce rate limits.

______________________________________________________________________

## Data flow

The full time-and-data contract — what the feed sends, what is dropped, what each
decision layer reads, and the latency budget — lives in
[DATAFLOW.md](DATAFLOW.md). The summary below is the shape only.

```
Lightstreamer feed (feed/streaming.py)
      │  consolidated 1-min candles only (intra-candle frames are dropped)
      ▼
IGStreamingClient ──persist──▶ CandleStore (DB candle table)
      │  on_candle callback
      ▼
PriceBuffer (in-memory, per epic)
      │
      ├──▶ EntryStrategy.evaluate()  ──▶ EntryIntent (direction only)
      │         │
      │         ▼  scheduler composes the two
      │    CloseProfile.initial_plan()  ──▶ stop level (drives sizing)
      │         │
      ▼         ▼
BotScheduler ──▶ TradingService.open_from_intent() ──▶ POST /positions/otc
                                                     └▶ DB position (+close_profile)

monitoring pass:  CloseProfile.evaluate(position, bid, buffer)
                    → HOLD / CLOSE(reason) / UPDATE_STOP
                    → TradingService.manage_position() applies it
```

A monitoring pass is triggered by the **arrival of candles** (debounced into one
whole-book pass), with the 30 s cron kept as a heartbeat for when streaming is off
or the feed goes silent. Both funnel through a single-flight guard, and both
respect the job's manual/paused mode. See [DATAFLOW.md](DATAFLOW.md) for the
latency budget and why the pass covers the whole book at once.

On startup the buffer is **rehydrated** from the candle table with **today's**
candles (from midnight UTC, capped at the buffer's capacity) so indicators are
immediately valid without waiting for live candles — and without spending the IG
historical-data allowance.

______________________________________________________________________

## Database tables

| Table      | Contents                                      | Storage rationale                                                                                         |
| ---------- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `position` | Every opened/closed trade (+ `close_profile`) | Cannot be reconstructed — permanent record                                                                |
| `epic`     | Instrument metadata (deposit, type)           | Rarely changes, avoids redundant API calls                                                                |
| `day`      | Daily P&L summary                             | One row per day — aggregated                                                                              |
| `resume`   | Per-epic direction summary (day/week)         | Direction analysis                                                                                        |
| `candle`   | 1-minute OHLC + volume                        | Rehydrates buffer on restart; archived per-week to CSV then pruned (see [BACKTESTING.md](BACKTESTING.md)) |

**Not stored — and not held anywhere at all:** market tick data. The finest
granularity the bot ever sees is the 1-minute candle; `/prices` returns those same
candles, not ticks. Sub-minute frames are received from the feed and dropped (see
[DATAFLOW.md](DATAFLOW.md)).

______________________________________________________________________

## API call serialisation

All calls to the IG REST API are routed through `APIQueue` (`core/api_queue.py`):

```
APIQueue (single async worker)
  └─ respects APIGuard limits (30 req/min, 20 req/s)
  └─ retries transient failures (3 attempts, exponential backoff)
  └─ waits on quota blocks and resumes automatically
```

This ensures the bot never exceeds IG rate limits even when multiple components
(market scanner, scheduler, streaming bootstrap) submit requests concurrently.

______________________________________________________________________

## Streaming vs polling

The Lightstreamer feed (`streaming_enabled = true`, default) is the **primary** source
of live price data. The REST `/prices` endpoint is used only as a **fallback** to:

1. Seed the buffer on first start (no candle history yet).
1. Rehydrate after a gap in the feed.

This avoids consuming the IG historical data allowance on every cycle.

The feed subscribes to `CHART:{epic}:1MINUTE` and keeps **only consolidated
candles** (`CONS_END == "1"`); the sub-second frames that update the forming bar
are dropped. That filter is **load-bearing**, not an optimisation: both write paths
deduplicate on a strictly increasing timestamp and all frames of a minute share one
timestamp, so letting partial frames through would keep the first sample of each
minute and silently discard the finished candle. See
[DATAFLOW.md](DATAFLOW.md) §6.

______________________________________________________________________

## Dashboard live updates

The dashboard is server-rendered once, then kept current **client-side** without
full-page reloads:

- `_gather_dashboard_state()` collects a single snapshot (buffer, KPIs, guard,
  queue, open positions) and is shared by the page route and the JSON poller.
- `_build_fragments()` turns that snapshot into one HTML string per dynamic
  region (`kpi_bar`, `market_rows`, `queue_modal`, `api_modal`,
  `positions_modal`). It is the single source of truth, so the initial render
  and the incremental updates can never drift.
- `GET /api/dashboard-fragments` returns `{fragments, bot_paused, scheduler_available, server_time}`.
- The browser polls that endpoint every **2 s** and replaces the inner HTML of a
  `#frag-*` container **only when its markup changed** since the previous poll.
  Each region also displays `server_time` as its "last refresh" label.

This is the "unified Option A" model: one request per cycle (same network volume
as the previous 30 s full reload, but at 2 s cadence) with surgical DOM updates,
preserving scroll position, open modals and collapse state.
