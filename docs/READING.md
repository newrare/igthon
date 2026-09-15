# READING.md — Reading a market

Everything measurable about an epic's curve, and about an open position, behind
one read-only object each. This is the layer a strategy reads *before* it decides
anything: it takes no decision itself, holds no state and never touches the
broker.

| Module                                                | What it holds                                                   |
| ----------------------------------------------------- | --------------------------------------------------------------- |
| [`src/core/reading.py`](../src/core/reading.py)       | `CurveReading` (a market) and `PositionReading` (an open trade) |
| [`src/core/levels.py`](../src/core/levels.py)         | Swing pivots, level clustering, round-number levels             |
| [`src/core/indicators.py`](../src/core/indicators.py) | The pure maths every reading is built from                      |

______________________________________________________________________

## Why it exists

Every entry, stop and zone updater used to start with the same ten lines: slice
the bid closes, fit a regression, divide by an ATR, hunt for a swing low. Each
one re-derived the measure slightly differently (bid here, mid there; 30 candles
here, 20 there), which makes two strategies impossible to compare and a bug
impossible to fix once. The reading layer holds the *measures*, so a strategy
holds only its *decision*.

```python
from src.core.reading import CurveReading, Regime

r = CurveReading.of(buf)
if r.regime is Regime.TREND_UP and r.trend_15m.is_clean and r.spread_in_atr < 0.3:
    support = r.nearest_support          # the level a stop must sit behind
    room = r.in_atr(r.room_below)        # ... in units of this market's own noise
```

## Five rules that shape the API

1. **Read-only.** Nothing here mutates the buffer or the position.
1. **A reading is one tick.** The candles are snapshotted at construction and
   every measure is cached, so building one is cheap and reading fifty fields
   costs nothing. Rebuild it each pass — `is_stale` tells you the buffer moved on.
1. **Scale-free by default.** Trends come back in percent, steepness as a chart
   angle, and any distance converts to volatility units with `in_atr()`. Absolute
   points are meaningless across a universe of 50 epics.
1. **One candle = one minute.** The application holds no sub-minute data
   ([DATAFLOW.md](DATAFLOW.md) §1), so a "tick" is a 1-minute candle and every
   `minutes` argument is a candle count, silently clamped to the history held.
1. **Price side is explicit.** Shape measures read the **mid** by default, so a
   widening spread is not mistaken for a move. Pass `side="bid"` / `"offer"` for
   close-out terms — which `PositionReading` does for you from the direction.

A cold or half-warmed epic never raises: every field answers on the history that
exists (check `minutes` or `is_warm(n)` when that matters).

______________________________________________________________________

## `CurveReading` — the market

### Identity and freshness

| Field                                | Reads                                      |
| ------------------------------------ | ------------------------------------------ |
| `epic`, `side`, `minutes`            | What this reading covers                   |
| `candles`, `closes`, `highs`, `lows` | The snapshotted series on the chosen side  |
| `price`, `bid`, `offer`              | Last prices                                |
| `last_timestamp`, `age_minutes`      | Feed freshness for this epic               |
| `is_stale`, `is_warm(n)`             | Guards: snapshot outdated / enough history |

### Cost of trading it

| Field                 | Reads                                                      |
| --------------------- | ---------------------------------------------------------- |
| `spread`              | Live spread in price units                                 |
| `spread_avg(minutes)` | Its own 30-minute baseline                                 |
| `spread_ratio`        | Live / baseline — above ~1.5 the market quotes defensively |
| `spread_in_atr`       | Share of one candle's movement lost to the round trip      |

### Candle balance — pressure without amplitude

`ticks(minutes, flat_threshold=None)` → `TickCounts`; shortcut `ticks_15m`. A
candle counts as *flat* when its move is under half the mean spread: the market
did not resolve.

