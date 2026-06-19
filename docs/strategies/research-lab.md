# Research lab — candidate strategy backtests

The lab ([src/backtest/strategies.py](../../src/backtest/strategies.py)) is a
**long + short** backtest engine with honest spread costs (longs fill at the
offer and exit at the bid; shorts the reverse) plus five candidate strategies.
It exists to compare ideas quickly on synthetic curves before promoting one to
production; it never touches the live pipeline.

Production promotion path: lab candidate → re-implementation against
[`BaseStrategy`](../../src/strategies/base.py) → registry + `STRATEGY_NAME`
(see [README.md](README.md)).

## Tools

```bash
# Benchmark the 5 candidates (+ live baseline) across all curve profiles:
python -m src.scripts.compare_strategies --days 60 --epics 3 --seed 12345

# Sweep the efficiency-ratio regime gate on Donchian:
python -m src.scripts.donchian_regime_filter --days 60 --epics 3 --seed 12345
```

All candidates run on **identical curves** per profile (same seeds), so the
comparison is fair; the live baseline runs on its own seed path of the same
profile.

## The five candidates

| Strategy            | Style                  | Entry                                       | Exit                        |
| ------------------- | ---------------------- | ------------------------------------------- | --------------------------- |
| `MeanRev-Zscore`    | Mean reversion         | z-score of mid ≤ −2 (long) / ≥ +2 (short)   | Mean touch; 3.5 σ stop      |
| `Donchian-Breakout` | Trend breakout         | Close outside prior 20-candle high/low band | ATR trail (2.5×) only       |
| `RSI-Reversion`     | Oscillator reversion   | RSI(14) < 30 (long) / > 70 (short)          | RSI back to 50; 3× ATR stop |
| `MACD-Momentum`     | Momentum crossover     | EMA(12)/EMA(26) cross                       | Opposite cross; ATR trail   |
| `DualThrust-ORB`    | Opening-range breakout | open ± 0.5 × first-30-min range             | ATR trail; 2.5× ATR stop    |

## Results (60 days × 3 epics, **single seed 12345**, € at 1 €/point)

> ⚠️ **Single-seed numbers — high variance, do not treat as expected value.**
> These are long + short with no daily gates (NOT what the live bot does). On
> the marginal profiles the sign flips across seeds: e.g. Donchian on
> `sideways` is +386 € at seed 12345 but −472 € at seed 7 and −184 € at seed
> 42\. Only the *trending* profiles are sign-stable. For live expectation use
> the **long-only, multi-seed** table in [donchian-er.md](donchian-er.md).

Total P&L per profile (winner bolded):

| Profile        | Donchian     | MACD    | DualThrust | MeanRev-Z  | RSI      |
| -------------- | ------------ | ------- | ---------- | ---------- | -------- |
| random         | **+94 149**  | +54 989 | +32 023    | −58 757    | −83 952  |
| mixte          | **+98 305**  | +63 790 | +12 926    | −59 116    | −81 564  |
| trend_up       | **+172 330** | +147    | +171 729   | −86 615    | −144 132 |
| trend_down     | **+149 862** | 0       | +149 509   | −76 386    | −127 538 |
| sideways       | **−1 344**   | −1 854  | −4 911     | −4 366     | −5 631   |
| volatile       | **+112 465** | +58 922 | +37 958    | −81 994    | −97 437  |
| mean_reverting | −16 514      | −10 304 | −18 826    | **+7 487** | +5 373   |

## Findings

1. **Sideways is the regime that matters** (closest to the observed real
   market) and *every* strategy loses there without a regime filter — the
   loss per trade ≈ the spread. A directionless market plus spread cost is a
   negative-sum game; the only winning move is to trade less.
1. **Breakout/momentum beats reversion overall**: Donchian is positive on 5/7
   profiles and merely flat-negative on the other two; mean-reversion styles
   only win in the one regime made for them and get destroyed by trends.
1. **Volume is the symptom of the wrong regime**: Donchian trades
   ~2/epic/day where it wins, 18–22/epic/day where it loses. This motivated
   the Kaufman Efficiency-Ratio gate — see the sweep and conclusions in
   [donchian-er.md](donchian-er.md).
1. **Synthetic-trend caveat**: trending profiles are unrealistically clean
   (win rates > 93 %, profit factors > 1000). Treat absolute P&L as an
   artefact; only relative rankings and regime behaviour are meaningful.

## Promoted

`Donchian-Breakout` + ER gate → [`donchian_er`](donchian-er.md), the live
default since June 2026.
