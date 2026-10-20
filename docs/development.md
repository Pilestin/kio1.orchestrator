# Development Guide

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) installed and running
- Docker with Docker Compose (optional, for observability integration testing)

## Setup

```bash
git clone <repo-url>
cd orchestrator
make install
source .venv/bin/activate
```

This creates a `.venv` virtual environment and installs all runtime and development dependencies.

If using Ollama, pull the required model:

```bash
ollama pull ministral-3:8b
```

## Project Structure

```
main.py              - Entry point (startup, run_turn() turn logic, Ollama request/response handling)
webapp.py            - Flask web UI (chat and file-upload routes)
config_loader.py     - Configuration loading
prompt_loader.py     - System prompt file reader
chat_history.py      - JSONL conversation persistence
formatter.py         - JSON pretty-printing
session_logger.py    - Per-session log setup and duration measurement
telemetry.py         - OpenTelemetry tracing and metrics
templates/, static/  - Chat UI markup, styling, and client-side rendering
config.json          - Application settings
requirements.txt     - Runtime dependencies (installed in the Docker image)
requirements-dev.txt - Adds testing/formatting/linting tools on top
observability/       - Collector, Tempo, and Prometheus configuration
prompts/             - System prompt files
chats/               - Session history (auto-created, gitignored)
logs/                - Session logs (auto-created, gitignored)
tests/               - Unit tests
docs/                - Documentation
```

## Running Tests

```bash
make test          # run full test suite
make test-cov      # run with coverage report
```

Or directly:

```bash
pytest tests/ -v
```

Tests use mocks for the Ollama HTTP API and telemetry exporters, along with temporary directories for file I/O. No running Ollama install or observability service is required for unit testing.

## Adding a New Module

1. Create the module file at the project root.
2. Import and use it from `main.py`.
3. Add corresponding tests in `tests/test_orchestrator.py`.
4. Update `docs/api-reference.md` with the new functions.

## System Prompt Development

System prompts live in `prompts/`. To iterate on a prompt:

1. Create a new version file (e.g., `kio1_system_prompt_ver2.txt`).
2. Update `prompt_path` in `config.json`.
3. Restart the orchestrator.

Keep previous versions for comparison and rollback.

## Code Quality

All tools are installed as part of `make install` and wired to `make` targets:

| Target | Tool(s) | Purpose |
| :--- | :--- | :--- |
| `make format` | black, isort | Auto-format code and imports |
| `make format-check` | black, isort | Verify formatting without changing files |
| `make lint` | ruff | Fast linting and style checks |
| `make typecheck` | mypy | Static type analysis |
| `make check` | ruff + mypy | All static checks combined |
| `make test` | pytest | Run test suite |
| `make test-cov` | pytest-cov | Run tests with coverage report |
| `make ci` | format + ruff + mypy + pytest | Full pipeline — run before pushing |
| `make clean` | — | Remove caches and build artefacts |

Run `make help` to see all targets.

## Dependencies

`requirements.txt` lists only runtime dependencies — it is what the Docker image installs. `requirements-dev.txt` adds testing, formatting, and linting tools on top via `-r requirements.txt`; `make install` uses it so local development gets everything.

- **Runtime**: Ollama requests use `urllib.request` from the standard library — no HTTP client dependency.
- **Runtime observability**: `opentelemetry-api >= 1.44.0`, `opentelemetry-sdk >= 1.44.0`, `opentelemetry-exporter-otlp-proto-http >= 1.44.0`, all constrained below version `2.0.0`.
- **Testing**: `pytest >= 7.0`, `pytest-cov >= 4.0`.
- **Formatting**: `black >= 24.0`, `isort >= 5.13`.
- **Linting**: `ruff >= 0.4`.
- **Type checking**: `mypy >= 1.10`.

## Code Style

- Type annotations on all function signatures.
- Docstrings in Google style with `Args:` and `Returns:` sections.
- No classes except dataclasses for configuration.
- Run `make format` before committing to apply consistent formatting.
- Run `make ci` before opening a pull request.
- Log with `%`-style placeholders (`logger.info("model=%s", model)`), not f-strings, so formatting is deferred until a record is actually emitted.
