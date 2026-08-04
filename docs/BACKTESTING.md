# Backtesting — replay a strategy on archived real-market data

> **Companion to the [Simulator](../src/web/routes/simulator.py).** The
> simulator replays the trading rules on **synthetic** pseudo-random curves; the
> backtester replays the same rules on **real** candles recorded in production
> and saved to disk. Both share one replay engine, so a backtest exercises the
> exact open/close logic the live bot uses.

## Why a separate, file-based system

The live bot records one-minute candles (bid + offer OHLC) for every tracked
epic. They feed the in-memory buffer and are persisted to the `candle` table
(see [candle_store.py](../src/feed/candle_store.py)). To keep that table
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
[scheduler.py](../src/core/scheduler.py), runs daily at 02:00 UTC, also
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

| Layer       | File                                                            | Responsibility                                                                            |
| ----------- | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Archive     | [backtest_archive.py](../src/backtest/backtest_archive.py)      | Read dump CSVs (no DB). `datasets()` lists weeks/epics; `load()` returns candles per epic |
| Contracts   | [contract_values.py](../src/backtest/contract_values.py)        | `epic → € per point`, read from a JSON file — the euro dimension the archive lacks        |
| Engine      | [backtester.py](../src/backtest/backtester.py)                  | Selection overlay, `build_days()`, `run_backtest()`, the euro / percentage summaries      |
| Replay core | [simulator.py](../src/backtest/simulator.py)                    | Shared `StrategySimulator.run_days()` — the same engine the synthetic simulator uses      |
| Web         | [backtest.py](../src/web/routes/backtest.py)                    | `/backtest` page + `/api/backtest/datasets` + `/api/backtest/run`                         |
| Capture     | [dump_euro_per_point.py](../src/scripts/dump_euro_per_point.py) | One-off: fill the contract table from IG `/markets` so backtests stay offline             |

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
one trading day holding all epics that traded it — identical to the live
scheduler's per-day reset. Within a day, candles across epics are merged into a
single timestamp-ordered stream, so misaligned real series (different start times
/ lengths) interleave correctly.

The replay applies the **same** rules as production: the pluggable entry
strategy (`evaluate`), the pre-open gates
([trading.py](../src/execution/trading.py)), the win/stop levels, and the per-zone
stop updates. A market order fills on the side that pays the spread, the
protective stop fills intra-candle when the close-out price reaches it, and
anything still open at end of day is force-closed.

### Both directions are replayed

A SELL intent is kept exactly when the live path keeps it — the strategy declares
`emits_shorts` — and the shared pre-open gate is handed the same `allow_short`, so
a two-sided strategy (`open_fade`, `open_five`, `open_linear`, `open_pullback`,
`open_steady`) is measured on both of its sides while a long-only one still cannot
short by accident.

Everything below follows from one helper, `direction_sign()` in
[simulator.py](../src/backtest/simulator.py) — there is no separate short code
path:

| Aspect         | BUY                                    | SELL                         |
| -------------- | -------------------------------------- | ---------------------------- |
| Fill           | the **offer**                          | the **bid**                  |
| Close-out      | the **bid**                            | the **offer**                |
| Broker stop    | fills on the **bid low**               | fills on the **offer high**  |
| Break-even     | `level_zero` = entry offer             | `level_zero` = entry bid     |
| P&L            | `close − open`                         | `open − close`               |
| Break-even @BE | first close-out **above** `level_zero` | first close-out **below** it |

The spread is therefore paid once, at the open, on both sides — and the close
profile needs no special case: it already mirrors every zone for a short (see
[exit tests](../tests/test_exit_short.py)).

## Using the web page

Open **`/backtest`** (linked from the nav bar on every page):

1. **Archived data** — pick a week from the dropdown. The line below shows its
   date range, epic count and candle count.
1. **Run backtest** — set the six selectors (see below), then run. *Back to live*
   puts them all back on the `.env` values.
1. **Results** — the KPI row, the euro equity curves, close-reason and
   open-rejection breakdowns, and the full trade list.

### The six selectors — the whole configuration, not just the strategy

