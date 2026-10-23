# TODO — research roadmap

Every entry strategy measured so far (W24–W39, 15 strategies, stops from 1 to
1000 ATR, all-day or from 14:30) is either negative or not significant once the
spread is paid. Changing the stop does not fix it, so the next step is not a
sixteenth curve-shape strategy but a change of method.

## 1. Measure against chance, net of costs — **in progress**

- [x] Random-direction baseline: replay a strategy with each open's direction
  drawn by a coin flip (same opens, same markets, same close, same gates), over
  N seeds → null distribution and p-value. `make baseline`
  ([random_baseline.py](../src/backtest/random_baseline.py)).

- [x] Research dataset: one row per epic every few minutes, look-ahead-free
  features + the forward outcome in R net of spread for a BUY and a SELL under a
  fixed stop/target bracket. `make research ARGS="build"`
  ([research.py](../src/backtest/research.py)).

- [x] Train/test analysis by time (first days train, last days test): feature
  deciles and categories whose mean R is positive on **both** halves, with a
  day-clustered bootstrap p-value on the test half. `make research ARGS="analyze"`.

- [x] First results (2026-10-01, W24–W39):

  - dataset (206 784 rows, stop 2.5 / target 5 ATR, 120 min): unfiltered
    −0.29 R (BUY) / −0.29 R (SELL) on train, −0.35 / −0.33 R on test; **0 of
    220 buckets** positive on both halves. The loss falls monotonically with
    `atr_spread` (D1 −0.60 R → D10 −0.10 R on both sides and both halves): the
    median spread is about one 1-minute ATR, and −1 / (2.5 + 1) ≈ −0.29 R is
    exactly what a random walk paying that spread loses. Cost, not signal,
    drives the result at this scale → point 2.
  - baseline `open_fade` (20 seeds): +0.042 R vs coin-flip median −0.090 R
    (p95 −0.033), z = 2.54, p(z) = 0.0055 — its side choice beats chance, but
    the edge over chance (~+0.13 R) is mostly eaten by the cost. One strategy
    tested so far: with all 15 the Bonferroni threshold is 0.0033.

- [x] Baseline on every strategy (`--all --seeds 20`, 2026-10-01, ~1 h on 7
  workers, results in `dumps/research/baseline_all.json`). Bonferroni threshold
  over 15: p(z) < 0.0033.

  | Strategy           | Trades | Mean R | Coin-flip median R | z         | p(z)   | Euro    |
  | ------------------ | ------ | ------ | ------------------ | --------- | ------ | ------- |
  | `open_progressive` | 479    | −0.042 | −0.145             | +2.96     | 0.0015 | −3 286  |
  | `open_fade`        | 258    | +0.042 | −0.090             | +2.54     | 0.0055 | +5 129  |
  | 8 strategies       | —      | —      | —                  | −1.5…+0.7 | ≥ 0.23 | —       |
  | `open_linear`      | 580    | −0.158 | −0.095             | −1.63     | —      | −10 192 |
  | `open_momentum`    | 186    | −0.239 | −0.107             | −1.93     | —      | −5 246  |
  | `open_rebound`     | 1 322  | −0.218 | −0.151             | −1.96     | —      | −22 395 |
  | `open_projection`  | 43     | −0.233 | −0.005             | −2.16     | —      | +521    |
  | `open_pullback`    | 574    | −0.131 | −0.057             | −2.26     | —      | −6 885  |

  Only `open_progressive` clears the corrected threshold, and it still loses
  money (its side choice beats chance, its R does not beat zero). `open_fade`
  is the only one that makes money and sits just above the threshold.
  Consistency check: `open_pullback` — `open_fade`'s measurement run the other
  way round — is the most *worse*-than-chance (z −2.26), so the fade signal
  carries some information whichever way it is read.

- [x] Retire the strategies with z < 2 (13 of 15) from consideration; keep
  `open_fade` and `open_progressive` under forward validation.

- [ ] Forward-validate `open_fade` and `open_progressive` from the freeze date
  **2026-10-01**: weekly
  `make baseline ARGS="--strategy open_fade --weeks <weeks after W40>"`; about
  270 post-freeze trades (~14 weeks at ~4 trades/day) are needed to confirm a
  +0.13 R edge over chance.

