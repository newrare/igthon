# `trend_template` — hourly cross-epic up-trend selector

**Status:** experimental (`STRATEGY_NAME=trend_template`).

- Code: [src/strategies/trend_template.py](../../src/strategies/trend_template.py)
- Orchestration: the `trend_select` scheduler job in
  [src/core/scheduler.py](../../src/core/scheduler.py)

## Idea

Every hour, look at all ~40 livestreamed epics and ask one question of each:
**how close is its recent price curve to a theoretical straight up-trend?** Give
each a score, rank them, and open the single highest-ranked one — that is the
hour's winner. The bet is that, over a short horizon, the cleanest-rising market
is the most likely to keep rising just enough to bank a small, easy profit.

The criteria are **soft scoring components, not pass/fail gates**: the objective
is to open one epic every hour, so the strategy never rejects a market on a
single criterion. It ranks them and opens the best available — even a mediocre
best — and lets the martingale sizing absorb the resulting losers. The score
only decides _which_ epic, never _whether_ to trade.

Because the unit of profit is the spread, the target is deliberately modest
(a couple of spreads). Diversifying the epic each hour and keeping the objective
"simple" is what is meant to limit long losing streaks.

## How a strategy this shape differs from the others

Every other strategy is **per-epic and stateless**: the 30s analysis loop runs
`evaluate()` on each epic and opens whatever passes, up to `STRATEGY_MAX_POSITIONS`.
This one is **cross-epic, hourly, and stateful**, so it splits in two:

- the **per-epic** half (this module) scores one epic and builds its levels;
- the **cross-epic** half (the `trend_select` scheduler job) ranks all epics,
  applies the once-per-hour / one-open-at-a-time / martingale rules, and opens
  the single best.

Setting `hourly_selection = True` on the strategy tells the scheduler to skip the
per-epic auto-open and drive opens through `trend_select` instead. Everything
downstream (order placement, the ATR trailing stop, monitoring, the dashboard) is
the shared pipeline, unchanged.

## Per-epic scoring

`evaluate()` returns a `score` in `[0, 1]` for (almost) every epic. The only
reasons it returns nothing are **structural** — there is no way to compute a
signal at all:

- **Not enough data** — the buffer holds fewer than `warmup` candles
  (`max(REGRESSION_PERIOD, STOP_LOOKBACK, ATR_PERIOD) + 1`). Early in the day the
  buffer may be too short, so the very first hours can still open nothing.
- **No volatility** — a non-positive ATR, so a protective stop cannot be sized.

Everything else is a **soft component**, each normalized to `[0, 1]` (higher =
closer to ideal) and combined with **R²-dominant** weights so the trend shape
drives the ranking:

| Component            | Weight | Score                                                                                                  |
| -------------------- | ------ | ------------------------------------------------------------------------------------------------------ |
| **Shape** (dominant) | 0.60   | Raw R² of the regression _when_ the slope is positive and R² ≥ `MIN_R2`; otherwise `0`.                |
| **Spread tightness** | 0.25   | `clamp(1 − (spread / bid) / MAX_SPREAD_RATIO, 0, 1)` — `1` at zero spread, `0` at/above the ceiling.   |
| **Reachability**     | 0.15   | `clamp(slope × PROJECTION_HORIZON / target_distance, 0, 1)` — fraction of the target reachable in ~1h. |

`composite = 0.60 × shape + 0.25 × spread_tightness + 0.15 × reachability`.

A flat or falling market scores `0` on shape and so ranks only on the secondary
components — it can still be opened if it is the best available that hour, which
is exactly the point.

### Levels

- **Take-profit** — `level_win = bid + spread + WIN_RATIO × spread` (the first
  spread covers the BUY round-trip, `WIN_RATIO` spreads on top is the net gain).
- **Protective stop** — the **support of the last hour**: the lowest bid low over
  `STOP_LOOKBACK` candles (≈ 60 min), minus a small `STOP_BUFFER_ATR_K` ATR cushion
  just below it so a wick back to that low does not stop us out. The stop sits at
  the genuine support however far it is — there is **no ATR distance cap** (capping
  pulled the stop back up close to the entry and caused noise stop-outs); the
  `STRATEGY_EURO_LOSS` open gate is what bounds the downside. When that support
  sits at/above the entry (price at fresh lows), the stop **falls back** to an
  ATR-sized distance just below the bid, so a valid stop always exists and the
  epic stays openable. `level_loose`, `level_follower` and `level_security` all
  sit at that stop, so the shared trailing logic only ratchets it up.

