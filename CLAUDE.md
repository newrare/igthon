# CLAUDE.md — Project Guidelines

## Language

- **All code must be written in English**: variable names, function names, class names, module names.
- **All comments must be written in English**.
- **All docstrings must be written in English**.
- The programming language for this project is **Python 3.11+**.

______________________________________________________________________

## Project scope

The new Python project is developed exclusively in the `python/` folder.
The existing PHP code (root-level `MyClass/`, `cron/`, `script/`) is **legacy** — do not modify it.
All new architecture, modules, and files belong inside `python/`.

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

```
python/
├── README.md
├── pyproject.toml
├── .env.example
├── alembic/                    # DB migrations
├── src/
│   ├── __init__.py
│   ├── config.py               # Settings via pydantic-settings
│   ├── main.py                 # Entry point
│   ├── api/
│   │   ├── __init__.py
│   │   ├── client.py           # IG HTTP client (auth, refresh, requests)
│   │   ├── session.py          # OAuth token management (access + refresh)
│   │   ├── endpoints/
│   │   │   ├── accounts.py
│   │   │   ├── positions.py
│   │   │   ├── markets.py
│   │   │   ├── prices.py
│   │   │   ├── watchlists.py
│   │   │   └── history.py
│   │   └── streaming.py        # Lightstreamer client
│   ├── models/
│   │   ├── __init__.py
│   │   ├── database.py         # SQLAlchemy engine + Base
│   │   ├── position.py
│   │   ├── epic.py
│   │   ├── day.py
│   │   └── resume.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── price_buffer.py     # In-memory rolling candle buffer (replaces DB history)
│   │   ├── market_data.py      # Fetches prices from IG → feeds PriceBuffer
│   │   ├── trading.py          # Open/close positions (ported from Action.php)
│   │   ├── compute.py          # Technical analysis (ported from Compute.php)
│   │   ├── recorder.py         # Logging + alerts (ported from Record.php)
│   │   └── scheduler.py        # Scheduled tasks (replaces CRON)
│   ├── web/
│   │   ├── __init__.py
│   │   ├── app.py              # FastAPI application
│   │   ├── routes/
│   │   │   ├── dashboard.py
│   │   │   ├── positions.py
│   │   │   └── history.py
│   │   └── templates/
│   ├── scripts/
│   │   ├── fetch_markets.py    # One-shot market discovery script
│   │   ├── test_buffer.py      # Manual buffer inspection script
│   │   └── verify_connection.py # Smoke-test IG API connectivity
│   └── utils/
│       ├── __init__.py
│       └── tools.py            # Utilities (ported from Tools.php)
└── tests/
    ├── test_client.py
    ├── test_trading.py
    ├── test_compute.py
    └── test_price_buffer.py
```

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
- No business logic in routes or models — put it in `services/`.
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
- Aim for meaningful coverage on `services/` and `api/` layers.

### Logging

- Use Python's standard `logging` module (configured in `src/services/recorder.py`).
- Log levels: `DEBUG` for detailed traces, `INFO` for normal operations, `WARNING` for recoverable issues, `ERROR` for failures.
- Never use `print()` in production code.

### Markdown formatting

- Use `mdformat` to align Markdown tables before running tests or committing code:
  ```bash
  mdformat docs/ README.md CLAUDE.md
  ```

### Git

- **Before committing:** run `mdformat docs/ README.md CLAUDE.md` to align all Markdown tables.
- **Before running tests:** run `mdformat docs/ README.md CLAUDE.md` to keep docs consistent.
- Commit messages in English, imperative mood: `Add OAuth token refresh`, `Fix position close logic`.
- One logical change per commit.

______________________________________________________________________

## Development phases

1. **Phase 1 — API connection**: `config.py`, `session.py`, `client.py` (OAuth v3, auto refresh)
1. **Phase 2 — DB models**: SQLAlchemy models + Alembic migrations
1. **Phase 3 — Data reading**: fetch markets, prices, history → store in DB
1. **Phase 4 — Trading**: open/close positions, strategy logic
1. **Phase 5 — Interface**: web dashboard with visualisation
1. **Phase 6 — Automation**: scheduler, notifications, production mode