- [ ] Freeze any rule the analysis keeps, then validate it forward on weeks that
  did not exist when it was found (see "Forward validation" in
  [BACKTESTING.md](BACKTESTING.md#research-tools)).

## 2. Start from the cost: timeframe and market selection — **measured**

- [x] Rank epics by movement per unit of cost (`tf_atr_spread`) on the train
  half and measure the cheapest on the test half:
  `make research ARGS="markets --path …"`.

- [x] Size the bracket on longer bars (`--timeframe`), hold longer (`--horizon`,
  wall-clock minutes), across nights with IG funding (`--overnight --funding`).

- [x] Results (2026-10-01, W24–W39, stop 2.5 / target 5 ATR of the timeframe,
  funding 4 %/yr overnight). *Drift-neutral* = (BUY + SELL) / 2, the pure cost
  of trading at that scale (the market's drift adds to one side what it takes
  from the other):

  | Dataset                 | Rows    | Drift-neutral test, all | Cheapest 10 % | Cheapest 25 % |
  | ----------------------- | ------- | ----------------------- | ------------- | ------------- |
  | 1 min, 2 h, intraday    | 206 784 | −0.337 R                | −0.110 R      | −0.195 R      |
  | 5 min, 8 h, intraday    | 206 760 | −0.201 R                | −0.036 R      | −0.094 R      |
  | 15 min, 10 h, intraday  | 68 634  | −0.135 R                | −0.040 R      | −0.053 R      |
  | 15 min, 24 h, overnight | 68 695  | −0.173 R                | −0.022 R      | −0.068 R      |
  | 1 h, 3 days, overnight  | 16 586  | −0.135 R                | −0.017 R      | −0.073 R      |
  | 4 h, 7 days, overnight  | 5 007   | −0.063 R                | −0.061 R      | −0.011 R      |

  The cost drag falls from −0.34 R to about −0.02 R once both levers are used
  (≥ 5-minute scale **and** the cheapest markets), but never turns positive on
  its own, and no feature bucket is significant at any scale. The positive
  SELL figures on the cheapest markets (+0.07 to +0.21 R) are the test
  period's downward drift (BUY is negative by about as much), not skill.
  Recurring but not significant: buying after a sharp fall (`trend60_pct` D1
  BUY: +0.21 / +0.02 R at 1 h, +0.22 / +0.12 R at 4 h) — the same fade idea as
  `open_fade`.

- [x] Combine: `open_fade`'s direction on longer scales, measured with
  `make research ARGS="build --strategy open_fade …"` then `ARGS="signal …"`
  (the signal is read on the live 1-minute buffer, the outcome on the
  timeframe's bracket; *excess* = signal R − coin flip on the same rows, so drift
  and cost cancel). First signal per epic and day, as the live opens:

  | Scale, horizon         | Train excess | Test excess | Test p | Test signal R |
  | ---------------------- | ------------ | ----------- | ------ | ------------- |
  | 1 min, 2 h             | +0.076       | +0.023      | 0.376  | −0.287        |
  | 5 min, 8 h             | +0.041       | +0.122      | 0.042  | −0.058        |
  | 15 min, 10 h           | +0.005       | +0.034      | 0.288  | −0.109        |
  | 1 h, 3 days, overnight | +0.012       | −0.075      | 0.851  | −0.147        |

  The side choice is slightly better than a coin flip at the short scales
  (+0.02 to +0.12 R) but nowhere significant once the scales tested are counted,
  and too small to cover even the reduced cost: the signal itself stays
  negative everywhere. The replay's z = 2.54 over chance therefore owes more to
  the 1-minute trailing exit than to a direction edge that survives a change of
  scale. **Not worth the engine work** (15-min entry/stop, multi-day replay)
  on this basis.

## 3. Lean on market structure rather than curve shape

- [x] Hour-of-day / session effects from the `hour_paris` feature (2026-10-01):
  every hour 09–18 Paris is negative on train **and** test, both sides, at the
  5-minute (−0.08 to −0.29 R) and 15-minute (−0.03 to −0.23 R) scales — the
  least bad is 09h SELL at 15 min (−0.081 / −0.032 R). No session effect beats
  the cost; nothing to build.
- [ ] Twin-contract spread (cocoa NY vs London, corr ≈ 0.94): trade the
  divergence of the pair rather than the direction of the market; two spreads
  are paid, so measure the net first.
