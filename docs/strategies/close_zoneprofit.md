# Close profile — `close_zoneprofit`

The project's single close profile. It **composes** the two decoupled
stop responsibilities rather than owning them:

- at open, it delegates the initial protective stop to a **stop-distance policy**
  (`src/stops/`), typically the recency-weighted `stop_support` distance;
- on every tick it classifies the live bid into one of three zones and delegates
  to the matching **per-zone stop updater** (`src/exit/zones/`), **each selected
  independently in `.env`** so a zone can be tuned without influencing the others:
  - `CLOSE_ZONESTART` — open → break-even (`hold`: keep the initial stop);
  - `CLOSE_ZONEMARGE` — break-even → margin (`hold`: keep the initial stop);
  - `CLOSE_ZONEPROFIT` — above the margin (`trailing_ratchet`: momentum-gated ATR
    chandelier that follows the bid up in steps).

The trailing behaviour works well in live trading and is moved **verbatim** into
the profit-zone updater — it must not change.

Implemented in [`src/exit/close_zoneprofit.py`](../../src/exit/close_zoneprofit.py),
the single composer profile (persisted on the position as `close_zoneprofit`); the
per-zone updaters live in [`src/exit/zones/`](../../src/exit/zones/) and are
registered per zone in [`src/exit/zones/__init__.py`](../../src/exit/zones/__init__.py).
The three zones are set once at startup via `CLOSE_ZONESTART` / `CLOSE_ZONEMARGE` /
`CLOSE_ZONEPROFIT` (the single source of truth — no default, no runtime switching);
the initial stop distance is chosen independently via `STOP_STRATEGY`
(`stop_support` or `stop_atr`).

## Why the support distance

A flat `stop_atr_k × ATR(14)` stop below the entry is glued to the entry after a
quiet patch (on 1-minute candles that ATR spans only ~14 minutes), so ordinary
bid/offer noise closes the position before it can breathe. The `support`
distance ([`src/stops/stop_support.py`](../../src/stops/stop_support.py)) instead anchors
the stop **below a real support level** and makes its distance **per-epic and
noise-aware**.

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
gate, is the only bound). Only the BUY stop is support-derived (long-only
pipeline); a SELL falls back to a flat `stop_atr_k × ATR` above the offer.

## Parameters (constants in `src/stops/stop_support.py`)

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
percentile roughly matched the flat-ATR return while cutting noise stop-outs ~in
half and lifting the win rate from 35% to 50%.

The trailing knobs (`atr_k_pre`, `atr_k_post`, `trailing_step_ratio`) are
constants on the profit-zone updater
([`src/exit/zones/trailing_ratchet.py`](../../src/exit/zones/trailing_ratchet.py));
the noise-margin `noise_k` is a constant on the profile
([`src/exit/close_zoneprofit.py`](../../src/exit/close_zoneprofit.py)).
