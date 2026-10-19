# Configuration

All application settings are stored in `config.json` at the project root.

## Configuration File Reference

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Ollama model identifier |
| `prompt_path` | string | Path to the system prompt file (relative to project root) |
| `chat_directory` | string | Directory for chat history files |
| `temperature` | float | Sampling temperature (lower = more deterministic) |
| `request_timeout` | float | Request timeout in seconds |
| `keep_alive` | int | Ollama model residency in seconds (`-1` = keep loaded; default: `-1`) |
| `max_output_tokens` | int | Maximum number of generated output tokens; must be positive (default: `4096`) |
| `endpoint` | string | Ollama base URL |
| `context_window_size` | int | Ollama context window in tokens; must be larger than `max_output_tokens` |
| `telemetry` | object | Optional OpenTelemetry configuration; telemetry is disabled when omitted |
| `telemetry.enabled` | boolean | Enables trace and metric export (default: `false`) |
| `telemetry.service_name` | string | Non-empty service name attached to exported telemetry (default: `kio1-orchestrator`) |
| `telemetry.otlp_http_endpoint` | string | OpenTelemetry Collector OTLP/HTTP base URL (default: `http://localhost:4318`) |
| `telemetry.otlp_bearer_token` | string | Bearer token sent as `Authorization: Bearer <token>` to the Collector, if it requires authentication (default: `""`, no header sent) |
| `telemetry.metric_export_interval_ms` | int | Positive metric-export interval in milliseconds (default: `5000`) |
| `telemetry.trace_sample_ratio` | number | Fraction of traces sampled, from `0.0` to `1.0` (default: `1.0`) |

## Example `config.json`

```json
{
    "model": "ministral-3:8b",
    "prompt_path": "prompts/kio1_system_prompt_ver1.txt",
    "chat_directory": "chats",
    "temperature": 0.1,
    "request_timeout": 120,
    "keep_alive": -1,
    "max_output_tokens": 4096,
    "endpoint": "http://localhost:11434",
    "context_window_size": 16384,
    "telemetry": {
        "enabled": false,
        "service_name": "kio1-orchestrator",
        "otlp_http_endpoint": "http://localhost:4318",
        "otlp_bearer_token": "",
        "metric_export_interval_ms": 5000,
        "trace_sample_ratio": 1.0
    }
}
```

## Keeping Model Loaded

Use `keep_alive` to reduce cold starts after launch.

- `-1`: keep model loaded indefinitely (until Ollama restart or resource eviction)
- `N > 0`: keep model loaded for `N` seconds between requests

## Changing the Model

To use a different Ollama model:

1. Pull the model: `ollama pull <model-name>`
2. Update `config.json`:
   ```json
   "model": "<model-name>"
   ```

The `KIO1_OLLAMA_ENDPOINT` environment variable overrides `endpoint` at runtime, which is how the Docker image points at the host's Ollama install without a second config file.

## Context Window

`context_window_size` sets the total token budget for a request — the system prompt, the whole conversation so far, and the generated response all share it. It maps to Ollama's `num_ctx` option. Because every turn re-sends the full transcript, this is what limits how long a conversation can run.

`max_output_tokens` caps the generated response and maps to Ollama's `num_predict`. It must be smaller than `context_window_size` to leave room for the prompt.

Ollama's own default is 4096 regardless of what the model supports (`ministral-3:8b` supports 262144), which is why an explicit value is required. Raising it costs RAM, since the cache is allocated when the model loads — 16384 comfortably fits roughly twenty turns.

Zero and negative values are not sentinels for "use the model maximum". Ollama clamps them to a roughly 4-token window and still returns HTTP 200, producing unrelated output from a prompt that was silently discarded, so they are rejected at startup instead.

## Telemetry

The `telemetry` object is optional. When it is omitted or `telemetry.enabled` is `false`, the application does not initialize OpenTelemetry exporters and can run without the local observability stack.

When enabled, `otlp_http_endpoint` is treated as the OTLP/HTTP base URL. The application automatically sends traces to `/v1/traces` and metrics to `/v1/metrics`.

`trace_sample_ratio` controls trace sampling:

- `1.0`: record every trace
- `0.5`: record approximately half of traces
- `0.0`: record no traces

Trace sampling does not disable metrics.

If the OTLP Collector requires authentication, set `telemetry.otlp_bearer_token` in `config.json`. When set, it is sent as `Authorization: Bearer <token>` on both the trace and metric exporters. When left empty (the default), requests are sent without an `Authorization` header; if the Collector requires one, exports fail with a timeout/retry error rather than a clear auth error — see [troubleshooting.md](troubleshooting.md).

`metric_export_interval_ms` controls how frequently metrics are sent. Smaller values update Prometheus more frequently but increase export activity.

For setup, querying, retention, and safe shutdown instructions, see the [Observability Guide](observability.md).

## Security

Do not put secrets such as `otlp_bearer_token` in source files or committed shell scripts; keep them only in `config.json`, which is gitignored.

## Custom System Prompts

Create a new file under `prompts/` and update `prompt_path` in `config.json`:

```json
"prompt_path": "prompts/my_custom_prompt.txt"
```

The prompt file is plain text. It is sent as the `system` role message to the model on every request.

