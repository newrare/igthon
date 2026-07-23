# `open_rebound` — buy the rebound off a sharp dip inside an up-day

**Status:** opt-in entry (`OPEN_STRATEGY=open_rebound`).

- Code: [src/entry/open_rebound.py](../../src/entry/open_rebound.py)
- Indicators: [src/core/indicators.py](../../src/core/indicators.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))
- Siblings / baselines: [open_ranking](open_ranking.md),
  [open_saferanking](open_saferanking.md), [open_allincrease](open_allincrease.md)

## Idea

Same contract as the other rankers — a **ranker, not a gate**
(`cross_epic_selection = True`), exit-agnostic (`EntryIntent` = direction +
score, the exit belongs to the composed `CloseProfile`). The scheduler scores
every tradable epic, ranks the BUY candidates and opens the best affordable ones.
This module owns only the per-epic half: *"how closely does this curve match a
rebound off a sharp dip?"*, expressed as a score in `[0, 1]` that reads directly
as a percentage.

The shape it looks for (the spec): *the day's general trend is bullish, but there
has been a sharp drop, and the market is now climbing back up out of that drop* —
a **"V" / buy-the-dip** entry, not a fresh breakout to new highs. Scanning the
last `dip_period` candles it locates the **trough** (lowest bid) and the **peak
that preceded it**, and rewards a genuine drop followed by an early-to-mid
recovery.

## Scoring

A weighted **sum** (weights sum to `1.0`, so the score stays in `[0, 1]` /
readable as a percentage):

```
score = w_trend·trend + w_drop·drop + w_rebound·rebound
        + w_recency·recency + w_spread·spread
```

| Component | Weight | What it measures                                                    |
| --------- | ------ | ------------------------------------------------------------------- |
| `trend`   | `0.30` | cleanliness (R²) of the rising **day** (whole buffered session)     |
| `drop`    | `0.25` | depth of the **peak → trough** fall, in ATRs, vs. `drop_atr_target` |
| `rebound` | `0.30` | fraction of the drop **recovered**, on a sweet spot × recent-leg R² |
| `recency` | `0.05` | how recently the dip **bottomed** (trough near the window end)      |
| `spread`  | `0.10` | `1 − (spread/bid)/max_spread_ratio` — cheaper-to-trade tie-breaker  |

### Drop — measured in ATRs

The peak→trough fall is expressed in units of the market's own ATR, so a *forte
chute* on a volatile market and on a calm one are compared on the same scale:

```
drop      = peak_before − trough           # points
drop_atr  = drop / ATR
drop_score = clamp01(drop_atr / drop_atr_target)   # saturates at drop_atr_target
```

### Rebound — a sweet spot, not "the more the better"

The rebound component scores the **fraction of the drop already recovered**, on a
triangular ("tent") response peaking at `rebound_ideal_frac`, scaled by how
cleanly the recent leg rises:

```
recovery_frac = (bid − trough) / (peak_before − trough)      # 0 at the low, 1 back at the peak
rebound = tent(recovery_frac, rebound_ideal_frac) · clamp01(recent_R²)
```

- `recovery_frac → 0` — the bounce has barely turned → **unconfirmed** → low.
- `recovery_frac ≈ rebound_ideal_frac` (`0.40`) — healthy early-to-mid recovery →
  **highest**.
- `recovery_frac → 1` — fully retraced back to the old peak → the dip entry has
  been **missed** (that is a breakout, not a rebound) → low.

## Hard gates (`evaluate → None`)

Beyond the structural rejects (too little history `< warmup`, non-positive bid,
`ATR ≤ 0`), three gates enforce the *shape* — a soft penalty is not enough
because the selector must open the best of the pool:

1. **Bullish day** — the whole-session regression slope must be `> 0`. A market
   *baissière sur la journée* is dropped outright.
1. **Recovering now** — the slope of the last `recent_period` candles must be
   `> 0` (*le marché remonte*).
1. **A real drop** — the peak→trough fall must reach `min_drop_atr` ATRs. A move
   shallower than that is a steady climb, not a rebound setup (that is
   [open_allincrease](open_allincrease.md)'s job).

## Rolling selection (scheduler)

The per-epic score above is only half the strategy; *how many* positions are held
and *when* they open live in the scheduler's rolling selector
(`_select_and_open`), driven by class-constant knobs:

| Knob                      | Value   | Effect                                                         |
| ------------------------- | ------- | -------------------------------------------------------------- |
| `wallet_bounded`          | `True`  | keep opening the best affordable epic until the wallet is dry  |
| `wallet_reserve`          | `0.10`  | keep 10 % of available funds free                              |
| `allow_same_day_reopen`   | `False` | one open per epic per day — rotate across different markets    |
| `open_cooldown_minutes`   | `5`     | ≥ 5 min between opens; at most one open per pass               |
| `min_participation_ratio` | `0.5`   | > half the warmed-up universe before crowning a winner         |
| `concurrent_positions`    | `1`     | fallback cap only, used when the account balance is unreadable |

- **Wallet-bounded** *(on ouvre tant que le wallet le permet)*: every pass opens
  the top-ranked epic whose margin the spendable balance (available − reserve)
  can still cover.
- **Cooldown** *(on attend 5 min avant d'ouvrir un nouvel epic)*: when
  `open_cooldown_minutes > 0` the selector opens at most one position per pass and
  only once ≥ 5 min have elapsed since the most recent open
  (`_minutes_since_last_open`, on `Position.time_open`, UTC).
- **No same-day re-open** *(pour éviter des éventuels doublons de marché
  similaire)*: the `_traded_today` diversity filter drops an epic already used
  today, so the portfolio rotates across *different* markets rather than doubling
  up on one rebound.
- **Never opens a still-open epic** *(on ouvre si l'epic choisi n'est actuellement
  ouvert)*: the shared `epic_already_open` gate blocks any concurrent duplicate,
  independently of the flags above.

## Parameters

All parameters are class constants in
[`OpenRebound`](../../src/entry/open_rebound.py) (tune there; select at runtime
via `OPEN_STRATEGY`):

| Parameter            | Default  | Meaning                                            |
| -------------------- | -------- | -------------------------------------------------- |
| `dip_period`         | `60`     | window scanned for the peak → trough → rebound     |
| `recent_period`      | `10`     | recent leg that must be rising back up             |
| `atr_period`         | `14`     | volatility window (also gates stop sizing at open) |
| `min_drop_atr`       | `1.0`    | min peak→trough fall (in ATRs) to qualify as a dip |
| `drop_atr_target`    | `3.0`    | fall (in ATRs) earning the full drop score         |
| `rebound_ideal_frac` | `0.40`   | recovery fraction earning the full rebound score   |
| `max_spread_ratio`   | `0.0015` | spread/bid at which the spread score hits 0        |
| `weight_trend`       | `0.30`   | clean bullish-day weight                           |
| `weight_drop`        | `0.25`   | drop-depth weight                                  |
| `weight_rebound`     | `0.30`   | recovery weight                                    |
| `weight_recency`     | `0.05`   | dip-recency weight                                 |
| `weight_spread`      | `0.10`   | spread-tightness weight                            |
| `min_score`          | `0.0`    | composite floor; below it the epic stays flat      |
