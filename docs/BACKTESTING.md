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
([gates.py](../src/execution/gates.py) — including the spread budget and the
same-bet gate, which refuses a candidate whose curve is the same bet as a
position already open), the win/stop levels, and the per-zone stop updates. A market order fills on the side that pays the spread, the
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
| `CLOSE_ZONESTART` | `basket`       | closes the whole book from the same pre-pass — a replay would never close the basket                                   |

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

## Research tools

A backtest answers "what did this strategy earn?". Two offline tools answer the
questions that come before it. Both read the archive only — no DB, no IG call,
safe while the bot runs — and are tracked in [TODO.md](TODO.md).

### Random-direction baseline (`make baseline`)

[random_baseline.py](../src/backtest/random_baseline.py) replays a strategy once
for real and `--seeds` times with every open's **side drawn by a coin flip** —
same epics, same minutes, same ranking, same close profile, same gates. The flip
is a hash of `(seed, epic, minute)`, so a seed reproduces exactly and a ranker
re-evaluating an epic gets the same side every time. A strategy has direction
skill only when its mean R sits in the upper tail of those coin-flip replays.

```bash
make baseline ARGS="--strategy open_fade --seeds 20"
make baseline ARGS="--all --seeds 20 --json baseline.json"   # every strategy
```

The report gives, per strategy, the real trade count / mean R / euros, the null
median and 95th percentile, `z` (distance from chance in null standard
deviations), `p(z)` (its normal p-value, compared with the Bonferroni threshold)
and the empirical `p` (share of coin flips at least as good, floored at
`1 / (seeds + 1)` — which is why `p(z)` carries the decision). Each replay is a
full backtest; they run in parallel worker processes (`--workers`, default
CPU − 1), so expect roughly *(seeds + 1) × strategies ÷ workers* backtest
durations.

### Research dataset (`make research`)

[research.py](../src/backtest/research.py) samples every epic every `--step`
minutes (07:00–17:00 UTC) and writes one row per moment to
`<CANDLE_DUMP_DIR>/research/dataset.csv`:

- **features**, read on the same day's candles up to that minute only (no
  look-ahead): `trend60_pct`, `r2_60`, `chan60`, `er60`, `roc30_atr`,
  `atr_spread` (ATR ÷ spread — movement per unit of cost), `vol_ratio`,
  `day_move_atr`, `day_pos`, `hour_paris`, `weekday`, `asset_class`;
- **outcomes** `r_buy` / `r_sell`: the R a BUY and a SELL opened right then would
  have paid, net of the spread, under a fixed bracket (default stop 2.5 ATR,
  target 5 ATR, 120-minute horizon, marked to market at the 17:00 UTC
  force-close; a candle touching both levels counts as a stop).

```bash
make research ARGS="build"                       # whole archive (~6 s per week)
make research ARGS="build --stop-atr 2.5 --target-atr 5 --horizon 120 --step 5"
make research ARGS="analyze"                     # train 60 % / test 40 % of the days
make research ARGS="analyze --test-from 2026-10-05"   # forward check of a frozen rule
```

`analyze` cuts the **days** in time order, buckets every numeric feature into
deciles on the train half only (categorical ones by value) and prints the
buckets whose mean R is positive on **both** halves, with a day-clustered
bootstrap p-value on the test half, flagged when below the Bonferroni threshold
over every bucket × side tested. The unfiltered BUY / SELL mean is printed first:
it is what any filter has to beat.

### Scale and cost (`--timeframe`, `--overnight`, `markets`)

On the 1-minute scale the median spread is about one ATR, so a 2.5-ATR stop pays
roughly `1 / 3.5` of its risk at entry — exactly the −0.29 R a random walk loses
(see [TODO.md](TODO.md)). Three knobs move the bracket away from that cost:

| Option          | Effect                                                                                                        |
| --------------- | ------------------------------------------------------------------------------------------------------------- |
| `--timeframe N` | size the stop/target on the ATR of `N`-minute bars (noise grows ≈ √N, the spread does not)                    |
| `--horizon M`   | hold for up to `M` **wall-clock** minutes (a feed gap is not walked through as trading time)                  |
| `--overnight`   | no 17:00 UTC force-close: the trade is held across nights                                                     |
| `--funding F`   | with `--overnight`, IG's overnight funding: `F / 365` of the price per night held (annual, e.g. `0.04` = 4 %) |

The new `tf_atr_spread` feature is the bracket's ATR ÷ spread (equal to
`atr_spread` on the 1-minute scale), and `markets` ranks the epics by its median
on the **train** half, then measures the cheapest 10 / 25 / 50 % on the **test**
half against the rest:

```bash
make research ARGS="build --timeframe 15 --horizon 600 --step 15 --path dumps/research/tf15.csv"
make research ARGS="build --timeframe 60 --overnight --funding 0.04 --horizon 4320 --step 60 --path dumps/research/tf60n.csv"
make research ARGS="markets --path dumps/research/tf60n.csv"
make research ARGS="analyze --path dumps/research/tf60n.csv"
```

The replay engine itself still force-closes every position at the end of the
day; a multi-day result here is a reason to lift that, not a backtest of it.

### A strategy's signal on the dataset (`--strategy`, `signal`)

`build --strategy NAME` stamps every row with the side that entry strategy
would have proposed at that minute, read on a live-sized one-minute buffer
(200 candles, reset each UTC day) — the strategy's own signal, whatever
`--timeframe` the outcome is measured on. `signal` then scores the signalled rows
on both halves: **signal R** (following it), **coin R** (a coin flip on the same
rows: the mean of the BUY and SELL outcomes, so the drift and the cost are the
same) and **excess** = signal − coin, the direction skill, each with a
day-clustered p-value — also restricted to the cheapest markets (`--top`).

```bash
make research ARGS="build --timeframe 15 --horizon 600 --step 5 --strategy open_fade --path dumps/research/fade15.csv"
make research ARGS="signal --path dumps/research/fade15.csv --top 0.25 0.5 --first"
```

By default every row where the signal holds is counted (overlapping minutes of
one episode included); `--first` keeps only the first signalled row of each epic
and day, the live one-open-per-epic-per-day.

### Forward validation

A bucket that passes `analyze` was still *chosen* on the archive. It becomes a
rule only once it holds on data that did not exist when it was chosen:

1. **Freeze** the rule (feature, bucket, side, bracket) and the date `D` it was
   chosen — write them in [TODO.md](TODO.md). No re-tuning afterwards.
1. **Let new weeks accumulate.** The retention dump archives each week about 7
   days after it ends; *Snapshot DB now* on `/backtest` adds the recent days.
1. **Re-score** weekly with `make research ARGS="build"` then
   `ARGS="analyze --test-from D"`: the train half (and every decile edge) stays
   the pre-`D` archive, only post-`D` days are scored.
1. **Decide** on a pre-set criterion — the frozen bucket's test mean R > 0 with a
   day-clustered `p` below 0.05 over enough days (see the sizing below), not on
   whichever bucket looks best that week.
1. **Then live, in notify mode**: implement the rule as an entry module, run it
   with `OPEN_MODE=notify` on the demo account and compare the signalled fills
   (spread, slippage) with the replay before any `auto` opening.

Sizing: one trade's R has a standard deviation of about 1.3 R here, so a true
edge of `e` R needs about `(1.645 × 1.3 / e)²` independent trades to show at
`p < 0.05` — ~450 trades for +0.1 R, ~110 for +0.2 R. Samples of one day are not
independent: count days, not rows.

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
