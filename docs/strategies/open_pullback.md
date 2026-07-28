# `open_pullback` — join a clean trend on a pull-back

**Status:** opt-in entry (`OPEN_STRATEGY=open_pullback`). **Two-sided.**
⚠️ **Published for live evaluation — it measured below breakeven on the stored
history.** See [Measured performance](#measured-performance).

- Code: [src/entry/open_pullback.py](../../src/entry/open_pullback.py)
- Indicators: [src/core/indicators.py](../../src/core/indicators.py)
  (`trend_pct`, `channel_position`, `rate_of_change`, `atr`)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Counterpart: [open_fade](open_fade.md) — the same measurement, opposite side

## Idea

A **ranker, not a gate** (`cross_epic_selection = True`), exit-agnostic.
Reverse-engineered from the **twelve manual opens of the 2026-07-24 session**:
each was replayed against the stored one-minute candles and its indicator state
placed inside a baseline of 8 586 market points from the same day.

The manual entries were strikingly consistent — this was a real, repeated gesture,
not noise:

| Feature                           | Manual median | Baseline percentile |
| --------------------------------- | ------------- | ------------------- |
| `trend_pct` over 60 candles       | +0.71 %       | 95th                |
| `trend_pct` over 30 candles       | +0.40 %       | 94th                |
| R² of the 60-candle fit           | 0.83          | 88th                |
| ATR as % of price                 | 0.069 %       | 80th                |
| channel position (60)             | 0.83          | 78th                |
| distance below the 60-bar extreme | 1.71 ATR      | 76th                |
| `roc` over 5 candles (BUY only)   | −0.045 %      | **16th**            |

In words: *wait for a clean, strong hour-long trend on a market that actually
moves, let it pause, and join it below the extreme rather than at it.* The
16th-percentile `roc5` is the distinctive part — the entry is taken on the
breath, not on the thrust.

## Measured performance

Replayed over 6 sessions and ~100 000 resolved outcomes (1.5 ATR stop / 3.0 ATR
target → breakeven **33.33 %**):

| Rule                       | n      | Win rate    |
| -------------------------- | ------ | ----------- |
| unfiltered baseline        | 99 732 | 33.63 %     |
| **this rule (pull-back)**  | 794    | **29.60 %** |
| trend-continuation variant | 1 317  | 30.98 %     |

The decile scan is what makes this hard to dismiss as a threshold artefact:
**every** axis of the signature points into a losing bucket — `slope60` decile 10
scores 32.4 % against 35.0 % for decile 1, `chan60` 31.5 % against 35.4 %,
`brk60` 31.9 % against 35.1 %. On this universe and this sample the sign of the
relationship is inverted: strength is priced, and joining it pays less than
fading it.

Six sessions of 45 heavily-correlated instruments is nonetheless a thin,
non-independent sample. Live evaluation over more sessions is the right way to
settle it — which is why this module exists.

## Hard gates

| Gate                                              | Parameter                  | Default |
| ------------------------------------------------- | -------------------------- | ------- |
| Trend **with** the trade, implied % of the fit    | `min_trend_pct`            | `0.40`  |
| That trend is a clean line (R²)                   | `min_r_squared`            | `0.70`  |
| Still going at mid-range                          | `min_mid_trend_pct`        | `0.20`  |
| Short leg flat — not extended                     | `max_entry_trend_pct`      | `0.30`  |
| The pause: last candles flat or against the trade | `max_roc_pct`              | `0.0`   |
| Not at the extreme                                | `min_extreme_distance_atr` | `0.5`   |
| Trend not broken                                  | `max_extreme_distance_atr` | `3.5`   |
| Instrument actually moves (ATR / price, %)        | `min_atr_pct`              | `0.04`  |

The sign of the `trend_period` fit **sets the direction**: an up-trend is bought,
a down-trend sold.

### Divergence from the observed session

Stated plainly: the five manual **SELLs** of 2026-07-24 were *continuations* —
their short-term slope was aligned with the trade, at the 92nd percentile — not
pull-backs. Mirroring the BUY gesture is the coherent design and is what this
module implements, so its SELL side is a hypothesis about the trader's intent
rather than a replay of what was traded. The continuation variant was measured
separately and scored 30.98 %, no better.

## Scoring

```
score = w_trend·trend + w_cleanliness·R² + w_entry·entry + w_spread·spread
```

| Component     | Weight | What it measures                                               |
| ------------- | ------ | -------------------------------------------------------------- |
| `trend`       | `0.35` | strength of the trend joined (÷ `trend_pct_target`)            |
| `cleanliness` | `0.30` | R² of that trend                                               |
| `entry`       | `0.25` | placement inside the allowed pull-back band (peaks at its mid) |
| `spread`      | `0.10` | cheaper-to-trade tie-breaker                                   |

## Selection-layer behaviour

| Knob                    | Value  | Why                                                                    |
| ----------------------- | ------ | ---------------------------------------------------------------------- |
| `emits_shorts`          | `True` | two-sided by construction                                              |
| `wallet_bounded`        | `True` | keep opening the best affordable candidate while funds allow           |
| `open_cooldown_minutes` | `3`    | a sector trending together must not be opened as one de-facto position |

Same-day re-opening is **not** a strategy knob: it is the global
`ALLOW_SAME_DAY_REOPEN` boolean in `.env` (see [README](README.md)). This
strategy expects `ALLOW_SAME_DAY_REOPEN=true`.

## Tests

[tests/test_open_pullback.py](../../tests/test_open_pullback.py) — registry, the
two-sided direction contract, the score contract, structural `None` cases and
each hard gate. The `emits_shorts` plumbing is exercised end-to-end in
[tests/test_scheduler.py](../../tests/test_scheduler.py)
(`TestTwoSidedRankerSelection`).
