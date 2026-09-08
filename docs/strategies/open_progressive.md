# `open_progressive` — one market at a time, risk funded by the day's gains

Entry strategy (`OPEN_STRATEGY`, `src/entry/open_progressive.py`). Cross-epic
**ranker** (`cross_epic_selection = True`), **one position at a time**, **long-only**,
with a **risk ladder** driven by the day's realised P&L.

```env
OPEN_STRATEGY=open_progressive
```

## Idea

Two rules, and they only make sense together.

**One market at a time.** `concurrent_positions = 1`, no wallet-bounded fan-out: the
bot holds exactly one position, re-ranks the whole tradable universe the moment it
closes, and opens the next one. Nothing is diversified away — the day is a *sequence*
of single bets, each judged on its own.

**The bet size is paid for by the day.** Because only one position exists at a time,
the amount at risk is the only lever left, and it is driven by what the day has
already **banked**. The bot starts allowed to lose at most **50 €** on a stop-out.
Once 50 € are realised, it stops looking at those small markets and only ranks the
ones whose stop-out costs **50–100 €**; at 100 € banked it moves up to **100–200 €**,
then **200–500 €**. A loss walks the ladder straight back down.

## The ladder

The rung is recomputed on **every selection pass** from a single allowance:

```text
allowance = 50 € + max(0, realised_today)
ceiling   = the highest ladder rung <= allowance
floor     = the rung just below it (0 on the first rung)
```

A candidate qualifies when `floor < estimated risk <= ceiling`.

| Realised today | Allowance | Band opened to candidates |
| -------------- | --------- | ------------------------- |
| ≤ 0 €          | 50 €      | 0 – 50 €                  |
| +50 €          | 100 €     | 50 – 100 €                |
| +150 €         | 200 €     | 100 – 200 €               |
| +450 €         | 500 €     | 200 – 500 €               |
| +5 000 €       | 5 050 €   | 200 – 500 € (top rung)    |

**The invariant this buys**, and the reason the ladder is written this way: *a full
stop-out can never put the day below −50 €*, whatever rung it happens on, because the
risk taken is never more than the base 50 € plus what is already in the bank. The
escalation is paid for by the market, not by the account.

The band is **derived, never latched**: `realised_today` is read fresh on every pass,
so handing the gains back walks the ladder down again with no reset to trigger.

`realised_today` counts **closed positions only** — an open position in profit is not
a gain. It is the exact figure the dashboard's daily P&L tile shows (stored `euro`,
level-based fallback for legacy rows, `never_opened` phantoms excluded), so the ladder
and the screen can never disagree.

### The strict band, and the fallbacks

While the band holds candidates the cheaper markets are not ranked at all
(`strict_band = True`): escalating means escalating, and a 20 € trade taken at +120 €
realised cannot move the day. Set `strict_band = False` to turn the band into a plain
ceiling if that preference is not wanted.

**An empty band is not a reason to sit out.** It only means no epic happens to be
priced in that slice right now, which says nothing about the opportunity. Three scopes
are tried in order and the first that yields candidates wins:

| #   | Scope         | Range                            | Invariant                        |
| --- | ------------- | -------------------------------- | -------------------------------- |
| 1   | the band      | `floor < risk <= ceiling`        | held                             |
| 2   | **cheaper**   | `risk <= floor`                  | held — a smaller stop costs less |
| 3   | **stretched** | `ceiling < risk <= ceiling×1.25` | loosened, bounded by the stretch |

Trading *down* comes first because it is free: a stop-out below the rung can only cost
less than the rung already paid for. Stretching the ceiling (`band_stretch`, 25 %) is
the one move that loosens the invariant — a stop-out can then reach
`−base − stretch × ceiling` — so it is tried last, only when nothing at all is
affordable below the ceiling, and logged as a warning:

```text
Progressive risk ladder: no candidate at or below 100€ — stretching the ceiling
by 25% to 125€ (2 found); a stop-out can now cost more than the day has banked
```

It exists because the euro risk of a candidate is an estimate to begin with, so a rung
boundary is a soft line. Set `band_stretch = 0.0` (and/or
`allow_cheaper_fallback = False`) to restore "stay flat rather than trade off-rung".
Nothing is opened only when all three scopes are empty.

Two limits worth knowing before reading a live log:

1. **The ladder cannot conjure a rung.** An epic's euro risk is a property of the
   market (minimum stop distance, euro-per-point) and of `STOP_STRATEGY` — not of this
   entry. When the universe holds nothing priced into the band, escalating means
   falling back, so the log routinely shows a trade taken outside the nominal rung.
1. **The risk figures are estimates.** They come from `euro_per_point`, exact for a
   euro-quoted instrument and a reference-rate approximation for a foreign quote.
   Treat a rung boundary as a soft line, not a guarantee to the cent.

### Enough candidates before opening

This entry acts on a **single** winner, so a tournament of three epics would crown the
least-bad of three rather than the best of the day. Both participation gates are on:

- `min_participation_ratio = 0.5` — strictly more than half the livestreamed tradable
  universe must be warmed up (`len(buf) >= warmup`) before a winner is declared, which
  guards against the "false tournament" right after a mid-session restart;
- `min_participation_count = 20` — at least 20 scorable epics, the gate that bites when
  the universe itself is small (a 10-epic universe passes the ratio with 6 ready).

Both are enforced by `BotScheduler._select_and_open` before any scoring, so a pass that
cannot form a real ranking is a cheap no-op:

```text
Rolling select [open_progressive]: only 12 warmed-up epic(s) < 20 required
candidates — ranking not valid, skipping
```

### How a candidate is priced

