# Close profile — `close_zoneprofit`

The project's single close profile. It **composes** the two decoupled
stop responsibilities rather than owning them:

- at open, it delegates the initial protective stop to a **stop-distance policy**
  (`src/stops/`), typically the recency-weighted `stop_support` distance;
- on every tick it classifies the live **close-out price** into one of four zones
  and delegates to the matching **per-zone stop updater** (`src/exit/zones/`),
  **each selected independently in `.env`** so a zone can be tuned without
  influencing the others:
  - `CLOSE_ZONESTART` — follower (red) → break-even (white): `hold` keeps the
    initial stop; `trendcut`, `timedlift`, `smartgroup` reduce the risk carried;
  - `CLOSE_ZONEMARGE` — break-even (white) → margin (dotted blue): `hold`,
    `breakeven_lock`, `breakeven_safe`, or `limitloose` (move the stop under the
    market at once);
  - `CLOSE_ZONESECURE` — margin (dotted blue) → profit trigger (dotted green):
    `hold` or `breakeven_half` (secure the midpoint of the break-even→margin band
    at once);
  - `CLOSE_ZONEPROFIT` — past the profit trigger: `trailing_ratchet`
    (momentum-gated ATR chandelier that follows price in steps) or
    `trailing_ratchetmore` (the same, but it also caps how much of the peak gain
    can be given back and narrows the trail as the run extends).

