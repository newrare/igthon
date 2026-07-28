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

## Available stop-distance policies

Registered in [src/stops/\_\_init\_\_.py](../../src/stops/__init__.py):

| Name           | File                        | Style                                           |
| -------------- | --------------------------- | ----------------------------------------------- |
| `stop_support` | `src/stops/stop_support.py` | Stop below a recency-weighted last-hour support |
| `stop_atr`     | `src/stops/stop_atr.py`     | Flat `stop_atr_k × ATR` from the entry          |

## Available close profiles

Registered in [src/exit/\_\_init\_\_.py](../../src/exit/__init__.py):

| Name               | File                           | Doc                                        | Style                                              |
| ------------------ | ------------------------------ | ------------------------------------------ | -------------------------------------------------- |
| `close_zoneprofit` | `src/exit/close_zoneprofit.py` | [close_zoneprofit.md](close_zoneprofit.md) | Composes a stop distance + three per-zone updaters |

## Available zone updaters

Registered per zone in [src/exit/zones/\_\_init\_\_.py](../../src/exit/zones/__init__.py);
each zone selector takes a name from its own registry.

| Selector           | Name               | File                                 | Style                                                                |
| ------------------ | ------------------ | ------------------------------------ | -------------------------------------------------------------------- |
| `CLOSE_ZONESTART`  | `hold`             | `src/exit/zones/underwater.py`       | Keep the stop posted at open, untouched                              |
| `CLOSE_ZONESTART`  | `trendcut`         | `src/exit/zones/underwater.py`       | Cut a clean, confirmed adverse trend at a fraction of `-1R`          |
| `CLOSE_ZONESTART`  | `timedlift`        | `src/exit/zones/timedlift.py`        | Re-read the recent floor every 10 min and move the stop in behind it |
| `CLOSE_ZONESTART`  | `smartgroup`       | `src/exit/zones/smartgroup.py`       | Spend the book's locked-in profit to cap the losers' downside        |
| `CLOSE_ZONEMARGE`  | `hold`             | `src/exit/zones/breakeven_band.py`   | Keep the stop where it is across the break-even band                 |
| `CLOSE_ZONEMARGE`  | `breakeven_lock`   | `src/exit/zones/breakeven_band.py`   | Park the stop behind a confirmed swing low past break-even           |
| `CLOSE_ZONEMARGE`  | `breakeven_safe`   | `src/exit/zones/breakeven_band.py`   | Same lock, gated on a stricter safety confirmation                   |
| `CLOSE_ZONEMARGE`  | `breakeven_half`   | `src/exit/zones/breakeven_band.py`   | Lock half of the acquired move                                       |
| `CLOSE_ZONEPROFIT` | `trailing_ratchet` | `src/exit/zones/trailing_ratchet.py` | Momentum-gated ATR chandelier trailing price in steps                |

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
