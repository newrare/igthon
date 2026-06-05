# IG Trading Bot

An automated intraday trading bot for [IG Markets](https://www.ig.com), written in Python 3.11+.

It uses the **IG REST API v3 (OAuth)** and the **Lightstreamer streaming feed** to detect trends,
open/close CFD positions, and expose a live web dashboard — replacing a legacy PHP bot.

______________________________________________________________________

## Quick start

```bash
# 1. Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure credentials
cp .env.example .env
# Edit .env — set IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACCOUNT_ID

# 3. Create the database
alembic upgrade head

# 4. Test the connection (no trading)
python -m src.main --analyze-only

# 5. Start bot + web dashboard
python -m src.main --web
# → http://localhost:8000
```

______________________________________________________________________

## Running modes

| Command                                       | Effect                                                        |
| --------------------------------------------- | ------------------------------------------------------------- |
| `python -m src.main --analyze-only`           | Fetch prices, compute signals, print table — no orders placed |
| `python -m src.main`                          | Full bot with scheduler (no web UI)                           |
| `python -m src.main --web`                    | Full bot + FastAPI dashboard at `http://localhost:8000`       |
| `python -m src.main --epics IX.D.DAX.IFMM.IP` | Override the tracked epics list                               |
| `python -m src.main --log-level DEBUG`        | Verbose output for all API calls and decisions                |

______________________________________________________________________

## Documentation

| Document                                       | Description                                           |
| ---------------------------------------------- | ----------------------------------------------------- |
| [docs/USE.md](docs/USE.md)                     | Usage guide — commands, web endpoints, daily workflow |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)   | Project structure, layers, and data strategy          |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | All `.env` settings with descriptions                 |
| [docs/STRATEGY.md](docs/STRATEGY.md)           | Trend Volume Intraday strategy explained              |
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
