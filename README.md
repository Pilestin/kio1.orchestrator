# KIO1: Orchestrator

A terminal application that turns software-development requests into structured JSON workflow plans.

KIO1 supports multiple model providers:

- Ollama
- OpenAI
- Anthropic

Providers are loaded dynamically from `<provider>_client.py`, keeping the main application and configuration loader provider-neutral.

## Requirements

- Python 3.10 or newer
- An Ollama installation, OpenAI API key, or Anthropic API key
- Access to the model configured in `config.json`
- Docker with Docker Compose (optional, for local trace and metric storage)

## Installation

```bash
make install
source .venv/bin/activate
```

Or manually:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Further information about the project is available in the docs folder

## Configuration

The active provider and model are selected in `config.json`. API keys are read from environment variables, never from `config.json` itself. See [Configuration](docs/configuration.md) for the full field reference and per-provider setup.

## Usage

```bash
python3 main.py
```

See [Usage Guide](docs/usage.md) for example sessions and sample requests.

## Observability

<!-- KIO1 can export OpenTelemetry traces and metrics to a local persistent Tempo and Prometheus stack. Telemetry is disabled by default.

Start the observability services with:

```bash
docker compose -f observability/compose.telemetry.yaml up -d
```

See the [Observability Guide](docs/observability.md) for configuration, querying, retention, and safe-shutdown instructions. -->

## Workflow Dispatch

KIO1 can send the steps of a generated plan to the KIO agents that carry them out, following the `KIO1 - KIO10` integration protocol. Dispatch is disabled by default; enable it with `dispatch.enabled` in `config.json` and list the reachable agents under `dispatch.agents`.

A dependency-free stub agent is included for validating the protocol before real agents exist:

```bash
python tools/dummy_agent.py --agent-id KIO2 --port 8102
```

See the [Workflow Dispatch Guide](docs/workflow-dispatch.md) for the message format, reply contract, execution modes, and known gaps.

## Running Tests

```bash
pytest -q
```

See [Development Guide](docs/development.md) for the full test, lint, and format workflow.

## Documentation

- [Architecture](docs/architecture.md) — System overview, components, and data flow
- [Configuration](docs/configuration.md) — All config.json settings explained
- [Usage Guide](docs/usage.md) — Interactive session examples and workflows
- [API Reference](docs/api-reference.md) — Module and function documentation
- [Chat History Format](docs/chat-history.md) — JSONL session file specification
- [Development Guide](docs/development.md) — Setup, testing, and contribution guidelines
- [Troubleshooting](docs/troubleshooting.md) — Common errors and how to resolve them
- [Workflow Dispatch](docs/workflow-dispatch.md) — Sending plan steps to KIO agents
- [Observability](docs/observability.md) — Tracing, metrics, storage, querying, and safe shutdown
