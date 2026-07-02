# CLAUDE.md — Project Guidelines

## Language

- **All code must be written in English**: variable names, function names, class names, module names.
- **All comments must be written in English**.
- **All docstrings must be written in English**.
- The programming language for this project is **Python 3.11+**.

______________________________________________________________________

## Project scope

The project is developed exclusively in the `src/` folder.
All architecture, modules, and files belong inside `src/`.

______________________________________________________________________

## Git Conventions

This project follows **Conventional Commits** standard for commit messages:

```
type(scope): description
```

**Types:**

- `feat` — new feature
- `fix` — bug fix
- `refactor` — code changes without feature or bug changes
- `style` — formatting, missing semicolons, etc.
- `test` — adding or updating tests
- `docs` — documentation changes
- `chore` — build process, dependencies, tooling
- `perf` — performance improvements

______________________________________________________________________

## Project architecture

The codebase is organised **by domain**. The two trading decisions —
**opening** and **closing** — are fully decoupled and live in separate domains
that never import each other (see "Open/close decoupling" below). Everything
else is shared, purely-functional infrastructure.

```
src/
├── main.py                 # CLI entry point
├── core/                   # INFRA — shared plumbing (no trading decisions)
│   ├── config.py           # Settings via pydantic-settings
│   ├── scheduler.py        # APScheduler jobs (thin: orchestration only)
│   ├── indicators.py       # Technical analysis (regression, SMA, ROC, ATR, ER)
│   ├── recorder.py         # Logging + alerts
│   ├── api_queue.py / api_guard.py / api_error_log.py
│   └── api/                # IG HTTP client (client.py, session.py, endpoints/)
├── feed/                   # FLUX — streaming, market_data, price_buffer, candle_store
├── markets/                # MARCHÉS — market_scanner (build the epic list)
├── entry/                  # OUVERTURE — EntryStrategy → EntryIntent (direction only)
│   ├── base.py · open_donchian.py · open_projection.py · open_ranking.py
├── stops/                  # ARRÊT — StopDistance.initial_stop() (drives sizing)
│   ├── base.py · stop_support.py · stop_atr.py
├── exit/                   # FERMETURE — CloseProfile (owns stop/target/trailing)
│   ├── base.py · trailing.py · close_zoneprofit.py
│   └── zones/              # per-zone stop updaters (underwater, breakeven_band, trailing_ratchet)
├── execution/              # MAINS — gates.py (pre-open gates), trading.py (TradingService)
├── backtest/               # OUTILS — simulator, backtester, archive, curve_generator
├── models/                 # DATA — SQLAlchemy ORM (database.py + tables)
├── web/                    # VUES — FastAPI app + dashboard/routes
├── scripts/                # OPS — one-off tools (trace_activity, adopt_orphans)
└── utils/                  # tools.py
tests/                      # one test file per module + isolated entry/exit/risk tests
```

### Open/close decoupling (core rule)

- **Opening** code lives only in `src/entry/`. An `EntryStrategy.evaluate()`
  returns an `EntryIntent` (direction + optional size hint) and **never any exit
  level**. New opening ideas are new modules here.
- **Closing** code lives only in `src/exit/`. A `CloseProfile` owns the entire
  exit: `initial_plan()` chooses the protective stop at open (which drives
  sizing) and `evaluate()` makes every per-tick decision (hold / close / ratchet
  stop). The single composer profile (`close_zoneprofit`) splits the exit into
  **three price zones** — open→break-even, break-even→margin, above-margin — each
  managed by a `StopUpdater` (`src/exit/zones/`) **selected independently** so a
  zone can be tuned without influencing the others. New closing scenarios are new
  updater modules registered in the relevant zone registry.
- They are **composed at runtime** by `core/scheduler.py` + `execution/` and are
  linked only through the persisted `Position.close_profile`. Each can be
  swapped or unit-tested in isolation. Selection is the `.env` file — the
  **single source of truth**, all **required** (no default in `config.py`, no DB
  persistence, no runtime switching): `OPEN_STRATEGY` / `STOP_STRATEGY` and the
  three per-zone close selectors `CLOSE_ZONESTART` / `CLOSE_ZONEMARGE` /
  `CLOSE_ZONEPROFIT`.