The page exposes exactly the selectors `.env` holds, in the order the price zones
are crossed:

| Selector           | Registry              | Chooses                                |
| ------------------ | --------------------- | -------------------------------------- |
| `OPEN_STRATEGY`    | `ENTRY_STRATEGIES`    | the entry signal (direction only)      |
| `STOP_STRATEGY`    | `STOP_DISTANCES`      | where the initial protective stop goes |
| `CLOSE_ZONESTART`  | `ZONESTART_UPDATERS`  | follower → break-even                  |
| `CLOSE_ZONEMARGE`  | `ZONEMARGE_UPDATERS`  | break-even → margin                    |
| `CLOSE_ZONESECURE` | `ZONESECURE_UPDATERS` | margin → profit trigger                |
| `CLOSE_ZONEPROFIT` | `ZONEPROFIT_UPDATERS` | above the profit trigger               |

Each starts on the live value (marked *(live)*) and each is sent explicitly with
the run, so a result is reproducible from its payload even after `.env` changes.
A name absent from its registry is a **400**, never a silent fallback.

#### Names the backtest refuses

Some valid *live* names cannot be reproduced offline, and a backtest that replayed
a degraded look-alike under their name would be worse than no backtest. They are
listed in `UNTESTABLE_NAMES` ([backtester.py](../src/backtest/backtester.py)), hidden
from the picker, and answered with a **400** — including when they are only
inherited from `.env` rather than requested, since a plain "backtest this week"
call must not quietly replay something else:

| Selector          | Name           | Why                                                                                                                    |
| ----------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `OPEN_STRATEGY`   | `open_manual`  | waits for a human order — there is no signal to replay                                                                 |
| `OPEN_STRATEGY`   | `open_testing` | opens unconditionally — replaying it tests nothing                                                                     |
| `CLOSE_ZONESTART` | `smartgroup`   | decides for the whole book from the scheduler's cross-position pre-pass (`plan_group`), which the engine does not have |

When one of them *is* the live value, the page still lists it — hiding it would
leave you wondering why the page disagrees with `.env` — but as a **disabled**
option, so the browser falls back to a name that can actually run.

Under the hood a `StrategySelection` is **overlaid** on the settings object
(`_SettingsOverlay`) and the overlaid settings are handed to the very same
factories the bot uses at startup (`get_entry_strategy`, `get_close_profile` →
`CloseZoneProfit.from_settings`). Nothing is re-assembled by hand and every
tuning parameter (ATR periods, ratios, margins) passes through untouched, so what
runs is the configuration under test — not an approximation of it.

### The run covers everything in the selection

A run always replays **every epic** of the chosen week (minus the correlated
duplicates collapsed by `dedupe_correlated_epics`) over **every one of its
days**. There is deliberately
no epic filter and no trade cap in the UI, so two runs of the same week differ
only by the strategy under test — which is the only comparison that means
anything. `BacktestConfig.target_trades` still exists for programmatic callers
and defaults to `NO_TRADE_CAP`; on a finite archive a cap would silently truncate
the data (the synthetic simulator needs one only because it generates days
endlessly).

`open_manual` and `open_testing` are **not offered**: the former waits for a
human order, the latter opens unconditionally, so neither carries a signal worth
replaying. `/api/backtest/run` returns 400 for both.

Like the simulator, a run is pushed to a worker thread so the event loop (and
the 1-second dashboard poll) stays responsive.

## Results

### The KPI row

| KPI                 | Meaning                                                    |
| ------------------- | ---------------------------------------------------------- |
| **Trades**          | positions opened *and* closed during the replay            |
| **Wins**            | trades whose close level beat their open level — see below |
| **Losses**          | every other trade (a flat trade counts as a loss)          |
| **Win rate**        | `wins / trades`                                            |
| **Total euro**      | Σ `move × € per point`, over the **priced** trades         |
| **Total euro @BE**  | the same sum under the break-even-exit scenario            |
| **Wins/Losses @BE** | the scenario's own win/loss split, and its win rate        |

