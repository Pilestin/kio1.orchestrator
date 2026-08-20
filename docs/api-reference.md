# API Reference

```
config_loader --> Config (dataclass)
               --> load_config()
               --> TelemetryConfig (dataclass)

provider_client --> ProviderModule (Protocol)
                --> load_provider()

prompt_loader --> load_prompt()

session_logger --> generate_session_id()
                --> init_logger()
                --> measure_duration()

ollama_client / openai_client / anthropic_client
                --> create_client(config)
                --> preload(config, client)
                --> send_request(config, client, system_prompt, messages)
                --> extract_content(response)

chat_history  --> create_chat_file(chat_directory, system_prompt, session_id)
              --> append_user_message()
              --> append_assistant_message()
              --> load_messages()
formatter     --> format_json()
telemetry      --> init_telemetry() / shutdown_telemetry()
               --> trace_operation() / trace_turn()
               --> trace_provider_preload() / trace_gen_ai_request()
               --> record_gen_ai_response()
               --> record_session_started() / record_session_completed()
               --> record_format_fallback() / record_workflow_plan()
```

## `core.config_loader`

### `Config` (dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `provider` | `str` | Provider module name |
| `allowed_providers` | `frozenset[str]` | Providers permitted to load |
| `model` | `str` | Model identifier |
| `prompt_path` | `str` | Path to system prompt file |
| `chat_directory` | `str` | Chat history directory |
| `temperature` | `float` | Sampling temperature |
| `request_timeout` | `float` | HTTP timeout in seconds |
| `keep_alive` | `int` | Ollama residency control (`-1` keeps loaded) |
| `max_output_tokens` | `int` | Maximum generated output tokens |
| `telemetry` | `TelemetryConfig` | OpenTelemetry export configuration |
| `provider_options` | `dict[str, Any]` | Provider-specific configuration |

### `TelemetryConfig` (dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | `bool` | Enables trace and metric export |
| `service_name` | `str` | Service name attached to exported telemetry |
| `otlp_http_endpoint` | `str` | OTLP/HTTP Collector base URL |
| `metric_export_interval_ms` | `int` | Metric-export interval in milliseconds |
| `trace_sample_ratio` | `float` | Trace-sampling ratio from `0.0` to `1.0` |

### `load_config(config_path="config.json") -> Config`

Loads a `Config` from a JSON file. Validates provider selection, provider options, generation limits, and optional telemetry settings.

Raises `FileNotFoundError` if the file does not exist. Raises `KeyError` if a required field is missing. Raises `ValueError` when a provider, provider option, generation limit, or telemetry setting is invalid.

## `providers.provider_client`

### `load_provider(provider_name, allowed_providers) -> ProviderModule`

Dynamically imports `<provider_name>_client.py` and validates it implements `create_client`, `preload`, `send_request`, and `extract_content`.

Raises `ValueError` if `provider_name` is not in `allowed_providers` or is not a valid identifier. Raises `TypeError` if the imported module is missing a required function.

## `core.prompt_loader`

### `load_prompt(prompt_path) -> str`

Reads and returns the stripped text content of a prompt file.

Raises `FileNotFoundError` if the file does not exist.

## `core.session_logger`

### `generate_session_id() -> str`

Returns a `<YYYYMMDD>_<HHMMSS>_<8-char-uuid>` id, generated once per run and shared by the session's log file and chat file so the two can be correlated.

### `init_logger(log_directory, session_id) -> logging.Logger`

Records are formatted as UTC ISO 8601 with milliseconds, followed by the session id, level, module, and message. Creates `log_directory` if absent, clears any pre-existing root handlers so repeated calls do not duplicate output, and raises `httpx`, `httpcore`, `openai`, and `anthropic` to `WARNING` to keep third-party request logging out of the session file.

| Handler | Destination | Level |
|---------|-------------|-------|
| `FileHandler` | `logs/log_<session_id>.log` | `DEBUG` |
| `StreamHandler` | stderr | `ERROR` |

