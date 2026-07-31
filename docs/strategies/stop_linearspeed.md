# `stop_linearspeed` — speed-adaptive initial stop

Initial-stop placement (`STOP_STRATEGY`, `src/stops/stop_linearspeed.py`). Works
symmetrically for **BUY and SELL**, so it can be paired with the two-sided entry
strategies (`open_fade`, `open_pullback`, `open_linear`).

```env
STOP_STRATEGY=stop_linearspeed
```

## Idea

The distance is chosen from **how fast the market moved in the trade's direction
over the last 10 minutes**:

- **Fast window** — the price is travelling straight and quickly *with* the
  trade. The bet is continuation: a reversal or an adverse spike in the next ticks
  is unlikely, so the stop is parked **just outside the noise, right behind the
  entry**. Small risk per unit, larger size for the same euro risk.
- **Slow window** — the price has barely progressed. The entry could sit anywhere
  inside a range, so momentum protects nothing and the stop goes **behind real
  structure**: the last hour's support (BUY) or resistance (SELL).

## Measuring the speed

`directional_speed()` = **net regression travel over the window, in ATR units,
signed by the direction**:

```
speed = sign × slope(mid closes of the last speed_lookback candles) × (n − 1) / ATR
```

- the least-squares **slope × window span** is what the *trend* accounts for, so a
  single freak tick barely moves it (a raw last-minus-first delta would);
- dividing by **ATR** makes the number comparable across epics — `speed = 2.0`
  reads "the last 10 minutes travelled two ATR in the trade's direction";
- `sign` is `+1` for a BUY and `−1` for a SELL, so a positive speed always means
  "moving with the trade" and an adverse window scores negative → structure stop;
- a **choppy** window scores near zero by construction (the regression of an
  oscillation is flat), so no separate choppiness term is needed.

## Blending, not switching

```
t        = clamp01((speed − slow_speed) / (fast_speed − slow_speed))
distance = t × (noise_atr_k × ATR) + (1 − t) × structure_distance
```

Interpolating removes the cliff where two almost-identical windows would get
wildly different risk. `t = 1` is the pure noise margin, `t = 0` the pure
structure stop.

`structure_distance` reuses the robust estimator of `stop_support`: a
recency-weighted low quantile of the last hour's bid lows (`weighted_support`),
mirrored on the offer highs for a short (`weighted_resistance`), plus a
`structure_buffer_atr_k × ATR` cushion beyond it. A lone wick is outvoted by the
mass of the distribution and recent candles weigh more than hour-old ones.

The slow leg is floored at `slow_min_atr_k × ATR` (the reference ATR stop): when
the window is **adverse** the structure sits on the *wrong* side of the entry — a
market falling into a BUY has its hourly lows above the current bid — and the raw
distance would be meaningless (even negative). "No usable structure" therefore
degrades to the reference distance instead of handing the worst possible regime
the tightest possible stop.

The blended distance is finally **floored** at
`max(min_stop_spread_k × spread, min_stop_atr_k × ATR)` — never inside the
bid/offer churn nor inside a minimal volatility gap — and **capped** at
`max_stop_atr_k × ATR` so a far structure cannot risk the whole hourly range. The
floor always wins over the cap.

The stop is placed relative to the side it is triggered on: the **bid** for a BUY
(`entry_level`), the **offer** for a SELL.

## Parameters

All are constants on the class (`.env` only selects the policy by name).

| Parameter                     | Default | Role                                                     |
| ----------------------------- | ------: | -------------------------------------------------------- |
| `atr_period`                  |      14 | ATR period — the normalising volatility unit             |
| `speed_lookback`              |      10 | Speed window, in candles (≈ last 10 min on 1-min data)   |
| `slow_speed`                  |     0.5 | ≤ this many ATR travelled → full structure stop          |
| `fast_speed`                  |     2.0 | ≥ this many ATR travelled → full noise stop              |
| `noise_atr_k`                 |     1.0 | Fast-leg distance, in ATR (the noise margin kept)        |
| `structure_lookback`          |      60 | Structure window, in candles (≈ last hour)               |
| `structure_percentile`        |    0.20 | Weighted low/high quantile → robust support/resistance   |
| `structure_recency_half_life` |    30.0 | Recency weighting half-life, in candles                  |
| `structure_buffer_atr_k`      |     0.5 | ATR cushion placed beyond the detected structure         |
| `slow_min_atr_k`              |     2.5 | Slow-leg floor (× ATR) — used when structure is unusable |
| `min_stop_atr_k`              |    0.75 | Distance floor (× ATR)                                   |
| `min_stop_spread_k`           |     2.0 | Distance floor (× spread)                                |
| `max_stop_atr_k`              |     4.0 | Distance cap (× ATR); `0` = no cap                       |

## Trade-off

A tight stop on a fast window trades **win rate for R-multiple**: stop-outs are
more frequent but each costs little, and the winners that keep accelerating pay
several times the risk. If the noise margin proves too tight in live results,
raise `noise_atr_k` (or `min_stop_atr_k`) before touching the speed thresholds —
the thresholds decide *which regime* the trade is in, `noise_atr_k` decides how
much noise the fast regime tolerates.
