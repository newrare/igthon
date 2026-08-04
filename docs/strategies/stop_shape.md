# `stop_shape` — the stop level the *shape* of the curve justifies

Initial-stop placement (`STOP_STRATEGY`, `src/stops/stop_shape.py`). Works
symmetrically for **BUY and SELL**, so it can be paired with the two-sided entry
strategies (`open_fade`, `open_pullback`, `open_linear`, `open_steady`).

```env
STOP_STRATEGY=stop_shape
```

## Idea

Every other policy in `src/stops/` answers *"how far?"* with one formula and applies
it to every market. This one answers **"which level?"** first: it builds several
candidate stops out of the epic's recent history and picks the one the current shape
of the curve makes meaningful. The distance is a consequence of that choice, never
the input.

A protective stop is only informative when the level it sits under would, if broken,
**invalidate the reason for the trade**. Whether a given low carries that meaning
depends entirely on how the price got there — and no multiple of ATR can tell the
difference, because ATR grows with a clean trend exactly as it grows with chop.

## The three candidate levels

All three are **raw extremes** read on the side the stop is triggered on (the bid
lows for a BUY, the offer highs for a SELL), so the wick *is* the level and the stop
is never inside a range the market has already visited.

| Candidate       | Window                | Source                                 |
| --------------- | --------------------- | -------------------------------------- |
| hour extreme    | `hour_lookback` (60)  | price buffer                           |
| 3-hour extreme  | `long_lookback` (180) | price buffer                           |
| session extreme | the whole UTC day     | **the `candle` table** (`day_extreme`) |

