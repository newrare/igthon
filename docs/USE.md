# Usage Guide

## Prerequisites

```bash
cd igthon/
source .venv/bin/activate
```

Make sure `.env` is configured:

```bash
cp .env.example .env
# Edit .env — set IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACCOUNT_ID
```

______________________________________________________________________

## 1. First-time setup

### Create the database

```bash
alembic upgrade head
```

Creates `ig_trading.db` (SQLite) with tables: `position`, `epic`, `day`, `resume`, `candle`.

### Verify the API connection

```bash
python -m src.main --analyze-only --log-level DEBUG
```

If the connection works you'll see prices and indicator scores for each tracked epic.

______________________________________________________________________

## 2. Running modes

### Analysis only (no trading)

Fetches live prices, computes all indicators, prints a summary table, then exits.
No orders are placed — safe to run at any time.

```bash
python -m src.main --analyze-only
```

Example output:

```
Epic                                Score    Dir     R²    ROC  Spread
---------------------------------------------------------------------------
  IX.D.DAX.IFMM.IP                  0.81    BUY   0.88   0.04   12.00
  IX.D.FTSE.DAILY.IP                0.43  NEUTRAL  0.55  -0.01    5.00
  CS.D.EURUSD.TODAY.IP              0.22  NEUTRAL  0.31   0.00    0.00
```

Analyse a specific set of epics:

```bash
python -m src.main --analyze-only --epics IX.D.DAX.IFMM.IP CS.D.EURUSD.TODAY.IP
```

### Bot only (no web UI)

Starts the full scheduler. The bot will:

- fetch prices every 30 seconds (via Lightstreamer or REST fallback)
- open positions when a BUY signal is triggered
- monitor open positions and apply trailing stop
- force-close all positions at 17:30
- write daily summary at 18:00

```bash
python -m src.main
```

### Bot + web dashboard

