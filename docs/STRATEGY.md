# Trading Strategy — Trend Volume Intraday

## Philosophy

The bot targets **volume-based intraday trading** built on three pillars:

1. **Mathematical confirmation of an existing trend** — no prediction, only confirmation
1. **Low spread cost offset by trade frequency** — spread is the primary cost; spread ratio is monitored per epic
1. **Strict daily cycle** — all positions opened and closed within the same trading day

The core principle: we don't try to predict reversals. We identify price movements already underway
mathematically, then ride them with enough volume so that occasional losses are absorbed by cumulative gains.

______________________________________________________________________

## Pillar 1 — Mathematical trend identification

### Entry criteria (BUY signal)

All of the following must be true simultaneously:

| Criterion        | Method                                | Default threshold                     |
| ---------------- | ------------------------------------- | ------------------------------------- |
| Short-term trend | Linear regression over last N candles | Slope > 0 and R² > 0.70               |
| Momentum         | Rate of Change over last P candles    | ROC > 0 and increasing                |
| Range position   | Current bid vs day high/low           | 30%–70% (avoids overbought extremes)  |
| Spread quality   | Current spread / bid                  | < 0.15% (`STRATEGY_MAX_SPREAD_RATIO`) |
| Composite score  | Weighted sum of normalised signals    | ≥ 0.75 (`STRATEGY_MIN_SCORE`)         |

### Computed indicators

All computed in [src/services/compute.py](../src/services/compute.py):

**Linear regression** — over the last `STRATEGY_LOOKBACK_POINTS` bid values:

- Slope > 0 = upward trend
- R² ≥ `STRATEGY_MIN_R2` = trend is statistically clean (not random noise)

**Simple Moving Averages:**

- SMA_fast (`STRATEGY_SMA_FAST` = 5 periods)
- SMA_slow (`STRATEGY_SMA_SLOW` = 20 periods)
- Signal: SMA_fast > SMA_slow (intraday golden cross)

**Rate of Change:**

- `ROC = (bid_now − bid_N_ago) / bid_N_ago × 100`
- Signal: ROC > 0 and accelerating (`STRATEGY_ROC_PERIOD` = 10 periods)

**Composite score:**

```
score = w1 × slope_norm + w2 × r² + w3 × roc_norm + w4 × sma_signal
```

Position is opened only when `score ≥ STRATEGY_MIN_SCORE`.

### Why this works

- We **confirm**, not predict: the trend already exists before entry.
- R² filters erratic movements — a high slope with low R² (random walk) is rejected.
- Entry is mid-trend, avoiding both the risky start and the exhausted end.

______________________________________________________________________

## Pillar 2 — Frequency absorbs spread cost

### Economic model

```
Assumptions (per epic, per day):
  Average spread: 1 pip
  Average gain per winning trade: 3–5 pips
  Average loss per losing trade: 2–3 pips (tight stop)
  Target win rate: 60–65 %

Example with 50 trades/day:
  30 wins × 4 pips = +120 pips
  20 losses × 2.5 pips = −50 pips
  Spread cost (50 × 1 pip) = −50 pips
  Net: +20 pips/day/epic
```

### Epic selection for this strategy

Good candidates (low spread ratio, high liquidity):

- **Indices:** DAX 40, FTSE 100, CAC 40, S&P 500
- **Forex:** EUR/USD, GBP/USD, USD/JPY

Avoid:

- Exotic FX pairs (spread too wide)
- Crypto instruments (spread too volatile)
- Illiquid markets outside their active session

Spread guard: `spread / bid < STRATEGY_MAX_SPREAD_RATIO` (default 0.15%).

### Risk management per position

- **Stop loss:** `STRATEGY_STOP_MULTIPLIER × spread` (default 2.5×)
- **Take profit:** `STRATEGY_TARGET_MULTIPLIER × spread` (default 4.0×)
- **Min risk/reward ratio:** 1:1.5 (enforced by the multiplier defaults)
- **Fixed position size:** no martingale — constant volume per trade

______________________________________________________________________

## Pillar 3 — Strict intraday cycle

### Typical day timeline

```
08:50   Start bot  →  python -m src.main --web
09:00   Trading opens (STRATEGY_HOUR_START)
        → positions open when score ≥ 0.75 and R² ≥ 0.70
09:00–11:00  High-activity phase (post-open European volatility)
11:00–14:00  Quieter phase — fewer signals, spread may widen
14:30–16:00  US-open phase — new volume peak
16:00   No new positions (STRATEGY_HOUR_END)
17:30   Force-close all open positions (STRATEGY_HOUR_CLOSE + 30 min)
18:00   Daily summary written to database
```

### Circuit breakers (automatic stops)

| Trigger                                                     | Action                     |
| ----------------------------------------------------------- | -------------------------- |
| Daily P&L ≤ `STRATEGY_DAILY_LOSS_LIMIT` (−500 €)            | Stop opening new positions |
| Daily P&L ≥ `STRATEGY_DAILY_WIN_TARGET` (300 €)             | Stop opening new positions |
| Win rate < `STRATEGY_MIN_WIN_RATE` (40 %) after ≥ 10 trades | Stop opening new positions |
| Trade count = `STRATEGY_MAX_TRADES_DAY` (50)                | Stop opening new positions |

### No overnight positions

Positions are **always** closed before end of day:

- Avoids overnight gap risk
- Avoids overnight financing cost
- Provides a clean daily P&L reset

______________________________________________________________________

## Main loop (per 30-second tick)

```
1. COLLECT  — fetch bid/ask for all tracked epics (streaming or REST)
             → store completed 1-min candles in DB

2. COMPUTE  — for each epic: regression, SMA, ROC, composite score

3. DECIDE   — if score > threshold AND no open position for epic:
                 → OPEN  (POST /positions/otc)
             — if open position AND target/stop reached:
                 → CLOSE (DELETE /positions/otc/{dealId})

4. RECORD   — log trade in DB, update daily P&L, check circuit breakers
```

______________________________________________________________________

## Comparison: old PHP bot vs new Python bot

| Aspect         | PHP (legacy)                             | Python (new)                        |
| -------------- | ---------------------------------------- | ----------------------------------- |
| Frequency      | 1 trade/epic/day                         | 20–50 trades/day                    |
| Signal         | Bid vs fixed level                       | Linear regression + composite score |
| Stop           | Fixed (spread + loose + security levels) | Dynamic — proportional to spread    |
| Overnight      | Possible                                 | Forbidden (strict intraday)         |
| Martingale     | Yes (increase size after loss)           | No (constant volume)                |
| Validation     | None                                     | R² ≥ 0.7 statistical filter         |
| Authentication | CST/file (daily)                         | OAuth v3 (60-second auto-refresh)   |
