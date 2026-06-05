# Architecture

## Directory layout

```
igthon/
├── .env.example            # Configuration template (copy to .env)
├── alembic.ini             # Alembic migration config
├── pyproject.toml          # Dependencies and tool config
├── alembic/
│   ├── env.py
│   └── versions/           # DB migration scripts
├── src/
│   ├── config.py           # pydantic-settings — loads .env
│   ├── main.py             # CLI entry point (argparse + asyncio runner)
│   ├── api/
│   │   ├── client.py       # IGClient — httpx async HTTP wrapper
│   │   ├── session.py      # OAuth v3 token lifecycle (login + auto-refresh)
│   │   ├── streaming.py    # Lightstreamer client — live candle feed
│   │   └── endpoints/
│   │       ├── accounts.py
│   │       ├── history.py
│   │       ├── markets.py
│   │       ├── positions.py
│   │       ├── prices.py
│   │       └── watchlists.py
│   ├── models/
│   │   ├── database.py     # SQLAlchemy engine + session factory
│   │   ├── candle.py       # Persisted 1-minute candles (chart history)
│   │   ├── day.py          # Daily P&L summary
│   │   ├── epic.py         # Instrument metadata
│   │   ├── position.py     # Trade record (open / closed)
│   │   └── resume.py       # Per-epic weekly direction summary
│   ├── services/
│   │   ├── api_error_log.py  # Ring buffer of recent API errors
│   │   ├── api_guard.py      # Rate-limit tracker (per-second + per-minute)
│   │   ├── api_queue.py      # Single-worker queue — serialises all IG calls
│   │   ├── candle_store.py   # Persist + prune candles; CSV dump for backtesting
│   │   ├── compute.py        # Technical indicators (regression, SMA, ROC, score)
│   │   ├── market_data.py    # Fetches /prices → feeds PriceBuffer
│   │   ├── market_scanner.py # Discovers tradeable epics via search terms
│   │   ├── price_buffer.py   # In-memory rolling candle buffer per epic
│   │   ├── recorder.py       # Structured logging + alert notifications
│   │   ├── scheduler.py      # APScheduler jobs — tick, monitor, EOD summary
│   │   └── trading.py        # Open/close position logic (Action.php port)
│   ├── scripts/
│   │   ├── fetch_markets.py    # One-shot market discovery
│   │   ├── test_buffer.py      # Manual buffer inspection
│   │   └── verify_connection.py # Smoke-test IG API connectivity
│   ├── utils/
│   │   └── tools.py          # Number formatting, misc helpers
│   └── web/
│       ├── app.py            # FastAPI app factory
│       ├── routes/
│       │   ├── charts.py     # /api/prices — candle data for charts
│       │   ├── dashboard.py  # / — live buffer status + bot state
│       │   └── positions.py  # /positions/ — position list + P&L summary
│       ├── static/
│       └── templates/
└── tests/
    ├── test_api_queue.py
    ├── test_candle_store.py
    ├── test_client.py
    ├── test_compute.py
    ├── test_market_scanner.py
    ├── test_price_buffer.py
    ├── test_streaming.py
    └── test_trading.py
```

______________________________________________________________________

## Layers

```
┌─────────────────────────────────────────┐
│  CLI (src/main.py)  │  Web (src/web/)   │  ← entry points
├─────────────────────────────────────────┤
│           Services (src/services/)      │  ← business logic
├─────────────────────────────────────────┤
│  API layer (src/api/)  │  Models (DB)   │  ← external I/O + persistence
└─────────────────────────────────────────┘
```

**Rules:**

- No business logic in routes or models — only in `services/`.
- No global state — pass dependencies via constructor or FastAPI `Depends`.
- All IG API calls go through `APIQueue` to enforce rate limits.

______________________________________________________________________

## Data flow

```
Lightstreamer feed
      │  live 1-min candles
      ▼
IGStreamingClient ──persist──▶ CandleStore (DB candle table)
      │
      │  on_candle callback
      ▼
PriceBuffer (in-memory, per epic)
      │
      ▼
ComputeSignal ──▶ TradingSignal (score, direction, R², ROC, spread)
      │
      ▼
BotScheduler ──▶ TradingService ──▶ POST /positions/otc (IG API)
                                  └▶ DB position record
```

On startup the buffer is **rehydrated** from the candle table (last 90 minutes by default)
so indicators are immediately valid without waiting for live candles to accumulate.

______________________________________________________________________

## Database tables

| Table      | Contents                              | Storage rationale                                    |
| ---------- | ------------------------------------- | ---------------------------------------------------- |
| `position` | Every opened/closed trade             | Cannot be reconstructed — permanent record           |
| `epic`     | Instrument metadata (deposit, type)   | Rarely changes, avoids redundant API calls           |
| `day`      | Daily P&L summary                     | One row per day — aggregated                         |
| `resume`   | Per-epic direction summary (day/week) | Direction analysis                                   |
| `candle`   | 1-minute OHLC + volume                | Rehydrates buffer on restart; CSV-dumped then pruned |

**Not stored:** intraday tick-by-tick data — fetched on demand from `/prices` when needed.

______________________________________________________________________

## API call serialisation

All calls to the IG REST API are routed through `APIQueue`:

```
APIQueue (single async worker)
  └─ respects APIGuard limits (50 req/min, 25 req/s)
  └─ retries transient failures (3 attempts, exponential backoff)
  └─ waits on quota blocks and resumes automatically
```

This ensures the bot never exceeds IG rate limits even when multiple services
(market scanner, scheduler, streaming bootstrap) submit requests concurrently.

______________________________________________________________________

## Streaming vs polling

The Lightstreamer feed (`streaming_enabled = true`, default) is the **primary** source
of live price data. The REST `/prices` endpoint is used only as a **fallback** to:

1. Seed the buffer on first start (no candle history yet).
1. Rehydrate after a gap in the feed.

This avoids consuming the IG historical data allowance on every tick.
