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
| `http://localhost:8000/positions/`              | JSON: position list (all or filtered)             |
| `http://localhost:8000/positions/summary`       | JSON: today's P&L summary                         |
| `http://localhost:8000/docs`                    | Auto-generated Swagger UI                         |

### Live updates

The dashboard no longer reloads the whole page. Every **2 seconds** it issues a
single request to `/api/dashboard-fragments` and swaps **only the regions whose
content changed** (KPI bar, market table, queue / IG API / positions modals).
Each section shows its own "last refresh" time, and the **Pause** button (or
`localStorage` key `ig_refresh_paused`) freezes the live updates without
stopping the bot. Pausing the **bot** (the Bot KPI tile) is independent and
suspends the scheduled API jobs instead.

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
STRATEGY_MIN_R2=0.70          # Minimum R² to validate a trend
STRATEGY_MIN_SCORE=0.75       # Minimum composite score to open a position
STRATEGY_LOOKBACK_POINTS=20   # Candles used for linear regression
STRATEGY_SMA_FAST=5           # Fast SMA period
STRATEGY_SMA_SLOW=20          # Slow SMA period
STRATEGY_STOP_MULTIPLIER=2.5  # Stop = X × spread
STRATEGY_TARGET_MULTIPLIER=4.0 # Take-profit = X × spread
STRATEGY_MAX_POSITIONS=6      # Max simultaneous open positions
STRATEGY_DAILY_LOSS_LIMIT=-500  # Stop trading below this daily P&L (€)
STRATEGY_DAILY_WIN_TARGET=300   # Stop trading above this daily P&L (€)
STRATEGY_HOUR_START=9         # No positions before this hour
STRATEGY_HOUR_END=16          # No new positions after this hour
STRATEGY_HOUR_CLOSE=17        # Force-close all positions at HH:30
STRATEGY_CLOSE_TARGET=follower  # follower | win | now | zero
```

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

09:00   Trading starts (STRATEGY_HOUR_START=9)
        → prices every 30 seconds
        → positions open when score > 0.75 and R² > 0.70

09:00–16:00   Active window
        → monitor via http://localhost:8000/positions/summary

16:00   No new positions (STRATEGY_HOUR_END=16)

17:30   Force-close all open positions

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
