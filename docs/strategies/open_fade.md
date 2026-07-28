# `open_fade` — fade a clean, extended trend at the edge of its channel

**Status:** opt-in entry (`OPEN_STRATEGY=open_fade`). **Two-sided** — the first
automatic entry that opens SELL as well as BUY.

- Code: [src/entry/open_fade.py](../../src/entry/open_fade.py)
- Indicators: [src/core/indicators.py](../../src/core/indicators.py)
  (`trend_pct`, `channel_position`, `atr`)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Counterpart: [open_pullback](open_pullback.md) — the same measurement run the
  other way round
- Siblings / baselines: [open_rebound](open_rebound.md),
  [open_allincrease](open_allincrease.md)

## Idea

A **ranker, not a gate** (`cross_epic_selection = True`), exit-agnostic
(`EntryIntent` = direction + score; the exit belongs to the composed
`CloseProfile`). Where the other rankers join a move, this one takes the other
side of a move that has run cleanly into the edge of its own range: **buy what
has been falling, sell what has been rising** — once the fall or rise is a clean
line and price has arrived at the far end of its channel.

## Where the parameters come from

Measured over the stored one-minute candles: **6 sessions, 45-51 epics, ~100 000
resolved outcomes**. Every point of the universe was scored in both directions
and resolved against a fixed **1.5 ATR stop / 3.0 ATR target**, so the reference
to beat is the 2:1 breakeven rate of **33.33 %**. An unfiltered entry measured
**33.63 %** — this universe is close to a coin flip.

Bucketing each indicator into deciles put the *trend-following* end of every axis
below breakeven and the *fading* end above it:

| Feature   | Decile 1 (fade end) | Decile 10 (trend end) |
| --------- | ------------------- | --------------------- |
| `slope60` | 35.0 %              | 32.4 %                |
| `chan60`  | 35.4 %              | 31.5 %                |
| `brk60`   | 35.1 %              | 31.9 %                |
| `roc30`   | 33.8 % (D2: 36.1 %) | 33.3 %                |

Stacking the fade end and splitting the sample (train = first 5 sessions,
test = last 3) left exactly one rule that reproduced out-of-sample:

| Rule                         | Train       | Test        |
| ---------------------------- | ----------- | ----------- |
| unfiltered baseline          | 33.44 %     | 33.85 %     |
| fade, all instrument classes | 35.84 %     | 34.33 %     |
| **fade, commodities only**   | **37.30 %** | **37.62 %** |
| fade + commodities + 10-13h  | 40.46 %     | 35.71 %     |

Two defaults follow directly from that table:

- **`commodity_only = True` is structural, not cosmetic.** Measured on distinct
  signal *episodes* (one entry per contiguous qualifying run — what the scheduler
  actually opens), the commodity-only rule holds at **34.82 %** while the
  all-classes version falls to **30.55 %**, below breakeven.
- **No hour-of-day filter**, despite it scoring highest on train. It decays on
  test — the signature of overfitting.

> **Caveat.** Six sessions of 45 heavily-correlated instruments is a thin,
> non-independent sample: the effective number of observations is far below the
> raw count, so any confidence interval implied by `n` is optimistic. Treat the
> edge above as a hypothesis to validate live, not as a measured expectancy.

## Hard gates

| Gate                                              | Parameter         | Default |
| ------------------------------------------------- | ----------------- | ------- |
| Trend **against** the trade, implied % of the fit | `min_trend_pct`   | `0.30`  |
| That trend is a clean line (R²)                   | `min_r_squared`   | `0.60`  |
| Price at the faded extreme of the channel         | `max_channel_pos` | `0.30`  |
| Instrument actually moves (ATR / price, %)        | `min_atr_pct`     | `0.03`  |
| Commodity markets only (`CC.D.` / `CO.D.`)        | `commodity_only`  | `True`  |

The sign of the `trend_period` fit **sets the direction**: a falling market is
bought, a rising one sold. `channel_position` is then re-expressed from the
trade's point of view, so `0` always means "hard against the extreme we fade".

## Scoring

A weighted sum (weights sum to `1.0`, so the score stays in `[0, 1]` and reads as
a percentage). Ranking only — it never gates.

```
score = w_stretch·stretch + w_cleanliness·R² + w_channel·channel + w_spread·spread
```

| Component     | Weight | What it measures                                       |
| ------------- | ------ | ------------------------------------------------------ |
| `stretch`     | `0.40` | how far the faded trend has run (÷ `trend_pct_target`) |
| `cleanliness` | `0.25` | R² of that trend                                       |
| `channel`     | `0.25` | how hard against the faded extreme the entry sits      |
| `spread`      | `0.10` | cheaper-to-trade tie-breaker                           |

## Selection-layer behaviour

| Knob                    | Value  | Why                                                                         |
| ----------------------- | ------ | --------------------------------------------------------------------------- |
| `emits_shorts`          | `True` | two-sided: the scheduler keeps SELL intents and lifts the long-only gate    |
| `wallet_bounded`        | `True` | keep opening the best affordable candidate until the balance is exhausted   |
| `open_cooldown_minutes` | `3`    | commodities move together — 2026-07-24 opened five soft commodities in 82 s |

Same-day re-opening is **not** a strategy knob: it is the global
`ALLOW_SAME_DAY_REOPEN` boolean in `.env` (see [README](README.md)). This
strategy expects `ALLOW_SAME_DAY_REOPEN=true`.

## Tests

[tests/test_open_fade.py](../../tests/test_open_fade.py) — registry, the
two-sided direction contract, the score contract, structural `None` cases and
each hard gate. The `emits_shorts` plumbing is exercised end-to-end in
[tests/test_scheduler.py](../../tests/test_scheduler.py)
(`TestTwoSidedRankerSelection`).
