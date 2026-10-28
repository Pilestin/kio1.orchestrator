.DEFAULT_GOAL := help

# Every target here has a plain equivalent in docs/usage.md: `make` is a
# convenience, not a requirement, and it is not installed everywhere.
VENV        := .venv
# A virtual environment puts its executables in Scripts/ on Windows and bin/
# everywhere else. Without this the targets below silently point at nothing.
ifeq ($(OS),Windows_NT)
BINDIR      := $(VENV)/Scripts
else
BINDIR      := $(VENV)/bin
endif
PYTHON      := $(BINDIR)/python
PIP         := $(BINDIR)/pip
SOURCES     := main.py app.py core/ providers/ dispatch/ services/ tools/ tests/ examples/
COMPOSE     := docker compose -f deploy/compose.yaml

# ── Bootstrap ───────────────────────────────────────────────────────────────

.PHONY: install
install: ## Create venv and install all dependencies
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

.PHONY: start
start: ## Set everything up if needed and open the console (same as start.py)
	python start.py

# ── Formatting ───────────────────────────────────────────────────────────────

.PHONY: format
format: ## Auto-format code (black + isort)
	$(BINDIR)/isort --profile black $(SOURCES)
	$(BINDIR)/black $(SOURCES)

.PHONY: format-check
format-check: ## Verify code formatting (isort + black)
	$(BINDIR)/isort --profile black --check-only $(SOURCES)
	$(BINDIR)/black --check $(SOURCES)

# ── Linting & type-checking ──────────────────────────────────────────────────

.PHONY: lint
lint: ## Run ruff linter
	$(BINDIR)/ruff check $(SOURCES)

.PHONY: typecheck
typecheck: ## Run mypy static type checker
	$(BINDIR)/mypy $(SOURCES)

.PHONY: check
check: lint typecheck ## Run all static checks (lint + typecheck)

# ── Services ─────────────────────────────────────────────────────────────────

.PHONY: services
services: config.json ## Start PostgreSQL, the Session Manager, and the Workflow API
	$(COMPOSE) up -d --build

# Personal and untracked: created once from the shipped example.
config.json:
	python -c "import shutil; shutil.copyfile('config.example.json', 'config.json')"


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
	KIO1_DATABASE_URL=postgresql://kio1:kio1@localhost:5432/kio1 		$(BINDIR)/uvicorn services.session_manager.app:app --port 8001 --reload

.PHONY: run-workflow-api
run-workflow-api: ## Run the Workflow API and console (needs the Session Manager)
	KIO1_SESSION_MANAGER_URL=http://localhost:8001 		$(BINDIR)/uvicorn app:app --port 8101 --reload

# ── Testing ───────────────────────────────────────────────────────────────────

.PHONY: example
example: ## Run the Use-Case 1 example end to end
	$(PYTHON) examples/uc1/run.py

.PHONY: example-console
example-console: ## Serve Use-Case 1 at http://127.0.0.1:8101/
	$(PYTHON) examples/uc1/serve.py

.PHONY: test
test: ## Run test suite
	$(BINDIR)/pytest tests/ -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(BINDIR)/pytest tests/ -v --cov=. --cov-report=term-missing

.PHONY: test-postgres
test-postgres: ## Run the suite with the PostgreSQL repository tests enabled
	KIO1_TEST_DATABASE_URL=postgresql://kio1:kio1@localhost:5432/kio1 		$(BINDIR)/pytest tests/ -v

.PHONY: test-live
test-live: ## Run every test, including those needing PostgreSQL and NATS
	KIO1_TEST_DATABASE_URL=postgresql://kio1:kio1@localhost:5432/kio1 	KIO1_TEST_NATS_SERVERS=nats://localhost:4222 		$(BINDIR)/pytest tests/ -v

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