### `measure_duration() -> Iterator[Callable[[], int]]`

Context manager measuring wall-clock time with `time.perf_counter()`. Yields a callable returning elapsed whole milliseconds:

```python
with measure_duration() as elapsed:
    response = client.messages.create(**request_kwargs)

logger.info("Response received: duration_ms=%d", elapsed())
```

## Provider modules (`providers.ollama_client`, `providers.openai_client`, `providers.anthropic_client`)

Each provider module implements the same four functions:

### `create_client(config) -> Any`

Creates and returns the provider's SDK client (`None` for Ollama, which uses direct HTTP requests).

### `preload(config, client) -> None`

Performs provider-specific startup work. Ollama loads the model into memory with the configured context window; OpenAI and Anthropic verify the configured model exists via `client.models.retrieve(config.model)`. Each preload operation logs its duration separately from request timing.

### `send_request(config, client, system_prompt, messages) -> Any`

Sends a chat request to the provider and returns the raw response. Raises `ValueError` when the provider reports that generation stopped at a configured token limit.


### `extract_content(response) -> str`

Extracts the assistant's text content from a provider response.


## `core.chat_history`

### `create_chat_file(chat_directory, system_prompt, session_id=None) -> Path`

Creates a JSONL chat file with the system prompt as the first line.
Filename format: `chat_<session_id>.jsonl`

`session_id` should be the id from `generate_session_id()`, pairing the chat file with the session's log file. When omitted, a fresh id is generated internally.

### `append_user_message(chat_file, message) -> None`

Appends a user message as a JSONL line.

### `append_assistant_message(chat_file, message) -> None`

Appends an assistant message as a JSONL line.

### `load_messages(chat_file) -> list[dict]`

Returns all messages from the chat file, excluding the system prompt.

## `core.formatter`

### `format_json(raw_json) -> str`

Parses and pretty-prints a JSON string with 2-space indentation. Falls back to `ast.literal_eval` for single-quoted Python dict output from the model. When telemetry is enabled, the function records response-formatting duration, fallback parsing, workflow execution mode, and workflow step count without recording response content.

## `core.telemetry`

### `init_telemetry(config) -> None`

Initializes OpenTelemetry trace and metric providers when telemetry is enabled. Configures OTLP/HTTP exporters, trace sampling, batching, periodic metric export, and the `service.name` resource attribute.

### `shutdown_telemetry() -> None`

Flushes pending metrics and spans and shuts down the configured telemetry providers.

### `trace_operation(name, attributes=None, *, kind=SpanKind.INTERNAL) -> Iterator[Span]`

Creates a span context manager. Errors are recorded using only their type; exception messages and stack traces are not exported.

### `trace_turn(*, session_id, turn_number, provider, model) -> Iterator[Span]`

Traces one complete user turn and records turn count, duration, status, provider, model, and error type.

### `trace_provider_preload(*, provider, model) -> Iterator[Span]`

Traces provider preload or model-validation work and records its count, duration, and status.

### `trace_gen_ai_request(...) -> Iterator[Span]`

Creates a generative-AI client span and records request duration, provider, model, conversation ID, turn number, message count, output-token limit, temperature, and streaming mode.

### `record_gen_ai_response(...) -> None`

Records normalized provider-response metadata, including response model, response ID, finish reason, input tokens, output tokens, and truncation status. Response content is never recorded.

### `record_session_started(*, provider, model) -> None`

Increments the session-started counter.

### `record_session_completed(*, provider, model, success) -> None`

Increments the session-completed counter with a success or error status.

### `record_format_fallback() -> None`

Records use of the Python-literal response-format fallback and adds a formatting event to the active span.

### `record_workflow_plan(*, workflow_id, execution_mode, step_count) -> None`

Records successfully parsed workflow-plan count, execution mode, step count, and bounded workflow ID metadata.
