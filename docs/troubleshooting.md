# Troubleshooting

## Ollama endpoint missing

`config.json` requires:

```json
{
    "endpoint": "http://localhost:11434",
    "context_window_size": 16384
}
```

## Ollama connection refused

Ollama is separate from the observability stack. Start it with:

```bash
ollama serve
```

Then confirm availability:

```bash
ollama list
```

## Model not found

Confirm that the configured model ID exists and has been pulled with `ollama pull <model-name>`.

## Invalid JSON response

The system prompt asks the model to return JSON only. The formatter also removes optional Markdown JSON fences before parsing the model's output.

## Response truncated

A request failing with `Response truncated before completion` reached either `max_output_tokens` or `context_window_size`. Since each turn re-sends the whole transcript, long conversations eventually consume the available context.

Increase `max_output_tokens` if the output cap was reached. Otherwise, raise `context_window_size` or start a new session. The error reports both limits and the actual token counts.

## Reading the logs

Each run writes `logs/log_<session_id>.log`, where `session_id` is `<YYYYMMDD>_<HHMMSS>_<8-char-uuid>`. The same id names that session's `chats/chat_<session_id>.jsonl`, so the transcript and the log can be read side by side.

Every stage of the pipeline logs its own `duration_ms`, so a single turn's time budget (history load, model request, response formatting, history persistence) can be broken down line by line, in addition to the turn and session totals.

Each line is UTC ISO 8601 with milliseconds, then the session id, level, module, and message:

```
2026-07-24T09:19:43.465Z 20260724_101943_ff849b40 INFO main Session started: session_id=20260724_101943_ff849b40
2026-07-24T09:19:43.466Z 20260724_101943_ff849b40 INFO config_loader Config loaded: model=ministral-3:8b prompt_path=prompts/kio1_system_prompt_ver1.txt endpoint=http://localhost:11434 duration_ms=1
2026-07-24T09:19:43.470Z 20260724_101943_ff849b40 INFO prompt_loader Prompt loaded: path=prompts/kio1_system_prompt_ver1.txt duration_ms=0 chars=2184
2026-07-24T09:19:43.712Z 20260724_101943_ff849b40 INFO main Model preloaded: model=ministral-3:8b duration_ms=199
2026-07-24T09:19:56.220Z 20260724_101943_ff849b40 INFO main Turn started: turn=1
2026-07-24T09:19:56.221Z 20260724_101943_ff849b40 DEBUG main History loaded: turn=1 duration_ms=1 message_count=1
2026-07-24T09:21:15.523Z 20260724_101943_ff849b40 INFO main Response received: model=ministral-3:8b duration_ms=79303 input_tokens=2280 output_tokens=550 tokens_per_second=6.9
2026-07-24T09:21:15.531Z 20260724_101943_ff849b40 DEBUG main Response formatted: turn=1 duration_ms=1
2026-07-24T09:21:15.534Z 20260724_101943_ff849b40 DEBUG main History persisted: turn=1 duration_ms=2
2026-07-24T09:21:15.535Z 20260724_101943_ff849b40 INFO main Turn completed: turn=1 duration_ms=79305
2026-07-24T09:21:20.108Z 20260724_101943_ff849b40 INFO main Session ended: turns=1 duration_ms=83686
```

Timestamps are UTC, matching the `created_at` values inside Ollama's responses. The console shows only `ERROR`; everything else lives in the file.

| Level | Contents |
|-------|----------|
| `DEBUG` | Full request payloads and model responses (including message text), and per-stage durations: history load, response formatting, history persistence |
| `INFO` | Session and turn markers (with total durations), config and prompt load duration, model preload duration, per-request duration, token counts and throughput |
| `WARNING` | Recoverable issues, such as the formatter falling back to `ast.literal_eval` |
| `ERROR` | Startup failures and failed requests (with `duration_ms` up to the point of failure), with tracebacks |

Useful greps:

```bash
LOG=logs/$(ls -t logs/ | head -1)

grep "Response received:" $LOG   # duration, tokens and throughput per turn
grep "Turn completed:\|Turn failed:" $LOG  # total time spent per turn
grep -c "Turn started:" $LOG     # turns attempted this session
tail -1 $LOG                     # "Session ended: duration_ms=N" if the server shut down cleanly
grep ERROR logs/*.log            # failures across all sessions
```

A log containing `Startup failed` with no further entries never reached the web server. A log with neither `Startup failed` nor `Session ended` was killed mid-session.

`input_tokens` grows every turn because the whole transcript is re-sent, while `output_tokens` stays roughly flat — the two diverging shows the cost of replaying conversation history. `tokens_per_second` (output tokens divided by request duration) is the clearest single number for comparing model/hardware throughput across turns and machines.

