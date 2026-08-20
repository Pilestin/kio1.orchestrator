.DEFAULT_GOAL := help

VENV        := .venv
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
SOURCES     := main.py core/ providers/ dispatch/ tools/ tests/

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

# ── Testing ───────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run test suite
	$(VENV)/bin/pytest tests/ -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(VENV)/bin/pytest tests/ -v --cov=. --cov-report=term-missing

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
