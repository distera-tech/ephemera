# Ephemera developer tasks. `make help` lists them.
API := apps/api
WEB := apps/web
COMPOSE := docker compose -f infra/docker-compose.yml
export DATABASE_URL ?= postgresql+asyncpg://ephemera:ephemera@localhost:5432/ephemera

.PHONY: help install up down logs migrate api worker web test test-unit lint typecheck e2e demo-docs check

help:  ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:  ## Install backend (uv) and frontend (npm) dependencies
	cd $(API) && uv sync
	cd $(WEB) && npm ci

up:  ## Start the full stack with Docker Compose (simulation by default)
	$(COMPOSE) up --build -d
	@echo "UI: http://localhost:3000  API: http://localhost:8000/docs"

down:  ## Stop the stack (worker gets 10 min to tear down any GPU)
	$(COMPOSE) down

logs:  ## Follow worker logs
	$(COMPOSE) logs -f worker

migrate:  ## Apply database migrations (local Postgres)
	cd $(API) && uv run alembic upgrade head

api:  ## Run the API locally with reload
	cd $(API) && uv run uvicorn app.main:app --reload --port 8000

worker:  ## Run the worker locally
	cd $(API) && uv run python -m app.workers

web:  ## Run the frontend dev server
	cd $(WEB) && npm run dev

test:  ## Backend tests (integration tests need Postgres: TEST_DATABASE_URL)
	cd $(API) && uv run pytest -q

test-unit:  ## Backend unit tests only (no database)
	cd $(API) && uv run pytest -q tests/unit

lint:  ## Ruff + ESLint
	cd $(API) && uv run ruff check app tests && uv run ruff format --check app tests
	cd $(WEB) && npm run lint

typecheck:  ## mypy (strict) + tsc
	cd $(API) && uv run mypy app
	cd $(WEB) && npm run typecheck

e2e:  ## Playwright against a running stack (make up first)
	cd $(WEB) && npx playwright test

demo-docs:  ## Regenerate synthetic demo PDFs
	$(API)/.venv/bin/python scripts/generate_demo_documents.py

check: lint typecheck test  ## Everything CI runs