Compare `Model preloaded:` against `Response received:` durations to separate startup cost from request cost. With `keep_alive: -1` the model load is paid once at startup rather than per request.

## Observability

> KIO1 now sends telemetry to the shared, remote AI4SWENG observability platform instead of a local Docker Compose stack (see [observability.md](observability.md)). The subsections below that reference `docker compose -f observability/compose.telemetry.yaml` describe the previous local setup and are pending a follow-up revision.

### Telemetry export timeouts

Messages such as:

```text
Failed to export span batch
Failed to export metrics batch
```

mean the application cannot reach the configured OpenTelemetry Collector, or the Collector rejected the request.

If the Collector requires bearer-token authentication (an `OTLP_BEARER_TOKEN` set on the Collector side, e.g. via its `docker-compose` environment), KIO1 must send the same token back, or every export attempt fails with this same timeout/retry message — the OTLP/HTTP exporter does not surface `401`/`403` responses distinctly from network failures. Set `telemetry.otlp_bearer_token` in `config.json` to match the Collector's configured token:

```json
"telemetry": {
    "otlp_bearer_token": "local-dev-otlp-token"
}
```

Confirm that the observability stack is running:

```bash
docker compose -f observability/compose.telemetry.yaml ps -a
```

Check Collector readiness:

```bash
curl -f http://127.0.0.1:13133/
```

The default application endpoint is:

```text
http://localhost:4318
```

Start the observability stack before starting KIO1 when telemetry is enabled:

```bash
docker compose -f observability/compose.telemetry.yaml up -d
```

Telemetry-export failures do not prevent provider requests from completing. However, telemetry that cannot reach the Collector may eventually be discarded after the application exporter exhausts its retries or shuts down.

### Collector missing from `docker compose ps`

The normal `docker compose ps` output only shows running containers. Include stopped containers with:

```bash
docker compose -f observability/compose.telemetry.yaml ps -a
```

Inspect the Collector logs:

```bash
docker compose -f observability/compose.telemetry.yaml logs otel-collector
```

The storage initializer should show:

```text
Exited (0)
```

This is expected. The OpenTelemetry Collector itself should show `Up`.

### Collector storage permission denied

An error similar to:

```text
mkdir /var/lib/otelcol/storage: permission denied
```

means the Collector persistent volume does not have the required ownership.

The `collector-storage-init` service normally fixes the ownership automatically. Run it again explicitly:

```bash
docker compose -f observability/compose.telemetry.yaml run --rm --no-deps \
  collector-storage-init
```

Then start the stack and inspect all container states:

```bash
docker compose -f observability/compose.telemetry.yaml up -d
docker compose -f observability/compose.telemetry.yaml ps -a
```

Do not solve this by deleting the named volume because doing so would remove queued telemetry.

### Empty Prometheus query result

A plain query such as:

```promql
kio1_turns_total
```

can return no data when the application has stopped or telemetry has been disabled. Prometheus instant queries only return recently active series.

Use a historical query:

```promql
last_over_time(kio1_turns_total[24h])
```

Confirm that KIO1 metric names exist:

```bash
curl -sS -G http://127.0.0.1:9090/api/v1/label/__name__/values \
  --data-urlencode 'match[]={__name__=~"kio1_.*|gen_ai_.*"}' \
  | python3 -m json.tool
```

If this returns metric names, the data is stored and the original query was outside the active-series time window.

### No traces returned by Tempo

Confirm that telemetry was enabled before running KIO1:

```json
"enabled": true
```

Check Collector and Tempo readiness:

```bash
curl -f http://127.0.0.1:13133/
curl -f http://127.0.0.1:3200/ready
```

Run at least one KIO1 request and stop the server with `Ctrl+C` so pending telemetry is flushed.

Search again:

```bash
curl -sS -G http://127.0.0.1:3200/api/search \
  --data-urlencode 'q={ resource.service.name = "kio1-orchestrator" }' \
  | python3 -m json.tool
```

Ensure the searched service name matches `telemetry.service_name` in `config.json`.

### Observability service fails during startup

Inspect the affected service:

```bash
docker compose -f observability/compose.telemetry.yaml ps -a
```

Then read its logs:

```bash
docker compose -f observability/compose.telemetry.yaml logs SERVICE_NAME
```

Replace `SERVICE_NAME` with one of:

```text
otel-collector
tempo
prometheus
```

Validate all configuration files using the commands in the [Observability Guide](observability.md#validating-the-configuration).

### Stored telemetry was removed

The following command deletes the named observability volumes:

```bash
docker compose -f observability/compose.telemetry.yaml down -v
```

Data deleted this way cannot be recovered unless the volumes were backed up separately.

The safe shutdown command is:

```bash
docker compose -f observability/compose.telemetry.yaml stop
```
