# Usage Guide

## Prerequisites

Every command in this guide has a `make` shortcut — `make help` lists them all,
grouped, with the dashboard port currently configured in `.env`. The raw commands
are kept alongside so nothing is hidden. The `make` targets use `.venv/bin/python`
when the virtual environment exists, so activating it is optional.

```bash
cd igthon/
make help
```

Make sure `.env` is configured (`make env` creates it from the example, never
overwriting an existing one):

```bash
cp .env.example .env
# Edit .env — set IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACCOUNT_ID
```

______________________________________________________________________

## 1. First-time setup

Everything at once — virtual environment, dependencies, `.env`, database:

```bash
make setup
```

### Create the database

```bash
make migrate          # alembic upgrade head, then alembic current
```

Creates `ig_trading.db` (SQLite) with tables: `position`, `epic`, `day`, `resume`, `candle`.

### Verify the API connection

```bash
make run-debug        # python -m src.main --web --log-level DEBUG
```

The login, the epic selection and every IG call are printed on the console. If
the credentials are wrong the bot keeps the dashboard up and retries the
connection every 30 s, so the error is visible in the log rather than fatal.

______________________________________________________________________

## 2. Running modes

| Command          | Raw equivalent                               | Effect                                        |
| ---------------- | -------------------------------------------- | --------------------------------------------- |
| `make run`       | `python -m src.main --web`                   | Bot + dashboard on `WEB_PORT` (the usual one) |
| `make bot`       | `python -m src.main`                         | Scheduler only, no web UI                     |
| `make run-debug` | `python -m src.main --web --log-level DEBUG` | Same as `run`, every API call printed         |
| `make status`    | —                                            | Is a bot running? PID and start time          |
| `make stop`      | —                                            | Stop it and free the port                     |
| `make restart`   | —                                            | `stop`, then `run`                            |
| `make open`      | —                                            | Open the dashboard in the browser             |

### Bot + web dashboard

Starts the full scheduler and the web interface. The bot will:

- receive live prices from the Lightstreamer feed (REST fallback when it drops)
- open positions when the entry strategy validates a signal — or record it as a
  dashboard notification when `OPEN_MODE=notify`
- monitor open positions and ratchet the stop through the four exit zones
- write the daily summary at the end of the session

```bash
make run
# → http://localhost:9000  (WEB_PORT in .env)
```

On macOS `make run` prefixes the command with `caffeinate -i -s`, so the machine
does not fall asleep while positions are open.

### Bot only (no web UI)

```bash
make bot
```

### Only one bot at a time

`run`, `run-debug` and `bot` refuse to start when another bot process is alive:
two schedulers race on the per-epic open lock and can double-open a position.

```bash
make status    # who is running
make stop      # stop it
make restart   # stop then start again (e.g. to load new code)
```

Without the guard the second start fails deeper, on
`[Errno 48] address already in use`.

### Debug mode

```bash
make run-debug
make bot ARGS="--log-level DEBUG"
```

______________________________________________________________________

## 3. Web dashboard

Start: `make run` (`python -m src.main --web`)

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
make crawl                                    # python -m src.scripts.crawl_catalog

# Where does it stand? (no IG session, no API call)
make crawl-status                             # …crawl_catalog --status

# Bounded run, or re-walk the whole tree from the root
make crawl ARGS="--max-nodes 200"
make crawl ARGS="--reset"

# Prune whole subtrees by node name (repeatable)
make crawl ARGS="--exclude Options --exclude Bonds"
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

### Market Data table

One row per tracked epic, refreshed by the live poll. Every column is read from
the in-memory buffer or a background-refreshed cache, so browsing the table
never costs IG quota.

| Column        | Reads                                                                                                      |
| ------------- | ---------------------------------------------------------------------------------------------------------- |
| `Spread (€)`  | Euro cost of crossing the spread once at minimum deal size                                                 |
| `Momentum`    | How far along its current momentum leg the market is — `Start` / `Mid` / `End` with the leg's direction    |
| `Regime`      | Market state over the last 30 min: trend up/down, range (contained) or volatile (big swings going nowhere) |
| `Bid % range` | Where the bid sits in the buffer's own high/low range                                                      |
| `Dots`        | Candles buffered for the epic                                                                              |

