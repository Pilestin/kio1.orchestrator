# API Reference

```
config_loader --> Config (dataclass)
               --> load_config()
               --> TelemetryConfig (dataclass)

prompt_loader --> load_prompt()

session_logger --> generate_session_id()
                --> init_logger()
                --> measure_duration()

main          --> preload_model(config)
              --> send_request(config, system_prompt, messages)
              --> extract_content(response)
              --> run_turn(config, system_prompt, chat_file, session_id, turn, query)

webapp        --> create_app(config, system_prompt, chat_file, session_id)

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

## `config_loader`

### `Config` (dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `model` | `str` | Ollama model identifier |
| `prompt_path` | `str` | Path to system prompt file |
| `chat_directory` | `str` | Chat history directory |
| `temperature` | `float` | Sampling temperature |
| `request_timeout` | `float` | HTTP timeout in seconds |
| `keep_alive` | `int` | Ollama residency control (`-1` keeps loaded) |
| `max_output_tokens` | `int` | Maximum generated output tokens |
| `endpoint` | `str` | Ollama base URL |
| `context_window_size` | `int` | Ollama context window in tokens; must be larger than `max_output_tokens` |
| `telemetry` | `TelemetryConfig` | OpenTelemetry export configuration |

### `TelemetryConfig` (dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | `bool` | Enables trace and metric export |
| `service_name` | `str` | Service name attached to exported telemetry |
| `otlp_http_endpoint` | `str` | OTLP/HTTP Collector base URL |
| `metric_export_interval_ms` | `int` | Metric-export interval in milliseconds |
| `trace_sample_ratio` | `float` | Trace-sampling ratio from `0.0` to `1.0` |

### `load_config(config_path="config.json") -> Config`

Loads a `Config` from a JSON file. Validates the Ollama endpoint, context window, generation limits, and optional telemetry settings. Logs the load duration alongside the loaded model, prompt path, and endpoint.

Raises `FileNotFoundError` if the file does not exist. Raises `KeyError` if a required field is missing. Raises `ValueError` when the endpoint, context window, generation limit, or telemetry setting is invalid.

## `prompt_loader`

### `load_prompt(prompt_path) -> str`

Reads and returns the stripped text content of a prompt file. Logs the load duration and character count.

Raises `FileNotFoundError` if the file does not exist.

## `session_logger`

### `generate_session_id() -> str`

Returns a `<YYYYMMDD>_<HHMMSS>_<8-char-uuid>` id, generated once per run and shared by the session's log file and chat file so the two can be correlated.

### `init_logger(log_directory, session_id) -> logging.Logger`

Records are formatted as UTC ISO 8601 with milliseconds, followed by the session id, level, module, and message. Creates `log_directory` if absent and clears any pre-existing root handlers so repeated calls do not duplicate output.

| Handler | Destination | Level |
|---------|-------------|-------|
| `FileHandler` | `logs/log_<session_id>.log` | `DEBUG` |
| `StreamHandler` | stderr | `ERROR` |

### `measure_duration() -> Iterator[Callable[[], int]]`

Context manager measuring wall-clock time with `time.perf_counter()`. Yields a callable returning elapsed whole milliseconds:

```python
with measure_duration() as elapsed:
    response_data = json.loads(response.read().decode("utf-8"))

logger.info("Response received: duration_ms=%d", elapsed())
```

## `main`

### `preload_model(config) -> None`

Loads the configured model into Ollama's memory using the configured context window and `keep_alive`. Logs its duration separately from request timing.

### `send_request(config, system_prompt, messages) -> dict[str, Any]`

Sends a chat request to the Ollama `/api/chat` endpoint and returns the parsed JSON response. Logs request duration, input/output token counts, and output tokens per second (`n/a` when Ollama omits `eval_count`). Raises `ValueError` when Ollama reports that generation stopped at a configured token limit.

### `extract_content(response) -> str`

Extracts the assistant's text content from an Ollama response.

### `run_turn(config, system_prompt, chat_file, session_id, turn, query) -> str`

Runs one chat turn: loads prior messages, sends the conversation plus `query` to Ollama inside a turn trace, and appends the user/assistant exchange to the chat file. Returns the raw (unformatted) assistant content; used by `webapp.create_app`'s `/api/chat` route.

### `main() -> None`

Loads configuration, preloads the model, creates the session's chat file, then builds and starts the Flask app from `webapp.create_app`. Binds to `KIO1_WEB_HOST`/`KIO1_WEB_PORT` (default `127.0.0.1:5000`). The session's total duration is logged on shutdown (`Session ended: duration_ms=N`).

## `webapp`

### `create_app(config, system_prompt, chat_file, session_id) -> Flask`

Builds the Flask application serving the chat UI. Routes:

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serves the chat UI page |
| `/api/chat` | POST | Accepts `{"query": str}`, runs one turn via `main.run_turn`, and returns `{"formatted": str}` on success, `{"raw": str, "error": str}` when the model's reply isn't valid JSON, or `{"error": str}` with a 4xx/5xx status on a bad request or upstream failure |
| `/api/upload` | POST | Accepts a multipart `file` field, sanitizes the filename with `werkzeug.utils.secure_filename`, prefixes it with a short id to avoid collisions, and saves it under `assets/`. Returns `{"filename": str}` |

Turns are serialized with a lock so chat-file writes and turn numbering stay consistent across concurrent requests; the web UI serves one operator at a time, mirroring the single-session REPL it replaces.

## `chat_history`

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

## `formatter`

### `format_json(raw_json) -> str`

Parses and pretty-prints a JSON string with 2-space indentation. Falls back to `ast.literal_eval` for single-quoted Python dict output from the model. When telemetry is enabled, the function records response-formatting duration, fallback parsing, and (when the parsed response carries `workflow_id`/`execution_mode`/`steps`) workflow execution mode and step count, without recording response content.

## `telemetry`

### `init_telemetry(config) -> None`

Initializes OpenTelemetry trace and metric providers when telemetry is enabled. Configures OTLP/HTTP exporters, trace sampling, batching, periodic metric export, and the `service.name` resource attribute.

### `shutdown_telemetry() -> None`

Flushes pending metrics and spans and shuts down the configured telemetry providers.

### `trace_operation(name, attributes=None, *, kind=SpanKind.INTERNAL) -> Iterator[Span]`

Creates a span context manager. Errors are recorded using only their type; exception messages and stack traces are not exported.

### `trace_turn(*, session_id, turn_number, provider, model) -> Iterator[Span]`

Traces one complete user turn and records turn count, duration, status, provider, model, and error type.

### `trace_provider_preload(*, provider, model) -> Iterator[Span]`

Traces model preload work and records its count, duration, and status.

### `trace_gen_ai_request(...) -> Iterator[Span]`

Creates a generative-AI client span and records request duration, provider, model, conversation ID, turn number, message count, output-token limit, temperature, and streaming mode.

### `record_gen_ai_response(...) -> None`

Records normalized response metadata, including response model, response ID, finish reason, input tokens, output tokens, and truncation status. Response content is never recorded.

### `record_session_started(*, provider, model) -> None`

Increments the session-started counter.

### `record_session_completed(*, provider, model, success) -> None`

Increments the session-completed counter with a success or error status.

### `record_format_fallback() -> None`

Records use of the Python-literal response-format fallback and adds a formatting event to the active span.

### `record_workflow_plan(*, workflow_id, execution_mode, step_count) -> None`

Records the shape of a response that happens to carry `workflow_id`/`execution_mode`/`steps`: count, execution mode, step count, and bounded workflow ID metadata.