## Hourly selection rules (`trend_select`)

Runs at the top of each hour, `STRATEGY_HOUR_START`–`STRATEGY_HOUR_END`, Mon–Fri.
A no-op for any non-`hourly_selection` strategy. Each run:

1. **One open at a time** — if any position is still OPEN, skip the hour (the last
   epic is "in progress"). This is why a position that never closes silently
   blocks every later hour — keep the `monitor_positions` job in automatic mode.
1. **No repeats** — exclude any epic already traded today (open or closed).
1. **Rank & open** — score every remaining tradable epic, sort by descending
   score and open the **best one that the pre-open gates accept**. If the top
   pick is refused (euro risk too large, stop beyond IG's max, not tradeable),
   fall through to the next-best — so an epic opens whenever one possibly can.
   Only an empty candidate set (no data anywhere, or every epic already traded)
   opens nothing.
1. **Martingale sizing** — quantity = `minDealSize × multiplier`, where the
   multiplier follows the day's trailing loss streak: a winning last trade resets
   to ×1; each consecutive loss multiplies by `STRATEGY_TREND_TEMPLATE_BASE_MULTIPLIER`
   (1 → 3 → 9 …), capped at `STRATEGY_TREND_TEMPLATE_MAX_MULTIPLIER`. The goal is
   to "cover" the previous loss; the per-open `STRATEGY_EURO_LOSS` risk gate is the
   hard backstop against an escalating streak.

A position becomes a "loser" when its ATR stop (pushed to IG at open and ratcheted
by the trailing follower) is hit below entry, picked up by the monitor/sync pass
and marked `win = 0` — which then drives the next hour's multiplier.

> **Risk note.** A ×3 martingale grows fast (1 → 3 → 9 → 27). Consecutive losses
> are exactly this strategy's weak point; `MAX_MULTIPLIER` and `STRATEGY_EURO_LOSS`
> are the two ceilings. Validate the loss-streak behaviour before trusting it live.

## Configuration

| Setting                                      | Default | Meaning                                   |
| -------------------------------------------- | ------- | ----------------------------------------- |
| `STRATEGY_TREND_TEMPLATE_REGRESSION_PERIOD`  | 30      | Candles for the R² fit                    |
| `STRATEGY_TREND_TEMPLATE_MIN_R2`             | 0.80    | R² floor below which the shape score is 0 |
| `STRATEGY_TREND_TEMPLATE_WIN_RATIO`          | 2.0     | Take-profit in net spread multiples       |
| `STRATEGY_TREND_TEMPLATE_PROJECTION_HORIZON` | 60      | Candles (~1h) to reach the target         |
| `STRATEGY_TREND_TEMPLATE_STOP_LOOKBACK`      | 60      | Support window — the last hour (candles)  |
| `STRATEGY_TREND_TEMPLATE_STOP_BUFFER_ATR_K`  | 0.5     | ATR cushion below the detected support    |
| `STRATEGY_TREND_TEMPLATE_BASE_MULTIPLIER`    | 3       | Martingale factor per consecutive loss    |
| `STRATEGY_TREND_TEMPLATE_MAX_MULTIPLIER`     | 27      | Hard cap on the martingale size           |

Reuses `STRATEGY_ATR_PERIOD` and `STRATEGY_MAX_SPREAD_RATIO`.

## Simulator note

The shared `simulator.py` recognises `hourly_selection` strategies and mirrors the
live selection path: at the top of each simulated hour, when flat, it scores every
untraded epic, ranks them and opens the single best one that clears the gates
(`_select_hourly`). The once-per-hour cadence, one-open-at-a-time rule and
no-repeat-epic exclusion are all reproduced. The **martingale sizing** is not
modelled — the simulator opens at a fixed quantity — so it backtests the
selection/exit logic, not the loss-streak scaling.
