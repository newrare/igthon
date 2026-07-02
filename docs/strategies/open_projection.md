# `open_projection` — Donchian breakout confirmed by a multi-model projection

**Status:** opt-in entry (`OPEN_STRATEGY=open_projection`).

- Code: [src/entry/open_projection.py](../../src/entry/open_projection.py)
- Projection maths: [src/core/projection.py](../../src/core/projection.py)
- Builds on: [open_donchian.md](open_donchian.md)

## Idea

`open_donchian` opens a breakout as soon as the regime gate (Kaufman Efficiency
Ratio) says the market is trending. This entry keeps that decision exactly as
is and adds one more, harder gate: **before opening, the day's bid curve is
extrapolated by several independent mathematical models, and the breakout is
taken only if those projections agree, across models, with the breakout
direction.**

The point is *verification by agreement*. A single fit always projects
something — a straight line always has a slope. Several diverse models that
independently point the same way is a stronger statement than any one of them.
When they diverge, that disagreement is itself the signal to stay flat.

This is the *open* side only. It emits an `EntryIntent` (direction + the
consensus score); the stop/target/trailing belong to the composed
`CloseProfile` (reference pairing: `close_zoneprofit`).

## Mechanics

### Gates (in order, every step must pass)

1. **Spread gate** — skip when `spread / bid > STRATEGY_MAX_SPREAD_RATIO`.
1. **Regime gate** — Kaufman Efficiency Ratio over `STRATEGY_EFFICIENCY_PERIOD`
   candles must reach `STRATEGY_MIN_EFFICIENCY` (identical to `open_donchian`).
1. **Volatility check** — ATR over `STRATEGY_ATR_PERIOD` must be positive.
1. **Donchian breakout** — the bid closes outside the prior
   `STRATEGY_DONCHIAN_CHANNEL`-period high/low band → candidate direction
   (BUY above the band, SELL below).
1. **Projection consensus** — the new gate (below).

### Projection consensus

The day's bid closes are fitted by each model with a non-zero weight, and each
fit is extrapolated `STRATEGY_PROJECTION_HORIZON` candles past the last point:

| Model        | Fit                                    | Confidence              |
| ------------ | -------------------------------------- | ----------------------- |
| `linear`     | least-squares straight line            | fit R²                  |
| `polynomial` | degree-`d` least-squares (default 2)   | fit R²                  |
| `ema`        | local slope of an EMA, extrapolated    | R² of a line on the EMA |
| `exp`        | log-linear (compounding), back via exp | R² of the log fit       |

A model **agrees** when its projected price moves the same way as the breakout
direction (up for BUY, down for SELL). The opening score is

```
score = Σ(weight × confidence | agreeing models) / Σ(weight | active models)   ∈ [0, 1]
```

so disagreeing models contribute nothing and pull the score down. The entry is
taken only when `score ≥ STRATEGY_PROJECTION_MIN_SCORE`. The score is carried on
the `EntryIntent` for the dashboard/logs (it does not affect the exit).

Setting every weight but one to `0` reduces the gate to a single mathematical
model — useful for A/B testing one model in isolation.

## Configuration

All parameters are **constants in the strategy class**
([src/entry/open_projection.py](../../src/entry/open_projection.py)), not
`.env`/`Settings`: tune them by editing the class. The strategy itself is
selected in `.env` (`OPEN_STRATEGY=open_projection`) — the single source of
truth, set once at startup. The dataclass-field defaults:

| Constant               | Default                                 | Meaning                               |
| ---------------------- | --------------------------------------- | ------------------------------------- |
| `projection_horizon`   | `30`                                    | candles ahead each model extrapolates |
| `projection_degree`    | `2`                                     | polynomial-model degree               |
| `projection_ema_span`  | `10`                                    | EMA-model span                        |
| `min_projection_score` | `0.50`                                  | min weighted consensus to open        |
| `projection_weights`   | linear .40 / poly .30 / ema .30 / exp 0 | per-model weights                     |
| `channel`              | `20`                                    | Donchian lookback                     |
| `efficiency_period`    | `30`                                    | ER regime window                      |
| `min_efficiency`       | `0.60`                                  | regime gate threshold                 |
| `atr_period`           | `14`                                    | volatility check window               |
| `max_spread_ratio`     | `0.0010`                                | spread gate                           |

Setting every weight but one to `0` reduces the gate to a single model.

## Notes

- `exp` is shipped off (`weight = 0`): on 1-minute intraday data a log-linear
  fit extrapolates aggressively and can dominate the score; enable it
  deliberately. It also falls back to the linear projection on any non-positive
  value.
- `polynomial` falls back to linear when the normal equations are singular or
  there are fewer than `degree + 1` points.
- The horizon participates in the warmup, so the buffer must hold at least
  `max(channel, efficiency_period, atr_period, projection_horizon) + 1` candles
  before the strategy evaluates.
