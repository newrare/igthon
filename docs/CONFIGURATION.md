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

| Variable                 | Default                        | Description                                                                                                                                                                                |
| ------------------------ | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `CANDLE_RETENTION_DAYS`  | `7`                            | Days of candles kept in DB before per-week CSV archive + deletion                                                                                                                          |
| `CANDLE_DUMP_DIR`        | `./dumps`                      | Directory for per-week candle archives (`candles_<year>-W<week>.csv`), read by the [backtester](BACKTESTING.md)                                                                            |
| `BACKTEST_CONTRACT_FILE` | `./config/euro_per_point.json` | `epic → € per point` table giving the [backtester](BACKTESTING.md) its euro P&L. Filled once by `python -m src.scripts.dump_euro_per_point`; a missing file only disables the euro figures |

______________________________________________________________________

## Strategy — Trend Volume Intraday

### Signal quality

| Variable                   | Default | Description                                |
| -------------------------- | ------- | ------------------------------------------ |
| `STRATEGY_MIN_R2`          | `0.70`  | Minimum R² to validate a linear trend      |
| `STRATEGY_MIN_SCORE`       | `0.75`  | Minimum composite score to open a position |
| `STRATEGY_LOOKBACK_POINTS` | `20`    | Candles used for linear regression         |

### Indicators

| Variable              | Default | Description             |
| --------------------- | ------- | ----------------------- |
| `STRATEGY_SMA_FAST`   | `5`     | Fast SMA period         |
| `STRATEGY_SMA_SLOW`   | `20`    | Slow SMA period         |
| `STRATEGY_ROC_PERIOD` | `10`    | Rate-of-Change lookback |

The market-scanner spread gate is no longer an env variable — it is a class
constant (`MarketScanner.DEFAULT_MAX_SPREAD_RATIO`), tuned in
`src/markets/market_scanner.py`.

`SCANNER_SEARCH_TERMS` caps what the daily discovery can ever see: it only
matches instrument names, and `GET /markets?searchTerm=` has no pagination (IG
truncates the answer). The exhaustive list of what the broker offers is built
separately, from IG's navigation tree, into the `market_catalog` table — see
[USE.md § Market catalog](USE.md#market-catalog-full-instrument-list). No env
variable drives it: it is a manual action.

### Watched-epic selection (risk tiers)

The scanner discovers ~140 epics; IG's Lightstreamer connection only accepts
`STREAMING_MAX_EPICS` (40) subscriptions, and an epic that is not watched can
never be opened. Which 40 are kept is a **profit-funded ladder** on the euro cost
of being wrong (`MarketInfo.stop_loss_eur`, the risk column of the Epic List) —
see `src/markets/risk_tiers.py`.

| Variable                  | Default              | Description                                             |
| ------------------------- | -------------------- | ------------------------------------------------------- |
| `EPIC_RISK_TIERS_ENABLED` | `true`               | `false` restores the plain class-diversified pick       |
| `EPIC_RISK_TIER_BOUNDS`   | `[0,30,100,200,600]` | Ascending euro boundaries; N bounds describe N−1 tiers  |
| `EPIC_FOLLOW_CLOSED_MAX`  | `10`                 | Slots kept for markets already traded today (`0` = off) |

The default bounds read as four tiers — `0-30`, `30-100`, `100-200`,
`200-600 €`. Tier *i* unlocks once the day's **realised** P&L (closed positions
only) is strictly above its floor, so the day starts on the smallest markets and
only moves up with money already in the bank. The tier is recomputed on every
analysis tick, never latched: giving the gains back walks the watched list
straight back down to the small markets, within 30 seconds and without any extra
API call (the pick is replayed from the last `/markets` snapshot).

Three rules keep the list usable:

- an epic **holding an open position is always kept**, whatever tier it belongs
  to, so a live trade never loses its feed;
- an epic **already traded today stays watched after its exit**, won or lost, so
  the day keeps recording what the market the bot picked at time T did next —
  the table flags it `TRADED` with the euros it banked. `EPIC_FOLLOW_CLOSED_MAX`
  bounds how many such markets keep a slot (most recent exits first); they rank
  behind the open positions and ahead of the tier, and the ones that do not fit
  simply compete on risk like any other candidate. They are **not** re-opened:
  `ALLOW_SAME_DAY_REOPEN=false` still refuses a second trade on the same epic;
- the list is **always filled to the cap** — when the unlocked band holds fewer
  markets than there are slots, the rest is taken from below the floor first
  (cheaper is always safe), then from above the ceiling, then from the markets
  whose risk could not be priced.

Epics cut by the ladder are labelled `risk_tier_<band>` in the Epic List's
exclusion-reason column.

### Position sizing

