# Zone updater — `adaptive` (zones 1, 2 and 3)

```bash
# .env
CLOSE_ZONESTART=adaptive    # follower → break-even
CLOSE_ZONEMARGE=adaptive    # break-even → margin
CLOSE_ZONESECURE=adaptive   # margin → profit trigger
CLOSE_ZONEPROFIT=trailing_ratchet   # unchanged — the profit zone keeps trailing
```

Source: [src/exit/zones/adaptive.py](../../src/exit/zones/adaptive.py) ·
Composed by [close_zoneprofit](close_zoneprofit.md) ·
Reads [READING.md](../READING.md)

## What it is

Every other updater applies **the same rule to every epic**: a fraction of the
risk here, a swing low there, a fixed confirmation window everywhere. That is the
right shape for a stop *placement* rule, but once a position is open the question
is no longer "where should the stop sit?" — it is **"is this market still doing
what the trade was opened for?"**, and the answer is different on every epic and
every hour.

`adaptive` answers that question from the shared reading facade
([src/core/reading.py](../../src/core/reading.py) — the same measures the
dashboard's *read this market* panel shows), classifies the curve **since the
open** into one of four readable situations, and decides what that situation is
worth **in the zone price currently sits in**.

It is a **hold-or-close** layer: it never moves a stop. The protective stop placed
at open (`STOP_STRATEGY`) stays the floor under everything, and the profit zone
(`CLOSE_ZONEPROFIT`) keeps trailing exactly as configured.

## The reading — four situations

All measured in **profit terms** (rising always means "coming our way"), so a
SELL reads exactly like a long turned upside down. The window is the last
`window` candles (15 by default — the "flat/turned for the last 10 or 20 minutes"
the strategy is written around), clamped to the position's own age.

| Situation  | How it is read                                                                                                                                                                                        |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REVERSAL` | The position **worked first** (MFE ≥ 1 ATR *and* ≥ 2 spreads), the peak is ≥ 5 candles behind, and the window is a **clean** fit (R² ≥ 0.5) giving back ≥ 1.5 ATR — and it is at least 20 candles old |
| `FLAT`     | The window's net move is inside its own adverse-noise band (`PositionReading.is_stalling`) — it went nowhere                                                                                          |
| `FADING`   | Still coming our way, but the net move no longer clears **twice the band** the curve wanders in, or its speed has dropped under 40 % of the average speed since the open                              |
| `TREND`    | Still coming our way, clear of its own band, at speed                                                                                                                                                 |

Anything younger than `min_minutes_open` (10 candles) or without a volatility
scale reads `UNREADABLE` and nothing is decided — the protective stop governs
alone. Both decisions that **book a loss** wait longer still, at the upper end of
the "flat or turned for 10 or 20 minutes" range: the under-water cut
(`flat_cut_min_minutes`, 20) and the reversal (`reversal_min_minutes`, 20). An
adverse move that clears neither bar is an ordinary drawdown, and the protective
stop is what sizes it.

The order of the tests is the order of certainty: a clean adverse run is a
reversal whatever else it looks like, a window inside its own noise is flat
whatever the fit says, and only then is a favourable move split into a
continuation worth holding and a tiring one — on the band it clears, not on how
well it fits a line. An adverse move that is neither clean nor big
enough is an ordinary pull-back inside the trade — held, because that is exactly
what the initial stop is sized for.

## The decision — the same reading, priced per zone

| Situation  | Zone 1 (under break-even)                                          | Zone 2 (break-even → margin) | Zone 3 (margin → profit trigger) |
| ---------- | ------------------------------------------------------------------ | ---------------------------- | -------------------------------- |
| `TREND`    | hold                                                               | hold                         | hold — let it reach the trailing |
| `FADING`   | hold — let it reach break-even                                     | close `flat_breakeven`       | close `flat_profit`              |
| `FLAT`     | close `flat_cut` if old enough **and** the day's budget affords it | close `flat_breakeven`       | close `flat_profit`              |
| `REVERSAL` | close `reversal` **unless the hour behind it still climbs**        | close `reversal`             | close `reversal`                 |

Three cells are what make this different from a stop rule.

### Under water, a reversal is not booked against the background

Everything above reads a **15-candle window since the open**, which cannot tell
"the move turned" from "the move is breathing" — over a quarter of an hour the two
are the same picture. The hour behind them can: while `market.trend(60)` is still a
clean line (R² ≥ `clean_r2`) in the trade's direction, the under-water `reversal`
is refused and the protective stop keeps the floor.

Live on 2026-09-01 (position 926, USD/JPY BUY): cut as a `reversal` at 10:26 on a
15-minute fit of −1.75 ATR while `trend_60m` read +0.047 % at R² 0.53 and price sat
at 47 % of its own 60-minute channel. The cut booked −30.43 €, which is 53 % of the
74.58 € of risk still standing to the stop. The curve then climbed to 10 pips past
break-even (+68 € at its high) without ever coming near that stop.

The guard is deliberately **not** extended to the flat cut. Replayed over the ten
archived weeks that turns 54 cuts into 39 and 31 stop-outs into 38, for
**−1228 €**: a stagnating position held against a rising background mostly walks
to its stop. Only the reversal — the decision that claims to *read a turn* — is
worth checking against the hour behind it.

### Flat under water is *priced*, not guessed

A position that has gone nowhere for a quarter of an hour will not be saved by a
wider stop — but closing it costs real money now. So every flat cut of the day
draws on **one shared budget**, a fixed share of what the day banked *elsewhere*:

```
budget = flat_cost_ratio × max(0, realized_euro)  −  flat_cut_euro
close  when   −gain_euro ≤ budget   and   age ≥ flat_cut_min_minutes
```

A day holding +100 € can spend 20 € on freeing stagnating positions; the 21st
euro waits for the next winner. A day with nothing banked cuts nothing and lets
the protective stops do their job. A position whose exit cannot be priced (no
`€/point`) is never closed on this rule — a missing input must read as *unknown*,
never as *free*.

#### Why the budget is cumulative (and why the cuts are billed apart)

The first version tested each cut on its own against the day's **net**:
`cost ≤ 0.35 × day_realized_euro`. That leaks. Every cut lowers the net, the next
budget is a share of the lowered net, and the series `0.35 × net` telescopes: the
cuts can eventually consume **the whole** banked gain. Measured live on
2026-08-28 — five cuts totalling −51.52 € against the +38.60 € that was banked
when the first one fired, two of them (−27.88 €) authorised in the *same monitor
pass* against the same 16.82 € allowance.

Two things fix it, both carried by [`DayBook`](../../src/exit/zones/base.py):

- the base (`realized_euro`) **excludes** the flat cuts, so the rule cannot erode
  what it is budgeted against, and `flat_cut_euro` is a running bill subtracted
  from a fixed allowance — a hard cumulative cap whatever the sequence;
- the book is **advanced inside the pass** (`DayBook.after_close`) as each
  position closes, so the second stagnating position of a pass sees the budget the
  first one consumed.

Replayed on the 28th's real sequence, the rule as it now stands makes **one** cut
(−4.09 €) instead of five (−51.52 €): two are refused for age, one for budget, one
for both. (The four positions it keeps then live on under their own protective
stop — the 47 € is a loss this rule no longer books, not a gain it earns.)

`DayBook` is the one figure an exit rule cannot derive from a curve. It is opened
**once per monitor pass** (`Scheduler._open_day_book`: today's closed rows, minus
the `never_opened` phantoms — the same aggregation the dashboard's daily P&L tile
uses), advanced as positions close, and threaded down through
`TradingService.manage_position` → `CloseProfile.evaluate` → `StopContext`. The
simulator builds the same book from the day's closed trades, so a backtest
measures the same strategy.

### A visible reversal is closed *before* the stop, not at it

A move that **worked and then broke down** is worth closing at market: riding it
to the stop only pays the rest of the planned risk to learn the same thing.
Recorded as `reversal` so it is distinguishable in the history from a stop-out.

Four conditions, and the first two are the whole difference between a reversal and
a trade that is merely losing:

1. **age** (`reversal_min_minutes`, 20 candles) — a reversal is a *before* and an
   *after*. Read over a window as long as the position's entire life, the two
   collapse into one and every badly-timed entry looks like a turn;
1. **something to reverse** (`reversal_mfe_atr_k`, 1 ATR of MFE, **and**
   `reversal_mfe_spread_k`, 2 spreads) — the position must have shown a real
   favourable excursion first. A trade that never once went our way is not
   reversing: it is inside the drawdown its protective stop was sized for;
1. **the turn is established** (`reversal_turn_minutes`, 5 candles since the peak)
   — not one bad print at the top;
1. **the run is real** — a clean fit giving back at least `reversal_atr_k`
   (1.5) candle-ranges.

#### Why the excursion is measured in spreads too

The ATR floor alone asks the wrong question on a market whose round trip is itself
most of a candle range. USD/JPY on 2026-09-01 was quoted 0.010 wide on a 0.012 ATR
(**0.83 ATR of spread**), so "1 ATR of MFE" was satisfied by a 1.5-pip wick worth
+1.40 € at its best — 4 % of the position's life in profit — and every pull-back
after it read as a turn. The two floors are combined with `max`, so the stricter
one wins.

Measured on the ten archived weeks (54 days, 85 epics, the same
open/stop/profit selection, only this threshold changed): **−6794 € → −5718 €,
+1076 €**, with the reversal cuts falling from 26 to 10 and the max drawdown from
7024 € to 5949 €. With the background guard on top — the two changes as shipped —
the run lands at **−5756 €, +1038 €** (max drawdown 5986 €): the guard costs
nothing measurable once the spread floor has removed the wick-sized reversals, and
it is kept for the case the floor cannot catch. Week by week, the shipped pair is
better on 5, unchanged on 2 and worse on 3 by less than 75 € each.

#### Why conditions 1 and 2 exist

The first version had only conditions 3-4, with `reversal_atr_k` at 1.0. On
2026-08-28 it closed **fifteen** positions for **−718.40 €**. Replayed on the real
candles:

- **eleven of the fifteen had `mfe_in_atr = 0.00`** — they had never once been in
  profit. There was no trend to reverse; the rule had simply relabelled "this trade
  is losing";
- **eight fired at 11-13 minutes of age**, the earliest the rule then allowed, so
  the reading window *was* the position's whole life;
- holding the same fifteen to their protective stop (or to the last candle of the
  session, for those still running) was worth **+250.71 €** — a **969 €** swing.
  The cut won 9 of the 15 for ~23 € apiece and lost 6 for ~196 € apiece: the
  small-win/large-loss shape of every rule that truncates recoveries, the same one
  already measured on `trailing_ratchetmore`.

With conditions 1 and 2 in place, **none of the fifteen fires** — every one is left
to its stop. The rule is not dead code: 9 of the day's 29 positions reached ≥ 1 ATR
of favourable excursion, which is the population it is meant to protect. It simply
no longer fires on trades that never worked.

### A tiring rise is banked

"The market cleared break-even and is now dithering" is the state a trailing stop
handles worst — its stop is still parked behind the noise band, so the whole
excursion leaks away before it fires. The reading sees it directly: the
progression no longer stands out of the band it travels in, or it has kept its
size and lost its speed, so the gain is taken. In the break-even band that is the
*no-loss* exit the plan hoped for (`flat_breakeven`); past the margin line it is
money clear of the epic's own noise (`flat_profit`).

#### Why a band and not a line

The first version answered "is this still the move?" with an R² test: a
favourable window whose regression fit badly (below `clean_r2`) was called
`FADING` and banked. That confuses **the move is over** with **the move is
jagged**. A rise worth several candle-ranges still fits a line badly whenever the
path inside it zigzags — and a zigzag whose amplitude *is* the epic's own noise is
noise, which is precisely what the position was opened to sit through.

Live on 2026-09-02 (position 929, EUR/NZD BUY): closed `flat_profit` eleven
minutes in, at +56.94 €, on a window that had progressed **+2.76 ATR** in the
trade's favour. The regression over those eleven candles read **R² 0.22** — the
curve had made a full round trip back to the entry in the middle — while the same
window cleared **3.6 times** the thickness of its own band. One measure said
"this has stopped being a move", the other said "this is a move with noise in
it", and the second one was right: nothing about that curve had run out.

So the continuation is judged in the unit the objection is written in. The net
favourable move over the window must clear `trend_band_k` (2.0) times the
window's **band thickness** — the standard deviation around its own trend line
(`AmplitudeReading.band_noise`), which a clean ramp scores ~0 on and a wander
scores its whole amplitude on. Under it the progression is no bigger than the
noise carrying it: `FADING`. The R² test stays where a *before and after* is
genuinely being claimed — the reversal, and the background-trend guard.

The ladder that comes out of it reads in one unit, from FLAT to TREND:

```
move ≤ 1 × adverse noise        →  FLAT     (is_stalling — it went nowhere)
1 × noise < move < 2 × band     →  FADING   (progress inside its own noise)
move ≥ 2 × band                 →  TREND    (progress clear of it)
```

Measured over the ten archived weeks, with the same stop and profit-zone
selection and only this test changed, across four openers (the exit change is
worth ~2 % of a book this size, so one opener cannot settle it):

| Opener              |  R² test | band test (k=2) |       Δ |
| ------------------- | -------: | --------------: | ------: |
| `open_donchian`     |  −3898 € |         −3051 € |  +846 € |
| `open_slope`        | −28922 € |        −29702 € |  −780 € |
| `open_steady`       | −20864 € |        −17870 € | +2994 € |
| `open_ultraranking` | −38878 € |        −38024 € |  +854 € |

**+3914 €** in total, positive on three openers of four, and week by week (the
four openers summed) better on 6 of the 10 weeks. `k` at 1.0-1.5 is worse than
2.0 on three of the four openers and 3.0 gives most of the old behaviour back, so
2.0 is kept. The band test closes fewer positions in zones 2-3 and lets more of
them reach their stop or their trailing — that is the trade-off it makes, and the
reason it is measured on the whole book rather than on the win rate.

## Parameters (constants in `src/exit/zones/adaptive.py`)

| Parameter               | Default | Meaning                                                                        |
| ----------------------- | ------- | ------------------------------------------------------------------------------ |
| `window`                | `15`    | Candles the situation is read over, clamped to the position's age              |
| `min_minutes_open`      | `10`    | Nothing is decided before the position has this much history                   |
| `clean_r2`              | `0.5`   | R² above which a fit counts as a line rather than a wander — **reversal only** |
| `trend_band_k`          | `2.0`   | Bands of the window the net favourable move must clear to stay a `TREND`       |
| `reversal_atr_k`        | `1.5`   | Adverse move, in ATR units, that makes the window a reversal                   |
| `reversal_min_minutes`  | `20`    | Age before a reversal can be read — the window needs a before and an after     |
| `reversal_mfe_atr_k`    | `1.0`   | Favourable excursion, in ATR, the trade must have shown before the turn        |
| `reversal_mfe_spread_k` | `2.0`   | The same excursion floor in spreads — the stricter of the two wins             |
| `reversal_turn_minutes` | `5`     | Candles since the peak before the turn counts as established                   |
| `fade_ratio`            | `0.4`   | Window speed under this share of the since-open speed = fading                 |
| `flat_cost_ratio`       | `0.20`  | Share of the day's banked result **all** its flat cuts may consume, together   |
| `flat_cut_min_minutes`  | `20`    | Age before an under-water cut is considered — a cut books a loss               |
| `context_minutes`       | `60`    | Horizon of the background trend that holds the under-water reversal back       |

All of them are ratios or ATR units, so they mean the same thing on a forex pair
and on an index.

## Close reasons it writes

`reversal`, `flat_cut`, `flat_breakeven`, `flat_profit` — all four have their own
label and colour on the dashboard, so a position closed by a *reading* is never
confused with one closed by a price level.

## Mechanics it relies on

- **`ZoneDecision`** ([src/exit/zones/base.py](../../src/exit/zones/base.py)) — a
  zone updater used to be able to return only a stop level. Expressing "close
  this position" as a stop parked just behind the live price would work only by
  accident: it goes through the touch watcher, fills a spread away, and is
  recorded as a stop-out. So `StopUpdater.decide()` returns a stop level **or** a
  close reason; the default implementation wraps `propose()`, so every other
  updater is untouched.
- **`StopContext.reading()`** — builds the `PositionReading` once per tick from
  the unsliced buffer plus the open instant, so `curve` measures only what
  happened since the open while `market` keeps the history the amplitude measures
  need. Available to every future updater, not just this one.

## Backtesting — the open instant is now replayed

`SimulatedTrade` used to carry only an `HH:MM` label, so
`CloseZoneProfit._opened_at` returned `None` and the buffer was **not sliced** to
the position's life: `minutes_open`, the MFE and the since-open average speed all
read the whole day. It affected every since-open gate (`trendcut` and
`breakeven_lock` included), and it distorted this updater's verdicts most of all —
positions read as hours old the moment they opened.

The simulator now stamps `date` / `time_open` from the opening candle (and its
fictional days are UTC-aware, so the slicing compares like with like), which makes
a replay read exactly what the live monitor pass reads. The figures quoted above
are all from the corrected engine; anything measured on `adaptive` before it should
be re-run.
