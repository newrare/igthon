# Backtesting — replay a strategy on archived real-market data

> **Companion to the [Simulator](../src/web/routes/simulator.py).** The
> simulator replays the trading rules on **synthetic** pseudo-random curves; the
> backtester replays the same rules on **real** candles recorded in production
> and saved to disk. Both share one replay engine, so a backtest exercises the
> exact open/close logic the live bot uses.

## Why a separate, file-based system

The live bot records one-minute candles (bid + offer OHLC) for every tracked
epic. They feed the in-memory buffer and are persisted to the `candle` table
(see [candle_store.py](../src/services/candle_store.py)). To keep that table
small, a retention job archives aged candles to CSV files and deletes them from
the database.

The backtester reads **only those archive files** — it never opens a database
session or calls the IG API. That independence is the whole point: you can run a
backtest in the middle of the week, while the main process keeps recording the
current week's data into the database, with zero contention between the two. The
recorder owns the DB; the backtester owns the files.

```
                 live recording                       offline backtest
   IG /streaming ─► PriceBuffer ─► candle table ─► (retention dump) ─► dumps/*.csv ─► BacktestArchive ─► run_backtest
                                       ▲                                                                      │
                                       └──────────── independent: never touched by a backtest ────────────────┘
```

## The weekly archive (retention dump)

The job `dump_and_purge_candles` (registered in
[scheduler.py](../src/services/scheduler.py), runs daily at 02:00 UTC, also
triggerable from the dashboard) does two things:

1. Selects every candle older than `CANDLE_RETENTION_DAYS` (default **7**).
1. Groups them by **ISO week** and appends each group to
   `dumps/candles_<year>-W<week>.csv` (e.g. `candles_2026-W24.csv`), writing the
   CSV header only when a week file is first created.
1. Deletes those candles from the `candle` table.

Because a candle is deleted right after it is archived, it is written exactly
once; successive daily runs simply append the newly-aged candles to the matching
week file. A week's file is therefore **complete roughly 7 days after that week
ends** — which is exactly what an offline backtest of past weeks needs.

### Backtesting recent data (snapshot)

The retention dump only archives candles **older than the retention window**, so
the most recent days still live only in the database and would not appear in the
archive. To backtest them without waiting 7 days, use the **Snapshot DB now**
button on the `/backtest` page (or `POST /api/backtest/export`): it copies the
**entire current candle table** into the per-week archive files **without
deleting anything**. The candles stay in the database for the live charts; they
are merely also written to the archive so the backtester can read them.

The snapshot is **idempotent** — rows already in a week file (matched by epic +
timestamp) are skipped, so it is safe to click repeatedly and it merges cleanly
with whatever the retention purge has already written (duplicates that can arise
from the overlap are also collapsed when the archive is read). This keeps the
backtester strictly file-based: it never queries the database itself.

The dump directory is set by `CANDLE_DUMP_DIR` (default `./dumps`). Each archive
row carries this schema:

```
epic, timestamp, bid_open, bid_close, bid_high, bid_low,
offer_open, offer_close, offer_high, offer_low, volume
```

> **Legacy dumps stay usable.** Any CSV in the dump directory whose header
> matches the schema above is read, regardless of filename, so older
> `candles_before_*.csv` dumps are picked up alongside the newer week files.

## Components

| Layer       | File                                                       | Responsibility                                                                            |
| ----------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Archive     | [backtest_archive.py](../src/services/backtest_archive.py) | Read dump CSVs (no DB). `datasets()` lists weeks/epics; `load()` returns candles per epic |
| Engine      | [backtester.py](../src/services/backtester.py)             | `build_days()` groups candles into trading days; `run_backtest()` replays the strategy    |
| Replay core | [simulator.py](../src/services/simulator.py)               | Shared `StrategySimulator.run_days()` — the same engine the synthetic simulator uses      |
| Web         | [backtest.py](../src/web/routes/backtest.py)               | `/backtest` page + `/api/backtest/datasets` + `/api/backtest/run`                         |

### Pipeline

```
BacktestArchive.load(weeks, epics)   →  dict[epic, list[Candle]]
build_days(candles_by_epic)          →  list of trading days, each a list of (epic, candles)
StrategySimulator.run_days(days)     →  SimulationResult (trades + summary stats)
```

