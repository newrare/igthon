# `dip_rebound` — buy the pullback inside a rising market

**Status:** experimental (`STRATEGY_NAME=dip_rebound`).

- Code: [src/strategies/dip_rebound.py](../../src/strategies/dip_rebound.py)

## Idea

A market that is **globally trending up** but has **just suffered a significant
drop** tends to resume its climb. Rather than chase fresh highs (the
[momentum scalper](momentum-scalper.md)'s job), this strategy waits for the dip
and opens the moment the price turns back up — capturing the rebound from a
better entry, with the dip bottom right below it as a natural stop.

The thesis has three parts, each a gate: the trend must be up, the recent drop
must be real (not noise), and the bounce must have actually started. Missing any
one means either no edge (flat/choppy market), a falling knife (no bounce yet),
or chasing the top (already recovered).

## Mechanics

### Quality gate (before anything else)

1. **Spread gate** — skip when `spread / bid > STRATEGY_MAX_SPREAD_RATIO`. The
   rebound's edge is eaten by a wide spread, exactly as for the scalper.

### Entry — up-trend, then dip, then bounce

2. **Global up-trend** — a linear regression over the last
   `STRATEGY_DIP_REBOUND_TREND_PERIOD` candles must have a *positive* slope and
   an `r_squared` of at least `STRATEGY_DIP_REBOUND_MIN_TREND_R2`. The R² bar is
   deliberately **looser** than a pure trend-follower's: a pullback dents the fit
   of a straight line, so demanding a near-perfect R² would reject the very
   setups this strategy exists to trade.

1. **Significant recent drop** — within the last
   `STRATEGY_DIP_REBOUND_PULLBACK_LOOKBACK` candles the market printed a swing
   high (`recent_high` = highest bid close). The dip bottom (`swing_low` = lowest
   bid low over `STRATEGY_DIP_REBOUND_STOP_LOOKBACK` candles) must sit at least
   `STRATEGY_DIP_REBOUND_MIN_PULLBACK_ATR_K` ATR below that high:

   ```
   pullback_depth = recent_high − swing_low ≥ MIN_PULLBACK_ATR_K × ATR
   ```

   The current bid must still be **below** `recent_high`, so a rebound has room
   left to run — we are not buying a fully recovered top.

1. **Rebound underway** — each of the last `STRATEGY_DIP_REBOUND_REBOUND_PERIOD`
   bid closes must be higher than the one before it. The bounce off the dip has
   actually started, so we buy a live up-tick rather than a knife still falling.

The strategy is **BUY-only** (the live pipeline opens BUY only) and **per-epic**
(immediate-open path; it does *not* use the hourly cross-epic selector).

### Stop — one ATR below the dip bottom

```
stop_level = swing_low − STOP_BUFFER_ATR_K × ATR
```

The dip bottom is the level whose break invalidates the rebound thesis, so the
protective stop sits one `STRATEGY_DIP_REBOUND_STOP_BUFFER_ATR_K` ATR below it.
`level_security` (the broker-side stop sent to IG at open), `level_loose` (the
close-below check) and `level_follower` (the trailing seed) are all pinned to
`stop_level`; the shared `follower` trailing logic can then only ratchet it up.

### Take-profit — reward/risk multiple

```
stop_distance = bid − stop_level
level_win     = bid + WIN_RATIO × stop_distance
```

A rebound has a natural floor (the dip low), so a reward/risk target is the
honest way to size the win: with the default `WIN_RATIO = 2.0` the position is
closed (`win`) by `decide_close_reason` once the bid has moved twice the risk
into profit. Otherwise it exits via the trailing stop or the end-of-day force
close.

## Parameters

| Setting (`.env`)                          | Field                | Default  | Meaning                                            |
| ----------------------------------------- | -------------------- | -------- | -------------------------------------------------- |
| `STRATEGY_DIP_REBOUND_TREND_PERIOD`       | `trend_period`       | `60`     | Candles for the global up-trend regression         |
| `STRATEGY_DIP_REBOUND_MIN_TREND_R2`       | `min_trend_r2`       | `0.55`   | Minimum R² for a genuine (if dented) up-trend      |
| `STRATEGY_DIP_REBOUND_PULLBACK_LOOKBACK`  | `pullback_lookback`  | `30`     | Window for the recent swing high (candles)         |
| `STRATEGY_DIP_REBOUND_MIN_PULLBACK_ATR_K` | `min_pullback_atr_k` | `1.5`    | Minimum dip depth below the high, in ATR multiples |
| `STRATEGY_DIP_REBOUND_REBOUND_PERIOD`     | `rebound_period`     | `2`      | Trailing rising closes confirming the bounce       |
| `STRATEGY_DIP_REBOUND_WIN_RATIO`          | `win_ratio`          | `2.0`    | Take-profit as a multiple of the risk (R/R)        |
| `STRATEGY_DIP_REBOUND_STOP_LOOKBACK`      | `stop_lookback`      | `10`     | Window for the dip bottom — the stop anchor        |
| `STRATEGY_DIP_REBOUND_STOP_BUFFER_ATR_K`  | `stop_buffer_atr_k`  | `0.5`    | ATR cushion placed below the dip bottom            |
| `STRATEGY_MAX_SPREAD_RATIO` (shared)      | `max_spread_ratio`   | `0.0010` | Maximum `spread / bid` to consider an entry        |
| `STRATEGY_ATR_PERIOD` (shared)            | `atr_period`         | `14`     | ATR window for the dip-depth gate and the stop     |

## Limitations

- **Pullback depth is regime-dependent.** `MIN_PULLBACK_ATR_K` is measured in
  ATR, so it self-scales with volatility, but a calm grind-up may never print a
  dip deep enough to trigger — expect few or no trades in low-volatility trends.
- **Catching a falling knife.** The rising-closes filter only proves the *very
  last* candles turned up; a deeper leg down can resume. The stop under the dip
  bottom bounds that, but a fast break-through still costs the full stop distance.
- **No fixed time horizon.** Unlike `trend_template`, there is no reachability
  projection — a rebound that stalls sideways sits open until the trailing stop
  or the end-of-day force close takes it out.
- **BUY-only**, like the rest of the live pipeline; it cannot trade pullbacks in
  a downtrend (which would be the symmetric short setup).

## Testing

The `/simulator` page has a *Strategy* selector — pick `dip_rebound` and compare
it against the live default on identical seeds. As with every synthetic run, the
figures are a **coherence check of the rules**, not a market prediction:
trending profiles rarely print real pullbacks, so weight the `volatile` profile
(where dips and bounces actually occur) most heavily.
