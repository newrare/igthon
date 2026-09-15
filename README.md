# IG Trading Bot

An automated intraday trading bot for [IG Markets](https://www.ig.com), written in Python 3.11+.

It uses the **IG REST API v3 (OAuth)** and the **Lightstreamer streaming feed** to detect trends,
open/close CFD positions, and expose a live web dashboard — replacing a legacy PHP bot.

______________________________________________________________________

## Quick start

Every command below is also a `make` target — run `make help` for the full list.

```bash
# 1. Install, configure, create the database (venv + deps + .env + alembic)
make setup

# 2. Fill in the credentials
#    Edit .env — set IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACCOUNT_ID

# 3. Start bot + web dashboard
make run
# → http://localhost:9000  (WEB_PORT in .env)
```

______________________________________________________________________

## Running modes

| Command          | Raw equivalent                               | Effect                                         |
| ---------------- | -------------------------------------------- | ---------------------------------------------- |
| `make run`       | `python -m src.main --web`                   | Full bot + FastAPI dashboard on `WEB_PORT`     |
| `make bot`       | `python -m src.main`                         | Full bot with scheduler, no web UI             |
| `make run-debug` | `python -m src.main --web --log-level DEBUG` | Same as `run`, verbose API calls and decisions |
| `make status`    | —                                            | Is a bot already running? (PID and start time) |
| `make stop`      | —                                            | Stop the running bot and free the port         |
| `make logs`      | `tail -f /tmp/ig_bot/ig_bot.log`             | Follow the rotating log                        |

`make run` refuses to start when a bot is already running: two schedulers race
on the per-epic open lock and can double-open a position. Use `make restart` to
stop the old one and start a fresh one.

Whether the bot places orders itself or only proposes them is `OPEN_MODE` in
`.env` (`auto` / `notify`) — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

______________________________________________________________________

## Make targets

| Group    | Targets                                                                               |
| -------- | ------------------------------------------------------------------------------------- |
| Setup    | `setup` · `venv` · `install` · `env`                                                  |
| Run      | `run` · `run-debug` · `bot` · `status` · `stop` · `restart` · `open`                  |
| Logs     | `logs` · `logs-api` · `logs-errors`                                                   |
| Quality  | `check` · `test` · `test-cov` · `lint` · `format` · `fix` · `docs`                    |
| Database | `migrate` · `revision M="…"` · `downgrade` · `db-current` · `db-history` · `db-shell` |
| Tools    | `crawl` · `crawl-status` · `trace` · `adopt` · `inspect EPIC=…` · `dump-euro`         |

Targets forward extra flags through `ARGS`, e.g. `make test ARGS="-k signals"` or
`make trace ARGS="--days 3"`. Full reference:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#make-targets).

______________________________________________________________________

## Documentation

| Document                                       | Description                                           |
| ---------------------------------------------- | ----------------------------------------------------- |
| [docs/USE.md](docs/USE.md)                     | Usage guide — commands, web endpoints, daily workflow |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)   | Project structure, layers, and data strategy          |
| [docs/DATAFLOW.md](docs/DATAFLOW.md)           | Time and data contract — feed, buffer, decision clock |
| [docs/READING.md](docs/READING.md)             | Reading a market — every measure a strategy can read  |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | All `.env` settings with descriptions                 |
| [docs/strategies/](docs/strategies/README.md)  | Pluggable entry strategies and close profiles         |
| [docs/API.md](docs/API.md)                     | IG REST API reference (auth, endpoints, streaming)    |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)     | Dev setup, tests, linting, migrations                 |

______________________________________________________________________

## Tech stack

| Component     | Technology                                        |
| ------------- | ------------------------------------------------- |
| Language      | Python 3.11+                                      |
| HTTP client   | `httpx` (async)                                   |
| Database      | SQLite (dev) / PostgreSQL (prod) via `SQLAlchemy` |
| Migrations    | `Alembic`                                         |
| Web interface | `FastAPI` + `Jinja2`                              |
| Streaming     | `lightstreamer-client-lib`                        |
| Scheduler     | `APScheduler`                                     |
| Tests         | `pytest` + `pytest-asyncio`                       |
| Config        | `pydantic-settings`                               |

______________________________________________________________________

## Resources

- [IG API Guide](https://labs.ig.com/rest-trading-api-guide.html)
- [IG API Reference](https://labs.ig.com/rest-trading-api-reference.html)
- [IG Streaming API](https://labs.ig.com/streaming-api-guide.html)
- [IG API Companion (interactive)](https://labs.ig.com/companion/api-rest-companion-release/index.html)
