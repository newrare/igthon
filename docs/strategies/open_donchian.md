# `open_donchian` — Donchian breakout gated by trend efficiency

**Status:** live entry (`OPEN_STRATEGY=open_donchian`).

- Code: [src/entry/open_donchian.py](../../src/entry/open_donchian.py)

## Idea

Two findings drive this strategy (June 2026 research session):

1. Of five candidate styles benchmarked on synthetic curves, the **Donchian
   channel breakout** had the best regime profile: clearly positive wherever
   the market has direction, and only a *small bounded loss* in pure chop
   (≈ the spread per trade). No strategy is net positive in a genuinely
   directionless market — a breakout least of all.
1. That residual loss is a **volume problem**: where it wins it trades
   ~2×/epic/day; where it loses it churns 18–22×/epic/day on false breakouts,
   paying the spread every time. Filtering *which markets are worth trading*
   — not changing the entry itself — cuts the chop bleed to a small bounded
   amount while leaving the trending-regime profit intact. It cannot turn
   chop positive (there is no edge to capture there); it just stops paying to
   churn.

Hence: **trade breakouts only on markets that are trending efficiently.**

## Mechanics

### Quality gates (in order, before any entry)

1. **Spread gate** — skip when `spread / bid > STRATEGY_MAX_SPREAD_RATIO`.

1. **Regime gate (market selection)** — compute the Kaufman Efficiency Ratio
   over the last `STRATEGY_EFFICIENCY_PERIOD` candles:

   ```
   ER = |net move| / Σ|candle-to-candle moves|     ∈ [0, 1]
   ```

   ER ≈ 1 → clean directional path; ER ≈ 0 → sideways chop. Entry is allowed
   only when `ER ≥ STRATEGY_MIN_EFFICIENCY`. This is the "select the right
   epics today" mechanism: a choppy market simply never arms the breakout.

### Entry

- **BUY** when the bid closes **above** the highest high of the previous
  `STRATEGY_DONCHIAN_CHANNEL` candles.
- **SELL** when it closes **below** the lowest low (emitted but currently
  rejected by the live gates — see Limitations).

### Exit — no fixed take-profit

`level_win = 0` **by design**. A Donchian breakout is a trend-following system:
the edge is the occasional large trend that pays for many small losses, so a
fixed take-profit (which caps exactly those large winners) is counter-productive.
The trade rides the trend and exits via:

- the **ATR chandelier trailing stop** (shared `compute_trailing_stop`): the stop
  trails `k × ATR` below the running high and only ratchets up — the normal exit
  (`follower`). `atr_k_pre` and `atr_k_post` are kept **equal** (2.5): tightening
  after break-even cut winners off at ~1.5 ATR while losers ran the full 2.5 ATR,
  which made winners smaller than losers. A single consistent width lets winners
  run, as a breakout system needs;
- the **protective stop** at `STRATEGY_DONCHIAN_STOP_ATR_K × ATR(14)` below
  entry, pushed to IG as an absolute `stopLevel` (`stop`);
- the **end-of-day force close** (strict intraday, unchanged).

### Levels mapping

| Level            | Value (BUY) | Role                                   |
| ---------------- | ----------- | -------------------------------------- |
| `level_win`      | 0 (none)    | Skipped — trailing exit only           |
| `level_zero`     | offer       | Break-even (trail switches k_pre→post) |
| `level_loose`    | bid − k×ATR | Software stop                          |
| `level_security` | bid − k×ATR | Broker-side protective stop            |
| `level_follower` | bid − k×ATR | Trailing stop start                    |

## Parameters

| Setting                        | Default         | Meaning                               |
| ------------------------------ | --------------- | ------------------------------------- |
| `OPEN_STRATEGY`                | `open_donchian` | Selects this strategy                 |
| `STRATEGY_DONCHIAN_CHANNEL`    | 20              | Channel lookback (candles)            |
| `STRATEGY_DONCHIAN_STOP_ATR_K` | 2.5             | Stop distance in ATR multiples        |
| `STRATEGY_EFFICIENCY_PERIOD`   | 30              | ER lookback window                    |
| `STRATEGY_MIN_EFFICIENCY`      | 0.60            | Regime gate threshold (0 disables)    |
| `STRATEGY_ATR_PERIOD`          | 14              | ATR window (shared with the trailing) |
| `STRATEGY_MAX_SPREAD_RATIO`    | 0.0010          | Spread gate (shared)                  |

## Backtest evidence

> **Live simulator.** The simulator (`/simulator`, `run_simulation`) is
> **long-only** and gated exactly like the bot — *this is what the bot does,
> trust it*.

### Live simulator (long-only) — mean over 8 seeds, 100-trade runs

| Profile        | Mean P&L | Range (min … max) | Win % | Read                               |
| -------------- | -------- | ----------------- | ----- | ---------------------------------- |
| trend_up       | +10 086  | +7 683 … +12 358  | 62 %  | strong edge in clean trends        |
| **mixte**      | +3 082   | +2 196 … +3 795   | 53 %  | **realistic "mixed market" proxy** |
| random         | +2 646   | +1 541 … +3 132   | 51 %  | positive                           |
| volatile       | +2 236   | +1 536 … +2 885   | 38 %  | positive (few large winners)       |
| trend_down     | ≈ 0      | −7 … 0            | —     | long-only: stays flat, no shorts   |
| **sideways**   | **−90**  | −247 … −13        | 16 %  | small bounded loss — see note      |
| mean_reverting | −250     | −327 … −208       | 4 %   | breakouts get faded                |

**Note on `sideways` (every seed is slightly negative, ≈ −0.9 €/trade ≈ the
spread):** this is *expected and not a bug*. A breakout strategy has no edge in
a directionless market — there is nothing to break out toward. The ER gate
cannot manufacture an edge the market does not contain; it only keeps the loss
small by trading rarely. A strategy that is *positive* in flat markets must
fade extremes (mean reversion) — the opposite trade, which loses badly in
trends. Pure sideways is the worst
case; real markets alternate regimes, which is why `mixte` (every seed
positive) is the honest proxy for live expectation.

Reproduce: `/simulator` page (pick `open_donchian`), or
`run_simulation(settings, SimulationConfig(profile=..., seed=...))`.

> ⚠️ **Caveat**: synthetic trends are far cleaner than real markets, so
> absolute figures are optimistic. Always read **averaged over many seeds**;
> the robust, repeatable finding is *relative* — Donchian+ER is solidly
> positive wherever the market has direction (random/mixte/volatile/trend_up)
> and only bleeds a small, bounded amount in pure chop.

## Limitations & next steps

1. **Long-only live**: the strategy emits SELL breakouts but
   `evaluate_open_gates` rejects them and `TradingService` only places BUY
   orders. Half the edge (down-trends) is unused. → Next step: short support
   in the trading service (order direction, mirrored levels, downward
   trailing).
1. **Thresholds tuned on real data**: `ER ≥ 0.60` / window 30 — raised from 0.45
   after the first real-candle backtests showed the looser gate let too many
   marginal breakouts bleed the spread. Keep validating on recorded real candles
   (the backtest reads the CSV archive) as more weeks accumulate.
1. **Trailing width**: `atr_k_pre` and `atr_k_post` are kept **equal** (2.5) — a
   single consistent chandelier width. The earlier two-speed setting
   (`atr_k_post=1.5`) tightened the stop after break-even and cut winners short,
   leaving winners smaller than losers; a breakout system needs to let winners
   run.
1. **One-shot churn**: in a strongly trending day the strategy may re-enter
   after each trailing exit (entry → trail out → new breakout). A per-epic daily
   entry cap could be added to bound this.