**A win is purely a price comparison.** The move is signed by direction —
`close − open` for a long, `open − close` for a short — and `win = move > 0`
(strictly: a trade that gets back to exactly its entry is a loss). The fill already
carries the spread (a BUY fills at the offer and closes on the bid, a SELL the
other way round), and there is no other fee, no threshold and no slippage. The
counts need no contract value, so they cover every trade, priced or not.

### Euro P&L needs the contract table

The archive holds **prices only** — no contract size, no quote currency, no deal
size — so a euro figure cannot be derived from it. A single global "€ / point" is
not a fix either: one DAX point is worth several euros while one EUR/USD "point"
is 0.0001, so a shared factor flattens every forex trade to `0.00 €` (that bug is
why this page reported percentages only for a while).

The missing dimension therefore lives in a file, captured once per epic from IG:

```bash
python -m src.scripts.dump_euro_per_point            # every epic in the archive
python -m src.scripts.dump_euro_per_point --refresh  # re-price known epics
python -m src.scripts.dump_euro_per_point --dry-run  # show, write nothing
```

It writes `BACKTEST_CONTRACT_FILE` (default `./config/euro_per_point.json`):

```json
{
  "generated_at": "2026-08-03T07:12:00+00:00",
  "epics": {
    "CC.D.CC.UNC.IP": {
      "euro_per_point": 6.6, "quantity": 1.0, "currency": "USD",
      "contract_size": 10.0, "conversion_rate": 0.66,
      "name": "Cacao New York (10$)"
    }
  }
}
```

`euro_per_point` is the euro value of one full point of movement **for the position
the bot would actually open** — `minDealSize × contractSize × quote→EUR rate`,
i.e. exactly what [tools.py](../src/utils/tools.py) `euro_per_point()` resolves at
open — so a backtest euro is comparable with a live one. It carries the same
caveat as the live figure: IG's `exchangeRate` is a reference rate, so expect the
order of magnitude on a foreign quote, not the cent. The other fields are
informational, for auditing an entry against
`python -m src.scripts.inspect_market <epic>`.

**Every call goes through the [API queue](../src/core/api_queue.py).** Pricing a
whole archive is over a hundred `/markets` reads, well past IG's per-minute
allowance: hitting the client directly returns `exceeded-api-key-allowance` after a
couple of dozen epics and loses the rest. Through the queue, a quota block makes
the worker wait for the guard cooldown and **re-queue** the call, so the run simply
takes as long as IG's limits require and comes back complete. The file is also
written **after every epic**, so a run stopped by Ctrl-C keeps what it captured and
a re-run only fetches what is still missing (an epic already in the table is
skipped unless `--refresh`).

The script opens an IG session. **Prefer running it with the bot stopped**, since
IG caps concurrent sessions. It is the only online step, and it is a one-off: once
the file exists every backtest is offline again.

An epic **missing from the table is never priced at a guessed value**: its trades
are excluded from the euro totals and the page names them
(`unpriced_trades` / `unpriced_epics`). Percentage returns stay in the response and
in the trade table as the instrument-agnostic fallback. With no table at all, the
counts and percentages still work and the euro KPIs simply read `0.00 €` with the
exclusion notice.

### The break-even-exit scenario (`@BE`)

Next to the real result the page reports a counterfactual: **what if every position
were closed the moment it went past break-even?**

- while replaying, the first close-out price that goes **strictly past**
  `level_zero` is recorded on the trade (`level_breakeven_exit`, plus its time) —
  above it for a long, below it for a short, read from the same once-a-minute
  close-out price the live monitor sees;
- a candle on which the broker stop fires does **not** count: there is no way to
  prove price went green before it reached the stop;
- the scenario values that trade at the recorded price; a trade that never crossed
  break-even keeps its **real** outcome. The scenario can only ever cut a trade
  short, it never rescues a losing one.

`level_zero` *is* the entry price in close-out terms (the entry offer for a long,
the entry bid for a short — i.e. `level_open` either way), so every crosser is a
small win by construction and `Wins @BE` is exactly the number of trades that ever
turned green. Comparing `Total euro` with `Total euro @BE` is therefore a direct
read on what letting winners run is worth against banking every green tick. The
equity chart draws both curves.

