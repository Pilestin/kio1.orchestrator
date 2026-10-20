# KIO1: Orchestrator

A browser-based chat UI that sends a user's query to a local Ollama model and renders the model's structured JSON response, messenger-style. Datasets, model checkpoints, or other files can be uploaded from the UI and are saved under `assets/` (not committed to version control).

## Requirements

- Python 3.10 or newer
- An Ollama installation
- Access to the model configured in `config.json`
- Docker (optional, for containerized runs)

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

The model and Ollama endpoint are selected in `config.json`. See [Configuration](docs/configuration.md) for the full field reference.

## Usage

```bash
python3 main.py
```

This preloads the configured model and starts the web UI at `http://127.0.0.1:5000` (override with the `KIO1_WEB_HOST`/`KIO1_WEB_PORT` env vars). Open that address in a browser to chat and upload files.

See [Usage Guide](docs/usage.md) for example sessions and sample requests.

### In Docker

The orchestrator also ships a `Dockerfile`. It reaches Ollama on the host via `host.docker.internal`, so mount your `config.json` at runtime and override the endpoint with an env var — the same `config.json` then works both on the host and in the container. `config.json` is mounted (not baked into the image) so credentials such as `otlp_bearer_token` stay out of image layers, and `KIO1_OLLAMA_ENDPOINT` overrides the file's endpoint at runtime, so there is no second config file to drift. Ollama must be running on the host first.

#### Build the container

Rebuild the image whenever `Dockerfile`, `requirements.txt`, or the application source changes:

```bash
docker build -t kio1 .
```

#### Launch the container

Use this to start the already-built image — no rebuild required. Create the mounted host directories first if they do not already exist; otherwise Docker creates them as `root`, and the container's non-root user will not be able to write to them (this bites `assets/` in particular, since it is only created on first upload):

```bash
mkdir -p chats logs assets
docker run --rm -p 5000:5000 --add-host=host.docker.internal:host-gateway \
  -v "$PWD/config.json:/app/config.json" \
  -v "$PWD/chats:/app/chats" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/assets:/app/assets" \
  -e KIO1_OLLAMA_ENDPOINT=http://host.docker.internal:11434 \
  kio1
```

Open `http://localhost:5000` once the container is running. `docker run` always creates a new container rather than reusing one, so without `--rm` every launch leaves a stopped container behind (visible in `docker ps -a`). `--rm` deletes the container as soon as it exits; mounting `chats/`, `logs/`, and `assets/` keeps session history, logs, and uploaded files on the host regardless, so nothing is lost. To clear out containers created before this flag was added:

```bash
docker container prune
```

Stop Container:
```bash
docker stop <CONTAINER ID>
```

## Observability

<!-- KIO1 can export OpenTelemetry traces and metrics to a local persistent Tempo and Prometheus stack. Telemetry is disabled by default.

Start the observability services with:

```bash
docker compose -f observability/compose.telemetry.yaml up -d
```

See the [Observability Guide](docs/observability.md) for configuration, querying, retention, and safe-shutdown instructions. -->

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
- [Observability](docs/observability.md) — Tracing, metrics, storage, querying, and safe shutdown
