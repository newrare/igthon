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
  close-out price (the bid for a BUY, the offer for a SELL) into one of three
  zones (underwater / break-even band / real profit) and delegates the
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
The exit is split into three independently-selected **zones** (open→break-even,
break-even→margin, above-margin) so each zone's stop behaviour is tuned without
influencing the others. The `.env` file is the **single source of truth**: every
selection is **required** (there is no default in `config.py`, no database
persistence and no runtime switching from the dashboard). If any is missing or
unknown the bot and the dashboard refuse to start with a clear error.

```bash
# .env
OPEN_STRATEGY=open_projection
STOP_STRATEGY=stop_support
CLOSE_ZONESTART=hold              # zone open → break-even
CLOSE_ZONEMARGE=hold             # zone break-even → margin
CLOSE_ZONEPROFIT=trailing_ratchet  # zone above the margin
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

**How it opens.** Through the same guarded path as any other open (`entry` →
`EntryIntent` → `TradingService.open_from_intent`), so the reverse trade is sized
and stopped by the selected `STOP_STRATEGY` and managed by the same close zones.
Two gates are lifted because the rule cannot work without them: the long-only
restriction (the reverse of a long *is* a short) and `ALLOW_SAME_DAY_REOPEN` (the
epic was traded seconds ago). Everything else still applies — it is an
**automatic** open, so the dashboard auto-open switch blocks it, and the
duplicate-epic and "market closes soon" gates apply too. The reverse position is
stamped `reason_open = recovery_revert`.

It is wired in `BotScheduler._revert_after_stop_loss`, called from the two places
a stop-out becomes visible: the monitor tick (software backstop) and the position
sync (broker stop filled at IG). The **backtest simulator does not model it** —
a simulated run reports the entry strategy alone.

## Available entry strategies

Registered in [src/entry/\_\_init\_\_.py](../../src/entry/__init__.py):

| Name              | File                           | Doc                                      | Style                                                            |
| ----------------- | ------------------------------ | ---------------------------------------- | ---------------------------------------------------------------- |
| `open_donchian`   | `src/entry/open_donchian.py`   | [open_donchian.md](open_donchian.md)     | Breakout gated by trend efficiency                               |
| `open_projection` | `src/entry/open_projection.py` | [open_projection.md](open_projection.md) | Breakout + multi-model projection gate                           |
| `open_ranking`    | `src/entry/open_ranking.py`    | [open_ranking.md](open_ranking.md)       | Cross-epic ranker, one rolling position                          |
| `open_testing`    | `src/entry/open_testing.py`    | [open_testing.md](open_testing.md)       | Diagnostic: open max markets/day at random                       |
| `open_fade`       | `src/entry/open_fade.py`       | [open_fade.md](open_fade.md)             | **Two-sided** ranker: fade an extended trend at the channel edge |
| `open_pullback`   | `src/entry/open_pullback.py`   | [open_pullback.md](open_pullback.md)     | **Two-sided** ranker: join a clean trend on a pause              |
| `open_steady`     | `src/entry/open_steady.py`     | [open_steady.md](open_steady.md)         | **Two-sided** ranker: the most regular 10-minute curve           |
| `open_five`       | `src/entry/open_five.py`       | [open_five.md](open_five.md)             | **Two-sided** ranker: a series of 5 distinct curve shapes        |

## Available stop-distance policies

Registered in [src/stops/\_\_init\_\_.py](../../src/stops/__init__.py):

| Name               | File                            | Doc                                        | Style                                                                       |
| ------------------ | ------------------------------- | ------------------------------------------ | --------------------------------------------------------------------------- |
| `stop_support`     | `src/stops/stop_support.py`     | —                                          | Stop below a recency-weighted last-hour support                             |
| `stop_atr`         | `src/stops/stop_atr.py`         | —                                          | Flat `stop_atr_k × ATR` from the entry                                      |
| `stop_regression`  | `src/stops/stop_regression.py`  | —                                          | Choppiness-scaled residual-noise band below the entry                       |
| `stop_linearspeed` | `src/stops/stop_linearspeed.py` | [stop_linearspeed.md](stop_linearspeed.md) | **Two-sided**: noise margin when the last 10 min accelerate, else structure |
| `stop_hourlow`     | `src/stops/stop_hourlow.py`     | [stop_hourlow.md](stop_hourlow.md)         | **Two-sided**: stop at the last hour's lowest low / highest high            |

## Available close profiles

Registered in [src/exit/\_\_init\_\_.py](../../src/exit/__init__.py):

| Name               | File                           | Doc                                        | Style                                              |
| ------------------ | ------------------------------ | ------------------------------------------ | -------------------------------------------------- |
| `close_zoneprofit` | `src/exit/close_zoneprofit.py` | [close_zoneprofit.md](close_zoneprofit.md) | Composes a stop distance + three per-zone updaters |

## Available zone updaters

Registered per zone in [src/exit/zones/\_\_init\_\_.py](../../src/exit/zones/__init__.py);
each zone selector takes a name from its own registry.

| Selector           | Name               | File                                 | Style                                                                     |
| ------------------ | ------------------ | ------------------------------------ | ------------------------------------------------------------------------- |
| `CLOSE_ZONESTART`  | `hold`             | `src/exit/zones/underwater.py`       | Keep the stop posted at open, untouched                                   |
| `CLOSE_ZONESTART`  | `trendcut`         | `src/exit/zones/underwater.py`       | Cut a clean, confirmed adverse trend at a fraction of `-1R`               |
| `CLOSE_ZONESTART`  | `timedlift`        | `src/exit/zones/timedlift.py`        | Re-read the recent floor every 10 min and move the stop in behind it      |
| `CLOSE_ZONESTART`  | `smartgroup`       | `src/exit/zones/smartgroup.py`       | Book-wide: park **every** stop on `price − noise` once the group is green |
| `CLOSE_ZONEMARGE`  | `hold`             | `src/exit/zones/breakeven_band.py`   | Keep the stop where it is across the break-even band                      |
| `CLOSE_ZONEMARGE`  | `breakeven_lock`   | `src/exit/zones/breakeven_band.py`   | Park the stop behind a confirmed swing low past break-even                |
| `CLOSE_ZONEMARGE`  | `breakeven_safe`   | `src/exit/zones/breakeven_band.py`   | Same lock, gated on a stricter safety confirmation                        |
| `CLOSE_ZONEMARGE`  | `breakeven_half`   | `src/exit/zones/breakeven_band.py`   | Lock half of the acquired move                                            |
| `CLOSE_ZONEPROFIT` | `trailing_ratchet` | `src/exit/zones/trailing_ratchet.py` | Momentum-gated ATR chandelier trailing price in steps                     |

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
