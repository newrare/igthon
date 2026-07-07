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
  bid into one of three zones (underwater / break-even band / real profit) and
  delegates the hold/close/ratchet decision to the matching stop updater in
  `src/exit/zones/`.

They are composed at runtime and linked only through the persisted
`Position.close_profile`. Everything below stays identical whatever entry, stop
distance and exit are plugged in:

- scheduler jobs (collect & analyze, monitor, sync, end-of-day…)
- API queue, IG client, streaming feed, price buffer
- pre-open gates (`evaluate_open_gates`: trading hours, BUY direction,
  duplicate-epic suppression)
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
```

## Available entry strategies

Registered in [src/entry/\_\_init\_\_.py](../../src/entry/__init__.py):

| Name              | File                           | Doc                                      | Style                                      |
| ----------------- | ------------------------------ | ---------------------------------------- | ------------------------------------------ |
| `open_donchian`   | `src/entry/open_donchian.py`   | [open_donchian.md](open_donchian.md)     | Breakout gated by trend efficiency         |
| `open_projection` | `src/entry/open_projection.py` | [open_projection.md](open_projection.md) | Breakout + multi-model projection gate     |
| `open_ranking`    | `src/entry/open_ranking.py`    | [open_ranking.md](open_ranking.md)       | Cross-epic ranker, one rolling position    |
| `open_testing`    | `src/entry/open_testing.py`    | [open_testing.md](open_testing.md)       | Diagnostic: open max markets/day at random |

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
