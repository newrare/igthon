# `open_ranking` — cross-epic ranker, keep a rolling position all day

**Status:** opt-in entry (`OPEN_STRATEGY=open_ranking`).

- Code: [src/entry/open_ranking.py](../../src/entry/open_ranking.py)
- Projection maths: [src/core/projection.py](../../src/core/projection.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))

## Idea

The per-epic entries (`open_donchian`, `open_projection`) watch every market
and open whenever one breaks out. This entry works the other way round: it
**keeps the account in the market all day with a single rolling position**. An
hour into the session it ranks every tradable epic by how promising (rising) its
curve looks and opens the best one the wallet can afford; the **moment that
position closes — win or loss — it re-ranks and re-opens**. So there is always
exactly one position running (the target count is configurable).

It is a **ranker, not a gate**: the components are soft scores, not pass/fail
filters, so "always hold the most promising market" is met by ranking rather
than rejecting. The strategy stays exit-agnostic — it emits an `EntryIntent`
(direction + score); the stop/target/trailing belong to the composed
`CloseProfile` (reference pairing: `close_zoneprofit`).

## Where the decision lives

| Concern                              | Owner                           |
| ------------------------------------ | ------------------------------- |
| Per-epic "how promising?" score      | `OpenRanking.evaluate` (entry/) |
| Cross-epic ranking, replace-on-close | scheduler rolling selection     |
| Warm-up delay + wallet check         | scheduler rolling selection     |
| Stop / target / trailing             | composed `CloseProfile` (exit/) |

`cross_epic_selection = True` on the strategy tells the scheduler to skip the
per-epic auto-open loop and drive opens through the rolling selection instead.

## Scoring

The day's bid closes feed several independent maths tools, each normalised to
[0, 1] (higher = more promising) and combined with weights that sum to 1, so the
composite score is itself in [0, 1] and directly comparable across epics.

| Component      | What it measures                                    | Source                            |
| -------------- | --------------------------------------------------- | --------------------------------- |
| **Projection** | weighted multi-model consensus that the curve rises | `core/projection.consensus` (BUY) |
| Trend shape    | regression R² when the slope is positive (else 0)   | `linear_regression`               |
| Momentum       | ROC over `roc_period`, mapped against `roc_target`  | `rate_of_change`                  |
| Regime         | Kaufman Efficiency Ratio (trend vs. chop)           | `efficiency_ratio`                |
| Spread         | `1 - (spread/bid)/max_spread_ratio`, clamped        | last candle                       |

```
score = w_proj·projection + w_shape·shape + w_mom·momentum
        + w_regime·regime + w_spread·spread        ∈ [0, 1]
```

The **projection** component is the "trade montant / prometteur" core: a curve
several diverse models (linear, polynomial, EMA-slope, log-linear) independently
agree is heading up scores highest. `evaluate` returns `None` only on structural
grounds (too little history, non-positive bid, non-positive ATR) or when the
composite falls below the optional `min_score` floor.

## Rolling selection (scheduler)

The selection runs **every analysis tick (~30 s)** during market hours — that is
what re-opens a fresh position the moment the previous one closes. The hourly
`trend_select` job (and its dashboard *Run* button) is a backstop that calls the
same routine. Each invocation:

1. waits until `STRATEGY_RANKING_OPEN_AFTER_MINUTES` past market open — the "one
   hour of livestream" warm-up before the first open of the day (a mid-day
   restart clears this immediately and re-opens without waiting);
1. returns early when the target position count is already met — the cheap
   steady state while a position is running;
1. otherwise scores every tradable epic with enough buffered history, ranks the
   BUY candidates by score (highest first), and opens the best ones that pass the
   shared open gates **and** the wallet check until the target is reached.

A lock serialises the tick loop and the hourly backstop, and the open-count is
re-checked inside it, so the target is never overshot.

Epics are **not** excluded after being traded: when a position closes, the very
next ranking may re-open the same epic if it is still the most promising.

**Wallet.** The routine reads the live available balance (`GET /accounts`),
subtracts `STRATEGY_RANKING_WALLET_RESERVE`, and opens an epic only while the
remainder covers that epic's margin (`funds_needed`); margin is deducted from the
running balance after each open (relevant when
`STRATEGY_RANKING_CONCURRENT_POSITIONS > 1`). When the balance can't be read, it
falls back to the per-position `euro_loss_max` cap only.

## Configuration

All parameters are **constants in the strategy class**
([src/entry/open_ranking.py](../../src/entry/open_ranking.py)), not
`.env`/`Settings`: tune them by editing the class. The strategy itself is
selected in `.env` (`OPEN_STRATEGY=open_ranking`) — the single source of truth,
set once at startup. The dataclass-field defaults:

| Constant              | Default                                 | Meaning                                     |
| --------------------- | --------------------------------------- | ------------------------------------------- |
| `projection_horizon`  | `60`                                    | candles each projection model extends ahead |
| `regression_period`   | `30`                                    | candles for the trend-shape R² fit          |
| `roc_period`          | `10`                                    | momentum lookback (candles)                 |
| `roc_target`          | `0.5`                                   | ROC% earning a full momentum score          |
| `min_score`           | `0.0`                                   | composite floor (0 = always open the best)  |
| `weight_projection`   | `0.40`                                  | weight of the projection component          |
| `weight_shape`        | `0.25`                                  | weight of the trend-shape component         |
| `weight_momentum`     | `0.15`                                  | weight of the momentum component            |
| `weight_regime`       | `0.10`                                  | weight of the regime (ER) component         |
| `weight_spread`       | `0.10`                                  | weight of the spread-tightness component    |
| `projection_weights`  | linear .40 / poly .30 / ema .30 / exp 0 | per-model projection weights                |
| `projection_degree`   | `2`                                     | polynomial-model degree                     |
| `projection_ema_span` | `10`                                    | EMA-model span                              |
| `efficiency_period`   | `30`                                    | ER regime window                            |
| `atr_period`          | `14`                                    | volatility check window                     |
| `max_spread_ratio`    | `0.0015`                                | spread/bid at which the spread score hits 0 |

The rolling-selection knobs the scheduler reads (also class constants):

| Constant               | Default | Meaning                                     |
| ---------------------- | ------- | ------------------------------------------- |
| `concurrent_positions` | `1`     | open positions to hold (1 = single rolling) |
| `open_after_minutes`   | `60`    | delay after market open before first open   |
| `wallet_reserve`       | `0.10`  | fraction of available funds kept free       |

## Notes

- Long-only, like the rest of the live pipeline: `evaluate` emits BUY intents and
  the risk gate rejects SELL.
- The horizon and the longest component window drive the warmup; the buffer must
  hold at least `max(projection_horizon, regression_period, roc_period, efficiency_period, atr_period) + 1` candles before an epic can be scored. With the
  defaults (~60 one-minute candles) this naturally coincides with the "one hour
  of livestream" warm-up delay.
- This is the decoupled `entry/` + `exit/` reimplementation of the same
  cross-epic up-trend selection idea.