Same as above, plus a web interface at [http://localhost:8000](http://localhost:8000).

```bash
python -m src.main --web
```

### Debug mode

```bash
python -m src.main --analyze-only --log-level DEBUG
python -m src.main --web --log-level DEBUG
```

______________________________________________________________________

## 3. Web dashboard

Start: `python -m src.main --web`

### Available endpoints

| URL                                             | Description                                       |
| ----------------------------------------------- | ------------------------------------------------- |
| `http://localhost:8000/`                        | Dashboard: live prices and buffer status per epic |
| `http://localhost:8000/api/dashboard-fragments` | JSON: HTML fragments for every live region (poll) |
| `http://localhost:8000/api/status`              | JSON: bot status, tracked epics                   |
| `http://localhost:8000/api/prices/{epic}`       | JSON: last 50 candles for an epic                 |
| `http://localhost:8000/api/reading/{epic}`      | JSON: full market reading of an epic (panel data) |
| `http://localhost:8000/api/catalog`             | JSON: market-catalog size + crawl progress        |
| `http://localhost:8000/positions/`              | JSON: position list (all or filtered)             |
| `http://localhost:8000/positions/summary`       | JSON: today's P&L summary                         |
| `http://localhost:8000/docs`                    | Auto-generated Swagger UI                         |

### Market catalog (full instrument list)

The daily epic discovery only sees what `SCANNER_SEARCH_TERMS` matches. The
**complete** list of instruments IG offers is built separately by walking IG's
market-navigation tree into the `market_catalog` table — a static reference list
that the daily discovery never prunes.

Launch it from the dashboard: **Actions → Crawl Market Catalog** (manual only, no
schedule), or from the terminal:

```bash
# Crawl / resume (goes through the API queue at background priority)
python -m src.scripts.crawl_catalog

# Where does it stand? (no IG session, no API call)
python -m src.scripts.crawl_catalog --status

# Bounded run, or re-walk the whole tree from the root
python -m src.scripts.crawl_catalog --max-nodes 200
python -m src.scripts.crawl_catalog --reset

# Prune whole subtrees by node name (repeatable)
python -m src.scripts.crawl_catalog --exclude Options --exclude Bonds
```

**The crawl is resumable, so hitting the API quota is harmless.** Every node is
persisted as `pending` before it is visited and flipped to `done` in the same
transaction that stores its children and markets:

- quota block, HTTP 5xx, network drop, Ctrl-C → the node stays `pending`, the run
  stops there, and the **next run starts on that exact node**. Nothing already
  fetched is fetched again;
- a node IG rejects for good (4xx) is marked `error` and skipped, so one bad
  branch can never block the catalog;
- markets are upserted, never deleted — `last_seen_at` distinguishes a market IG
  has dropped from a current one.

Practically: launch it in the evening and re-launch it (button or command) until
it reports `complete`. Check progress any time with `--status` or
`GET /api/catalog`:

```
catalog running: 4211 markets · 118/126 nodes done, 8 pending, 0 error
```

Beware: the CLI opens its own IG session (IG allows a limited number of
concurrent ones), so prefer it while the bot is stopped — otherwise use the
dashboard button, which reuses the bot's session and its queue.

### Chart modal

Clicking any row (Market Data, Open / Closed Positions) opens the whole-day curve
for that epic. The header names the **market** (the instrument description, not
the epic code — that one sits discreetly under the curve) and carries the actions:
`Buy` / `Sell`, the exits below, and `Reading`.

Overlaid on the top-left of the chart:

- the running (`en cours`) or realised (`validé`) **P&L** per trade, prefixed by
  its BUY / SELL chip;
- the euro **spread** — what one round trip at minimum size pays away;
- the **regime** (range, trend up, …) followed by one **trend arrow per horizon**,
  most recent first and biggest first: 5, 15, 30 then 60 minutes;
- a **noise / spread dial** — two concentric discs on one scale, the market's
  adverse tick-noise band against the dealing spread, with their ratio. Above ×1
  the tick noise alone is wider than the spread, so a stop placed one spread away
  can be tripped by noise only;
- the **queued close**, when one is armed, with a `✕` to cancel it.

Three exits, all only while the epic holds an open position:

| Button       | Effect                                                                                                                                                    |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Close ±x €` | Closes now at market price. Shown in the red too — cutting a loss by hand is as valid as banking a gain; the label carries the P&L that will be realised. |
| `⏳ ≥ 0 €`   | Queues the close: exit **as soon as the running P&L turns positive**.                                                                                     |
| `⏳ ≥ x €`   | Same order with a threshold you type — e.g. `-20` for "exit as soon as it is better than -20 €".                                                          |

A queued close is a standing order stored on the position
(`close_when_euro_above`), so it survives a restart. It is evaluated on every
monitoring pass, before the close profile decides anything, on the same close-out
price the badge shows (the bid for a long, the offer for a short) and strictly
above the threshold — arming `0` never closes a flat position at break-even.
Closes it fires are stamped `reason_close = euro_target` ("Euro target" in the
tables). Nothing is sent to IG when arming: only the close itself is an order.

The chart refreshes every 3 seconds, freezes while you zoom (double-click to
resume) and the `‹` `›` arrows browse the rows of the table it was opened from.

### Market reading panel

The **gauge icon** opens the read-only reading of a market: regime, trend per
horizon (5 / 15 / 30 / 60 min) with its chart angle, candle pressure, the four
noise measures, readable supports / resistances, the round numbers around the
price — and, when a position is open on the epic, everything measured since the
open (gain, best/worst excursion, give-back, stop room). It refreshes every 5
seconds while open and closes with `Escape`.

It is reachable from three places:

- the **Market Data** table — one icon per epic, next to Buy / Sell;
- the **Open Positions** modal — next to Close, where the since-open section is;
- any **chart** — the *Reading* button in the chart modal header, and on the
  standalone `/charts/{epic}` page.

Data comes from `/api/reading/{epic}`, which only reads the in-memory buffer and
one position row: no IG API call, so opening the panel never costs quota. See
[READING.md](READING.md) for what each measure means.

### Live updates

The dashboard no longer reloads the whole page. Every **2 seconds** it issues a
single request to `/api/dashboard-fragments` and swaps **only the regions whose
content changed** (KPI bar, market table, queue / IG API / positions modals).
Each section shows its own "last refresh" time, and the **Pause** button (or
`localStorage` key `ig_refresh_paused`) freezes the live updates without
stopping the bot. Pausing the **bot** (the Bot KPI tile) is independent and
suspends the scheduled API jobs instead.

### Auto-open switch (authorise / block new openings)

The banner at the top of the dashboard is the master switch for **automatic
openings**. Click **Block for today** once you consider the trading day over:

- the bot opens **nothing** more on its own — BUY or SELL, whatever
  `OPEN_STRATEGY` is selected in `.env` (per-epic entries and cross-epic rankers
  alike are refused with `Auto-open disabled for today`);
- **positions already open keep living normally**: monitoring, stop ratchets,
  targets and closes are untouched;
- the manual **Buy / Sell** buttons keep working — the switch stops the bot, not
  the user.

The block is scoped to the **current day**: it survives a server restart (stored
in the `job_preference` table under the reserved `auto_open` key) but expires on
its own at the next calendar day, so a "day is over" decision never silently
carries into the next session. **Allow openings** lifts it immediately.

| URL                                       | Method | Effect                              |
| ----------------------------------------- | ------ | ----------------------------------- |
| `http://localhost:8000/api/auto-open/off` | POST   | Block every automatic opening today |
| `http://localhost:8000/api/auto-open/on`  | POST   | Authorise automatic openings again  |

`/api/status` and the fragments poll both report the current state as
`auto_open`.

### Position endpoint filters

```bash
# All positions
curl http://localhost:8000/positions/

# Open positions only
curl "http://localhost:8000/positions/?state=open"

# Closed positions for a specific epic
curl "http://localhost:8000/positions/?state=close&epic=IX.D.DAX.IFMM.IP"

# Last 10 positions
curl "http://localhost:8000/positions/?limit=10"

# Today's P&L summary
curl http://localhost:8000/positions/summary
```

______________________________________________________________________

## 4. Monitoring

### Live log output

```
2026-06-01 09:15:00 [INFO]  ig_bot — BUY signal: IX.D.DAX.IFMM.IP (score=0.81, R²=0.88)
2026-06-01 09:15:01 [INFO]  ig_bot — Opening position: epic=IX.D.DAX.IFMM.IP, qty=1, stop=45, risk=12.50€
2026-06-01 09:15:02 [INFO]  ig_bot — Position opened: epic=IX.D.DAX.IFMM.IP, deal=REF123, level=18450.0
2026-06-01 09:45:15 [INFO]  ig_bot — Trailing stop updated for IX.D.DAX.IFMM.IP: 18465.000
2026-06-01 10:02:45 [INFO]  ig_bot — Position closed: epic=IX.D.DAX.IFMM.IP, reason=win, P&L=32.50€
2026-06-01 18:00:00 [INFO]  ig_bot — Daily summary: 8 trades, P&L=87.20€
```

### Save logs to a file

```bash
python -m src.main --web 2>&1 | tee bot_$(date +%Y%m%d).log
```

### Direct database queries

```bash
# Open SQLite shell
sqlite3 ig_trading.db

# Open positions
SELECT epic, level_open, level_win, level_loose, time_open
FROM position WHERE state='open';

# Today's closed positions with P&L
SELECT epic, euro, reason_close, time_open, time_close
FROM position WHERE date=date('now') AND state='close';

# Total P&L by day
SELECT date, COUNT(*) as trades, SUM(euro) as pnl
FROM position WHERE state='close'
GROUP BY date ORDER BY date DESC;

# Win rate by epic
SELECT epic, COUNT(*) total, SUM(CASE WHEN euro>0 THEN 1 ELSE 0 END) wins,
       ROUND(100.0*SUM(CASE WHEN euro>0 THEN 1 ELSE 0 END)/COUNT(*),1) win_pct
FROM position WHERE state='close' GROUP BY epic;
```

______________________________________________________________________

## 5. Running tests

```bash
# All tests
python -m pytest tests/ -v

# Quick run
python -m pytest tests/ -q

# Single file
python -m pytest tests/test_compute.py -v

# With coverage
python -m pytest tests/ --cov=src --cov-report=term-missing
```

______________________________________________________________________

## 6. Database migrations

```bash
# Apply pending migrations
alembic upgrade head

# Generate a migration from model changes
alembic revision --autogenerate -m "Add column to position"

# Roll back one step
alembic downgrade -1

# Check current state
alembic current
alembic history --verbose
```

______________________________________________________________________

## 7. Strategy parameters (`.env`)

All thresholds are configurable without code changes. See [configuration.md](configuration.md)
for the full reference.

Key settings:

```env
# Only the selection names live in the environment; every parameter is a
# constant on its entry/stop/exit class (src/entry/, src/stops/, src/exit/).
# These are the single source of truth and are REQUIRED — there is no default
# and no runtime switching; the bot refuses to start if any is missing or
# unknown. The exit is split into four independently-selected zones
# (follower→break-even, break-even→margin, margin→profit trigger, above it).
OPEN_STRATEGY=open_projection
STOP_STRATEGY=stop_support
CLOSE_ZONESTART=hold
CLOSE_ZONEMARGE=hold
CLOSE_ZONESECURE=hold
CLOSE_ZONEPROFIT=trailing_ratchet

# Global open policy (also REQUIRED, applies to every entry strategy):
# false = one opening per epic per day (BUY or SELL), no re-open after a close;
# true  = several openings per epic per day, as soon as the epic is flat again.
# Two concurrent positions on one epic are always refused; a manual dashboard
# open bypasses the policy.
ALLOW_SAME_DAY_REOPEN=false

# Recovery revert (also REQUIRED, applies to every entry strategy):
# true  = when a trade (BUY or SELL) is stopped out at a LOSS on the stop placed
#         at its open, immediately open the opposite side on the same epic to
#         follow the market that turned. One hop only — a stopped-out revert is
#         never reverted back.
# false = a stop-out is just a closed trade; the next open waits for a signal.
ALLOW_RECOVERY_REVERT=true
```

There is no trading-hours gate: an epic is opened whenever its live market
status is TRADEABLE, and each position is closed just before its own market
close (per-epic `Epic.market_close_utc`).

______________________________________________________________________

## 8. Tracked epics

Defaults in [src/main.py](../src/main.py):

```python
DEFAULT_EPICS = [
    "IX.D.DAX.IFMM.IP",       # DAX 40 (1€/point mini)
    "IX.D.FTSE.DAILY.IP",     # FTSE 100
    "IX.D.CAC.IDF.IP",        # CAC 40
    "CS.D.EURUSD.TODAY.IP",   # EUR/USD
    "CS.D.GBPUSD.TODAY.IP",   # GBP/USD
]
```

Override without editing code:

```bash
python -m src.main --epics IX.D.DAX.IFMM.IP IX.D.CAC.IDF.IP
```

______________________________________________________________________

## 9. Typical daily workflow

```
08:50   python -m src.main --web
        → subscribe to the tradeable epics via Lightstreamer

During market hours
        → an epic is opened whenever its live status is TRADEABLE
        → monitor via http://localhost:8000/positions/summary

Per epic, just before its own market close
        → the open position on that epic is force-closed

18:00   Daily summary → http://localhost:8000/positions/summary
```

______________________________________________________________________

## 10. Switching to production (PostgreSQL)

```env
# .env
IG_ENV=live
DATABASE_URL=postgresql+asyncpg://user:password@server:5432/ig_trading
```

Then run migrations on the production DB:

```bash
alembic upgrade head
```

No code changes required — the app detects the driver from the URL.
