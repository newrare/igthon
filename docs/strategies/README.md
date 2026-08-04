# Strategies — decoupled entry, stop distance and close profile

The bot's architecture separates **shared infrastructure** from the three trading
decisions, which are themselves **fully decoupled** and each selected by name:

- **Opening** lives in `src/entry/` — an `EntryStrategy.evaluate()` returns an
  `EntryIntent` (direction only, never an exit level).
- **Initial-stop placement** lives in `src/stops/` — a `StopDistance.initial_stop()`
  returns the absolute protective stop at open (which drives sizing). Swappable
  without touching the entry idea or the exit management.
- **Closing** lives in `src/exit/` — a `CloseProfile` composes the stop distance at
  open (`initial_plan()`) and, on every tick (`evaluate()`), classifies the live
  close-out price (the bid for a BUY, the offer for a SELL) into one of four
  zones (underwater / break-even band / secure / real profit) and delegates the
  hold/close/ratchet decision to the matching stop updater in `src/exit/zones/`.
  The zones are **direction-aware**: the same updaters and the same `CLOSE_ZONE*`
  selectors manage a short, mirrored (see
  [close_zoneprofit.md](close_zoneprofit.md#buy-and-sell-use-the-same-zones)).

They are composed at runtime and linked only through the persisted
`Position.close_profile`. Everything below stays identical whatever entry, stop
distance and exit are plugged in:

- scheduler jobs (collect & analyze, monitor, sync, end-of-day…)
- API queue, IG client, streaming feed, price buffer
- pre-open gates (`evaluate_open_gates`: trading hours, signal direction,
  duplicate-epic suppression, same-day re-open policy). Automatic entries are
  **long-only by default**: a strategy that genuinely trades both ways declares
  `emits_shorts = True` and the scheduler then keeps its SELL intents and forwards
  `allow_short` to the gate
- order placement and the broker-side protective stop (`TradingService`)
- the simulator, the dashboard, the charts

Entry, stop distance and exit are each selected **by name** in the configuration.
The exit is split into four independently-selected **zones** (follower→break-even,
break-even→margin, margin→profit trigger, above the profit trigger) so each zone's
stop behaviour is tuned without influencing the others. The `.env` file is the **single source of truth**: every
selection is **required** (there is no default in `config.py`, no database
persistence and no runtime switching from the dashboard). If any is missing or
unknown the bot and the dashboard refuse to start with a clear error.

```bash
# .env
OPEN_STRATEGY=open_projection
STOP_STRATEGY=stop_support
CLOSE_ZONESTART=hold               # zone follower → break-even
CLOSE_ZONEMARGE=hold               # zone break-even → margin
CLOSE_ZONESECURE=hold              # zone margin → profit trigger
CLOSE_ZONEPROFIT=trailing_ratchet  # zone above the profit trigger
ALLOW_SAME_DAY_REOPEN=false         # global open policy (see below)
ALLOW_RECOVERY_REVERT=true          # global open policy (see below)
```

### `ALLOW_SAME_DAY_REOPEN` — one or several openings per epic per day

This boolean is **global to every entry strategy** (it used to be a per-strategy
`allow_same_day_reopen` class attribute) and is **required** like the selections
above. Whatever the direction — BUY or SELL — it answers a single question: may
the same epic be opened more than once during the same trading day?

| Value   | Behaviour                                                                                                                                                     |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `false` | One opening per epic per day. An epic that was used today is dropped for the rest of the day, even after its position closed — the bot rotates across markets |
| `true`  | Several openings per epic per day: the epic is a candidate again as soon as it holds no open position                                                         |

Two positions on the same epic **at the same time** are always refused
(`epic_already_open`), whatever the value. A **manual** open from the dashboard
bypasses the policy — it is an explicit human action. The rule is enforced in two
places: the rolling selector drops already-used epics from its candidate set, and
the shared pre-open gate re-checks it under the per-epic open lock, so per-epic
entry strategies obey it too. The simulator reads the same flag, so a backtest
reports what the live bot would do.

### `ALLOW_RECOVERY_REVERT` — follow the market that turned against a trade

This boolean is **global to every entry strategy** and **required** like the
selections above. It answers: when a position is taken out at a **loss** by the
protective stop it was **opened with**, should the bot immediately open the
opposite side on the same epic?

| Value   | Behaviour                                                                                                                                       |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `true`  | The reverse side is opened at once — a BUY stopped out becomes a SELL, a SELL becomes a BUY — to follow the market that walked through the stop |
| `false` | A stop-out is simply a closed trade; the next opening waits for a normal entry signal                                                           |

The reasoning: price coming all the way back to the level the trade was built on
is not just a loss, it is evidence the direction was wrong. Rather than leaving
that move alone until an entry strategy happens to signal it, the bot turns
around with it.

**Which closes qualify** (all of it, or nothing happens) — the rule is the pure
`should_revert_after_stop_loss` function in
[src/execution/gates.py](../../src/execution/gates.py):

- the close is a **stop hit**: `reason_close` is `stop` / `loose` (the software
  backstop aligned with the follower) or `closed_externally` (the broker order
  resting at IG fired — the usual case, since it sits a spread plus a noise
  cushion beyond the software stop). A `win`, a `manual` close, an `end_of_day`
  or a market-close force-close never reverts;
- the realized P&L is a **loss** — a stop that fires in profit is a secured
  winner, not a reversal to chase;
- the stop that fired is the **original** one. When the stop never ratcheted this
  follows from the close reason; when it did ratchet, the close level must still
  have reached the original level (a gap straight through the raised stop).
  Otherwise the trade was stopped on a *raised* stop, which is the trailing logic
  doing its job;
- the position is **not itself a revert**: the chain is capped at **one hop**, so
  a choppy market cannot ping-pong the account through an endless BUY/SELL/BUY
  sequence.

**Which curves qualify** — the conditions above only identify *which stop* fired,
which is not enough: the same "loss on the original stop" bookkeeping covers a
market that broke through the level and a market that never went anywhere.
Reverting into the second one buys a spread in the direction of nothing. So the
curve **since that position's own open** is read too, by the second pure rule
`curve_supports_revert` in [src/execution/gates.py](../../src/execution/gates.py).

That filter is deliberately **permissive**: the revert is the default answer to a
stop-out and only the blatant "nothing happened" curves are dropped. Its main
question is *where the adverse move is concentrated* — a break puts a visible chunk
of the stop distance into a **single candle**, a flat range rubbing against the
stop or a slow leak down to it never does. **Holding time is not a criterion**: a
market can sit flat for twenty minutes and then break in one candle, which is a
prime revert, while the same stop-out reached in a hundred tiny steps is not.
Every threshold is a fraction of the trade's own risk (`|level_open − original_stop|`) or a ratio, so it means the same thing on a forex pair and on an
index:

| Rejected shape   | Test                                                                                                           | Why it is not a revert                                                                               |
| ---------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **grazed stop**  | price no longer past the stop by `REVERT_MIN_BREAK_RATIO` (10% of the risk) when the revert is decided         | a wick took the stop out and the market is already back on the other side of it                      |
| **no impulse**   | biggest single-candle adverse move below `REVERT_MIN_IMPULSE_RATIO` (20% of the risk)                          | the stop distance was covered in dribs and drabs — a flat or slowly-leaking market                   |
| **violent chop** | path efficiency below `REVERT_MIN_EFFICIENCY_K` (1.2) × the `1/√n` a random walk of the same length would show | candles big enough to pass the impulse test can still add up to a market that went nowhere           |
| **swing**        | ran more than `REVERT_MAX_FAVOURABLE_RATIO` (100% of the risk) **in profit** first                             | the direction was right at least once — the stop-out is the tail of an oscillation, not a wrong call |

The curve is the close-out price of each 1-minute candle since the open (bid for a
long, bid + spread for a short — the terms the stop levels are in), and the walk
starts at the opening level itself, so a position whose very first candle crashed
through the stop still shows its impulse. Below two candles there is no shape to
read at all: that is accepted only when the position genuinely died within those
couple of minutes (a stop-out that fast is a break), and refused otherwise — a
silent feed means the curve is unknown, not flat. Both rules refuse rather than
guess whenever an input is missing (no open time, no opening level, no live price).

These thresholds are **constants in the module**, like every other strategy
parameter — only the on/off selector lives in `.env`. Each refusal is logged at
INFO with the shape that caused it (`Recovery revert on … dropped — the curve since open does not justify it: …`), so a run can be reviewed for over- or
under-filtering.

**How it opens.** Through the same guarded path as any other open (`entry` →
`EntryIntent` → `TradingService.open_from_intent`), so the reverse trade is sized
and stopped by the selected `STOP_STRATEGY` and managed by the same close zones.
Two gates are lifted because the rule cannot work without them: the long-only
restriction (the reverse of a long *is* a short) and `ALLOW_SAME_DAY_REOPEN` (the
epic was traded seconds ago). Everything else still applies — it is an
**automatic** open, so the dashboard auto-open switch blocks it, and the
duplicate-epic and "market closes soon" gates apply too. The reverse position is
stamped `reason_open = recovery_revert`.

It is wired in `BotScheduler._revert_after_stop_loss` (which builds the curve for
the filter in `_curve_supports_revert`), called from the two places
a stop-out becomes visible: the monitor tick (software backstop) and the position
sync (broker stop filled at IG). The **backtest simulator does not model it** —
a simulated run reports the entry strategy alone.

## Available entry strategies

Registered in [src/entry/\_\_init\_\_.py](../../src/entry/__init__.py):

| Name                | File                             | Doc                                          | Style                                                            |
| ------------------- | -------------------------------- | -------------------------------------------- | ---------------------------------------------------------------- |
| `open_donchian`     | `src/entry/open_donchian.py`     | [open_donchian.md](open_donchian.md)         | Breakout gated by trend efficiency                               |
| `open_projection`   | `src/entry/open_projection.py`   | [open_projection.md](open_projection.md)     | Breakout + multi-model projection gate                           |
| `open_ranking`      | `src/entry/open_ranking.py`      | [open_ranking.md](open_ranking.md)           | Cross-epic ranker, one rolling position                          |
| `open_testing`      | `src/entry/open_testing.py`      | [open_testing.md](open_testing.md)           | Diagnostic: open max markets/day at random                       |
| `open_fade`         | `src/entry/open_fade.py`         | [open_fade.md](open_fade.md)                 | **Two-sided** ranker: fade an extended trend at the channel edge |
| `open_pullback`     | `src/entry/open_pullback.py`     | [open_pullback.md](open_pullback.md)         | **Two-sided** ranker: join a clean trend on a pause              |
| `open_steady`       | `src/entry/open_steady.py`       | [open_steady.md](open_steady.md)             | **Two-sided** ranker: the most regular 10-minute curve           |
| `open_five`         | `src/entry/open_five.py`         | [open_five.md](open_five.md)                 | **Two-sided** ranker: a series of 5 distinct curve shapes        |
| `open_ultraranking` | `src/entry/open_ultraranking.py` | [open_ultraranking.md](open_ultraranking.md) | `open_saferanking` + a hard veto on a directionless market       |

## Available stop-distance policies

Registered in [src/stops/\_\_init\_\_.py](../../src/stops/__init__.py):

| Name               | File                            | Doc                                        | Style                                                                       |
| ------------------ | ------------------------------- | ------------------------------------------ | --------------------------------------------------------------------------- |
| `stop_support`     | `src/stops/stop_support.py`     | —                                          | Stop below a recency-weighted last-hour support                             |
| `stop_atr`         | `src/stops/stop_atr.py`         | —                                          | Flat `stop_atr_k × ATR` from the entry                                      |
| `stop_regression`  | `src/stops/stop_regression.py`  | —                                          | Choppiness-scaled residual-noise band below the entry                       |
| `stop_linearspeed` | `src/stops/stop_linearspeed.py` | [stop_linearspeed.md](stop_linearspeed.md) | **Two-sided**: noise margin when the last 10 min accelerate, else structure |
| `stop_hourlow`     | `src/stops/stop_hourlow.py`     | [stop_hourlow.md](stop_hourlow.md)         | **Two-sided**: stop at the last hour's lowest low / highest high            |
| `stop_shape`       | `src/stops/stop_shape.py`       | [stop_shape.md](stop_shape.md)             | **Two-sided**: picks WHICH recent level to anchor on from the curve's shape |

## Available close profiles

Registered in [src/exit/\_\_init\_\_.py](../../src/exit/__init__.py):

| Name               | File                           | Doc                                        | Style                                             |
| ------------------ | ------------------------------ | ------------------------------------------ | ------------------------------------------------- |
| `close_zoneprofit` | `src/exit/close_zoneprofit.py` | [close_zoneprofit.md](close_zoneprofit.md) | Composes a stop distance + four per-zone updaters |

## Available zone updaters

Registered per zone in [src/exit/zones/\_\_init\_\_.py](../../src/exit/zones/__init__.py);
each zone selector takes a name from its own registry.

| Selector           | Name                   | File                                 | Style                                                                                   |
| ------------------ | ---------------------- | ------------------------------------ | --------------------------------------------------------------------------------------- |
| `CLOSE_ZONESTART`  | `hold`                 | `src/exit/zones/underwater.py`       | Keep the stop posted at open, untouched                                                 |
| `CLOSE_ZONESTART`  | `trendcut`             | `src/exit/zones/underwater.py`       | Cut a clean, confirmed adverse trend at a fraction of `-1R`                             |
| `CLOSE_ZONESTART`  | `timedlift`            | `src/exit/zones/timedlift.py`        | Re-read the recent floor every 10 min and move the stop in behind it                    |
| `CLOSE_ZONESTART`  | `smartgroup`           | `src/exit/zones/smartgroup.py`       | Book-wide: park **every** stop on `price − noise` once the group is green               |
| `CLOSE_ZONEMARGE`  | `hold`                 | `src/exit/zones/breakeven_band.py`   | Keep the stop where it is across the break-even band                                    |
| `CLOSE_ZONEMARGE`  | `breakeven_lock`       | `src/exit/zones/breakeven_band.py`   | Park the stop behind a confirmed swing low past break-even                              |
| `CLOSE_ZONEMARGE`  | `breakeven_safe`       | `src/exit/zones/breakeven_band.py`   | Same lock, gated on a stricter safety confirmation                                      |
| `CLOSE_ZONEMARGE`  | `limitloose`           | `src/exit/zones/breakeven_band.py`   | Move the stop at once to a double noise band under the market                           |
| `CLOSE_ZONESECURE` | `hold`                 | `src/exit/zones/secure.py`           | Keep whatever stop the lower zones left                                                 |
| `CLOSE_ZONESECURE` | `breakeven_half`       | `src/exit/zones/secure.py`           | Secure the midpoint of the break-even→margin band at once                               |
| `CLOSE_ZONEPROFIT` | `trailing_ratchet`     | `src/exit/zones/trailing_ratchet.py` | Momentum-gated ATR chandelier trailing price in steps                                   |
| `CLOSE_ZONEPROFIT` | `trailing_ratchetmore` | `src/exit/zones/trailing_ratchet.py` | Same, plus a give-back cap on the peak gain and a width that narrows as the run extends |

## Contract

Entry ([src/entry/base.py](../../src/entry/base.py)):

- `warmup` — minimum candles before the first evaluation;
- `evaluate(epic, buf) -> EntryIntent | None` — return a direction (+ optional
  size hint) or `None` to stay flat.

Stop distance ([src/stops/base.py](../../src/stops/base.py)):

- `initial_stop(entry_level, direction, buf) -> float` — the absolute protective
  stop chosen at open (drives sizing).

Close ([src/exit/base.py](../../src/exit/base.py)):

- `initial_plan(...)` — delegates the stop to the distance policy, freezes the
  break-even / margin references;
- `evaluate(...)` — the per-tick hold / close / ratchet-stop decision, split by
  zone in `src/exit/zones/`.

## Adding an entry, a stop distance or a close profile

1. Implement it in `src/entry/<name>.py` (subclass `EntryStrategy`),
   `src/stops/<name>.py` (subclass `StopDistance`) or `src/exit/<name>.py`
   (subclass `CloseProfile`).
1. Register the class in `ENTRY_STRATEGIES` / `STOP_DISTANCES` / `CLOSE_PROFILES`.
1. Tune its parameters — most entries/profiles keep them as constants in their
   own class; the shared breakout/regime knobs live in `.env` (`STRATEGY_*`).
1. Document it here: `docs/strategies/<name>.md`.
1. Add tests in `tests/` (isolated entry/exit tests).
1. Validate on the simulator (`/simulator` page lets you pick the entry and
   compare it with the live one on identical seeds).

## Testing

- **Web**: the `/simulator` page runs replays of the exact live pipeline (gates,
  trailing, end-of-day) on synthetic curves.

> ⚠️ Synthetic-curve results are a **coherence check of the rules**, not a
> market prediction. Trending profiles are cleaner than any real market, so
> absolute P&L figures are optimistic; only relative comparisons and regime
> behaviour (does the strategy bleed in sideways?) are meaningful.