## Programmatic use

```python
from src.backtest.backtest_archive import BacktestArchive
from src.backtest.backtester import (
    BacktestConfig, StrategySelection, euro_summary, percentage_summary, run_backtest,
)
from src.backtest.contract_values import ContractTable

archive = BacktestArchive(settings.candle_dump_dir)
candles = archive.load(weeks=["2026-W24"])           # files only — no DB
result = run_backtest(
    settings,
    candles,
    BacktestConfig(),                                # no trade cap: whole archive
    StrategySelection(                               # unset = live .env value
        open_strategy="open_donchian",
        close_zonemarge="limitloose",
    ),
)
table = ContractTable.load(settings.backtest_contract_file)
print(euro_summary(result.trades, table))            # euros, real + @BE scenario
print(percentage_summary(result.trades))             # returns in %, price-based
```

## API

| Method | Path                     | Body / Query                             | Returns                                                                  |
| ------ | ------------------------ | ---------------------------------------- | ------------------------------------------------------------------------ |
| `GET`  | `/api/backtest/datasets` | —                                        | `{ weeks: [{ week, total_candles, first, last, epics: [...] }] }`        |
| `POST` | `/api/backtest/export`   | —                                        | `{ rows_written, files }` — snapshot DB → archive, no deletion           |
| `POST` | `/api/backtest/run`      | `{ weeks, epics?, ` *six selectors* ` }` | `{ strategy, selection, epics_loaded, candles_loaded, summary, trades }` |

The run body accepts each selector under its `.env` name in lower case
(`open_strategy`, `stop_strategy`, `close_zonestart`, `close_zonemarge`,
`close_zonesecure`, `close_zoneprofit`); `strategy` is kept as the legacy alias of
`open_strategy`. Any of them omitted falls back to the live value, and the response
echoes the six names it actually replayed under `selection`.

`epics` is a programmatic-only narrowing hook; leave it empty (as the web page
always does) to backtest every epic in the selected week(s).

`summary` carries three lenses on the same replay: the structural counts, the euro
figures (`total_euro`, `total_euro_breakeven`, `wins_breakeven`, `equity_euro*`,
`unpriced_*`) and the percentage figures (`total_return_pct`, `equity_pct`, …).
Each trade carries `direction`, `return_pct`, `euro`, `euro_breakeven` (both `null`
when its epic is unpriced) and `breakeven_time`.

`/api/backtest/run` returns **400** when no archived candle matches the selection,
or when any **resolved** selector name is unknown or untestable — including a name
inherited from `.env` rather than requested (see *Names the backtest refuses*).
`/api/backtest/export` returns **503** if the process has no candle store (e.g. a
web-only deployment).

## Caveats

- A backtest is a **coherence check** of the rules on past data, not a market
  prediction — past results do not guarantee future ones.
- Only weeks that have been archived are available. The current and most recent
  days still live in the database (within the retention window) and are
  deliberately **not** read by the backtester.
- Fills are modelled minimally (offer fill, intra-candle stop, end-of-day
  force-close). Slippage and partial fills are not simulated.
- The **wallet gate is not modelled**: the archive holds prices, not account
  balance or margin, so a ranker's concurrent-position count is an upper bound (see
  `wallet_bounded` in [simulator.py](../src/backtest/simulator.py)).
- Names the engine cannot reproduce are **refused**, not approximated — see *Names
  the backtest refuses*. `CLOSE_ZONESTART=smartgroup` is the notable one: making it
  backtestable means porting the scheduler's cross-position pre-pass into the replay
  engine, and pricing the book in euros while doing it.
- Euro figures depend on the contract table and inherit IG's reference exchange
  rate on a foreign quote; unpriced epics are excluded and reported.

## Related

- [strategies/README.md](strategies/README.md) — the pluggable strategy system.
- [CONFIGURATION.md](CONFIGURATION.md) — trading hours and the per-position
  `STRATEGY_EURO_LOSS` cap plus `CANDLE_RETENTION_DAYS`, `CANDLE_DUMP_DIR` and
  `BACKTEST_CONTRACT_FILE`.