The session extreme cannot come from the buffer: `buffer_max_candles` is 200 candles
(≈ 3 h 20), and a session routinely outruns that. On 2026-08-03 the DAX had 325
candles of session history at its open, the Cacao NY only 95 — so "the day" is
sometimes narrower and sometimes far wider than anything the buffer holds. It is
therefore read from the database by the caller and passed in; see
[Where `day_extreme` comes from](#where-day_extreme-comes-from).

## Shape classification

Two measures over the same `shape_period` window, both read on the **mid closes** so
the verdict is side-agnostic (the same path has to be judged for a long and a short):

- **Efficiency Ratio** (`efficiency_ratio`) — `|net move| / path travelled`, `1` for
  a clean ramp and `0` for pure chop. Separates *directional* from *directionless*.
- **Regression R²** — how tightly the points follow their own trend line. Among
  directional paths, separates a clean ramp from one that gets there through deep
  retracements.

R² is read for **cleanliness only, not direction**: the direction is an input (the
entry strategy already decided it), so a tightly fitted *fall* is "clean" here just
like a tightly fitted rise.

| Shape       | Test                                        | Stop anchors on | Why                                                                                 |
| ----------- | ------------------------------------------- | --------------- | ----------------------------------------------------------------------------------- |
| clean trend | `ER ≥ min_efficiency`, `R² ≥ min_r_squared` | hour extreme    | the price has *left* that level behind; it only returns if the move breaks          |
| noisy trend | `ER ≥ min_efficiency`, `R² < min_r_squared` | 3-hour extreme  | the hour's low is inside the breathing of the move — the price already dipped there |
| chop        | `ER < min_efficiency`                       | session extreme | no recent low means anything; the band has been swept and will be swept again       |

A window too short to measure (fewer than three candles) classifies as **chop** — an
unmeasured window is not evidence of a trend.

## The chop branch is a safety net, not a plan

No stop placement rescues a trade taken without direction: a wider stop only buys a
larger loss, a tighter one only reaches it sooner. Measured over the 153 positions of
2026-07-27 → 2026-08-03, **refusing** the low-ER opens outright was worth far more
than any placement change on them.

That refusal belongs to the entry side — see
[open_ultraranking.md](open_ultraranking.md), which vetoes those opens before they
happen. Pairing the two makes this branch the rare fallback it is meant to be:

```env
OPEN_STRATEGY=open_ultraranking   # refuses the chop opens
STOP_STRATEGY=stop_shape          # so its chop branch stays a fallback
```

Keep `StopShape.min_efficiency` and `OpenUltraRanking.min_regime_efficiency` in step
— they are the same judgement about the same 60-candle window.

This policy remains composable with any entry, which is why the branch exists at all:
when a chop trade does reach it, the widest available level is the only defensible
placement. The reasoning is the one documented in
[stop_hourlow.md](stop_hourlow.md): on `IX.D.DOW.IFE.IP` (2026-07-31 09:37) the
hourly low sat 7.0 points under the bid while a single candle averaged 10.6 points of
range — stop hit 21 seconds after the open.

## The wick cushion

`buffer_atr_k × ATR` is placed beyond the chosen level. Unlike `stop_hourlow` this
defaults to a **non-zero** cushion (`0.3`): a stop sitting exactly *on* the level is
taken out by the wick that defines it.

Observed on `PA.D.CC.MONTH2.IP` (2026-08-03 07:32): the initial stop was pierced by
**0.3 point** and the price returned above the entry within the hour, turning what
would have been a positive trade into −81 €.

## Floor and cap

The distance is floored at:

```
max(noise floor, min_stop_atr_k × ATR, min_stop_spread_k × spread)
```

The **noise floor** is `noise_floor_distance` reused verbatim from `stop_hourlow`, so
the two policies share one definition of "outside the band": the detrended band
thickness scaled by a multiplier interpolated on the chop (`1 − ER`) between
`noise_trend_k` and `noise_chop_k`. The ATR and spread terms remain as absolute
back-stops for a market so quiet the band itself collapses.

It is optionally capped at `max_stop_atr_k × ATR` (`0` = no cap, the default: the
point of the policy is to honour the level it selected). **The floor always wins over
the cap**, so a misconfigured cap can never tighten the stop below the floor.

## The broker minimum is not handled here

IG's minimum-stop-distance rule is applied **downstream**, in
`TradingService.open_position` (`src/execution/trading.py`): when a policy asks for
tighter, the stop is widened to `min_stop_price × (1 + stop_min_distance_margin)` and
the software levels are shifted with it. It is therefore a **hard floor under every
branch above**, whatever this policy returns — nothing to configure on the policy.

## Where `day_extreme` comes from

`StopDistance.initial_stop()` takes an optional keyword `day_extreme`, forwarded
untouched by `CloseProfile.initial_plan()`. It is `None` when unavailable, and the
policy then degrades to the widest window the buffer *does* hold rather than failing
the open. Every other stop policy ignores it — their windows fit in the buffer.

| Path     | Source                                                                                                    |
| -------- | --------------------------------------------------------------------------------------------------------- |
| live     | `TradingService._session_extreme` — one indexed `MIN(bid_low)` / `MAX(offer_high)` over today's candles   |
| backtest | `SessionExtremes` in `src/backtest/simulator.py` — a running per-epic extreme fed as candles are ingested |

Two deliberate properties:

- **No look-ahead.** The backtest tracker is fed *as candles arrive*, so the extreme
  covers only what had already been recorded at the moment of the open — never the
  rest of the day. Reading it off the whole curve instead would hand the backtest a
  level the live path cannot know, flattering every result.
- **The buffer cap still applies to the rolling window.** `EpicBuffer` is capped at
  `DEFAULT_MAX_CANDLES` in the simulator on purpose, so the backtest never sees more
  *recent* history than production. The session extreme is a separate, explicitly
  day-scoped input — not a way around that cap.

"Today" is the **UTC calendar day**, the same boundary `Position.date` and the
same-day re-open policy use — not a per-epic market open, which IG does not expose.

A failed query logs a warning and returns `None`: a stop placement is never worth
failing an already-accepted open for.

## Parameters

All constants of the class (`src/stops/stop_shape.py`) — tune them there, not in
`.env`.

| Parameter           | Default | Role                                                |
| ------------------- | ------- | --------------------------------------------------- |
| `atr_period`        | 14      | ATR window (cushion + floors + cap)                 |
| `hour_lookback`     | 60      | clean-trend candidate window (candles)              |
| `long_lookback`     | 180     | noisy-trend candidate window (candles)              |
| `shape_period`      | 60      | classification window; also the noise-floor window  |
| `min_efficiency`    | 0.15    | ER floor separating a directional path from chop    |
| `min_r_squared`     | 0.50    | R² floor separating a clean trend from a noisy one  |
| `buffer_atr_k`      | 0.3     | cushion beyond the chosen level (× ATR)             |
| `noise_lookback`    | 0       | noise-floor window; `0` = reuse `shape_period`      |
| `noise_trend_k`     | 0.5     | band multiplier at ER = 1 (level is real structure) |
| `noise_chop_k`      | 2.0     | band multiplier at ER = 0 (stand outside the band)  |
| `min_stop_atr_k`    | 0.5     | absolute distance floor (× ATR)                     |
| `min_stop_spread_k` | 2.0     | absolute distance floor (× spread)                  |
| `max_stop_atr_k`    | 0.0     | distance cap (× ATR); `0` = no cap                  |

⚠️ Sizing is **not** risk-based: `open_position` opens
`minDealSize × quantity_multiplier` whatever the stop distance, so a wider stop is a
proportionally larger euro loss, not a smaller position. Nothing in this policy — or
any other — bounds the worst-case euro risk per trade.

## Tests

`tests/test_stop_shape.py` — the classifier in isolation, that each shape routes to
its own candidate level, the `day_extreme` degradation path, the cushion, the shared
floors, the cap, the registry wiring and BUY/SELL symmetry.
