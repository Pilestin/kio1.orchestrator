.DEFAULT_GOAL := help

VENV        := .venv
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
SOURCES     := main.py core/ providers/ dispatch/ services/ tools/ tests/
COMPOSE     := docker compose -f deploy/compose.yaml

# ── Bootstrap ───────────────────────────────────────────────────────────────

.PHONY: install
install: ## Create venv and install all dependencies
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

# ── Formatting ───────────────────────────────────────────────────────────────

.PHONY: format
format: ## Auto-format code (black + isort)
	$(VENV)/bin/isort --profile black $(SOURCES)
	$(VENV)/bin/black $(SOURCES)

.PHONY: format-check
format-check: ## Verify code formatting (isort + black)
	$(VENV)/bin/isort --profile black --check-only $(SOURCES)
	$(VENV)/bin/black --check $(SOURCES)

# ── Linting & type-checking ──────────────────────────────────────────────────

.PHONY: lint
lint: ## Run ruff linter
	$(VENV)/bin/ruff check $(SOURCES)

.PHONY: typecheck
typecheck: ## Run mypy static type checker
	$(VENV)/bin/mypy $(SOURCES)

.PHONY: check
check: lint typecheck ## Run all static checks (lint + typecheck)

# ── Services ─────────────────────────────────────────────────────────────────

.PHONY: services
services: ## Start PostgreSQL, the Session Manager, and the Workflow API
	$(COMPOSE) up -d --build

.PHONY: services-down
services-down: ## Stop the service stack, keeping the database volume
	$(COMPOSE) down

.PHONY: services-logs
services-logs: ## Follow the service logs
	$(COMPOSE) logs -f

.PHONY: infra
infra: ## Start only PostgreSQL and NATS, for running the services from source
	$(COMPOSE) up -d postgres nats

.PHONY: postgres
postgres: ## Start only PostgreSQL
	$(COMPOSE) up -d postgres

.PHONY: run-session-manager
run-session-manager: ## Run the Session Manager from source (needs make postgres)
	KIO1_DATABASE_URL=postgresql://kio1:kio1@localhost:5432/kio1 		$(VENV)/bin/uvicorn services.session_manager.app:app --port 8001 --reload

.PHONY: run-workflow-api
run-workflow-api: ## Run the Workflow API from source (needs the Session Manager)
	KIO1_SESSION_MANAGER_URL=http://localhost:8001 		$(VENV)/bin/uvicorn services.workflow_api.app:app --port 8000 --reload

# ── Testing ───────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run test suite
	$(VENV)/bin/pytest tests/ -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(VENV)/bin/pytest tests/ -v --cov=. --cov-report=term-missing

.PHONY: test-postgres
test-postgres: ## Run the suite with the PostgreSQL repository tests enabled
	KIO1_TEST_DATABASE_URL=postgresql://kio1:kio1@localhost:5432/kio1 		$(VENV)/bin/pytest tests/ -v

.PHONY: test-live
test-live: ## Run every test, including those needing PostgreSQL and NATS
	KIO1_TEST_DATABASE_URL=postgresql://kio1:kio1@localhost:5432/kio1 	KIO1_TEST_NATS_SERVERS=nats://localhost:4222 		$(VENV)/bin/pytest tests/ -v

# ── Composite ────────────────────────────────────────────────────────────────

.PHONY: ci
ci: format check test ## Full CI pipeline: format + lint + typecheck + tests

# ── Housekeeping ─────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove generated artefacts
	find . -path ./$(VENV) -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -path ./$(VENV) -prune -o -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -path ./$(VENV) -prune -o -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -path ./$(VENV) -prune -o -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -path ./$(VENV) -prune -o -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov .coverage

# ── Help ─────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
