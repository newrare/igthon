# `open_testing` — open the maximum of different markets/day, at random

**Status:** opt-in **diagnostic** entry (`OPEN_STRATEGY=open_testing`).

- Code: [src/entry/open_testing.py](../../src/entry/open_testing.py)
- Orchestration: the scheduler's rolling selection
  ([src/core/scheduler.py](../../src/core/scheduler.py))

## Idea

This entry is **not** meant to make money — it exists to **stress-test the close
side**: the protective stop ([src/stops/](../../src/stops/)) and the three close
zones ([src/exit/zones/](../../src/exit/zones/)). Instead of holding one carefully
chosen position, it opens **as many different markets as the wallet allows in a
single day, at random**, so the stop placement and each zone's behaviour can be
observed across a wide, varied set of live positions in one session.

Like [`open_ranking`](open_ranking.md) it is a **cross-epic ranker**
(`cross_epic_selection = True`), so it reuses the scheduler's rolling-selection
routine unchanged. What it changes is only *which* markets that routine opens and
*how many*:

- **One opening per epic per day.** Requires the global `ALLOW_SAME_DAY_REOPEN=false`
  policy (`.env`, see [README](README.md)), enforced by the scheduler
  (`_traded_today_epics`): an epic used today is dropped from the candidate set,
  so the bot keeps spreading across **new** markets rather than re-opening one.
- **Open until the wallet is exhausted.** `concurrent_positions` is set very high
  (1000), so the scheduler's per-tick slot count is effectively unbounded and the
  **wallet gate** becomes the real limit: it opens ranked candidates one by one,
  subtracting each epic's margin from the spendable balance (available funds minus
  `wallet_reserve`, default 5 %) and stops when the next margin no longer fits.
- **Random, diversified selection.** `evaluate` returns a **random** score in
  `[0, 1)` for every scorable epic, so the ranking — and thus the order in which
  the wallet is spent — is reshuffled every tick. Which markets get the last few
  affordable slots varies day to day, giving broad, non-deterministic coverage.
  The tradable universe is already balanced across asset classes by the
  scheduler's `select_diversified_subset`.
- **Open immediately.** `open_after_minutes = 0` and `min_participation_ratio = 0`
  — opens start as soon as any epic has warmed up, instead of waiting for half the
  universe.

It stays exit-agnostic: `evaluate` emits only a direction and a score. The
stop/target/trailing belong to the composed `CloseProfile` and the selected
`StopDistance` — exactly the machinery this profile exists to exercise.

## Direction

BUY only. The live risk gate
([src/execution/gates.py](../../src/execution/gates.py)) rejects non-BUY
intents, so emitting SELL would only waste candidates. Exercising the short close
path would require short support in the execution layer first.

## Structural requirements

The only reason `evaluate` returns `None` (besides insufficient warm-up) is a
**non-positive ATR**: with no measurable volatility the composed stop distance
cannot size a protective stop at open, so the epic is skipped rather than opened
blind.

`warmup` is small (20 candles) so opens start early, but never below
`atr_period + 1` so a meaningful ATR is always available. The composed stop
distance degrades gracefully on a short window (it slices its own lookback and
applies ATR/spread floors), so a modest warm-up still yields a valid stop to
observe.

## Where the decision lives

| Concern                                   | Owner                                |
| ----------------------------------------- | ------------------------------------ |
| Random per-epic score                     | `OpenTesting.evaluate` (entry/)      |
| One open per epic/day, wallet gate, slots | scheduler `_select_and_open` (core/) |
| Initial protective stop                   | selected `StopDistance` (stops/)     |
| Break-even / margin / profit zones        | selected `CloseProfile` (exit/)      |

## Configuration

```dotenv
OPEN_STRATEGY=open_testing
# stop + close zones as usual — this is what you are testing:
STOP_STRATEGY=stop_support
CLOSE_ZONESTART=hold
CLOSE_ZONEMARGE=hold
CLOSE_ZONEPROFIT=trailing_ratchet
```

Tuning knobs are class constants in `src/entry/open_testing.py`
(`concurrent_positions`, `wallet_reserve`, `warmup_candles`, `atr_period`).
