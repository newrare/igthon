# `open_saferanking` — robust cross-epic ranker

**Status:** opt-in entry (`OPEN_STRATEGY=open_saferanking`).

- Code: [src/entry/open_saferanking.py](../../src/entry/open_saferanking.py)
- Projection maths: [src/core/projection.py](../../src/core/projection.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Sibling / baseline: [open_ranking](open_ranking.md)

## Idea

Same job as [`open_ranking`](open_ranking.md): keep the account in the market all
day, re-ranking every tradable epic and opening the most promising affordable
ones. The difference in *how many* it holds — `open_saferanking` is
**wallet-bounded**, opening epics until the wallet runs dry rather than holding a
single rolling position — is covered under
[Rolling selection](#rolling-selection-scheduler). Same contract too — a
**ranker, not a gate**, `cross_epic_selection = True`, exit-agnostic
(`EntryIntent` = direction + score, the exit belongs to the composed
`CloseProfile`).

What changes is **how the score is built**. `open_saferanking` is a *robust*
re-take of the same "clear rise" idea: it is designed so a market ranks high
**only when every dimension of a clean, safe up-trend holds at once**, rather
than letting one strong signal carry a fragile market.

## Why "safe" — the difference vs `open_ranking`

`open_ranking` combines its components as a **weighted sum**, which is
*compensatory*: the projection alone carries 40% of the weight, so the crowned
epic can be one where only the theoretical projection is high while the trend is
choppy, flat, or scarred by deep pull-backs. `open_saferanking` fixes that with
three upgrades.

| Upgrade         | `open_ranking`                  | `open_saferanking`                                |
| --------------- | ------------------------------- | ------------------------------------------------- |
| **Combination** | weighted **sum** (compensatory) | weighted **geometric mean** (conjunctive)         |
| **Projection**  | raw consensus score             | consensus × fraction of models agreeing (breadth) |
| **Drawdown**    | —                               | pull-back-safety component (`1 − maxDD/range`)    |
| **Trend shape** | single-window R²                | short **and** long window R² (geo-mean)           |
| **Hard gate**   | ATR > 0 only                    | ATR > 0 **and** `min_models_agree` models up      |

### 1. Conjunctive scoring — geometric, not additive

The composite is a **weighted geometric mean** of the components:

```
score = Π componentᵢ ^ wᵢ ,   Σ wᵢ = 1   →   score ∈ [0, 1]
```

The geometric mean is bounded above by its smallest term, so a single weak
dimension collapses the whole score toward zero — **no strong component can
rescue a weak one**. Each component is floored at a small `epsilon` before being
raised to its weight so the composite stays strictly monotone (still rankable
among mediocre markets — the goal is to hold the *least-bad* when forced) instead
of snapping ties to a flat 0.

### 2. Breadth-scaled projection

The projection component is `consensus.score × (agree / active)` — the raw score
times the *fraction* of independent models actually projecting up. A lone
over-confident model is discounted; genuine multi-model **unanimity** is
rewarded. A structural gate (`min_models_agree`, default 2) refuses outright any
market almost no model projects upward.

### 3. Two safety components `open_ranking` lacks

- **Pull-back safety** — `1 − max_drawdown / range`: the deepest peak-to-trough
  retracement of the bid curve over `drawdown_period`, relative to its full
  range. A monotone climb scores ~1; a rise punctuated by violent retracements
  scores low even when the net slope is up. This is the real adverse-excursion
  risk a holder faced, which the Efficiency Ratio (path *noise*) does not
  capture.
- **Multi-timeframe trend shape** — the positive-slope regression R² on **both**
  a short (`regression_period`) and a long (`regression_period_long`) window,
  combined as their geometric mean, so a trend counts only if it holds across
  horizons and a recent spike (short up, long flat) is penalised.

### 4. Pre-open bearish malus (safety gate before opening)

The three upgrades above build the composite from the *whole* warm-up window. But
a market can climb cleanly for an hour and still be **rolling over in the last few
minutes** right before the open — exactly the case that was producing opens into a
market already turning down. On top of the geometric mean, a least-squares fit of
the bid over the last `recent_trend_period` candles (~10 min) produces a **score
multiplier** in `[recent_bearish_malus, 1]`:

```
recent_factor = 1                                   if slope ≥ 0 (flat/rising)
              = 1 − severity·(1 − recent_bearish_malus)   if slope < 0
severity      = clamp01(decline / recent_drop_full_malus) · clamp01(R²)
decline       = (−slope)·span / last_bid            # relative slide over the window
score        *= recent_factor
```

The malus grows as the recent slide is both **steeper** (bigger relative decline,
capped at `recent_drop_full_malus`) **and cleaner** (higher regression R² — a
consistent down-trend, not noise). A clean, steep drop drags the score down to
`recent_bearish_malus` (default `0.05`, i.e. a 95% cut), pushing the candidate to
the back of the ranking; a shallow or noisy wobble barely dents it. Set
`recent_drop_full_malus = 0` to disable the guard.

## Scoring

| Component      | What it measures                                     | Source                            |
| -------------- | ---------------------------------------------------- | --------------------------------- |
| **Projection** | consensus × fraction of models projecting up         | `core/projection.consensus` (BUY) |
| Trend shape    | positive-slope R² on short × long windows (geo-mean) | `linear_regression`               |
| **Safety**     | `1 − max_drawdown/range` (monotonicity of the rise)  | local drawdown scan               |
| Momentum       | ROC over `roc_period`, mapped against `roc_target`   | `rate_of_change`                  |
| Regime         | Kaufman Efficiency Ratio (trend vs. chop)            | `efficiency_ratio`                |
| Spread         | `1 − (spread/bid)/max_spread_ratio`, clamped         | last candle                       |

A **pre-open bearish malus** then multiplies the composite: a fit of the bid over
the last `recent_trend_period` candles cuts the score toward `recent_bearish_malus`
when the market is sliding down into the open (see
[§4 above](#4-pre-open-bearish-malus-safety-gate-before-opening)).

`evaluate` returns `None` only on structural grounds (too little history,
non-positive bid, non-positive ATR), when fewer than `min_models_agree`
projection models point up, or when the composite falls below the optional
`min_score` floor.

## Rolling selection (scheduler)

Same scaffolding as [`open_ranking`](open_ranking.md#rolling-selection-scheduler)
— the selection runs every analysis tick (~30 s), applies the participation gate
and the one-opening-per-epic-per-day diversity rule, scores every warmed-up epic
and ranks the BUY candidates — with one deliberate difference: this ranker is
**wallet-bounded** (`wallet_bounded = True`).

`open_ranking` is *count-bounded*: it holds exactly `concurrent_positions` open
(default 1, a single rolling position) and returns early once that target is met.
`open_saferanking` drops the fixed count target: it keeps opening the
best-ranked affordable epics — each still passing the shared open gates — **until
the spendable balance (available funds minus `wallet_reserve`) can no longer
cover another epic's margin**. Every open decrements the running spendable
figure, so the wallet is the only limit. `concurrent_positions` is used only as a
conservative fallback cap for the pass when the account balance can't be read (so
an API hiccup can't dump orders across the whole ranking).

`cross_epic_selection = True` routes opens through this routine instead of the
per-epic auto-open loop.

## Configuration

All parameters are **constants in the strategy class**
([src/entry/open_saferanking.py](../../src/entry/open_saferanking.py)), not
`.env`/`Settings`. The strategy itself is selected in `.env`
(`OPEN_STRATEGY=open_saferanking`). The dataclass-field defaults:

| Constant                 | Default                                 | Meaning                                                                               |
| ------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------- |
| `projection_horizon`     | `60`                                    | candles each projection model extends ahead                                           |
| `regression_period`      | `30`                                    | candles for the short trend-shape R² fit                                              |
| `regression_period_long` | `60`                                    | candles for the long trend-shape R² fit                                               |
| `roc_period`             | `10`                                    | momentum lookback (candles)                                                           |
| `roc_target`             | `0.5`                                   | ROC% earning a full momentum score                                                    |
| `drawdown_period`        | `60`                                    | window for the pull-back-safety drawdown scan                                         |
| `min_models_agree`       | `2`                                     | structural gate: models that must project up                                          |
| `epsilon`                | `1e-3`                                  | per-component floor keeping the geo-mean rankable                                     |
| `min_score`              | `0.0`                                   | composite floor (0 = always open the best)                                            |
| `recent_trend_period`    | `10`                                    | candles (~10 min) of bid scanned before opening                                       |
| `recent_drop_full_malus` | `0.003`                                 | relative decline over the window earning full malus                                   |
| `recent_bearish_malus`   | `0.05`                                  | score multiplier floor at a clean steep drop (0 = off via `recent_drop_full_malus=0`) |
| `weight_projection`      | `0.35`                                  | exponent of the projection component                                                  |
| `weight_shape`           | `0.20`                                  | exponent of the trend-shape component                                                 |
| `weight_safety`          | `0.20`                                  | exponent of the pull-back-safety component                                            |
| `weight_momentum`        | `0.10`                                  | exponent of the momentum component                                                    |
| `weight_regime`          | `0.10`                                  | exponent of the regime (ER) component                                                 |
| `weight_spread`          | `0.05`                                  | exponent of the spread-tightness component                                            |
| `projection_weights`     | linear .40 / poly .30 / ema .30 / exp 0 | per-model projection weights                                                          |
| `projection_degree`      | `2`                                     | polynomial-model degree                                                               |
| `projection_ema_span`    | `10`                                    | EMA-model span                                                                        |
| `efficiency_period`      | `30`                                    | ER regime window                                                                      |
| `atr_period`             | `14`                                    | volatility check window                                                               |
| `max_spread_ratio`       | `0.0015`                                | spread/bid at which the spread score hits 0                                           |

The rolling-selection knobs the scheduler reads (also class constants):

| Constant               | Default | Meaning                                             |
| ---------------------- | ------- | --------------------------------------------------- |
| `wallet_bounded`       | `True`  | open epics until the wallet runs dry (no count cap) |
| `concurrent_positions` | `1`     | fallback cap only, used when the balance is unknown |
| `open_after_minutes`   | `60`    | delay after market open before first open           |
| `wallet_reserve`       | `0.10`  | fraction of available funds kept free               |

## Notes

- Long-only, like the rest of the live pipeline: `evaluate` emits BUY intents and
  the risk gate rejects SELL.
- The longest component window (`projection_horizon` / `regression_period_long` /
  `drawdown_period` = 60) drives the warm-up: the buffer must hold ≥ 61 candles
  before an epic can be scored, coinciding with the "one hour of livestream"
  warm-up.
- Same cross-epic up-trend-selection idea as [`open_ranking`](open_ranking.md),
  re-weighted for robustness. Swap between them with a one-line `OPEN_STRATEGY`
  change and compare on the same days.
