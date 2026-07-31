# `stop_hourlow` — stop at the last hour's extreme

Initial-stop placement (`STOP_STRATEGY`, `src/stops/stop_hourlow.py`). Works
symmetrically for **BUY and SELL**, so it can be paired with the two-sided entry
strategies (`open_fade`, `open_pullback`, `open_linear`, `open_steady`).

```env
STOP_STRATEGY=stop_hourlow
```

## Idea

The stop goes **exactly at the lowest level printed over the last hour of
recording**:

- **BUY** — the lowest `bid_low` of the last `lookback` candles;
- **SELL** — the highest `offer_high` of the same window (mirrored).

The extreme is read on the side the stop is actually triggered on (the bid for a
long, the offer for a short), so the level is directly comparable with the entry
reference.

No quantile, no weighting, no ATR shaping: if the market has not traded below that
level for an hour, going below it now says the reason for the trade is gone.

## Difference with `stop_support`

Both look at the same hour of lows, but they answer different questions:

| Policy         | Level used                               | A lone wick…                                     |
| -------------- | ---------------------------------------- | ------------------------------------------------ |
| `stop_support` | recency-weighted **20th percentile** low | is outvoted by the mass of the distribution      |
| `stop_hourlow` | the **raw minimum** low                  | **is** the level — the stop sits under the spike |

`stop_hourlow` therefore risks more per unit on a spiky window. What is bought is a
much lower chance of being shaken out by a repeat of a move the market has already
made.

⚠️ Sizing is **not** risk-based: `open_position` opens `minDealSize × quantity_multiplier` whatever the stop distance, so a wider stop is a
proportionally larger euro loss, not a smaller position (`src/execution/trading.py`
— `quantity`, `euro_risk` is computed for logging and the `maxStopOrLimitDistance`
check only).

## The noise floor — when the extreme is not a level

The hourly extreme is only *structure* when the curve has structure. On a flat,
choppy hour the price wanders inside a band and the hour's low is simply wherever
the noise last poked; an entry near the bottom of that band gets a stop a couple of
points away and is closed by the very oscillation that printed the level.

Real case — `IX.D.DOW.IFE.IP`, 2026-07-31 09:37 (BUY):

| Measure                        |    Value | Read as                         |
| ------------------------------ | -------: | ------------------------------- |
| hourly low → bid distance      |  7.0 pts | the raw extreme                 |
| spread floor (`2 × spread`)    |  8.0 pts | what the stop actually got      |
| ATR(14)                        |  8.9 pts | 0.9 pt **less** than the stop   |
| mean candle range              | 10.6 pts | one candle > the whole stop     |
| hour high − low                | 59.9 pts | the band the price lived in     |
| efficiency ratio over the hour |    0.029 | 8.7 pts net for 304 pts of path |

Stop hit **21 s after the open** for −12.74 €. A spread or ATR floor cannot catch
this: the spread says nothing about the hour, and ATR grows with a *clean* trend
just as much as with chop, so neither can tell the two apart.

The floor is therefore derived from the **global state of the curve**, from two
complementary measures over the same window:

- `band_noise` (`src/core/indicators.py`) — the standard deviation of the mid
  closes around their own regression line: the **thickness of the band**, in
  points, immune to how far the curve has travelled (a clean ramp scores ~0);
- `efficiency_ratio` — `|net move| / path travelled`: **how directional** the path
  is, `1` for a ramp, `0` for pure chop.

```
chop  = 1 − ER
k     = noise_trend_k + (noise_chop_k − noise_trend_k) × chop
floor = k × band_noise
```

A clean trend keeps the tight `noise_trend_k` band — its extreme is a level worth
honouring. A directionless hour gets the wide `noise_chop_k` band, the only
distance that survives an oscillation whose own amplitude produced the extreme.
Interpolating rather than switching on a threshold removes the cliff where two
almost-identical windows would get wildly different risk.

On the case above: `band_noise = 12.63`, `ER = 0.029` → `k = 1.96` → floor
**24.7 pts** instead of 8.0. The adverse excursion that followed bottomed 22.1 pts
under the bid, then the market came back above the entry ten minutes later.

## Buffer, floor and cap

```
distance = (reference − hourly extreme) + buffer_atr_k × ATR
distance = max(distance, noise floor, min_stop_atr_k × ATR, min_stop_spread_k × spread)
distance = min(distance, max_stop_atr_k × ATR)      # only when max_stop_atr_k > 0
```

- `buffer_atr_k` defaults to **0** — the stop sits *on* the level, as asked;
- the **noise floor** is the governing term (previous section);
- the **ATR and spread floors** remain as absolute back-stops for the degenerate
  cases the band cannot describe: a market so quiet the band itself collapses (the
  broker would reject a stop inside the churn), and an **adverse** window where
  every hourly low sits *above* the current bid, which makes the raw distance
  negative;
- the **cap** is off by default (`0`): the point of the policy is to honour the
  hourly extreme wherever it is. Set it if a single deep wick must not be allowed
  to risk the whole hourly range. The floor always wins over the cap.

## Parameters

All are constants on the class (`.env` only selects the policy by name).

| Parameter           | Default | Role                                                   |
| ------------------- | ------: | ------------------------------------------------------ |
| `atr_period`        |      14 | ATR period — the unit of the buffer, back-stop and cap |
| `lookback`          |      60 | Window, in candles (≈ the last hour on 1-min data)     |
| `buffer_atr_k`      |     0.0 | Cushion beyond the extreme; `0` = stop on the level    |
| `noise_lookback`    |       0 | Band-measurement window; `0` = reuse `lookback`        |
| `noise_trend_k`     |     0.5 | × band when the path is a clean trend (ER = 1)         |
| `noise_chop_k`      |     2.0 | × band when the path is pure noise (ER = 0)            |
| `min_stop_atr_k`    |     0.5 | Back-stop floor (× ATR) — never inside a flat hour     |
| `min_stop_spread_k` |     2.0 | Back-stop floor (× spread) — never inside the churn    |
| `max_stop_atr_k`    |     0.0 | Distance cap (× ATR); `0` = no cap (extreme honoured)  |

Set `noise_trend_k = noise_chop_k = 0` to restore the pre-noise-floor behaviour
(raw extreme + ATR/spread back-stops only).

## Trade-off

Wide, honest stops: fewer noise stop-outs and a higher win rate than an ATR
distance, but each loss costs the full hourly range and the R-multiple of the
winners shrinks accordingly. Widen `lookback` to demand an even stronger level;
raise `buffer_atr_k` if stop-outs cluster exactly on the hourly low (the market
sweeping the obvious level before continuing).

The noise floor sharpens that trade-off on choppy markets: it removes the
near-certain instant stop-out, and in exchange the euro risk of those trades rises
by the same ratio the distance did (on the case above, 8 → 24.7 pts, i.e. 8 € →
24.7 € at 1 €/point since sizing is not risk-based). Lower `noise_chop_k` to cap
that, or gate the *open* on the same `efficiency_ratio` so a directionless hour is
simply not traded — the floor makes the trade survivable, it does not make it a
good bet.