______________________________________________________________________

## Tech stack

| Component     | Technology                        |
| ------------- | --------------------------------- |
| Language      | Python 3.11+                      |
| HTTP client   | `httpx` (async) or `requests`     |
| Database      | PostgreSQL + `SQLAlchemy` (ORM)   |
| Migrations    | `Alembic`                         |
| Web interface | `FastAPI` + `Jinja2`              |
| Visualisation | `Plotly` / `Dash`                 |
| Scheduler     | `APScheduler`                     |
| Tests         | `pytest` + `pytest-asyncio`       |
| Configuration | `pydantic-settings` (`.env` file) |
| Streaming     | `lightstreamer-client-lib`        |

______________________________________________________________________

## Best practices

### Code style

- Follow **PEP 8** strictly.
- Use a formatter: **black** (line length 88).
- Use a linter: **ruff** (replaces flake8 + isort).
- Use **type hints** on all function signatures.
- Prefer `pathlib.Path` over `os.path`.
- Prefer f-strings over `.format()` or `%`.

### Architecture

- Follow **separation of concerns**: API layer, service layer, model layer, web layer.
- No business logic in routes or models — put it in the domain packages (entry/exit/execution/feed/markets/core).
- Use **dependency injection** (FastAPI `Depends`) rather than global state.
- Configuration is loaded once via `pydantic-settings` from `.env`; never hardcode credentials.
- Never store secrets in source files — use the `.env` file (excluded from git).

### Security

- Use **prepared statements** exclusively (SQLAlchemy ORM / parameterized queries) — no raw SQL string concatenation.
- Validate all external inputs (API responses, user inputs) with **Pydantic** models.
- Store OAuth tokens in memory only — never in files or DB.
- Use a lock (`asyncio.Lock`) to avoid race conditions during token refresh.
- Follow OWASP Top 10 guidelines.

### Async

- Prefer `async/await` (httpx async, asyncio) for all I/O operations.
- Use `asyncio.Lock` for shared mutable state (e.g. token refresh).
- Never block the event loop with synchronous I/O.

### Database

- All DB models inherit from SQLAlchemy `DeclarativeBase`.
- All schema changes go through **Alembic** migrations — never `create_all()` in production.
- Use `TIMESTAMPTZ` (timezone-aware) for all datetime columns.

### Testing

- Every module in `src/` must have a corresponding test file in `tests/`.
- Use `pytest` fixtures for DB sessions and HTTP mocks.
- Mock external API calls — never hit the real IG API in tests.
- Aim for meaningful coverage on the `entry/`, `exit/`, `execution/` and `core/api/` layers.

### Logging

- Use Python's standard `logging` module (configured in `src/core/recorder.py`).
- Log levels: `DEBUG` for detailed traces, `INFO` for normal operations, `WARNING` for recoverable issues, `ERROR` for failures.
- Never use `print()` in production code.

### Frontend / static assets

- Static assets (`src/web/static/`) are loaded with a cache-busting query string
  in `src/web/routes/dashboard/shell.py` (`dashboard.js?v=N`, `style.css?v=N`).
- **Whenever you edit a file under `src/web/static/`, bump its `?v=N` in
  `shell.py`** (e.g. `dashboard.js?v=14` → `?v=15`). The page HTML is rendered
  dynamically (never cached), so a new version forces the browser to fetch the
  updated asset. Forgetting this leaves users on a stale cached file and the
  change silently never deploys.

### Markdown formatting

- Use `mdformat` to align Markdown tables before running tests or committing code:
  ```bash
  mdformat docs/ README.md CLAUDE.md
  ```

### Git

- **Before committing:** run `mdformat docs/ README.md CLAUDE.md` to align all Markdown tables.
- **Before running tests:** run `mdformat docs/ README.md CLAUDE.md` to keep docs consistent.
- no commit files, no change branch without user ask