Before grouping, `run_backtest` **dedupes correlated contracts**
(`dedupe_correlated_epics`): IG lists the same market under several contracts
(`IX.D.DAX.IDF.IP`, `IX.D.DAX.IFMM.IP`, `IX.D.DAX.IMF.IP` are all the DAX), which
would otherwise count the same bet three times. One epic per underlying is kept
(the richest series); the dropped epics are reported in the run response.

`build_days` groups each epic's candles by **calendar date**; every date becomes
one trading day holding all epics that traded it. Daily gates (max trades,
daily P&L target, win-rate circuit-breaker) reset per day — identical to the
live scheduler. Within a day, candles across epics are merged into a single
timestamp-ordered stream, so misaligned real series (different start times /
lengths) interleave correctly.

The replay applies the **same** rules as production: the pluggable entry
strategy (`evaluate`), the pre-open gates
([trading.py](../src/services/trading.py)), the win/stop levels, and the ATR
trailing stop. A BUY fills at the offer, the protective stop fills intra-candle
when the bid low crosses it, and anything still open at end of day is
force-closed.

## Using the web page

Open **`/backtest`** (linked from the nav bar on every page):

1. **Archived data** — pick a week from the dropdown. The page shows the epics
   available that week and their candle counts. Optionally tick specific epics
   to narrow the run (leave all unticked to backtest the whole week).
1. **Run backtest** — choose the strategy (defaults to the live `STRATEGY_NAME`,
   other entries allow comparison) and the trades target, then run.
1. **Results** — win/loss counts, win rate, total return, average win/loss, max
   drawdown, the cumulative-return equity curve, close-reason and open-rejection
   breakdowns, and the full trade list.

Like the simulator, a run is pushed to a worker thread so the event loop (and
the 1-second dashboard poll) stays responsive.

### P&L is a percentage return, not euros

P&L is reported as the **percentage return computed from the actual fill
prices**: `(close - open) / open`. This is deliberate — the archive holds prices
only, not contract sizes or currency conversions, so there is no honest single
"euro per point" that fits both an index (e.g. the DAX, where one point is worth
several euros) and a forex pair (where one "point" is 0.0001 and a standard-lot
point is worth ~€8–10). A fixed euro-per-point made forex moves render as
`0.00 €` even when the trade was green. A percentage return is contract-agnostic
and directly comparable across every instrument, so the trade table, KPIs and
equity curve are all in percent.

## Programmatic use

```python
from src.services.backtest_archive import BacktestArchive
from src.services.backtester import BacktestConfig, percentage_summary, run_backtest

archive = BacktestArchive(settings.candle_dump_dir)
candles = archive.load(weeks=["2026-W24"])           # files only — no DB
result = run_backtest(
    settings,
    candles,
    BacktestConfig(target_trades=100),
    strategy_name="donchian_er",                      # defaults to STRATEGY_NAME
)
print(percentage_summary(result.trades))             # returns in %, price-based
```

## API

| Method | Path                     | Body / Query                                   | Returns                                                                                           |
| ------ | ------------------------ | ---------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `GET`  | `/api/backtest/datasets` | —                                              | `{ weeks: [{ week, total_candles, first, last, epics: [...] }] }`                                 |
| `POST` | `/api/backtest/export`   | —                                              | `{ rows_written, files }` — snapshot DB → archive, no deletion                                    |
| `POST` | `/api/backtest/run`      | `{ weeks, epics?, strategy?, target_trades? }` | `{ strategy, epics_loaded, candles_loaded, summary, trades }` — summary/trades report return in % |

Leave `epics` empty (or tick **All epics** in the UI) to backtest every epic in
the selected week(s).

`/api/backtest/run` returns **400** when no archived candle matches the
selection, or when the requested strategy is unknown. `/api/backtest/export`
returns **503** if the process has no candle store (e.g. a web-only deployment).

## Caveats

- A backtest is a **coherence check** of the rules on past data, not a market
  prediction — past results do not guarantee future ones.
- Only weeks that have been archived are available. The current and most recent
  days still live in the database (within the retention window) and are
  deliberately **not** read by the backtester.
- Fills are modelled minimally (offer fill, intra-candle stop, end-of-day
  force-close). Slippage and partial fills are not simulated.

## Related

- [STRATEGY.md](STRATEGY.md) — the shared risk management and daily cycle.
- [strategies/README.md](strategies/README.md) — the pluggable strategy system.
- [CONFIGURATION.md](CONFIGURATION.md) — `CANDLE_RETENTION_DAYS`, `CANDLE_DUMP_DIR`.
