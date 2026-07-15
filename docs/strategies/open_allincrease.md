# `open_allincrease` — paced, volatility-aware multi-timeframe uptrend ranker

**Status:** opt-in entry (`OPEN_STRATEGY=open_allincrease`).

- Code: [src/entry/open_allincrease.py](../../src/entry/open_allincrease.py)
- Indicators: [src/core/indicators.py](../../src/core/indicators.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Siblings / baselines: [open_ranking](open_ranking.md),
  [open_saferanking](open_saferanking.md)

## Idea

Same contract as the other rankers — a **ranker, not a gate**
(`cross_epic_selection = True`), exit-agnostic (`EntryIntent` = direction +
score, the exit belongs to the composed `CloseProfile`). The scheduler scores
every tradable epic, ranks the BUY candidates and opens the best affordable ones.
This module owns only the per-epic half: *"how strongly and cleanly is this curve
rising, across several time horizons?"*, expressed as a score in `[0, 1]` that
reads directly as a percentage.

It exists to combine three requirements the other rankers do not:

1. **Multi-timeframe trend, recent weighted more than old.**
1. **Volatility-aware** — a rise that is *relatively flat* (small vs. the
   market's own volatility) cannot score high.
1. **Re-openable and paced** — the same epic may be opened several times a day,
   one position at a time, at least ten minutes apart, until the wallet is dry.

## Scoring

The score **adds points** for a bullish trend on three horizons and combines
them as a weighted **sum** (weights sum to `1.0`, so the score stays in `[0, 1]`
/ readable as a percentage):

```
score = w_short·short + w_medium·medium + w_long·long
```

| Horizon | Window (1-min candles)   | Weight | Rationale                     |
| ------- | ------------------------ | ------ | ----------------------------- |
| short   | 10 (~10 min)             | `0.45` | most recent → the most weight |
| medium  | 60 (~1 h)                | `0.35` |                               |
| long    | 180 (~"24 h", see below) | `0.20` | oldest → the least weight     |

The weights **decrease with horizon length** — *le récent vaut plus que
l'ancien*.

### Each horizon — cleanliness × volatility-relative magnitude

A horizon score is **not** just "is the slope up?". For the last `period` bids:

```
reg     = linear_regression(window)
if reg.slope <= 0:  component = 0          # not rising → no points
clean   = clamp01(reg.r_squared)           # how straight the rise is
net_rise = reg.slope · (len(window) − 1)   # fitted rise over the window (points)
strength = clamp01(net_rise / (ATR · rise_atr_target))
component = clean · strength
```

- **`clean`** rewards a straight rise (high R²) over a noisy one.
- **`strength`** is the fitted net rise measured in units of the market's own
  **ATR**. A market that drifts up only slightly relative to its own volatility —
  *une hausse relativement plate* — earns a small `strength` and therefore a low
  score, **however clean the line looks**. This is the deliberate guard against
  crowning epics whose rise is flat. `rise_atr_target = 3.0` means a net rise of
  ≥ 3 ATRs over the window saturates the magnitude factor.

### Score floor

Below `min_score = 0.70` the epic stays flat (`evaluate → None`) — *si le score
est trop faible (< 70 %), on n'ouvre pas de position*. `evaluate` also returns
`None` on structural grounds: too little history (< `warmup`), a non-positive
bid, or no measurable volatility (`ATR ≤ 0`, which also blocks stop sizing).

### The "24 h" horizon and the buffer

The live price buffer keeps at most
[`DEFAULT_MAX_CANDLES`](../../src/feed/price_buffer.py) (200) one-minute candles
per epic and is reset daily, so a literal 24-hour window is **not available**.
The long horizon is therefore realized as the **whole buffered session** (up to
~3 h of one-minute candles); early in the day it uses whatever history has
accumulated. `warmup` requires only the medium window (~61 candles), so the
ranker can start trading about an hour into the session and the long horizon
fills in toward its target as the day progresses.

## Rolling selection (scheduler)

The per-epic score above is only half the strategy; *how many* positions are held
and *when* they open live in the scheduler's rolling selector
(`_select_and_open`), driven by class-constant knobs:

| Knob                      | Value  | Effect                                                         |
| ------------------------- | ------ | -------------------------------------------------------------- |
| `wallet_bounded`          | `True` | keep opening the best affordable epic until the wallet is dry  |
| `wallet_reserve`          | `0.10` | keep 10 % of available funds free                              |
| `allow_same_day_reopen`   | `True` | skip the one-open-per-epic-per-day filter                      |
| `open_cooldown_minutes`   | `10`   | ≥ 10 min between opens; at most one open per pass              |
| `min_participation_ratio` | `0.5`  | > half the warmed-up universe before crowning a winner         |
| `concurrent_positions`    | `1`    | fallback cap only, used when the account balance is unreadable |

- **Wallet-bounded** *(open positions as long as the wallet has funds)*: every
  pass opens the top-ranked epic whose margin the spendable balance (available −
  reserve) can still cover.
- **Same-day re-open** *(la même journée, un epic peut être ouvert plusieurs
  fois)*: the `_traded_today` diversity filter is skipped. An epic becomes a
  candidate again as soon as it holds **no** open position — the concurrent
  duplicate is still blocked by the shared `epic_already_open` gate, so a
  still-open epic is never opened twice at once.
- **Cooldown** *(éviter d'ouvrir plusieurs positions en même temps — attendre au
  moins 10 min)*: when `open_cooldown_minutes > 0` the selector opens at most one
  position per pass and only once ≥ 10 min have elapsed since the most recent open
  (`_minutes_since_last_open`, on `Position.time_open`, UTC). Combined with
  wallet-bounding, the account opens the best rising market roughly every ten
  minutes until the balance is exhausted.
- **Nothing qualifies is logged** *(… mais qu'aucune position ne satisfait une
  ouverture, il faut le noter dans les logs)*: when the wallet has room and the
  cooldown has elapsed but every warmed-up epic is rejected (below the 70 % floor
  or not rising), the selector logs at `INFO`
  *"none of N warmed-up epic(s) qualifies to open … — staying flat"* instead of a
  silent no-op.

## Parameters

All parameters are class constants in
[`OpenAllIncrease`](../../src/entry/open_allincrease.py) (tune there; select at
runtime via `OPEN_STRATEGY`):

| Parameter         | Default | Meaning                                              |
| ----------------- | ------- | ---------------------------------------------------- |
| `short_period`    | `10`    | short horizon window (candles ≈ minutes)             |
| `medium_period`   | `60`    | medium horizon window                                |
| `long_period`     | `180`   | long ("24 h") horizon window, bounded by the buffer  |
| `atr_period`      | `14`    | volatility window (also gates stop sizing at open)   |
| `rise_atr_target` | `3.0`   | net rise (in ATRs) at which the magnitude factor = 1 |
| `weight_short`    | `0.45`  | short-horizon weight                                 |
| `weight_medium`   | `0.35`  | medium-horizon weight                                |
| `weight_long`     | `0.20`  | long-horizon weight                                  |
| `min_score`       | `0.70`  | composite floor; below it the epic stays flat        |
