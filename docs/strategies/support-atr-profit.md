# Close profile — `support_atr_profit`

A drop-in variant of [`atr_trailing_profit`](#) that changes **only where the
protective stop is first placed at open**. Every per-tick decision afterwards —
the profit gate, the momentum confirmation, the ATR chandelier that follows the
bid up, the dead-band rule and the close triggers — is **inherited unchanged**
from `atr_trailing_profit`, because that trailing behaviour works well in live
trading and must not move.

Implemented in [`src/exit/support_atr_profit.py`](../../src/exit/support_atr_profit.py).
Select it at runtime from the dashboard, or via `CLOSE_PROFILE_NAME=support_atr_profit`.

## Why

The reference profile places the initial stop a flat `stop_atr_k × ATR(14)`
below the entry. On 1-minute candles that ATR spans only ~14 minutes, so after a
quiet patch the stop is glued to the entry and ordinary bid/offer noise closes
the position before it can breathe. This profile anchors the stop **below a real
support level** and makes its distance **per-epic and noise-aware**.

## The stop

```
support    = weighted_support(bid_lows[-stop_lookback:])   # robust last-hour low
raw_stop   = support − stop_buffer_atr_k × ATR              # cushion under support
min_dist   = max(min_stop_atr_k × ATR, min_stop_spread_k × spread)   # floor
distance   = max(entry − raw_stop, min_dist)               # never tighter
distance   = min(distance, max_stop_atr_k × ATR)           # optional cap (0 = off)
stop_level = entry − distance
```

### Weighted support (the noise measure)

Instead of the single lowest bid low of the window — which one freak wick can
drag to an extreme — the support is a **recency-weighted low quantile** of the
last `stop_lookback` bid lows:

- a lone spike low is outvoted by the mass of the distribution, so the stop sits
  under the level the market *actually defends*, not under a one-off wick;
- recent candles weigh more than hour-old ones (exponential decay,
  `support_recency_half_life`), so the support tracks the level being defended
  *now*.

### Distance floor (never tighter than today)

The distance is floored at `max(min_stop_atr_k × ATR, min_stop_spread_k × spread)` — i.e. **never tighter** than the reference profile's stop, and never
inside a couple of spreads. An **upper cap** (`max_stop_atr_k`, default `4×ATR`)
clips a far support so a single deep-support trade cannot risk the whole hourly
range; the floor always wins over the cap. Set `max_stop_atr_k = 0` to disable
the cap entirely and let the raw support stand (then `euro_loss_max`, the open
gate, is the only bound). Only the BUY initial stop is re-derived (long-only
pipeline); a SELL keeps the inherited ATR stop.

## Parameters (constants in the module)

| Constant                    | Default | Meaning                                            |
| --------------------------- | ------- | -------------------------------------------------- |
| `stop_lookback`             | 60      | support window (candles ≈ last hour on 1-min data) |
| `stop_buffer_atr_k`         | 0.5     | ATR cushion placed below the detected support      |
| `support_percentile`        | 0.20    | weighted low quantile (lower → wider stop)         |
| `support_recency_half_life` | 30.0    | recency weighting half-life, in candles            |
| `min_stop_atr_k`            | 2.5     | distance floor (× ATR) — never tighter than this   |
| `min_stop_spread_k`         | 2.0     | distance floor (× spread) — never inside noise     |
| `max_stop_atr_k`            | 4.0     | distance cap (× ATR); 0 = no cap                   |

Defaults tuned on a 6-day recorded-candle backtest: `cap=4×ATR` with the 20th
percentile roughly matched the reference profile's return while cutting noise
stop-outs ~in half and lifting the win rate from 35% to 50%.

The trailing knobs (`atr_k_pre`, `atr_k_post`, `noise_k`, …) are inherited from
`atr_trailing_profit` and unchanged.