| Field                                  | Reads                                                  |
| -------------------------------------- | ------------------------------------------------------ |
| `up`, `down`, `flat`, `total`          | Raw counts                                             |
| `up_ratio`, `down_ratio`, `flat_ratio` | Shares — `flat_ratio` is the market's hesitation       |
| `bias`                                 | `(up − down) / total`, blind to how big the moves were |
| `dominant`                             | `Trend.UP` / `DOWN` / `FLAT`                           |
| `CurveReading.streak`                  | Signed run of same-direction candles (`+4`, `−2`)      |

Many small ups against a few large downs give a **positive bias and a negative
trend** — the divergence is the signal, and no single trend figure shows it.

### Trend per horizon

`trend(minutes)` → `TrendReading`; shortcuts `trend_5m`, `trend_15m`, `trend_30m`,
`trend_60m`, and `trends` for all four at once.

| Field                   | Reads                                                                                               |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| `pct`                   | Implied move across the window, in % — the cross-epic figure                                        |
| `net_pct`               | Actual first-to-last move, in %; differs from `pct` when the path bends                             |
| `slope`                 | Raw least-squares slope (price units per minute)                                                    |
| `degrees`               | Steepness **as read on the chart**: 45° = the fit climbs the window's whole range across the window |
| `r_squared`, `is_clean` | Linearity; `is_clean` is R² ≥ 0.5                                                                   |
| `direction`             | `UP` / `DOWN` / `FLAT` (flat when the fit says essentially nothing)                                 |
| `per_minute_pct`        | Speed, comparable between horizons                                                                  |

Across horizons:

| Field                     | Reads                                                                                                                  |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `alignment`, `is_aligned` | Agreement of 5/15/30/60 in [−1, 1]; `+1` = all four point up                                                           |
| `acceleration`            | 5-minute speed minus 30-minute speed, in %/min                                                                         |
| `is_fading`               | The 30-minute trend still points somewhere, the 5-minute speed opposes it — the earliest readable end-of-trend warning |

### Amplitude and noise

`amplitude(minutes)` → `AmplitudeReading`; shortcuts `noise_5m`, `noise_15m`,
`efficiency(minutes)`, `atr`, `atr_pct`.

| Field                     | Measures                                                                                              |
| ------------------------- | ----------------------------------------------------------------------------------------------------- |
| `range`, `range_pct`      | The window's total vertical extent                                                                    |
| `atr`                     | Size of **one** candle                                                                                |
| `band_noise`              | Thickness of the band price wanders in, **detrended** — a clean ramp scores ~0 whatever its steepness |
| `adverse_noise`           | What **one** adverse step gives back (mean + 2σ of down-steps)                                        |
| `retracement`             | What a **sequence** of them gave back (deepest give-back from a peak)                                 |
| `efficiency`, `is_choppy` | Kaufman ratio: how much of the path travelled became net movement                                     |

`adverse_noise` and `retracement` answer different questions and neither
dominates: six candles losing 1.5 points each is a 9-point pull-back that the
single-step measure reports as 1.5. A stop wants both.

| Field              | Reads                                                             |
| ------------------ | ----------------------------------------------------------------- |
| `baseline_atr`     | Candle size over the **whole** buffer — this epic's own normal    |
| `volatility_ratio` | Recent window / baseline. 1.0 = business as usual, ≥1.5 = a spike |

### Momentum, channel, crossings

| Field / method                                   | Reads                                                                                        |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `channel(minutes)` → `ChannelReading`            | `high`, `low`, `position` (0 = low, 1 = high), `width`, `width_pct`, `from_high`, `from_low` |
| `position_in_range`                              | Position in the last hour's channel                                                          |
| `sma(period)`, `ema(period)`                     | Moving averages of the chosen side                                                           |
| `roc(period)`                                    | Rate of change, in %                                                                         |
| `rsi(period=14)`                                 | Wilder RSI, 0-100 (50 = balance)                                                             |
| `is_stretched_up` / `is_stretched_down`          | RSI ≥ 70 / ≤ 30                                                                              |
| `cross(fast, slow, kind="ema")` → `CrossReading` | `state`, `gap`, `bars_since_cross`, `just_crossed`, `is_established`, `crossed_in_history`   |

