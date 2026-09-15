# Development Guide

## Prerequisites

- Python 3.11+
- A virtual environment tool (`venv` built-in is fine)
- An IG demo account with an API key ([create one here](https://www.ig.com))

______________________________________________________________________

## Make targets

The `Makefile` at the repository root wraps every command in this guide. It is a
convenience only — the raw commands still work — but it removes the recurring
foot-guns: `make test` reformats the Markdown first, `make run` refuses to start
a second bot, `make migrate` prints `alembic current` so an unapplied revision
cannot be missed.

```bash
make help        # list every target, grouped, with the active WEB_PORT
```

| Group        | Target           | Runs                                                 |
| ------------ | ---------------- | ---------------------------------------------------- |
| Setup        | `setup`          | `venv` + `install` + `env` + `migrate`               |
|              | `venv`           | `python3 -m venv .venv`                              |
|              | `install`        | `pip install -e ".[dev]"`                            |
|              | `env`            | `cp .env.example .env` (never overwrites)            |
| Run          | `run`            | `python -m src.main --web` (+ `caffeinate` on macOS) |
|              | `run-debug`      | same, with `--log-level DEBUG`                       |
|              | `bot`            | `python -m src.main` (scheduler only)                |
|              | `status`         | the running bot's PID, start time and command        |
|              | `stop`           | `kill` the running bot                               |
|              | `restart`        | `stop`, then `run`                                   |
|              | `open`           | open the dashboard in the browser                    |
| Logs         | `logs`           | `tail -f /tmp/ig_bot/ig_bot.log`                     |
|              | `logs-api`       | `tail -f /tmp/ig_bot/ig_api_calls.log`               |
|              | `logs-errors`    | `tail -f /tmp/ig_bot/ig_api_errors.log`              |
| Quality      | `check`          | `lint` + `test` — run before committing              |
|              | `test`           | `mdformat`, then `pytest tests/ -q`                  |
|              | `test-cov`       | `pytest --cov=src --cov-report=term-missing`         |
|              | `lint`           | `ruff check src/ tests/`                             |
|              | `format`         | `black src/ tests/` + `mdformat`                     |
|              | `fix`            | `ruff check --fix`, then `format`                    |
|              | `docs`           | `mdformat docs/ README.md CLAUDE.md`                 |
| Database     | `migrate`        | `alembic upgrade head` + `alembic current`           |
|              | `revision M="…"` | `alembic revision --autogenerate -m "…"`             |
|              | `downgrade`      | `alembic downgrade -1` + `alembic current`           |
|              | `db-current`     | `alembic current`                                    |
|              | `db-history`     | `alembic history --verbose`                          |
|              | `db-shell`       | `sqlite3 ig_trading.db`                              |
| Tools        | `crawl`          | `python -m src.scripts.crawl_catalog`                |
|              | `crawl-status`   | the crawl progress (no IG session, no API call)      |
|              | `trace`          | `python -m src.scripts.trace_activity`               |
|              | `adopt`          | `python -m src.scripts.adopt_orphans`                |
|              | `inspect EPIC=…` | `python -m src.scripts.inspect_market <epic>`        |
|              | `dump-euro`      | `python -m src.scripts.dump_euro_per_point`          |
| Housekeeping | `clean`          | drop `__pycache__`, `.pytest_cache`, `.ruff_cache`   |

Conventions:

- **`ARGS`** forwards extra flags to the underlying command:
  `make test ARGS="-k signals -v"`, `make trace ARGS="--days 3"`,
  `make crawl ARGS="--max-nodes 200"`.
- **Named variables** are required where the command needs one:
  `make revision M="add my_column to position"`, `make inspect EPIC=CC.D.LCC.UNC.IP`.
- Targets use `.venv/bin/python` when the virtual environment exists, so they
  work without activating it.
- `run` / `run-debug` / `bot` refuse to start when another bot process is alive:
  a single scheduler is a hard invariant (two of them race on the per-epic open
  lock and can double-open a position).

______________________________________________________________________

## Setup

```bash
# Clone / enter the project
cd igthon/

# Everything at once: venv, dependencies, .env, database
make setup
```

Equivalent to:

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
```

Then edit `.env` — fill in `IG_API_KEY`, `IG_USERNAME`, `IG_PASSWORD`,
`IG_ACCOUNT_ID` (see [CONFIGURATION.md](CONFIGURATION.md) for every setting).

______________________________________________________________________

## Standalone scripts

Utility scripts live in `src/scripts/` — they are run as modules for debugging or
one-off tasks. Each has a `make` shortcut.

```bash
make trace ARGS="--days 3"          # python -m src.scripts.trace_activity
make adopt                          # python -m src.scripts.adopt_orphans
make inspect EPIC=CC.D.LCC.UNC.IP   # python -m src.scripts.inspect_market
make crawl                          # python -m src.scripts.crawl_catalog
make crawl-status                   # crawl progress, no IG session
make dump-euro                      # python -m src.scripts.dump_euro_per_point
```

`crawl` and `dump-euro` open their own IG session: run them with the bot stopped
(`make stop`), or expect the bot's session to be re-authenticated.

______________________________________________________________________

## Running the bot

```bash
# Bot + web dashboard (the usual command) — port is WEB_PORT in .env
make run

# Scheduler only, no web UI
make bot

# Verbose console output
make run-debug

# Is one already running? Stop it, restart it
make status
make stop
make restart
```

`make run` wraps `python -m src.main --web` and, on macOS, prefixes it with
`caffeinate -i -s` so the machine does not sleep mid-session.

Whether a validated intent is actually sent to the broker is `OPEN_MODE` in
`.env`: `auto` places the order, `notify` records it as a signal for the
dashboard. Same gates, same sizing either way — there is no "analyse only" flag.

______________________________________________________________________

## Tests

```bash
# The whole suite (reformats the Markdown first)
make test

# A single module, or any pytest flag, through ARGS
make test ARGS="tests/test_signals.py -v"
make test ARGS="-k trailing"

# With a coverage report
make test-cov
```

Tests must never hit the real IG API. Use `respx` to mock httpx calls.
Every module in `src/` has a corresponding test file in `tests/`.

______________________________________________________________________

## Linting and formatting

```bash
make lint      # ruff check src/ tests/ (includes import sorting)
make format    # black src/ tests/ + mdformat docs/ README.md CLAUDE.md
make fix       # ruff --fix, then format
make check     # lint + tests — the pre-commit gate
```

Configuration is in `pyproject.toml`:

- `black` — line length 88, target Python 3.11
- `ruff` — rules: E, F, I, N, W, UP

______________________________________________________________________

## Database migrations

```bash
# Apply all pending migrations (then prints `alembic current`)
make migrate

# Generate a migration from model changes
make revision M="add my_column to position"

# Roll back one migration
make downgrade

# Where does the DB stand?
make db-current
make db-history

# Query the local SQLite database
make db-shell
```

Migration files are in [alembic/versions/](../alembic/versions/).

**A new column means writing the revision *and* running `make migrate`.** The
running app queries every mapped column, so an unapplied revision turns the next
request into `no such column: …` and a 500 on the dashboard. `make migrate`
prints the current revision afterwards so the check is not forgotten.

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
