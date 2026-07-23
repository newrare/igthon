# `open_linear` — linear-day ranker (open the clean, rising straight lines)

**Status:** opt-in entry (`OPEN_STRATEGY=open_linear`).

- Code: [src/entry/open_linear.py](../../src/entry/open_linear.py)
- Indicators: [src/core/indicators.py](../../src/core/indicators.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Siblings / baselines: [open_allincrease](open_allincrease.md),
  [open_slope](open_slope.md), [open_rebound](open_rebound.md),
  [open_ranking](open_ranking.md)

## Idea

Same contract as the other rankers — a **ranker, not a gate**
(`cross_epic_selection = True`), exit-agnostic (`EntryIntent` = direction +
score, the exit belongs to the composed `CloseProfile`). The scheduler scores
every tradable epic, ranks the BUY candidates and opens the best affordable ones.

The setup, translated:

> The day's general trend is bullish and the curve has been roughly **linear
> since the morning** — a steady, ruler-straight climb, not a choppy grind and
> not a fresh spike.

This is the shape a trader recognises at a glance and opens on by hand. It is the
pure trend-following counterpart to [open_rebound](open_rebound.md) (which wants a
**V**) and to [open_slope](open_slope.md) (which looks only at the last ~10 min):
here the **whole buffered session** — "la journée" — must itself be a rising line.

## Scoring — bullish + linear + not flat

Two independent measures of *straightness* over the whole session are combined,
because they penalise different defects and a genuinely clean line scores high on
both:

| Term           | Measure                              | What it rewards / penalises                                               |
| -------------- | ------------------------------------ | ------------------------------------------------------------------------- |
| **Linearity**  | `R²` of the day-long bid regression  | closeness to the fitted line; collapses when the curve bends (parabola)   |
| **Efficiency** | Kaufman ER over the session          | directness — `\|net\| / Σ\|step\|`; near 0 for a wandering, choppy path   |
| **Strength**   | fitted net rise as a fraction of bid | anti-flat guard — a straight *flat* line rises only a sliver over the day |

A day can score well on one straightness term and poorly on the other (a smooth
parabola has high ER but mediocre R²; a straight line through a saw-tooth has
decent R² but low ER), so rewarding **both** is what pins the score to a truly
linear climb.

```
day_reg  = linear_regression(bids)          # whole session ("since the morning")
if day_reg.slope <= 0:  → None               # not bullish over the day → stay flat
linearity  = clamp01(day_reg.r_squared)      # straight-line fit
efficiency = efficiency_ratio(bids, N-1)     # path directness (choppiness guard)
net_rise   = day_reg.slope · (N − 1)         # fitted rise over the session (points)
strength   = clamp01((net_rise / bid) / rise_target)       # relative progression
score = 0.45·linearity + 0.30·efficiency + 0.25·strength
```

The **strength** term is the deliberate guard against a *flat* line: a curve can
be perfectly straight and perfectly efficient while barely rising, which is not a
tradable up-day. Expressing the net rise as a fraction of the current bid keeps
it comparable across epics of any price scale. (An ATR-relative measure would
not work: for a clean line the per-candle ATR scales with the slope, so
`net_rise / ATR` is invariant to steepness and could never tell a flat line from
a steep one — only a price-relative progression can.)

The composite is a **weighted sum** (weights sum to 1.0), so the score stays in
[0, 1], is directly comparable across epics and readable as a percentage. The two
straightness terms carry the majority — this ranker is about the linear *shape* —
with strength as the qualifying floor. Higher = ranked first.

### Long-only and structural rejects

The ranker is **long-only**: a day whose whole-session slope is not strictly
positive is not a rising line, so `evaluate → None` (a falling or flat day is
never opened). `evaluate` also returns `None` on structural grounds: too little
session accumulated (< `warmup`), a non-positive bid, or no measurable volatility
(`ATR ≤ 0`, which also blocks stop sizing at open) — and below the
`min_score` floor (default `0.60`). The floor is what keeps opens on *clean*
lines: it separates genuinely linear rising days (open-tick scores p10≈0.61,
median≈0.81) from volatile hump-shaped noise (median≈0.05), keeping ~91 % of
clean up-days while rejecting ~3/4 of volatile days. Because it gates every tick,
the strategy waits for a stretch that is actually a straight climb rather than
opening on an early choppy stretch. Set it to `0.0` for pure ranking (never
floor); raise it above `0.60` for even stricter lines.

## Rolling selection (scheduler)

The per-epic score above is only half the strategy; *how many* positions are held
and *when* they open live in the scheduler's rolling selector (`_select_and_open`),
driven by class-constant knobs:

| Knob                      | Value   | Effect                                                           |
| ------------------------- | ------- | ---------------------------------------------------------------- |
| `wallet_bounded`          | `True`  | keep opening the best affordable epic until the wallet is dry    |
| `wallet_reserve`          | `0.10`  | keep 10 % of available funds free                                |
| `open_cooldown_minutes`   | `5`     | ≥ 5 min between opens; at most one open per pass                 |
| `allow_same_day_reopen`   | `False` | one opening per epic per day; not re-opened even after it closes |
| `min_participation_ratio` | `0.5`   | > half the warmed-up universe before crowning a winner           |
| `concurrent_positions`    | `1`     | fallback cap only, used when the account balance is unreadable   |

## Parameters

All parameters are class constants in
[`OpenLinear`](../../src/entry/open_linear.py) (tune there; select at runtime via
`OPEN_STRATEGY`):

| Parameter           | Default | Meaning                                                     |
| ------------------- | ------- | ----------------------------------------------------------- |
| `min_period`        | `30`    | min minutes of session required before scoring straightness |
| `atr_period`        | `14`    | volatility window (gates stop sizing at open)               |
| `rise_target`       | `0.01`  | session rise (fraction of bid) at which strength saturates  |
| `weight_linearity`  | `0.45`  | weight of the R² straight-line fit                          |
| `weight_efficiency` | `0.30`  | weight of the Kaufman efficiency ratio                      |
| `weight_strength`   | `0.25`  | weight of the price-relative session rise (anti-flat)       |
| `min_score`         | `0.60`  | composite floor; below it the epic stays flat (0.0 = never) |
