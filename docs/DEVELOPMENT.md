# Development Guide

## Prerequisites

- Python 3.11+
- A virtual environment tool (`venv` built-in is fine)
- An IG demo account with an API key ([create one here](https://www.ig.com))

______________________________________________________________________

## Setup

```bash
# Clone / enter the project
cd igthon/

# Create and activate the virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# Install all dependencies (including dev extras)
pip install -e ".[dev]"

# Configure credentials
cp .env.example .env
# Edit .env — fill in IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACCOUNT_ID

# Create the database
alembic upgrade head

# Verify the IG API connection
python src/scripts/verify_connection.py
```

______________________________________________________________________

## Standalone scripts

Utility scripts live in `src/scripts/` — they are not part of the importable package and
are meant to be run directly for debugging or one-off tasks.

```bash
# Smoke-test IG API connectivity (no trading)
python src/scripts/verify_connection.py

# Discover tradeable markets and print results
python src/scripts/fetch_markets.py

# Inspect the in-memory price buffer state
python src/scripts/test_buffer.py
```

______________________________________________________________________

## Running the bot

```bash
# Analysis only (safe — no orders placed)
python -m src.main --analyze-only

# Full bot with scheduler
python -m src.main

# Bot + web dashboard at http://localhost:8000
python -m src.main --web

# Custom epics
python -m src.main --analyze-only --epics IX.D.DAX.IFMM.IP CS.D.EURUSD.TODAY.IP

# Verbose output
python -m src.main --analyze-only --log-level DEBUG
```

______________________________________________________________________

## Tests

```bash
# All tests
python -m pytest tests/ -v

# Quick run (minimal output)
python -m pytest tests/ -q

# Single module
python -m pytest tests/test_compute.py -v

# With coverage report
python -m pytest tests/ --cov=src --cov-report=term-missing
```

Tests must never hit the real IG API. Use `respx` to mock httpx calls.
Every module in `src/` has a corresponding test file in `tests/`.

______________________________________________________________________

## Linting and formatting

```bash
# Format code
black src/ tests/

# Lint (includes import sorting)
ruff check src/ tests/

# Auto-fix lint issues
ruff check --fix src/ tests/
```

Configuration is in `pyproject.toml`:

- `black` — line length 88, target Python 3.11
- `ruff` — rules: E, F, I, N, W, UP

______________________________________________________________________

## Database migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Generate a new migration from model changes
alembic revision --autogenerate -m "Add my_column to position"

# Roll back one migration
alembic downgrade -1

# Check current revision
alembic current

# Show migration history
alembic history --verbose
```

Migration files are in [alembic/versions/](../alembic/versions/).

**Never use `Base.metadata.create_all()` in production.**
All schema changes go through Alembic.

______________________________________________________________________

## Adding a new endpoint

1. Create `src/core/api/endpoints/my_endpoint.py` with an async function that takes `IGClient`.
1. Call it from the relevant domain (`feed/`, `markets/`, `execution/`, …).
1. Write tests in `tests/test_my_endpoint.py` using `respx`.
1. No direct calls from routes — always via a service.

______________________________________________________________________

## Code conventions

- **PEP 8** + `black` formatting (line length 88)
- **Type hints** on all function signatures
- **`async/await`** for all I/O — never block the event loop
- **`pathlib.Path`** over `os.path`
- **f-strings** over `.format()` or `%`
- No `print()` in production code — use `logging`
- No comments explaining *what* the code does — only *why* (non-obvious invariants)
- No secrets in source files — only in `.env`

______________________________________________________________________

## Project structure rules

- Business logic lives in the domain packages (`entry/`, `exit/`, `execution/`,
  `feed/`, `markets/`, `core/`) — not in routes, not in models. Opening and
  closing logic stay in `entry/` and `exit/` respectively and never import each
  other.
- Routes are thin: validate input, call a domain service, return the response.
- Models are pure data structures (SQLAlchemy ORM) — no logic.
- Configuration is loaded once via `get_settings()` — no direct `os.getenv()` calls.
- Use `asyncio.Lock` for any shared mutable state (e.g. token refresh in `IGSession`).

______________________________________________________________________

## Switching to production

1. In `.env`:
   ```env
   IG_ENV=live
   DATABASE_URL=postgresql+asyncpg://user:password@host:5432/ig_trading
   ```
1. Run migrations on the production DB: `alembic upgrade head`
1. The app auto-detects the DB driver from the URL — no code changes needed.
