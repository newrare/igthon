# Strategies — pluggable entry strategies

The bot's architecture separates **shared infrastructure** from the **entry
strategy**. Everything below stays identical whatever strategy is plugged in:

- scheduler jobs (collect & analyze, monitor, sync, end-of-day…)
- API queue, IG client, streaming feed, price buffer
- pre-open gates (`evaluate_open_gates`: hours, max positions, daily P&L
  circuit breakers, win-rate guard)
- order placement and the broker-side protective stop (`TradingService`)
- close rules + ATR trailing stop (`decide_close_reason`,
  `compute_trailing_stop`)
- the simulator, the dashboard, the charts

The strategy is the single decision point that turns a price buffer into an
entry signal. It is selected **by name** in the configuration:

```bash
# .env
STRATEGY_NAME=donchian_er
```

## Available strategies

| Name               | File                                 | Doc                                        | Style                                | Status            |
| ------------------ | ------------------------------------ | ------------------------------------------ | ------------------------------------ | ----------------- |
| `donchian_er`      | `src/strategies/donchian.py`         | [donchian-er.md](donchian-er.md)           | Breakout gated by trend efficiency   | **Live default**  |
| `trend_follower`   | `src/strategies/trend_follower.py`   | [trend-follower.md](trend-follower.md)     | Composite-score trend confirmation   | Legacy (original) |
| `momentum_scalper` | `src/strategies/momentum_scalper.py` | [momentum-scalper.md](momentum-scalper.md) | High-frequency spread-multiple scalp | Experimental      |
| —                  | `src/services/strategies.py` (lab)   | [research-lab.md](research-lab.md)         | 5-candidate research backtests       | Research only     |

## How it works

```
                    ┌──────────────────────────────┐
   STRATEGY_NAME ──▶│  src/strategies/__init__.py  │  get_strategy(name, settings)
                    └──────────────┬───────────────┘
                                   ▼
                       BaseStrategy.evaluate(epic, buf)
                                   │  TradingSignal | None
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                     ▼
        live scheduler        simulator             (future: CLI)
     _evaluate_epic()      StrategySimulator
              │                    │
              └──────── shared pipeline ────────────────┘
        evaluate_open_gates → open_position → check_and_close
              (gates)          (orders)     (close + ATR trail)
```

The contract ([src/strategies/base.py](../../src/strategies/base.py)):

- `warmup` — minimum candles needed before the first evaluation;
- `evaluate(epic, buf) -> TradingSignal | None` — return a full signal
  (direction + the position levels `level_win` / `level_zero` / `level_loose`
  / `level_security` / `level_follower`) or `None` to stay flat.

Convention: `level_win = 0` means "no fixed take-profit" — the win check is
skipped and the position exits through the trailing stop (or end-of-day).

## Adding a strategy

1. Implement `src/strategies/<name>.py`, subclassing `BaseStrategy`
   (provide `name`, `warmup`, `from_settings`, `evaluate`).
1. Register the class in `STRATEGIES` (`src/strategies/__init__.py`).
1. Add its parameters to `src/config.py` and `.env.example`.
1. Document it here: `docs/strategies/<name>.md` (one detailed file per
   strategy — mechanics, parameters, backtest results, limitations).
1. Add tests in `tests/test_strategies.py`.
1. Validate on the simulator (`/simulator` page lets you pick the strategy and
   compare it with the live one on identical seeds).

## Testing a strategy

- **Web**: the `/simulator` page has a *Strategy* selector; runs replay the
  exact live pipeline (gates, trailing, end-of-day) on synthetic curves.
- **Research lab**: `python -m src.scripts.compare_strategies` benchmarks the
  five research candidates (long + short) across all curve profiles;
  `python -m src.scripts.donchian_regime_filter` sweeps the efficiency-ratio
  gate thresholds.

> ⚠️ Synthetic-curve results are a **coherence check of the rules**, not a
> market prediction. Trending profiles are cleaner than any real market, so
> absolute P&L figures are optimistic; only relative comparisons and regime
> behaviour (does the strategy bleed in sideways?) are meaningful.