`cross()` is the "a turn is coming" read: a crossing alone is weak, its **age**
and the current gap are what make it usable.

### Regime

`regime` → `Regime`, decided over `regime_window` (30 minutes by default) by two
independent measures — the efficiency ratio (is the path directional?) and
`volatility_ratio` (is this market agitated *for itself*?).

| Value                     | Meaning                                                         |
| ------------------------- | --------------------------------------------------------------- |
| `TREND_UP` / `TREND_DOWN` | Directional path with a sign                                    |
| `RANGE`                   | Choppy and contained — mean-reverting                           |
| `VOLATILE`                | Choppy and agitated — swings that go nowhere, the dangerous one |
| `UNKNOWN`                 | Under 10 candles: nothing honest to say                         |

`is_trending` covers both trend values.

### Move phase

`move_phase` → `MovePhase`, with `move_direction` → `Trend` saying which way that
move points (`FLAT` exactly when the phase is `NONE`). It answers the question the
regime cannot — **am I early or late?** — since `TREND_UP` reads the same on the
first candle of a move and on its last.

The move measured is the **9/21 momentum leg**, deliberately not the regime: a
young leg inside a market the regime still calls `RANGE` is exactly the case worth
seeing, and by the time a give-back is deep enough to matter the regime has
already stopped calling the market trending.

| Value    | Meaning                                                                         |
| -------- | ------------------------------------------------------------------------------- |
| `START`  | The crossing carrying the leg is recent (not yet `is_established`)              |
| `MIDDLE` | The crossing has held and the price still holds most of the range it travelled  |
| `END`    | More than `LATE_MOVE_HOLD` (half) of that range has been given back             |
| `NONE`   | No leg: the averages sit within `MIN_MOVE_GAP_ATR` of each other, or no history |

Two decisions worth knowing about:

- **youth is tested before damage** — a violent start is young, not late, and the
  window of a fresh crossing still contains the leg it reversed, which would read
  as a give-back;
- **the RSI is not used** as an exhaustion sign: it sits stretched for the whole
  length of a healthy trend, so it would call every strong move finished.

`END` is an age, not a forecast: it says the leg is old and damaged, never that
the price is about to turn.

### Readable levels

| Field                                      | Reads                                                |
| ------------------------------------------ | ---------------------------------------------------- |
| `pivots`                                   | Confirmed local tops/bottoms (`Pivot`)               |
| `swing_levels`                             | Pivots merged into `PriceLevel`s, strongest first    |
| `supports` / `resistances`                 | Split around the live price, **nearest first**       |
| `nearest_support` / `nearest_resistance`   | The next thing under / above the market              |
| `room_below` / `room_above`                | Distance to it (`inf` when there is none)            |
| `swing_high` / `swing_low`                 | Most recent confirmed turn                           |
| `round_levels`, `strong_round_levels`      | Psychological levels within `round_radius_atr × ATR` |
| `round_below` / `round_above`              | The round numbers bracketing the price               |
| `nearest_round_level`, `distance_to_round` | The closest round number, whatever its distance      |
| `level_tolerance`                          | The price tolerance used to merge pivots             |

`round_below` / `round_above` / `nearest_round_level` deliberately ignore
`round_radius_atr`: "the nearest big figure is 12 ATR away" is a useful answer,
"there is none within reach" is not. `round_levels` keeps the bounded list.

A `PriceLevel` carries `price`, `kind` (`high` / `low` / `mixed` / `round`),
`touches`, `last_index`, `width` and a 0-1 `strength` (touches dominate, recency
breaks ties). Support versus resistance is decided *at read time*, because a
broken resistance becomes a support without moving.

### Conversions and summary

`in_atr(distance)`, `in_pct(distance)`, `in_euro(distance, euro_per_point)`, and
`as_dict()` for a flat snapshot of the headline figures (logs, dashboard, tests).

