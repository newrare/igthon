# `open_slope` — recent-slope ranker (open the fastest risers)

**Status:** opt-in entry (`OPEN_STRATEGY=open_slope`).

- Code: [src/entry/open_slope.py](../../src/entry/open_slope.py)
- Indicators: [src/core/indicators.py](../../src/core/indicators.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Siblings / baselines: [open_allincrease](open_allincrease.md),
  [open_ranking](open_ranking.md), [open_rebound](open_rebound.md)

## Idea

Same contract as the other rankers — a **ranker, not a gate**
(`cross_epic_selection = True`), exit-agnostic (`EntryIntent` = direction +
score, the exit belongs to the composed `CloseProfile`). The scheduler scores
every tradable epic, ranks the BUY candidates and opens the best affordable ones.

This is the **simplest** ranker: a single measure — the **recent slope** — drives
the ranking. The spec, translated:

> Compute the slope of the recent trend (over ~10 minutes), then rank the
> available (livestreamed) epics, placing first the one with the highest slope /
> progression. Keep opening as long as the wallet allows. An epic that is
> currently open cannot be opened again. Open at most one new position every
> 5 minutes.

There is no multi-horizon blend, no shape/regime/spread tie-breaker: the
fastest-rising market wins.

## Scoring — recent progression

The slope comes from a least-squares `linear_regression` over the last
`slope_period` bid closes (~10 min on the one-minute feed), which is more robust
to endpoint jitter than a two-point rate of change. A **raw** slope is not
comparable across epics — a €15 000 index moves in whole points while a forex pair
moves in ten-thousandths — so it is expressed as a **relative progression**: the
fitted net rise over the window divided by the current bid.

```
reg      = linear_regression(bids[-slope_period:])
if reg.slope <= 0:  → None          # not rising → long-only, stay flat
net_rise = reg.slope · (slope_period − 1)   # fitted rise over the window (points)
score    = net_rise / bid                    # progression over the window (fraction)
```

The score is the fraction by which the fitted line rose over the last ~10 minutes,
directly comparable across epics of any price scale and readable as a percentage
(a score of `0.002` = a ≈0.2 % rise over the window). Higher = ranked first.

### Long-only and structural rejects

The ranker is **long-only**: a market whose recent slope is not strictly positive
is not rising, so `evaluate → None` (a falling curve is never opened). `evaluate`
also returns `None` on structural grounds: too little history (< `warmup`), a
non-positive bid, or no measurable volatility (`ATR ≤ 0`, which also blocks stop
sizing at open) — and below the optional `min_score` floor (default `0.0` = never
floor, so pure ranking).

## Rolling selection (scheduler)

The per-epic score above is only half the strategy; *how many* positions are held
and *when* they open live in the scheduler's rolling selector (`_select_and_open`),
driven by class-constant knobs:

| Knob                      | Value  | Effect                                                         |
| ------------------------- | ------ | -------------------------------------------------------------- |
| `wallet_bounded`          | `True` | keep opening the best affordable epic until the wallet is dry  |
| `wallet_reserve`          | `0.10` | keep 10 % of available funds free                              |
| `open_cooldown_minutes`   | `5`    | ≥ 5 min between opens; at most one open per pass               |
| `allow_same_day_reopen`   | `True` | only "currently open" is blocked; re-open an epic once it flat |
| `min_participation_ratio` | `0.5`  | > half the warmed-up universe before crowning a winner         |
| `concurrent_positions`    | `1`    | fallback cap only, used when the account balance is unreadable |

- **Wallet-bounded** *(ouvrir tant que le wallet le permet)*: every pass opens the
  top-ranked epic whose margin the spendable balance (available − reserve) can
  still cover.
- **Cooldown** *(une nouvelle ouverture toutes les 5 minutes au mieux)*: when
  `open_cooldown_minutes > 0` the selector opens at most one position per pass and
  only once ≥ 5 min have elapsed since the most recent open
  (`_minutes_since_last_open`, on `Position.time_open`, UTC).
- **Re-open policy** *(un epic actuellement ouvert ne pourra pas être ouvert de
  nouveau)*: the only restriction is the concurrent duplicate, which the shared
  `epic_already_open` gate always blocks. Nothing forbids re-opening an epic once
  it has closed, so `allow_same_day_reopen = True` skips the one-open-per-epic-
  per-day diversity filter — a market that is flat again is a candidate again.
- **Nothing qualifies is logged**: when the wallet has room and the cooldown has
  elapsed but every warmed-up epic is falling (no positive recent slope), the
  selector logs at `INFO` *"none of N warmed-up epic(s) qualifies to open … —
  staying flat"* instead of a silent no-op.

## Parameters

All parameters are class constants in
[`OpenSlope`](../../src/entry/open_slope.py) (tune there; select at runtime via
`OPEN_STRATEGY`):

| Parameter      | Default | Meaning                                           |
| -------------- | ------- | ------------------------------------------------- |
| `slope_period` | `10`    | recent-trend window the slope is fitted on (~min) |
| `atr_period`   | `14`    | volatility window (gates stop sizing at open)     |
| `min_score`    | `0.0`   | composite floor; below it the epic stays flat     |