The full list of selectable updaters per zone is in
[README.md](README.md#available-zone-updaters).

## The four zones

The three references frozen at open cut the price axis into four regions, each
with its own selector. They are the lines drawn on the dashboard chart:

| Zone            | From → to                              | Selector           | Job                                             |
| --------------- | -------------------------------------- | ------------------ | ----------------------------------------------- |
| Underwater      | follower (red) → break-even (white)    | `CLOSE_ZONESTART`  | reduce the risk still carried                   |
| Break-even band | break-even (white) → margin (blue)     | `CLOSE_ZONEMARGE`  | stop carrying the open risk, gain still ≈ noise |
| Secure          | margin (blue) → profit trigger (green) | `CLOSE_ZONESECURE` | secure the gain that cleared the noise band     |
| Profit          | above the profit trigger (green)       | `CLOSE_ZONEPROFIT` | trail a sustained move                          |

> **Design fix (2026-07-31).** The margin→profit region had **no selector of its
> own**: the break-even band deliberately ran all the way up to the profit
> trigger, so `CLOSE_ZONEMARGE` governed a band it was not designed for and a
> price hovering just past the margin was managed by a break-even rule. It is now
> `CLOSE_ZONESECURE`, selected like the other three.

### `CLOSE_ZONEMARGE=limitloose` — cap the loss the instant break-even is cleared

The moment the close-out price trades past break-even, the position must stop
carrying the full risk it opened with. `limitloose` moves the stop **on that very
tick** — no confirmation streak — to

```
stop = price − noise_mult × adverse_tick_noise      # below for a BUY, above for a SELL
                                                    # noise_mult = 2 (double band)
```

a **double** noise band of the epic being traded (the same band `smartgroup` and
the profit trailing measure), floored at IG's minimum stop distance so the broker
accepts it. It is deliberately *loose*: the level may still sit short of
break-even — it caps the loss rather than locking a profit — and it keeps
following price, tighten-only, for as long as price stays in the band. When the
epic has no measurable noise and IG declares no minimum (a flat tape), the stop
would land on the live price and the software backstop would close at once, so the
updater holds instead.

### `CLOSE_ZONESECURE=breakeven_half` — secure the midpoint at once

Past the margin the move has cleared the epic's churn, so the gain is secured
**immediately**, again with no confirmation streak:

```
stop = level_zero + 0.5 × (level_margin − level_zero)      # the midpoint
```

exactly halfway between break-even and the margin line. Inside the band rather
than on the margin line: it locks half of the noise margin while staying at least
a full noise margin below the live price, so an ordinary pull-back inside the zone
cannot reach it. It is a fixed level, not a trailing — the stop moves once and
stays until the profit zone takes over past the green line. Tighten-only, and
never placed at or past the live price.

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
The four zones are set once at startup via `CLOSE_ZONESTART` / `CLOSE_ZONEMARGE` /
`CLOSE_ZONESECURE` / `CLOSE_ZONEPROFIT` (the single source of truth — no default,
no runtime switching);
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

### `trailing_ratchetmore` — give back less of a long run

`trailing_ratchet` trails an extended winner at the same width it started with,
and when price turns hard its sharp-reversal guard *holds* the stop where it was.
Both are deliberate (a trend needs room, and a lagging anchor must not tighten
into a reversal), but together they let a trade that ran several ATR into profit
walk all the way back to a stop set when the run began. `trailing_ratchetmore` is
the same updater — same floor, same momentum-gated chandelier, same tighten-only
discipline — with two additions, both driven by the **peak excursion since this
position's open**:

| Constant               | Default | Effect                                                                             |
| ---------------------- | ------- | ---------------------------------------------------------------------------------- |
| `giveback_retention`   | `0.5`   | Share of the peak gain the stop keeps locked — at most half the run is handed back |
| `giveback_arm_atr`     | `1.0`   | Peak gain (× ATR) required before either addition arms                             |
| `atr_k_floor`          | `1.2`   | Narrowest trailing width the shrink may reach (× ATR)                              |
| `atr_k_shrink_per_atr` | `0.25`  | Width removed per ATR of peak gain beyond the arming threshold                     |

The give-back cap is anchored on the **peak**, which is what makes it different in
kind from the other two candidates: the chandelier reads the live price and is
momentum-gated (idle exactly when price is falling), and the floor reads a swing
low that lags by its confirmation window. So the cap is the one candidate that
still works during the reversal, and it is deliberately exempt from the
sharp-reversal hold — a peak-anchored level cannot be pushed forward by a stale
anchor. Every other guard still applies: nothing lands in the dead band between
break-even and the margin, nothing sits at or past the live price, and the stop is
never loosened. Setting `giveback_retention=0` and `atr_k_shrink_per_atr=0` makes
it behave exactly like `trailing_ratchet`.

> **Measured (2026-08-03) — it is worse, do not select it.** Every real position
> closed over the six archived days (2026-07-27 → 2026-08-03, 147 trades) was
> replayed candle by candle from its own open, twice: once with `trailing_ratchet`
> and once with `trailing_ratchetmore`, everything else identical (same persisted
> references, same stop placed at open, same three other zones, same intra-candle
> stop fill). Result, on that replay basis: **−520 € with `trailing_ratchet` and
> −1463 € with `trailing_ratchetmore`.** Isolating the two additions: the width
> shrink alone costs −225 €, the give-back cap alone −688 €.
>
> The pattern is the same everywhere and is the point: `trailing_ratchetmore`
> **wins small and often, loses big and rarely** — 28 trades improved for +707 €,
> 18 degraded for −1649 €. Both mechanisms truncate the fat tail, and this system's
> P&L is carried by its few largest winners (one 2026-08-03 trade alone: 754 € →
> 476 € once the cap armed). Loosening the cap does not fix it (`0.35` retention:
> still −993 €); tightening it makes it far worse (`0.65`: −452 € on one day).
>
> Caveat on the absolute figures: the replay is an **A/B harness, not a P&L
> reconstruction** — it fills the stop intra-candle at the follower, whereas live
> the broker order rests a noise cushion beyond it and the real follower ratchets on
> sub-minute ticks, and it applies today's zone composition to earlier days. So the
> replay totals differ from the booked ones (2026-08-03: 967 € replayed vs 1427 €
> booked). Only the variant-vs-variant comparison, run on identical emulation, is
> meaningful — and it is stable in sign across all six days.

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

## Zone 1 — `smartgroup` (one decision for the whole book)

Every other updater reasons about a single position. `smartgroup`
([`src/exit/zones/smartgroup.py`](../../src/exit/zones/smartgroup.py)) reasons
about **the book**, and its decision applies to **every open position** — winners
included — not only to the ones its zone-1 slot would normally cover.

The rule, in one sentence: *if closing every open position at "its live price
minus its own noise" would already bank a net gain, park every stop exactly
there.*

On each monitor tick, for the whole book at once:

```
cushion_i   = max(adverse_tick_noise_i, min_stop_distance_i)   # the epic's own jitter
candidate_i = price_i − sign_i × cushion_i                     # just beyond the churn
slip_i      = exec_slip_k × (spread_i + cushion_i)             # what the fill costs
fill_i      = candidate_i − sign_i × slip_i                    # where it really closes
euro_i      = sign_i × (fill_i − level_open_i) × euro_per_point_i
euro_i     −= reconcile_margin_pct × |euro_i|                  # IG-vs-us drift

arm  ⇔  Σ euro_i > min_group_euro (0 €)
```

When it arms, every `candidate_i` that is a legal tightening becomes the position's
new stop in one pass. When it does not, nothing moves — the behaviour is exactly
`hold`, which is also what happens at open (a fresh book is never green net of
noise).

Worked example — five positions, 10 €/point, noise 0.3:

| Position   | Open | Price | Candidate | Euro at candidate |
| ---------- | ---- | ----- | --------- | ----------------: |
| winner     | 100  | 120   | 119.7     |            +197 € |
| flat loser | 50   | 49.5  | 49.2      |              −8 € |
| flat loser | 50   | 49.5  | 49.2      |              −8 € |
| sinking    | 50   | 46    | 45.7      |             −43 € |
| sinking    | 50   | 46    | 45.7      |             −43 € |
| **total**  |      |       |           |         **+95 €** |

+95 € > 0, so all five stops — including the winner's, which is pulled up past its
margin line — move onto their candidate. The trade is deliberate: some of those
stops **will** be hit and book small individual losses, but the arithmetic
guarantees the book is green when they all are, and the ones that survive keep
running for more.

### Two safety haircuts (why the book is valued below its stops)

Valuing each position **at its stop level** is optimistic, and not by a little. A
live book showed `ARMED — book at planned stops 19.29€`, carried by a winner
counted at +99.00 € on a follower of 5572.30. That stop fired 3 minutes later and
IG booked **+64.84 €** — 35 % of the contribution lost between the level and the
fill, on its own more than the whole 19.29 € the gate was cleared by. The book was
never green; only its arithmetic was.

Nothing was drifting: the sum was exact both before and after
(`19.29 = 99.00 − 56.93 − 22.78`, then `−79.71 = −56.93 − 22.78`). The error is
structural — a stop is never filled at the level it sits on:

- the **software follower** is only tested between two polls and then closes at
  market, so the fill is one poll of movement late;
- the **broker order** is parked deliberately further out — one spread plus an
  ATR-scaled cushion (`_broker_stop_level`) — so noise cannot trip it before the
  follower does. In the case above that gap was 12.11 points; the actual fill
  landed inside it, ~5.2 points below the follower.

So the valuation charges two pessimistic haircuts (the levels the plan *places*
are untouched — only what the book claims they are worth changes):

| Haircut                | What it covers                                                 | Default |
| ---------------------- | -------------------------------------------------------------- | ------- |
| `exec_slip_k`          | share of the follower→broker-stop gap lost on the fill         | 0.5     |
| `reconcile_margin_pct` | our `euro_per_point` vs IG's booked euros (FX, fees, rounding) | 0.02    |

`exec_slip_k = 0` restores the old (optimistic) valuation; `1.0` values every
position at its broker order — the worst level it can fill at, barring a gap. The
reconciliation margin is taken on the **absolute** euro figure, so it shrinks
gains *and* deepens losses. Replaying the case above with the defaults, the winner
no longer carries the book and the plan does not arm.

Two departures from a literal `price − noise`, both conservative:

- the cushion is floored at IG's `min_stop_distance` — a stop the broker would
  reject is not a stop, and a wider cushion only lowers the estimate;
- a stop is **never loosened**: a candidate that does not beat the position's
  current follower is skipped (it keeps the better stop it has), and one that
  would sit on the live price is skipped too (the software backstop would close
  the position on the spot).

A skipped position is valued in the sum **at the stop it actually keeps**, never
at the candidate it will not be moved to. That is what makes the gate honest:
every euro in the total sits behind a stop that is either already resting at IG
or about to be placed this tick. Counting a skipped position at its candidate
would break exactly the case the rule exists for — a position on a flat plateau
(noise 0, no broker minimum) cannot be tightened at all, so its whole paper
profit would be claimed as protected while its stop stays at the wide level it
opened with. A position that can neither be tightened nor already carries a stop
has unbounded downside and disarms the plan outright.

Because the decision is portfolio-level it is computed once per tick, before any
position is managed: the monitor loop resolves every open position's live bid,
`CloseZoneProfit.group_member()` reduces each to plain scalars, and the pure
`explain_group_tightening()` returns `position_id → stop level`. Each position is then
fed **its own** answer through `StopContext.group_tighten`, so the updater itself
stays as pure as the others. The level is applied in all four zones and wins
whenever it is tighter than what the zone's own updater proposed — the profit
trailing is never traded away for a looser group stop. From there it takes the
normal ratchet path (up-only, broker push a spread beyond, min-distance clamp).

### Reading the log

A tick that misses the gate produces an empty plan, which on its own looks exactly
like the pre-pass never running. So `explain_group_tightening()` returns the plan
**plus** its arithmetic (`GroupPlanReport`), and the monitor logs one line per pass:

```
Group stop plan: hold — book at planned stops -75.69€ vs gate 0.00€ (live -1.38€), 3/5 position(s) movable
Group stop plan: ARMED — book at planned stops 12.40€ vs gate 0.00€ (live 61.20€), 5/5 position(s) movable, tightening 5
```

The two totals answer different questions: **live** is the book's unrealized P&L
right now, **at planned stops** is what it would bank if every stop were hit —
net of both haircuts above. The second is always the lower of the two (each
candidate sits one noise cushion the wrong side of its price, and its fill one
slip further still), and it is the only one the gate looks at — a book that is
green live can still be well short of arming. `DISARMED` names the single
unprotected position that voided the plan; a `WARNING` names any position that could
not be priced. Per-position arithmetic (candidate, follower, the **fill level**
each was valued at — its stop moved one `exec_slip` the adverse way — and its
euros) is one `DEBUG` line each:

```
Group member 688 (CC.D.LSU.UNC.IP): candidate=457.17500 follower=457.70000 -> valued at 457.70000 = -151.80€ (keeps its stop)
```

| Constant                       | Default | Meaning                                                    |
| ------------------------------ | ------- | ---------------------------------------------------------- |
| `noise_window` / `noise_std_k` | 20/2.0  | adverse-tick-noise band = the step back from the price     |
| `min_group_euro`               | 0.0     | euros the book total must exceed for the plan to arm       |
| `exec_slip_k`                  | 0.5     | share of the follower→broker-stop gap charged to the fill  |
| `reconcile_margin_pct`         | 0.02    | fraction of each member's euros dropped for IG-vs-us drift |