| Variable                     | Default  | Description                        |
| ---------------------------- | -------- | ---------------------------------- |
| `STRATEGY_STOP_MULTIPLIER`   | `2.5`    | Stop distance = X × spread         |
| `STRATEGY_TARGET_MULTIPLIER` | `4.0`    | Take-profit = X × spread           |
| `STRATEGY_TACTIC`            | `spread` | `spread`, `percentage`, or `point` |

### Opening and closing

| Variable                | Default    | Description                                                                                                             |
| ----------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------- |
| `ALLOW_SAME_DAY_REOPEN` | *required* | `false` = one opening per epic per day (BUY or SELL); `true` = re-open once the epic is flat                            |
| `ALLOW_RECOVERY_REVERT` | *required* | `true` = open the opposite side at once when a trade is stopped out at a loss on its opening stop                       |
| `OPEN_MODE`             | *required* | `auto` = the bot places the order itself; `notify` = it raises a dashboard signal + browser notification and you decide |

`ALLOW_SAME_DAY_REOPEN` is **global to every entry strategy** and required (no
code default — a missing value stops startup). With `false`, an epic that already
had an opening today is dropped for the rest of the day, even after its position
closed. Two concurrent positions on the same epic are always refused, and a
manual dashboard open bypasses the policy. See
[strategies/README.md](strategies/README.md).

`ALLOW_RECOVERY_REVERT` is global and required too. With `true`, a position (BUY
or SELL) taken out at a **loss** by the stop it was **opened with** is followed
immediately by an opening on the opposite side of the same epic: the market walked
through the level the trade was built on, so the bot turns around with it. Only a
genuine stop hit at the original level qualifies (`stop` / `loose` /
`closed_externally`) — a win, a manual close, an end-of-day close or a stop-out on
a *ratcheted* stop never reverts — and the chain is capped at one hop, so a
stopped-out revert is not reverted back. On top of that, the **curve since the
open** must show a real move, though the filter there is permissive and only drops
the blatant cases: a stop distance covered in tiny steps with no single candle
carrying the move (flat curve or slow leak), a market that chopped its way to the
stop, a trade that first ran a full risk in profit, or a stop merely grazed by a
wick. Holding time is not a criterion — twenty flat minutes then one candle through
the stop is a prime revert (thresholds are constants in
[src/execution/gates.py](../src/execution/gates.py), not env variables — see
[strategies/README.md](strategies/README.md)). The revert lifts the long-only gate and
`ALLOW_SAME_DAY_REOPEN`, but it stays an automatic open: the auto-open switch, the
duplicate-epic gate and the "market closes soon" gate all apply, and the reverse
trade is sized/stopped by the configured `STOP_STRATEGY` and close zones. See
[strategies/README.md](strategies/README.md).

`OPEN_MODE` is global and required too. It changes **who places the order**, and
nothing else:

- `auto` — an entry intent that passes the pre-open gates becomes a real order.
- `notify` — the *same* intent, having passed the *same* gates, opens nothing.
  It is recorded as a **signal** ([src/core/signals.py](../src/core/signals.py)),
  listed in the dashboard's "Entry Signals — Your Call" section, and pushed to
  the browser as a desktop notification. You go and read the market (the card's
  **Chart** and **Read** buttons), then either open it yourself with **Buy** /
  **Sell** — a manual open, driving the very same sizing, `STOP_STRATEGY` and
  close zones — or **dismiss** it.

Everything else is unchanged in `notify`: prices stream, candles record, and every
position already open is still managed by its close profile. A signal is only
raised **after** the gates, so you are never notified about a market the bot could
not have opened (already held, closing soon, margin unaffordable). Signals live in
memory only (nothing is persisted), expire after 15 minutes, and the same epic
never signals twice within 10 minutes. Blocking auto-open, or ending the day, stops
the signals too — the decision has already been made. See
[USE.md](USE.md#entry-signals-notify-mode).

There is no wall-clock trading-hours gate: an epic can be opened whenever its
live market status is TRADEABLE (from the Lightstreamer subscription). Each open
position is force-closed just before its **own** market close, derived per epic
from `Epic.market_close_utc` (IG `openingHours`); epics with no known schedule
fall back to the execution layer's default close hour
(`trading.DEFAULT_MARKET_CLOSE_HOUR_UTC`).

______________________________________________________________________

## Market scanner

| Variable               | Default           | Description                                   |
| ---------------------- | ----------------- | --------------------------------------------- |
| `SCANNER_SEARCH_TERMS` | *(see config.py)* | JSON array of search terms for epic discovery |

Override example in `.env`:

```env
SCANNER_SEARCH_TERMS='["Germany 40","EUR/USD","Gold"]'
```
