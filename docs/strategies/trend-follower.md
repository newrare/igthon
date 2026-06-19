# `trend_follower` — composite-score trend confirmation (legacy)

**Status:** legacy — the project's original live strategy, kept selectable for
comparison. Superseded as live default by [`donchian_er`](donchian-er.md).

- Code: [src/strategies/trend_follower.py](../../src/strategies/trend_follower.py)
  (thin adapter over [src/core/indicators.py](../../src/core/indicators.py)
  `compute_signal`, where all the mathematics live)
- Philosophy & full background: [docs/STRATEGY.md](../STRATEGY.md)

## Mechanics

Long-only trend *confirmation*: open a BUY when several indicators agree that
an uptrend is already established.

A composite score is computed on every tick:

```
score = 0.30 × slope_norm + 0.25 × R² + 0.25 × roc_norm + 0.20 × sma_signal
```

| Component    | Indicator                                 | Contributes when       |
| ------------ | ----------------------------------------- | ---------------------- |
| `slope_norm` | Linear-regression slope (last 20 candles) | Positive slope         |
| `R²`         | Regression fit quality                    | Clean, non-noisy trend |
| `roc_norm`   | Rate of Change (last 10 candles)          | Positive momentum      |
| `sma_signal` | SMA(5) vs SMA(20) crossover               | Fast above slow        |

A **BUY** signal requires all of:

- `score ≥ STRATEGY_MIN_SCORE` (0.75)
- `R² ≥ STRATEGY_MIN_R2` (0.70)
- `spread / bid ≤ STRATEGY_MAX_SPREAD_RATIO` (0.0015)

Position levels are spread-multiples (`STRATEGY_TACTIC=spread`):

| Level            | Formula                     | Role                        |
| ---------------- | --------------------------- | --------------------------- |
| `level_win`      | bid + spread + 4.0 × spread | Fixed take-profit           |
| `level_zero`     | bid + spread                | Break-even                  |
| `level_loose`    | bid − 7.5 × spread          | Software stop               |
| `level_security` | bid − 12.5 × spread         | Broker-side protective stop |

Exits: fixed target (`win`), software/broker stop (`loose`/`stop`), ATR
trailing stop once in profit (`follower`), or end-of-day force close.

## Parameters

| Setting                      | Default | Meaning                          |
| ---------------------------- | ------- | -------------------------------- |
| `STRATEGY_LOOKBACK_POINTS`   | 20      | Regression window                |
| `STRATEGY_SMA_FAST/SLOW`     | 5 / 20  | SMA crossover periods            |
| `STRATEGY_ROC_PERIOD`        | 10      | Momentum lookback                |
| `STRATEGY_MIN_SCORE`         | 0.75    | Composite score threshold        |
| `STRATEGY_MIN_R2`            | 0.70    | Trend cleanliness threshold      |
| `STRATEGY_MAX_SPREAD_RATIO`  | 0.0015  | Spread gate                      |
| `STRATEGY_STOP_MULTIPLIER`   | 2.5     | Stop distance (spread multiples) |
| `STRATEGY_TARGET_MULTIPLIER` | 4.0     | Target distance                  |

## Backtest results (synthetic curves, 60 days × 3 epics, seed 12345)

| Profile        | Trades | Win % | P&L (€) | Expectancy €/trade     |
| -------------- | ------ | ----- | ------- | ---------------------- |
| random         | 246    | 41.1  | +395    | +1.61                  |
| mixte          | 364    | 65.9  | +1 276  | +3.51                  |
| trend_up       | 0      | —     | 0       | — (entries too strict) |
| trend_down     | 0      | —     | 0       | —                      |
| **sideways**   | 12     | 16.7  | −14     | −1.17                  |
| volatile       | 422    | 37.4  | +691    | +1.64                  |
| mean_reverting | 18     | 5.6   | −37     | −2.07                  |

## Known weaknesses (why it was replaced)

1. **Long-only**: half the directional opportunities are ignored; every false
   signal in a down/sideways market only pays spread.
1. **Late entries**: requiring score ≥ 0.75 *and* R² ≥ 0.70 means the trend is
   already mature at entry — the remaining move is often smaller than the
   spread+stop cost. On clean synthetic trends it paradoxically never enters
   (R² high but score normalisation saturates).
1. **No regime filter**: it trades sideways markets (slowly bleeding) instead
   of staying flat.
1. Live observation (2026): repeated runs across market conditions never
   produced a positive day — consistent with the structural issues above.
