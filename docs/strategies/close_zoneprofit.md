# Close profile — `close_zoneprofit`

The project's single close profile. It **composes** the two decoupled
stop responsibilities rather than owning them:

- at open, it delegates the initial protective stop to a **stop-distance policy**
  (`src/stops/`), typically the recency-weighted `stop_support` distance;
- on every tick it classifies the live **close-out price** into one of three zones
  and delegates to the matching **per-zone stop updater** (`src/exit/zones/`),
  **each selected independently in `.env`** so a zone can be tuned without
  influencing the others:
  - `CLOSE_ZONESTART` — open → break-even (`hold`: keep the initial stop;
    `trendcut`, `timedlift`, `smartgroup`: reduce the risk still carried);
  - `CLOSE_ZONEMARGE` — break-even → margin (`hold`: keep the initial stop);
  - `CLOSE_ZONEPROFIT` — past the margin (`trailing_ratchet`: momentum-gated ATR
    chandelier that follows price in steps).

The full list of selectable updaters per zone is in
[README.md](README.md#available-zone-updaters).

## BUY and SELL use the same zones

The profile is **direction-aware**: one instance manages both sides and every
`CLOSE_ZONE*` selector applies unchanged to a short. Direction enters in exactly
three places:

| Concept              | BUY                         | SELL                         |
| -------------------- | --------------------------- | ---------------------------- |
| Close-out price      | the **bid** (sell to close) | the **offer** (buy to close) |
| `level_zero`         | the entry offer             | the entry bid                |
| `level_margin`       | `level_zero + noise_margin` | `level_zero − noise_margin`  |
| Profit trigger       | `2 × margin − zero` (above) | `2 × margin − zero` (below)  |
| A tighter stop moves | up                          | down                         |

Every updater reasons in *profit* terms through `StopContext` — `gain(level)`,
`beyond(level, ref)`, `offset(ref, distance)` and the sign-normalised
`favourable_closes` series, in which "rising" always means "moving into profit".
Nothing in a zone updater branches on the side.

> **Regression (2026-07-27).** Shorts used to be routed to a separate `close_short`
> profile that had **no zones at all** — only the profit-zone chandelier, gated
> behind `new_stop < level_margin`. A SELL therefore had to run ≈ 4 × ATR past
> break-even before its stop moved a single point, and the margin-zone lock
> (`CLOSE_ZONEMARGE`) never ran on a short. That profile is gone; the SELL side is
> covered by [`tests/test_exit_short.py`](../../tests/test_exit_short.py).

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

## Break-even is re-anchored on the real fill

`initial_plan()` freezes the two references every zone is measured against —
`level_zero` (break-even) and `level_margin` (`level_zero ± noise_k × ATR`, the
sign towards profit) — from the **last recorded candle**, before the order is
sent: the offer for a long (a long is filled on the offer), the bid for a short.
That is exactly the touch the order prices through.

The market moves between that snapshot and the fill, so the level IG confirms can
land several points away (4 points observed on `CC.D.NG.UNC.IP`). The references
are therefore **translated onto the confirmed fill** as soon as it is known —
in `/confirms`, or at bind time from `GET /positions` when the confirm never came
back (`TradingService._reanchor_exit_references`). The band width
(`level_margin − level_zero`), and with it the derived profit trigger
(`2 × level_margin − level_zero`), is preserved; only the anchor moves.

Without it the whole exit runs on a frame the position never traded on: the zone
classifier reads a break-even below the real entry, `CLOSE_ZONEMARGE` locks a stop
that looks like profit but sits at break-even, and the chart draws a break-even
line the trade never crossed. The protective stops are **not** shifted — they are
market-structure levels already resting at the broker, so a slipped fill widens
the real risk instead of moving the stop (`euro_stop` keeps the pre-fill figure).

## Zone 1 — `timedlift` (periodic re-computation under break-even)

`hold` freezes the stop chosen at open for the whole under-water excursion,
whatever happens in between: a trade that opened on a spike and then built a
solid floor twenty points higher keeps risking the full initial distance.
`timedlift` ([`src/exit/zones/timedlift.py`](../../src/exit/zones/timedlift.py))
re-reads that floor **on a fixed cadence** instead:

- for the first `period_minutes` (10 by default) the stop posted at open is left
  strictly untouched — the trade needs room to breathe first;
- from then on, once per period, the **last completed period** is reviewed: the
  stop is moved in behind the floor that period printed, or kept exactly where it
  is. It is **never loosened**, and never placed at or past break-even (locking a
  profit is `CLOSE_ZONEMARGE`'s job). On a short the "floor" is the period's
  highest offer and the stop comes *down* onto it.

Two distances keep a tightened stop from becoming a fragile one:

```
noise      = adverse_tick_noise(bid)                       # the epic's own jitter
support    = min(bid_low of the last completed period)     # the floor just printed
cushion    = max(cushion_noise_mult × noise, cushion_atr_k × ATR, spread)
candidate  = support − cushion                             # under the floor, not on it
safety     = max(safety_noise_mult × noise, safety_atr_k × ATR,
                 safety_spread_k × spread, min_stop_distance)
lift only if  candidate ≤ bid − safety                     # else HOLD, never clamp
```

The safety clearance is a **veto, not a clamp**: when the period's floor sits too
close to where price trades now, the stop simply does not move — getting closer to
the bid than the epic's noise allows is never traded for a tighter stop. Because
the review window is quantised to period boundaries (not a window sliding with
each tick), the proposed level is constant for the whole period, so the stop moves
at most once per period instead of degenerating into an under-water trailing stop.

| Constant                                           | Default     | Meaning                                         |
| -------------------------------------------------- | ----------- | ----------------------------------------------- |
| `period_minutes`                                   | 10.0        | review cadence, and the initial grace period    |
| `cushion_noise_mult` / `cushion_atr_k`             | 1.0/0.5     | cushion kept below the period's floor           |
| `safety_noise_mult` / `safety_atr_k` / `_spread_k` | 2.0/1.0/2.0 | minimum clearance kept below the live bid       |
| `min_advance_atr_k`                                | 0.25        | minimum improvement (× ATR) worth a broker push |
