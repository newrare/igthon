# Makefile — shortcuts for the IG Trading Bot.
#
# Every target is a thin wrapper around a command already documented in
# docs/DEVELOPMENT.md and docs/USE.md: nothing here is required to use the
# project, it only saves typing and removes the usual foot-guns (forgetting
# `mdformat` before a commit, starting a second bot on a busy port…).
#
#   make help        list every target
#   make setup       venv + dependencies + .env + database
#   make run         bot + dashboard

SHELL := /bin/bash

VENV    := .venv
PYTHON  := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
PIP     := $(PYTHON) -m pip
MDFILES := docs/ README.md CLAUDE.md
LOG_DIR := /tmp/ig_bot

# Dashboard port: read from .env (WEB_PORT), else the config.py default.
PORT := $(shell sed -n 's/^[[:space:]]*WEB_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' .env 2>/dev/null | tail -1)
ifeq ($(PORT),)
PORT := 8000
endif

# macOS: keep the machine awake for as long as the bot runs (no-op elsewhere).
CAFFEINATE := $(shell command -v caffeinate >/dev/null 2>&1 && echo "caffeinate -i -s")

# Extra flags forwarded to the underlying command: make test ARGS="-k signals"
ARGS ?=

.DEFAULT_GOAL := help

##@ General

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nIG Trading Bot — usage: \033[36mmake <target>\033[0m\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@printf "\nDashboard port: \033[36m%s\033[0m (WEB_PORT in .env)\n\n" "$(PORT)"

##@ Setup

.PHONY: setup
setup: venv install env migrate ## Full first-time setup (venv, deps, .env, database)
	@echo "Setup complete — edit .env (IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACCOUNT_ID) then: make run"

.PHONY: venv
venv: ## Create the virtual environment in .venv/
	@test -d $(VENV) || python3 -m venv $(VENV)

.PHONY: install
install: ## Install the project with its dev extras
	$(PIP) install -e ".[dev]"

.PHONY: env
env: ## Create .env from .env.example (never overwrites an existing one)
	@test -f .env && echo ".env already exists — left untouched." || cp .env.example .env

##@ Run

.PHONY: run
run: _port-free _bot-free ## Start the bot + web dashboard (main command)
	$(CAFFEINATE) $(PYTHON) -m src.main --web $(ARGS)

.PHONY: run-debug
run-debug: _port-free _bot-free ## Same as `run` with DEBUG on the console
	$(CAFFEINATE) $(PYTHON) -m src.main --web --log-level DEBUG $(ARGS)

.PHONY: bot
bot: _bot-free ## Start the scheduler only (no web dashboard)
	$(CAFFEINATE) $(PYTHON) -m src.main $(ARGS)

.PHONY: status
status: ## Is a bot running? (PID, start time, command)
	@pid=$$(lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t 2>/dev/null | head -1); \
	if [ -n "$$pid" ]; then \
		echo "Bot listening on port $(PORT):"; \
		ps -o pid,lstart,command -p $$pid; \
	else \
		pids=$$(pgrep -f 'src\.main' 2>/dev/null); \
		if [ -n "$$pids" ]; then \
			echo "A bot process is running without the dashboard:"; \
			ps -o pid,lstart,command -p $$pids; \
		else \
			echo "No bot running."; \
		fi; \
	fi

.PHONY: stop
stop: ## Stop the running bot (frees the dashboard port)
	@pids=$$(pgrep -f 'src\.main' 2>/dev/null); \
	if [ -z "$$pids" ]; then \
		echo "No bot running."; \
	else \
		ps -o pid,lstart,command -p $$pids; \
		echo "Stopping…"; \
		kill $$pids; \
	fi

.PHONY: restart
restart: stop ## Stop then start the bot + dashboard
	@sleep 2
	@$(MAKE) --no-print-directory run

.PHONY: open
open: ## Open the dashboard in the browser
	@open http://localhost:$(PORT) 2>/dev/null || echo "http://localhost:$(PORT)"

##@ Logs

.PHONY: logs
logs: ## Follow the rotating bot log
	tail -f $(LOG_DIR)/ig_bot.log

.PHONY: logs-api
logs-api: ## Follow the IG API audit log (every call)
	tail -f $(LOG_DIR)/ig_api_calls.log

.PHONY: logs-errors
logs-errors: ## Follow the IG API error log
	tail -f $(LOG_DIR)/ig_api_errors.log

##@ Quality

.PHONY: test
test: docs ## Run the test suite (Markdown reformatted first)
	$(PYTHON) -m pytest tests/ -q $(ARGS)

.PHONY: test-cov
test-cov: docs ## Run the tests with a coverage report
	$(PYTHON) -m pytest tests/ --cov=src --cov-report=term-missing $(ARGS)

.PHONY: lint
lint: ## Lint with ruff (includes import sorting)
	$(PYTHON) -m ruff check src/ tests/ $(ARGS)

.PHONY: format
format: ## Format Python with black and Markdown with mdformat
	$(PYTHON) -m black src/ tests/
	$(PYTHON) -m mdformat $(MDFILES)

.PHONY: fix
fix: ## Auto-fix lint issues, then format everything
	$(PYTHON) -m ruff check --fix src/ tests/
	$(MAKE) --no-print-directory format

.PHONY: docs
docs: ## Reformat / align the Markdown tables (run before a commit)
	$(PYTHON) -m mdformat $(MDFILES)

.PHONY: check
check: lint test ## Lint + tests — run this before committing

##@ Database

.PHONY: migrate
migrate: ## Apply every pending Alembic migration
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m alembic current

.PHONY: revision
revision: ## Generate a migration from the models — make revision M="add col to position"
	@test -n "$(M)" || { echo 'Missing message: make revision M="add my_column to position"'; exit 1; }
	$(PYTHON) -m alembic revision --autogenerate -m "$(M)"
	@echo "Review the generated file, then: make migrate"

.PHONY: downgrade
downgrade: ## Roll the database back one revision
	$(PYTHON) -m alembic downgrade -1
	$(PYTHON) -m alembic current

.PHONY: db-current
db-current: ## Show the current Alembic revision
	$(PYTHON) -m alembic current

.PHONY: db-history
db-history: ## Show the migration history
	$(PYTHON) -m alembic history --verbose

.PHONY: db-shell
db-shell: ## Open a SQLite shell on the local database
	sqlite3 ig_trading.db

##@ Tools (src/scripts/)

.PHONY: crawl
crawl: ## Crawl / resume the full IG market catalog (stop the bot first)
	$(PYTHON) -m src.scripts.crawl_catalog $(ARGS)

.PHONY: crawl-status
crawl-status: ## Report the catalog crawl progress (no IG session, no API call)
	$(PYTHON) -m src.scripts.crawl_catalog --status

.PHONY: trace
trace: ## Trace the IG activity trail — make trace ARGS="--days 3 --epic CS.D.EURUSD.CFD.IP"
	$(PYTHON) -m src.scripts.trace_activity $(ARGS)

.PHONY: adopt
adopt: ## Adopt the live IG positions the database is not tracking
	$(PYTHON) -m src.scripts.adopt_orphans

.PHONY: inspect
inspect: ## Inspect an epic's pricing — make inspect EPIC=CC.D.LCC.UNC.IP
	@test -n "$(EPIC)" || { echo 'Missing epic: make inspect EPIC=CC.D.LCC.UNC.IP'; exit 1; }
	$(PYTHON) -m src.scripts.inspect_market $(EPIC) $(ARGS)

.PHONY: dump-euro
dump-euro: ## Dump euro-per-point for the archived epics (stop the bot first)
	$(PYTHON) -m src.scripts.dump_euro_per_point $(ARGS)

##@ Research (archive files only — safe while the bot runs)

.PHONY: baseline
baseline: ## Strategy vs coin-flip replays — make baseline ARGS="--strategy open_fade --seeds 20"
	$(PYTHON) -m src.scripts.random_baseline $(ARGS)

.PHONY: research
research: ## Research dataset — make research ARGS="build" then ARGS="analyze"
	@test -n "$(ARGS)" || { echo 'Missing sub-command: make research ARGS="build" (then ARGS="analyze")'; exit 1; }
	$(PYTHON) -m src.scripts.research $(ARGS)

##@ Housekeeping

.PHONY: clean
clean: ## Remove caches and compiled files (never touches .env or the database)
	find . -path ./$(VENV) -prune -o -type d -name __pycache__ -print0 | xargs -0 rm -rf
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov

# --- internal guards -------------------------------------------------------
# The bot must stay a single process: two schedulers race on the per-epic open
# lock and can double-open a position.

.PHONY: _port-free
_port-free:
	@pid=$$(lsof -nP -iTCP:$(PORT) -sTCP:LISTEN -t 2>/dev/null | head -1); \
	if [ -n "$$pid" ]; then \
		echo "Port $(PORT) is already in use (PID $$pid) — a bot is probably already running."; \
		echo "  make status    # see it"; \
		echo "  make stop      # stop it, then run again"; \
		exit 1; \
	fi

.PHONY: _bot-free
_bot-free:
	@pids=$$(pgrep -f 'src\.main' 2>/dev/null); \
	if [ -n "$$pids" ]; then \
		echo "A bot process is already running (PID $$pids) — only one scheduler may run at a time."; \
		echo "  make status    # see it"; \
		echo "  make stop      # stop it, then run again"; \
		exit 1; \
	fi
