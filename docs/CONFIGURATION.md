# Configuration

All settings are loaded from a `.env` file at the project root via `pydantic-settings`.
Copy `.env.example` to `.env` and fill in your credentials — never commit `.env`.

```bash
cp .env.example .env
```

______________________________________________________________________

## IG API credentials

| Variable        | Required | Description                                      |
| --------------- | -------- | ------------------------------------------------ |
| `IG_ENV`        |          | `demo` (default) or `live`                       |
| `IG_API_KEY`    | ✓        | Your IG API key (Settings → API in your account) |
| `IG_USERNAME`   | ✓        | IG account username                              |
| `IG_PASSWORD`   | ✓        | IG account password                              |
| `IG_ACCOUNT_ID` | ✓        | IG account ID (shown in the web portal)          |

The base URL is derived automatically:

- `demo` → `https://demo-api.ig.com/gateway/deal`
- `live` → `https://api.ig.com/gateway/deal`

______________________________________________________________________

## Database

| Variable       | Default                     | Description                  |
| -------------- | --------------------------- | ---------------------------- |
| `DATABASE_URL` | `sqlite:///./ig_trading.db` | SQLAlchemy connection string |

Switch to PostgreSQL for production:

```env
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/ig_trading
```

______________________________________________________________________

## Web interface

| Variable   | Default   | Description                         |
| ---------- | --------- | ----------------------------------- |
| `WEB_HOST` | `0.0.0.0` | Bind address for the FastAPI server |
| `WEB_PORT` | `8000`    | Listen port                         |

______________________________________________________________________

## Streaming (Lightstreamer)

| Variable                                  | Default   | Description                                         |
| ----------------------------------------- | --------- | --------------------------------------------------- |
| `STREAMING_ENABLED`                       | `true`    | `false` falls back to REST `/prices` polling        |
| `STREAMING_RESOLUTION`                    | `1MINUTE` | Candle resolution for the feed                      |
| `STREAMING_BOOTSTRAP_POINTS`              | `50`      | Points fetched via `/prices` when DB has no history |
| `STREAMING_MAX_EPICS`                     | `40`      | IG hard cap: 40 subscriptions per connection        |
| `STREAMING_REHYDRATE_WINDOW_MINUTES`      | `90`      | Minutes of candle history loaded on startup         |
| `STREAMING_RECONNECT_MAX_BACKOFF_SECONDS` | `60`      | Max reconnect backoff                               |

______________________________________________________________________

## API queue

| Variable                     | Default | Description                                           |
| ---------------------------- | ------- | ----------------------------------------------------- |
| `QUEUE_MAX_ATTEMPTS`         | `3`     | Retry budget for transient errors                     |
| `QUEUE_RETRY_MARGIN_SECONDS` | `5`     | Extra wait added on top of the guard cooldown         |
| `QUEUE_RECENT_SIZE`          | `50`    | Ring buffer size for the dashboard recent-tasks panel |

______________________________________________________________________

## Candle persistence

| Variable                | Default   | Description                                                                                                     |
| ----------------------- | --------- | --------------------------------------------------------------------------------------------------------------- |
| `CANDLE_RETENTION_DAYS` | `7`       | Days of candles kept in DB before per-week CSV archive + deletion                                               |
| `CANDLE_DUMP_DIR`       | `./dumps` | Directory for per-week candle archives (`candles_<year>-W<week>.csv`), read by the [backtester](BACKTESTING.md) |

______________________________________________________________________

## Strategy — Trend Volume Intraday

### Signal quality

| Variable                   | Default | Description                                |
| -------------------------- | ------- | ------------------------------------------ |
| `STRATEGY_MIN_R2`          | `0.70`  | Minimum R² to validate a linear trend      |
| `STRATEGY_MIN_SCORE`       | `0.75`  | Minimum composite score to open a position |
| `STRATEGY_LOOKBACK_POINTS` | `20`    | Candles used for linear regression         |

### Indicators

| Variable                    | Default  | Description                                      |
| --------------------------- | -------- | ------------------------------------------------ |
| `STRATEGY_SMA_FAST`         | `5`      | Fast SMA period                                  |
| `STRATEGY_SMA_SLOW`         | `20`     | Slow SMA period                                  |
| `STRATEGY_ROC_PERIOD`       | `10`     | Rate-of-Change lookback                          |
| `STRATEGY_MAX_SPREAD_RATIO` | `0.0015` | Max relative spread (0.15%) — skip noisy markets |

### Position sizing

| Variable                     | Default    | Description                                  |
| ---------------------------- | ---------- | -------------------------------------------- |
| `STRATEGY_STOP_MULTIPLIER`   | `2.5`      | Stop distance = X × spread                   |
| `STRATEGY_TARGET_MULTIPLIER` | `4.0`      | Take-profit = X × spread                     |
| `STRATEGY_TACTIC`            | `spread`   | `spread`, `percentage`, or `point`           |
| `STRATEGY_CLOSE_TARGET`      | `follower` | `follower`, `win`, `now`, or `zero`          |
| `STRATEGY_COMPENSATE_LOOSE`  | `false`    | Increase size after a loss (not recommended) |
| `STRATEGY_EURO_LOSS`         | `4000.0`   | Max loss per position in EUR                 |

### Risk management

| Variable                    | Default  | Description                                       |
| --------------------------- | -------- | ------------------------------------------------- |
| `STRATEGY_MAX_POSITIONS`    | `6`      | Max simultaneous open positions                   |
| `STRATEGY_MAX_TRADES_DAY`   | `50`     | Stop opening new trades after this count          |
| `STRATEGY_DAILY_LOSS_LIMIT` | `-500.0` | Stop trading if daily P&L drops below this (€)    |
| `STRATEGY_DAILY_WIN_TARGET` | `300.0`  | Stop trading if daily P&L reaches this (€)        |
| `STRATEGY_MIN_WIN_RATE`     | `0.40`   | Stop if win rate drops below 40% after 10+ trades |

### Trading hours (local server time)

| Variable              | Default | Description                          |
| --------------------- | ------- | ------------------------------------ |
| `STRATEGY_HOUR_START` | `9`     | No positions opened before this hour |
| `STRATEGY_HOUR_END`   | `16`    | No new positions after this hour     |
| `STRATEGY_HOUR_CLOSE` | `17`    | Force-close all positions at HH:30   |

______________________________________________________________________

## Market scanner

| Variable               | Default           | Description                                   |
| ---------------------- | ----------------- | --------------------------------------------- |
| `SCANNER_SEARCH_TERMS` | *(see config.py)* | JSON array of search terms for epic discovery |

Override example in `.env`:

```env
SCANNER_SEARCH_TERMS='["Germany 40","EUR/USD","Gold"]'
```
