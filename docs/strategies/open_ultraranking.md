# `open_ultraranking` — `open_saferanking` + a hard regime veto

Entry strategy (`OPEN_STRATEGY`, `src/entry/open_ultraranking.py`). Cross-epic
**ranker** (`cross_epic_selection = True`), wallet-bounded, **long-only**.

```env
OPEN_STRATEGY=open_ultraranking
```

## Idea

[`open_saferanking`](open_saferanking.md) already asks every dimension of a safe rise
to hold at once: conjunctive geometric mean, pull-back safety, multi-timeframe shape,
a whole-day + recent up-trend veto, a projection breadth gate. What it still cannot
refuse is a market that is **rising without going anywhere** — a directionless range
whose net drift happens to be positive.

Its efficiency-ratio term is one *soft* component out of six
(`weight_regime = 0.10`), and because a ranker must crown the best of the pool, a soft
penalty never rejects: **the least-bad chopping market still opens**.

This ranker adds exactly one thing: a **hard veto on the regime**. An epic whose path
over the last `regime_period` candles is not directional is dropped outright, before
any scoring. Everything else — the components, their weights, the trend gate, the
bearish malus, the wallet-bounded rolling selection — is inherited unchanged.

It is a **subclass** rather than a copy on purpose: the two strategies differ by one
rule, and duplicating the scoring machinery would leave two versions of it to keep in
step.

## Why the regime deserves a veto rather than a weight

The Efficiency Ratio is `|net move| / path travelled` over the window: `1` for a clean
ramp, `0` for pure chop. It measures something no other component does — not *whether*
the curve rose, but whether it **went** anywhere to get there.

Observed on `IX.D.HANGSENG.IFD.IP` (2026-08-03 07:24, ER = 0.00 over the hour before
the open):

| Measure                | Value                                  |
| ---------------------- | -------------------------------------- |
| net move over the hour | 0.3 point                              |
| path travelled         | 135 points                             |
| band swept             | 38.5 points                            |
| stop distance placed   | 26 points                              |
| outcome                | −30 €, full initial risk, after 5 h 20 |

The price travelled 450× its net move, oscillating inside a band and finishing where
it started. Every other dimension can look acceptable on such a curve — there is a
fitted slope, a projection, a spread — yet the trade has no thesis to be right about.
The stop sat *inside* a band the price sweeps in both directions.

That is also why the fix belongs on the entry side and not in `src/stops/`: no stop
placement rescues a trade taken without direction. A wider stop only buys a larger
loss, a tighter one only reaches it sooner. **The decision that matters is not
opening.**

## Measured effect, and how far to trust it

Replayed over the 153 positions of 2026-07-27 → 2026-08-03 (the window where the
`candle` table still holds the pre-open history), filtering on the efficiency ratio of
the 60 candles before each open:

| Threshold | Trades | Realised | Winning days |
| --------- | ------ | -------- | ------------ |
| none      | 153    | −293 €   | 2 / 6        |
| ER ≥ 0.15 | 97     | +592 €   | 3 / 6        |
| ER ≥ 0.20 | 71     | +1 419 € | 4 / 6        |
| ER ≥ 0.25 | 40     | −301 €   | 3 / 6        |

The **effect is real** — it holds on five of the six days rather than resting on one
lucky trade. The **exact threshold is not**: the collapse at 0.25 shows this is no
clean "more direction is better" monotone, so the apparent optimum at 0.20 is fitted
to 153 samples.

`min_regime_efficiency` therefore defaults to the more conservative **0.15** (broader
plateau, destroys less volume). Treat it as a constant to re-fit with
`src/backtest/backtester.py` over more history, not a tuned value.

Two further caveats on that table:

- It is **filter arithmetic, not a replay**. Refusing an open frees the epic, so with
  `ALLOW_SAME_DAY_REOPEN=false` the real day would have taken different trades
  afterwards, and `ALLOW_RECOVERY_REVERT` interacts too.
- The window is **six days**. It is bounded by candle retention, not by choice.

## Two efficiency-ratio windows, on purpose

| Role                     | Window                   | Effect                             |
| ------------------------ | ------------------------ | ---------------------------------- |
| inherited soft component | `efficiency_period` (30) | ranks among survivors, weight 0.10 |
| this ranker's veto       | `regime_period` (60)     | **drops the epic entirely**        |

The soft term ranks on a short horizon; the veto asks the coarser question *"has this
market gone anywhere at all in the last hour?"* — the horizon on which chop is
actually identifiable, and the same window
[`stop_shape`](stop_shape.md) classifies on.

The veto runs **first** and on its own data, so a chopping epic costs one
efficiency-ratio pass instead of the full projection consensus — this runs over every
tradable epic on every selection pass.

## Pairing with `stop_shape`

[`stop_shape`](stop_shape.md) classifies the same curve with the same measure over the
same 60-candle window, to choose *which* recent level its stop anchors on. Composing
the two makes the pair coherent:

```env
OPEN_STRATEGY=open_ultraranking
STOP_STRATEGY=stop_shape
```

This ranker refuses the chop opens, so the stop policy's chop branch — its widest,
least informative placement — becomes the rare fallback it is meant to be rather than
a routine case. Keep `min_regime_efficiency` and `StopShape.min_efficiency` in step:
they are the same judgement about the same window.

## Parameters

Only the two below are its own; everything else is inherited from
[`open_saferanking`](open_saferanking.md). All are constants of the class — tune them
there, not in `.env`.

| Parameter               | Default | Role                                               |
| ----------------------- | ------- | -------------------------------------------------- |
| `regime_period`         | 60      | candles (~1 h) of mid closes measured for the veto |
| `min_regime_efficiency` | 0.15    | ER floor below which the epic is not even ranked   |

`warmup` is `max(inherited warmup, regime_period + 1)` — `efficiency_ratio` consumes
`period + 1` values.

## Returns `None` when

Everything `open_saferanking` refuses, **plus** the new veto:

- the buffer holds fewer than `warmup` candles;
- **the efficiency ratio over `regime_period` is below `min_regime_efficiency`** (the
  veto — this strategy's only addition);
- the whole-day or recent slope is not rising (`require_uptrend`);
- fewer than `min_models_agree` projection models point up;
- no positive ATR, or a non-positive bid;
- the composite falls below `min_score`.

## Tests

`tests/test_open_ultraranking.py` — the registry and inheritance contract, the veto on
both sides of its threshold, that it is *hard* rather than a score penalty, that
rising chop is refused though the base ranks it, that accepted intents match the base
ranker exactly (scores stay comparable), and the warm-up arithmetic.
