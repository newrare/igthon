# `momentum_scalper` — high-frequency spread-multiple scalp

**Status:** experimental (`STRATEGY_NAME=momentum_scalper`).

- Code: [src/strategies/momentum_scalper.py](../../src/strategies/momentum_scalper.py)

## Idea

The opposite thesis to the trend followers: do **not** wait for a big move.
Open **many** short-lived trades and, the moment the move is worth clearly more
than the spread paid to get in, take the profit and step aside. The spread is
the enemy of a high-frequency strategy, so it is also the unit everything is
measured in — both the target and the entry filter are framed around it.

Trade-off to keep in mind: the take-profit is tiny (a couple of spreads), so the
win rate is naturally high but each win is small. The strategy only nets
positive if the (rarer) support-stop losses stay smaller, on average, than the
sum of the many small wins. That balance is what the `WIN_RATIO`,
`MAX_STOP_ATR_K` and `MIN_ROC` knobs control — validate it on the simulator
before trusting it live.

## Mechanics

### Quality gate (before anything else)

1. **Spread gate** — skip when `spread / bid > STRATEGY_MAX_SPREAD_RATIO`. A
   scalp's whole edge is a couple of spreads wide; a wide spread eats it whole.

### Entry — fresh upward momentum on two horizons

2. **Recent** — the rate of change over `STRATEGY_SCALPER_MOMENTUM_PERIOD`
   candles must reach `STRATEGY_SCALPER_MIN_ROC` (percent). The move is already
   running our way.

1. **Very recent** — each of the last `STRATEGY_SCALPER_CONFIRM_PERIOD` closes
   must be higher than the one before it, so we buy a *live* up-tick rather than
   a move that is already stalling or rolling over (the "last minutes" filter).

The strategy is **BUY-only** (the live pipeline opens BUY only).

### Take-profit — a fixed multiple of the spread

```
level_win = bid + spread + WIN_RATIO × spread
            └──────┬─────┘   └──────┬───────┘
            break-even cost     net profit grabbed
```

A BUY fills at the offer and exits at the bid, so the first `spread` just covers
the round-trip cost; the `WIN_RATIO × spread` on top is the actual gain. With
the default `WIN_RATIO = 1.5` the position is closed (`win`) by
`decide_close_reason` as soon as the bid has moved 1.5 spreads into net profit.

### Smart stop — detected support, ATR-capped

The lowest **bid low** over the last `STRATEGY_SCALPER_STOP_LOOKBACK` candles
(≈ the past hour on 1-minute data) is the level the market has defended. The
protective stop sits `STRATEGY_SCALPER_STOP_BUFFER_ATR_K` ATR **below** it:

```
support    = min(bid_low) over the last STOP_LOOKBACK candles
stop_level = support − STOP_BUFFER_ATR_K × ATR
```

A support that is far below price would ruin the reward/risk of a spread-sized
target, so the stop distance is capped at `STRATEGY_SCALPER_MAX_STOP_ATR_K` ATR:
when the detected support is further than that, the stop falls back to
`bid − MAX_STOP_ATR_K × ATR`.

`level_security` (the broker-side stop sent to IG at open), `level_loose` (the
close-below check) and `level_follower` (the trailing seed) are all pinned to
`stop_level`; the shared `follower` trailing logic can then only ever ratchet it
up. In practice the fast take-profit fires first; the support stop and the
end-of-day force close are the fallbacks.

## Parameters

| Setting (`.env`)                     | Field               | Default  | Meaning                                         |
| ------------------------------------ | ------------------- | -------- | ----------------------------------------------- |
| `STRATEGY_SCALPER_MOMENTUM_PERIOD`   | `momentum_period`   | `8`      | Recent-trend ROC window (candles)               |
| `STRATEGY_SCALPER_MIN_ROC`           | `min_roc`           | `0.20`   | Minimum ROC over that window (percent)          |
| `STRATEGY_SCALPER_CONFIRM_PERIOD`    | `confirm_period`    | `1`      | Number of last closes that must each rise       |
| `STRATEGY_SCALPER_WIN_RATIO`         | `win_ratio`         | `4.0`    | Take-profit as a multiple of the spread (net)   |
| `STRATEGY_SCALPER_STOP_LOOKBACK`     | `stop_lookback`     | `60`     | Support-detection window (candles, ≈ last hour) |
| `STRATEGY_SCALPER_STOP_BUFFER_ATR_K` | `stop_buffer_atr_k` | `0.5`    | ATR buffer placed below the detected support    |
| `STRATEGY_SCALPER_MAX_STOP_ATR_K`    | `max_stop_atr_k`    | `3.0`    | Cap on the stop distance, in ATR multiples      |
| `STRATEGY_MAX_SPREAD_RATIO` (shared) | `max_spread_ratio`  | `0.0010` | Maximum `spread / bid` to consider an entry     |
| `STRATEGY_ATR_PERIOD` (shared)       | `atr_period`        | `14`     | ATR window for the stop buffer and cap          |

## Limitations

- **Spread-sensitive by construction.** The edge is a couple of spreads; the
  spread gate is doing heavy lifting and should stay tight. Only worth running
  on the lowest-spread, most liquid epics.
- **High win rate ≠ profitable.** The asymmetry (tiny wins, support-sized
  losses) means one bad stop can erase many wins. Tune `WIN_RATIO` /
  `MAX_STOP_ATR_K` so the average loss stays bounded.
- **No regime gate.** Unlike `donchian_er`, the scalper deliberately trades for
  volume and does not filter sideways markets — chop with frequent up-ticks is
  exactly where it churns the most. Watch the simulator's `stop` count.
- **BUY-only**, like the rest of the live pipeline.

## Backtest tuning (2026 session, real candles)

The original defaults (`win_ratio=1.5`, `min_roc=0.02`, `momentum_period=5`,
`confirm_period=2`) carried the exact failure mode in the second limitation: a
high win rate (≈70 %) but an average loss roughly 3× the average win, leaving
the profit factor below 1 on every recorded week. A parameter sweep over the
archived weeks `2021-W41`, `2026-W24` and `2026-W25` (percentage-return basis,
contract-agnostic) isolated the levers that fix the asymmetry **and** generalise
across weeks:

- **`win_ratio` 1.5 → 4** lifts the average win from ≈0.08 % to ≈0.20-0.29 %,
  close to the average loss — the single biggest improvement.
- **`min_roc` 0.02 → 0.20** and **`momentum_period` 5 → 8** make entries more
  selective; **`confirm_period` 2 → 1** and the tighter shared
  **`max_spread_ratio` 0.0015 → 0.0010** each add a smaller, robust gain.
- **Do not tighten `max_stop_atr_k`.** Lowering the stop cap (3 → 1) collapsed
  the win rate (the support stop needs room) and made things strictly worse.

This config raised the profit factor on the two harder weeks (W24 0.58 → 0.96,
W25 0.65 → 0.87) and stayed near break-even on W41 (0.89 → 0.57). **Caveat:** no
single config was net positive (PF > 1) on all three weeks — three weeks is a
thin sample and `2026-W24` is a regime hostile to both live strategies. Treat
these defaults as the most robust starting point found, not a proven edge;
re-validate as more weeks accumulate.

## Testing

The `/simulator` page has a *Strategy* selector — pick `momentum_scalper` and
compare it against the live default on identical seeds. As with every synthetic
run, the figures are a **coherence check of the rules**, not a market
prediction: trending profiles overstate the win rate, so weight the `volatile`
and `random` profiles (where the support stops actually trigger) more heavily.