### Whole universe

`read_all(price_buffer)` returns one reading per tracked epic. Readings are lazy,
so this is cheap — the intended entry point for a cross-epic ranker.

______________________________________________________________________

## `PositionReading` — an open trade

```python
from src.core.reading import PositionReading

p = PositionReading.from_position(position, buf)   # duck-typed on the ORM row
p.gain_in_atr        # is this profit big for THIS market?
p.giveback_ratio     # how much of the best moment has already leaked away
p.time_in_profit     # did it ever really work, or only flicker green?
p.curve.trend_15m    # what the curve has done since I entered
```

Two readings come with it: `curve` is bounded to the candles **after the open**
(so a pre-entry rally can never satisfy a since-open test), and `market` is the
unbounded one, for amplitude measures that legitimately want history.

Everything is expressed in **profit terms**, not up/down: the reading carries the
direction and normalises internally, so every field means the same thing for a
long and a short. Gains count from `level_zero` (break-even, spread included) —
the money actually on the table — with `gain_from_open` kept for the raw fill.

| Group        | Fields                                                                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Age          | `minutes_open`, `closes`, `favourable_closes`                                                                                                           |
| Current gain | `gain`, `gain_from_open`, `gain_pct`, `gain_euro`, `gain_in_atr`, `is_profitable`                                                                       |
| Excursions   | `mfe`, `mae` (+ `_in_atr`, `_euro`), `peak_price`, `trough_price`, `minutes_since_peak`, `edge_ratio`                                                   |
| Leakage      | `giveback`, `giveback_ratio`                                                                                                                            |
| Behaviour    | `crossed_breakeven`, `minutes_to_breakeven`, `time_in_profit`, `favourable_streak`, `trend_since_open`, `efficiency_since_open`, `is_stalling(minutes)` |
| The plan     | `stop_room`, `stop_room_in_atr`, `stop_room_in_noise`, `locked_gain`, `is_secured`, `progress_to_margin`                                                |
| Summary      | `as_dict()`                                                                                                                                             |

Three of these change how a trade reads:

- **`mfe` / `giveback_ratio`** — a trade closed at 0 € after showing +40 points
  was an *exit* problem, not an entry problem, and only the excursion says so.
- **`minutes_to_breakeven` / `time_in_profit`** — a position that never cleared
  break-even in 40 minutes is a different animal from one that cleared it in 2 and
  came back; no gain field tells them apart.
- **`stop_room_in_noise`** — the stop distance divided by what a close-to-close
  step actually gives back, which is what reaches a stop between two candles.

______________________________________________________________________

## Reading it from the dashboard

The same measures are exposed in the UI by the **gauge icon** (market table, open
positions modal, chart modal header, and the standalone `/charts/{epic}` page),
which opens the reading panel over the page:

| Layer                                                            | Role                                        |
| ---------------------------------------------------------------- | ------------------------------------------- |
| `GET /api/reading/{epic}`                                        | Buffer + one position row → panel payload   |
| [`reading_view.py`](../src/web/routes/dashboard/reading_view.py) | Labels, formatting and colour tones         |
| [`static/reading.js`](../src/web/static/reading.js)              | Generic row renderer, 5 s refresh, `Escape` |

All the interpretation lives in `reading_view.py`, so **adding a row to the panel
is a Python change, never a JS one**. The endpoint reads the in-memory buffer and
one indexed row: it never calls IG and never awaits the API queue.

## Extending it

Add the *maths* to `indicators.py` (a pure function over a series) or `levels.py`
(a level detector), then expose it on `CurveReading` / `PositionReading` as a
cached property or a memoised method. Keep the split: the reading layer measures,
`entry/`, `stops/` and `exit/` decide. Tests live in `tests/test_reading.py` and
`tests/test_levels.py`; the short mirror test in `TestPositionReadingShort` is the
one that keeps every new position field working for a SELL as well as a BUY.