`Momentum` is a three-segment gauge — `▪▫▫` start, `▪▪▫` middle, `▪▪▪` end,
`▫▫▫` when there is no leg at all — and it is the **age** of the move, never a
forecast: `End` says the leg has already given back more than half of what it
travelled, not that the price is about to turn. It is deliberately independent of
`Regime`: a young leg inside a market still labelled `Range` is exactly the case
the regime alone cannot report. Both columns are sortable (`Momentum` by how far
along the move is, not alphabetically), and hovering either states what earned
the value. The full derivation is in [READING.md](READING.md#move-phase).

The raw `High / Low` pair these two replaced is gone: it only ever fed the
`Bid % range` bar, which says the same thing in a readable way.

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

### Entry signals (notify mode)

With `OPEN_MODE=notify` in `.env`, the bot **never opens by itself**: every entry
intent that passes the pre-open gates is handed to you instead of being ordered.

- the **Entry Signals — Your Call** section lists each pending proposal —
  direction, epic, instrument name, the strategy and its score, the close-out
  price when the signal was raised and how old it is;
- the browser raises a **desktop notification** per new signal (click
  **Notifications** in the section header once to grant permission — the browser
  only asks on a click). Clicking the notification focuses the dashboard and
  opens the chart on that epic. A toast is always shown as well, so a refused
  permission never means a missed signal;
- the page plays its own **chime** — two notes, rising for a BUY, falling for a
  SELL — so the side is audible without looking. It is the page's sound, not the
  operating system's: a web notification is silent unless macOS is configured to
  play a sound for the browser, so the desktop notification is deliberately
  created `silent` and the chime is the single, guaranteed sound. It rings even
  with the permission refused and with the tab in the background. **Sound
  on/off** in the section header mutes it (remembered per browser); turning it
  back on plays it once as a check. Browsers only allow audio after a click, so
  the chime is armed by the first click anywhere on the dashboard;
- the **tab itself** carries the count: while the dashboard sits in another tab
  or another window, its title reads `(2) IG Trading Bot` and the favicon shows a
  red badge. This is the one channel no permission can switch off — it works with
  notifications refused and the sound muted. It clears when you come back to the
  tab and returns if you leave again with signals still pending;
- delivery does **not** depend on the 1 s poll. A browser throttles a hidden
  tab's timers to roughly one tick a minute — exactly the case this feature
  exists for — so the page also holds an open **server-sent-events** connection
  (`/api/signals/stream`), which browsers do not throttle: a signal reaches the
  page the second it is raised, whatever window you are in. The poll stays as the
  fallback, and both paths ignore a signal already alerted on;
- the section header states what is actually armed (notification permission,
  sound, stream) and a **Test** button fires the whole chain on a fake signal, so
  a silent dashboard is never mistaken for a quiet market;
- each card offers **Chart** (the curve), **Read** (regime, trends, noise,
  levels), **Buy** / **Sell** (a manual open at minimum size — same sizing, same
  `STOP_STRATEGY`, same close zones as an automatic open) and **✕** to pass.

A signal is only raised **after** the gates, so nothing notifies you about a
market that could not be opened anyway. Signals are in-memory: they expire after
15 minutes, the same epic never signals twice within 10 minutes, opening the epic
yourself clears its signal, and blocking auto-open or ending the day clears them
all.

| URL                                           | Method | Effect                                                       |
| --------------------------------------------- | ------ | ------------------------------------------------------------ |
| `http://localhost:8000/api/signals`           | GET    | List the pending signals                                     |
| `http://localhost:8000/api/signals/stream`    | GET    | Server-sent events: the pending list, pushed on every change |
| `http://localhost:8000/api/signals/7/dismiss` | POST   | Dismiss signal `7`                                           |

The stream is what drives the alerts; the fragments poll carries the same list
under `signals` as a fallback.

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

### Follow the log files

The bot always writes rotating logs under `/tmp/ig_bot/`, whatever the console
shows:

```bash
make logs          # tail -f /tmp/ig_bot/ig_bot.log        (everything, DEBUG)
make logs-api      # tail -f /tmp/ig_bot/ig_api_calls.log  (every IG call)
make logs-errors   # tail -f /tmp/ig_bot/ig_api_errors.log (rejected calls)
```

Keeping a copy of one session's console output:

```bash
make run 2>&1 | tee bot_$(date +%Y%m%d).log
```

### Direct database queries

```bash
# Open SQLite shell — make db-shell
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
# All tests (reformats the Markdown first, as the commit rules require)
make test

# Any pytest flag, through ARGS
make test ARGS="-v"
make test ARGS="tests/test_signals.py -v"
make test ARGS="-k trailing"

# With coverage
make test-cov

# Lint + tests, the pre-commit gate
make check
```

______________________________________________________________________

## 6. Database migrations

```bash
# Apply pending migrations (prints the resulting revision)
make migrate

# Generate a migration from model changes
make revision M="add my_column to position"

# Roll back one step
make downgrade

# Check current state
make db-current
make db-history
```

A new or altered model column means writing the revision **and** running
`make migrate` straight away: the running app queries every mapped column, so an
unapplied revision turns the next request into `no such column: …` and a 500 on
the dashboard.

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

This list is only the seed handed to the scheduler at start-up: the watched set
is then rebuilt by the daily discovery (`markets/market_scanner.py` +
`markets/risk_tiers.py`), driven by the `.env` settings — see
[CONFIGURATION.md](CONFIGURATION.md). There is no CLI flag to override it.

______________________________________________________________________

## 9. Typical daily workflow

```
08:50   make run
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