The scheduler runs the same arithmetic the open path will, one step ahead of it
(`BotScheduler._risk_estimates`):

```text
risk = max(|bid − initial_plan(bid).stop_level| × euro_per_point,
           stop_loss_eur × (1 + STRATEGY_STOP_MIN_DISTANCE_MARGIN))
```

— the composed close profile's own stop, floored by IG's padded minimum stop distance
(a tighter strategy stop is clamped out at open and really costs that minimum), priced
for one minimum deal size. An epic whose figure cannot be resolved is **dropped, not
assumed cheap**: an unknown cost is precisely what the ladder's bound cannot cover.

## Which market — "the surest to beat break-even fastest"

A position opens in the red: a BUY is filled at the offer and closes on the bid, so
the bid must climb **one whole spread** before the trade is worth anything. That
distance is what this ranker times, from the fitted trends of `CurveReading`
(`src/core/reading.py`):

```text
minutes_to_breakeven = spread / min(slope_5m, slope_15m)
```

The **slower** of the two fitted slopes is used on purpose: a 5-minute spike cannot
promise a speed the 15-minute trend does not support.

Everything else answers *how sure is that?*, combined as a **weighted geometric mean**
— bounded above by its smallest term, so one weak dimension collapses the score and no
strong component rescues a fragile market (unlike an additive sum):

| Component    | Weight | Reads                                                       |
| ------------ | ------ | ----------------------------------------------------------- |
| `speed`      | 0.40   | `target / (target + minutes_to_breakeven)`, never saturates |
| `quality`    | 0.25   | 15-minute regression R² — a line, not a wander              |
| `efficiency` | 0.15   | Kaufman ER over 30 min — the path went somewhere            |
| `pullback`   | 0.10   | `1 − retracement / range` — the give-back a holder took     |
| `alignment`  | 0.10   | agreement of the 5/15/30/60-minute horizons                 |

### Hard vetoes (run first, before any scoring)

A ranker must crown the best of its pool, so a penalty never rejects — these do:

| Veto                              | Refuses                                        |
| --------------------------------- | ---------------------------------------------- |
| `regime is not TREND_UP`          | a path that is not directionally up            |
| 5- or 15-minute trend not `UP`    | a rise only one horizon can see                |
| `is_fading`                       | the 5-min speed already opposes the 30-min one |
| `spread_in_atr > 1.0`             | the round trip costs more than a whole candle  |
| `spread_ratio > 1.5`              | a book quoting defensively (news, thin)        |
| `volatility_ratio > 1.5`          | a spike against the epic's own baseline        |
| `room_above < spread + 1 × ATR`   | a resistance sitting where the climb stalls    |
| `minutes_to_breakeven > 20`       | break-even out of near-term reach              |
| `efficiency < 0.30` / `R² < 0.40` | a rise that merely wanders upward              |

## What it does not decide

It stays exit-agnostic like every entry: `evaluate()` emits an `EntryIntent` carrying
the BUY direction and a comparable score, nothing else. The stop that *sets* the euro
risk belongs to `STOP_STRATEGY` and the composed `CloseProfile`; this module only reads
the resulting euro figure back — through `filter_by_risk()` — to decide whether the
candidate belongs to the rung the day has paid for.

## Parameters

All are class constants in `src/entry/open_progressive.py` (not `.env`), tuned by
editing the class:

| Constant                                 | Default               | Role                                   |
| ---------------------------------------- | --------------------- | -------------------------------------- |
| `fast_minutes` / `slow_minutes`          | 5 / 15                | the two trends timing the climb        |
| `context_minutes`                        | 30                    | regime, efficiency, pull-back window   |
| `warmup_minutes`                         | 60                    | candles required before scoring        |
| `target_minutes`                         | 5.0                   | where the speed component scores 0.5   |
| `max_minutes_to_breakeven`               | 20.0                  | veto on a climb that is not near-term  |
| `max_spread_in_atr` / `max_spread_ratio` | 1.0 / 1.5             | cost and abnormality of the round trip |
| `max_volatility_ratio`                   | 1.5                   | spike veto against the epic's baseline |
| `min_room_atr`                           | 1.0                   | headroom demanded above break-even     |
| `min_efficiency` / `min_r_squared`       | 0.30 / 0.40           | floors under "is the rise real?"       |
| `min_score`                              | 0.0                   | composite floor (raise to stay flat)   |
| `risk_ladder`                            | (50, 100, 200, 500) € | the rungs; the first is the base risk  |
| `allow_cheaper_fallback`                 | `True`                | trade down when the band is empty      |
| `band_stretch`                           | 0.25                  | last-resort widening of the ceiling    |
| `min_participation_ratio` / `_count`     | 0.5 / 20              | how big a tournament must be to open   |
| `strict_band`                            | `True`                | a band, not a ceiling                  |

## Backtest limitation

`src/backtest/simulator.py` mirrors the live rolling selector — scoring, ranking,
`filter_ranked`, cooldown, flat-book — but **not the risk ladder**: it prices every
synthetic curve with a single fictional `euro_per_point`, so the euro risk of its
epics carries no relation to the 50/100/200/500 € rungs and a simulated band would be
an artefact of that constant, not a result. A simulated run therefore reports the
*ranking* half of this strategy only, as if every candidate were affordable. Judge the
ladder live, or on the realised P&L of a replayed day.

## Tests

`tests/test_open_progressive.py` (ranking, vetoes, ladder arithmetic and its
invariant, band filtering) and the `TestRealizedPnlToday` / `TestRiskEstimates` classes
in `tests/test_scheduler.py` (the day's banked P&L and the euro pricing of a
candidate).
