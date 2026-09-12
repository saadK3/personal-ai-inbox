.PHONY: install db-up db-down migrate dev bot test lint export restore

# Prefer the repository virtual environment so Make works in non-interactive
# shells where the user's activated environment is not inherited.
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

# Homebrew's Compose plugin may expose `docker-compose`, while Docker Desktop
# commonly exposes `docker compose`. Prefer whichever command is installed.
COMPOSE ?= $(shell command -v docker-compose >/dev/null 2>&1 && echo docker-compose || echo docker compose)

install:
	$(PYTHON) -m pip install -e ".[dev]"

db-up:
	$(COMPOSE) up -d db

db-down:
	$(COMPOSE) down

migrate:
	$(PYTHON) -m alembic upgrade head

dev:
	$(PYTHON) -m uvicorn app.main:app --reload

bot:
	$(PYTHON) -m app.discord_bot

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

export:
	$(PYTHON) -m app.backup export

restore:
	@test -n "$(ARCHIVE)" || (echo "Usage: make restore ARCHIVE=/path/to/export.zip" && exit 1)
	$(PYTHON) -m app.backup restore "$(ARCHIVE)"
