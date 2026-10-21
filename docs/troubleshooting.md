# Troubleshooting

## Provider module not found

If the configuration contains:

```json
"provider": "example"
```

the project must contain:

```text
example_client.py
```

## Ollama endpoint missing

Ollama requires:

```json
"provider_options": {
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

## OpenAI authentication error

Confirm the key is set:

```bash
python3 -c 'import os; print("set" if os.getenv("OPENAI_API_KEY") else "missing")'
```

## Anthropic authentication error

Confirm the key is set:

```bash
python3 -c 'import os; print("set" if os.getenv("ANTHROPIC_API_KEY") else "missing")'
```

## Model not found

Confirm that the configured model ID exists and is available to the selected provider account.

## Invalid JSON response

The system prompt asks providers to return JSON only. The formatter also removes optional Markdown JSON fences before parsing provider output.

## Response truncated

A request failing with `Response truncated before completion` reached either `max_output_tokens` or `provider_options.context_window_size`. Since each turn re-sends the whole transcript, long conversations eventually consume the available context.

Increase `max_output_tokens` if the output cap was reached. Otherwise, raise `context_window_size` or start a new session. The error reports both limits and the actual token counts.

## A step failed because a prerequisite had not run

A reply such as

```
no trace to replay: set data.trace_ref to a ref returned by a previous
'bug_localization' or 'diagnosis' step
```

means the agent was asked to do something that consumes what an earlier step
produces, and there was no earlier step. The agent is right; the **plan** was
wrong.

KIO1 does carry a step's output into the steps that declare it — they arrive
as `data.upstream`, keyed by step id — so a correctly ordered plan works
without anything else being configured. What went wrong is upstream of that:
the model put the consuming step first.

Look at the run's steps in the console. One step, or a first step whose
capability is the consuming one, is the signature.

Two things fix it, in order of how often they are the answer:

1. **Use a larger planning model.** The ordering rules are in the system
   prompt, and a small model simply does not follow them: a 3B model asked to
   extract a trace answers with a single `replay` step, which is exactly this
   error. Change it on the console's **Settings** tab and run again.
2. **Ask for the work, not the artifact.** "Localize the failure in `x.py`"
   names a capability the catalog lists, and the step that produces a trace is
   the one that gets chosen. "Get me the trace XML" describes an output, and
   the nearest-sounding capability is the one that *reads* traces.

## An agent cannot find the code

A reply such as `target script not found: E:/projects/app/main.py` when that
file plainly exists means the path was resolved somewhere else.

`dispatch.workspace.path` is **the path as the agent sees it**, not as KIO1
sees it. An agent running in its own container has its own filesystem: a
Windows path, or any path on the orchestrator's host, is not there.

Two things have to line up:

- The code must be mounted into the agent's container. That is configured on
  the agent's side — a `volumes:` entry in its compose file, for instance:

  ```yaml
  services:
    kio2:
      volumes:
        - E:/projects/app:/work:ro
  ```

- `dispatch.workspace.path` must be the path **inside** that container
  (`/work` above), with `entry_point` relative to it. Set both on the
  console's **Settings** tab.

To check what an agent can actually see:

```bash
docker inspect <container> --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{\"
\"}}{{end}}"
```

Nothing printed means nothing is mounted, and no workspace path will work
until one is.

## A run failed with "timed out"

That is the *planning* call timing out, not an agent. It is bounded by
`request_timeout`, which the console's **Settings** tab exposes.

A local model has to be read into memory before it can answer, and the first
request after choosing one pays for that read. Changing the model from the
console warms it in the background for this reason, but a large model on a
cold machine can still exceed a short timeout. Raise `request_timeout` when
you move to a larger model.

## Reading the logs

Each run writes `logs/log_<session_id>.log`, where `session_id` is `<YYYYMMDD>_<HHMMSS>_<8-char-uuid>`. The same id names that session's `chats/chat_<session_id>.jsonl`, so the transcript and the log can be read side by side.

Each line is UTC ISO 8601 with milliseconds, then the session id, level, module, and message:

```
2026-07-24T09:19:43.465Z 20260724_101943_ff849b40 INFO main Session started: session_id=20260724_101943_ff849b40
2026-07-24T09:19:43.466Z 20260724_101943_ff849b40 INFO config_loader Config loaded: provider=ollama model=ministral-3:8b prompt_path=prompts/kio1_system_prompt_ver1.txt
2026-07-24T09:19:43.712Z 20260724_101943_ff849b40 INFO ollama_client Model preloaded: model=ministral-3:8b duration_ms=199
2026-07-24T09:19:56.220Z 20260724_101943_ff849b40 INFO main Turn started: turn=1
2026-07-24T09:21:15.523Z 20260724_101943_ff849b40 INFO ollama_client Response received: model=ministral-3:8b duration_ms=79303 input_tokens=2280 output_tokens=550
2026-07-24T09:21:20.108Z 20260724_101943_ff849b40 INFO main Session ended: turns=1
```

Timestamps are UTC, matching the `created_at` values inside provider responses. The console shows only `ERROR`; everything else lives in the file.

| Level | Contents |
|-------|----------|
| `DEBUG` | Full request payloads and model responses, including message text |
| `INFO` | Session and turn markers, config, provider loaded, model preloaded/verified, per-request duration and token counts |
| `WARNING` | Recoverable issues, such as the formatter falling back to `ast.literal_eval` |
| `ERROR` | Startup failures and failed requests, with tracebacks |

Useful greps:

```bash
LOG=logs/$(ls -t logs/ | head -1)

grep "Response received:" $LOG   # duration and tokens per turn
grep -c "Turn started:" $LOG     # turns attempted this session
tail -1 $LOG                     # "Session ended: turns=N" if it exited cleanly
grep ERROR logs/*.log            # failures across all sessions
```

A log containing `Startup failed` with no `Session ended` line never reached the prompt. A log with neither was killed mid-session.

`input_tokens` grows every turn because the whole transcript is re-sent, while `output_tokens` stays roughly flat — the two diverging shows the cost of replaying conversation history.

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

Run at least one KIO1 request and type `exit` so pending telemetry is flushed.

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

### Exporter errors appear in the terminal while typing

They should not. OpenTelemetry export failures are filtered out of the console handler and written only to `logs/log_<session_id>.log`, so they cannot corrupt the line being typed at the prompt. If they still reach the terminal, the run predates that filter, or a library outside the `opentelemetry` logger namespace is failing.

A silent absence of telemetry is the expected trade-off: check the session log for `Failed to export`, and confirm `telemetry.otlp_http_endpoint` points at a reachable Collector.
