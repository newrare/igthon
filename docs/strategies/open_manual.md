# `open_manual` — the bot never opens; the user opens from the dashboard

**Status:** opt-in **manual** entry (`OPEN_STRATEGY=open_manual`).

- Code: [src/entry/open_manual.py](../../src/entry/open_manual.py)
- Manual open path: `POST /api/positions/open/{epic}`
  ([src/web/routes/dashboard/router.py](../../src/web/routes/dashboard/router.py))

## Idea

This entry is the *open* side reduced to a **no-op**: `evaluate` always returns
`None`, so the analysis loop
([src/core/scheduler.py](../../src/core/scheduler.py)) can run on its normal
schedule without ever opening a position on its own. Opening becomes a purely
**manual** act performed by the user through the dashboard **BUY** button, which
bypasses the entry strategy entirely (it hard-codes the direction) and drives the
**same** open path as an automatic open: sizing, risk gates, the composed
`CloseProfile` protective stop, IG confirmation and the DB record.

Everything else in the pipeline stays fully live:

- prices still stream and candles still record;
- the composed `CloseProfile` still manages every open position's exit
  (break-even / margin / profit zones, trailing);
- the monitor / sync / reconcile jobs still run.

Only the **open decision** is handed to the user.

## Why a strategy rather than "just disable the analysis job"

The three strategy selections in `.env` are all **required** and validated at
startup (`validate_strategy_selection`), so `OPEN_STRATEGY` must name a
registered entry. `open_manual` is the first-class way to say "no automatic
opening" without leaving the auto path armed with a real strategy — and it keeps
the rest of the bot (streaming, close management, monitoring) running.

## Direction

None automatically. Each manual open is a **BUY** (the dashboard button and the
live risk gate in [src/execution/gates.py](../../src/execution/gates.py) are
long-only).

## Where the decision lives

| Concern                            | Owner                                          |
| ---------------------------------- | ---------------------------------------------- |
| Open decision                      | **the user**, via `POST /api/positions/open/…` |
| Automatic open (disabled here)     | `OpenManual.evaluate` → always `None` (entry/) |
| Initial protective stop            | selected `StopDistance` (stops/)               |
| Break-even / margin / profit zones | selected `CloseProfile` (exit/)                |

## Configuration

```dotenv
OPEN_STRATEGY=open_manual
# stop + close zones still apply to every manual open:
STOP_STRATEGY=stop_support
CLOSE_ZONESTART=hold
CLOSE_ZONEMARGE=hold
CLOSE_ZONESECURE=hold
CLOSE_ZONEPROFIT=trailing_ratchet
```

The strategy takes no tuning knobs — it is a pure no-op.
