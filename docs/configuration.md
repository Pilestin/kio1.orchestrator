# Configuration

All application settings are stored in `config.json` at the project root.

The repository ships `config.example.json`, not `config.json`. The first run —
`start.py`, `main.py`, or `uvicorn app:app` — copies the example into place,
and from then on `config.json` is yours: it holds your workspace path, your
agents, and the console's edits, and git ignores it.

## Configuration File Reference

| Field | Type | Description |
|-------|------|-------------|
| `provider` | string | Provider module name: `ollama`, `openrouter`, `openai`, or `anthropic`. Can be switched on the console's Settings tab |
| `allowed_providers` | array of strings | Providers permitted to load; `provider` must be one of these |
| `model` | string | Model identifier used by the selected provider |
| `prompt_path` | string | Path to the system prompt file (relative to project root) |
| `chat_directory` | string | Directory for chat history files |
| `temperature` | float | Sampling temperature (lower = more deterministic) |
| `request_timeout` | float | How long to wait for the model to produce a plan, in seconds. On a machine without a GPU a small local model takes 30–90 s and a 4B reasoning model several minutes, so the example uses `300` |
| `keep_alive` | int | Ollama model residency in seconds (`-1` = keep loaded; default: `-1`) |
| `max_output_tokens` | int | Maximum number of generated output tokens; must be positive (default: `4096`) |
| `provider_options.endpoint` | string | Ollama base URL (required for Ollama) |
| `provider_options.context_window_size` | int | Ollama context window in tokens (required for Ollama); must be larger than `max_output_tokens` |
| `provider_options.think` | boolean | Lets an Ollama reasoning model (such as `qwen3.5`) think before answering. Off by default: thinking costs minutes on a CPU and adds little to a plan. Models that do not reason ignore it |
| `telemetry` | object | Optional OpenTelemetry configuration; telemetry is disabled when omitted |
| `telemetry.enabled` | boolean | Enables trace and metric export (default: `false`) |
| `telemetry.service_name` | string | Non-empty service name attached to exported telemetry (default: `kio1-orchestrator`) |
| `telemetry.otlp_http_endpoint` | string | OpenTelemetry Collector OTLP/HTTP base URL (default: `http://localhost:4318`) |
| `telemetry.otlp_bearer_token` | string | Bearer token sent as `Authorization: Bearer <token>` to the Collector, if it requires authentication (default: `""`, no header sent) |
| `telemetry.metric_export_interval_ms` | int | Positive metric-export interval in milliseconds (default: `5000`) |
| `telemetry.trace_sample_ratio` | number | Fraction of traces sampled, from `0.0` to `1.0` (default: `1.0`) |
| `dispatch` | object | Optional workflow dispatch configuration; dispatch is disabled when omitted |
| `dispatch.enabled` | boolean | Sends generated plans to KIO agents (default: `false`) |
| `dispatch.request_timeout` | number | Positive per-agent reply timeout in seconds (default: `60`) |
| `dispatch.max_parallel_steps` | int | Positive bound on concurrent dispatches in `parallel` mode (default: `4`) |
| `dispatch.transport` | string | How execution messages travel: `http` (default) or `nats` |
| `dispatch.selection` | object | Whether recent outcomes reorder the candidates for a step; see [Choosing by history](selection.md) |
| `dispatch.failover` | object | Whether a step may run on a target other than the one the plan named; see [Failover](failover.md) |
| `dispatch.health` | object | How often targets are probed and refreshed, and when an unreachable one is withdrawn; see [The live directory](live-directory.md) |
| `dispatch.discovery` | object | Manifest URLs agents describe themselves through; see [Discovery](discovery.md) |
| `dispatch.policy` | object | Gates saying what must have passed before a step runs; see [Policy gates](policy-gates.md) |
| `dispatch.agents` | object | Everything KIO1 can dispatch to, keyed by the id plans use. Each entry has a `type`: `kio` (default, the KIO1-KIO10 protocol), `http` (any REST API), `cli` (a local command), or `mcp` (a Model Context Protocol server, which reports its own tools). See [Workflow Dispatch](workflow-dispatch.md), [Connecting your own module](connecting-a-tool.md), and [Connecting an MCP server](mcp.md) |
| `orchestration.review` | boolean | Whether the model reads the results and writes an answer, planning again for missing work (default: `true`). See [Reviewing a run](review.md) |
| `orchestration.max_rounds` | int | Plans per run, the first included, from 1 to 10 (default: `2`) |
| `orchestration.review_prompt_path` | string | The reviewer's system prompt (default: `prompts/kio1_review_prompt.txt`) |
| `orchestration.approval` | string | `auto` (ask when a plan could reach a target marked `approval`), `always`, or `never` (default: `auto`). See [Approving a plan](approval.md) |
| `orchestration.approval_timeout` | number | Seconds a plan waits for approval before it is cancelled (default: `1800`) |
| `dispatch.agents.<id>.approval` | boolean | Plans that could reach this target wait for a person to approve them (default: `false`) |
| `dispatch.agents.<id>.enabled` | boolean | Whether the planner may use this target (default: `true`). A disabled target keeps its definition and history and its row in the console, but is neither probed nor offered — the console's **Disable** button sets this |
| `dispatch.workspace.path` | string | Repository root the agents should analyse, as *they* can see it (default: `""`, not sent) |
| `dispatch.workspace.entry_point` | string | Runnable script reproducing the behaviour, relative to `workspace.path` (default: `""`, not sent) |
| `dispatch.nats.servers` | array of strings | NATS server URLs; required when `transport` is `nats` |
| `dispatch.nats.subject_template` | string | Subject pattern for agents that do not name one; must contain `{agent}` (default: `kio.{agent}.execute`) |

## Example `config.json`

```json
{
    "provider": "ollama",
    "allowed_providers": ["ollama", "openai", "anthropic"],
    "model": "ministral-3:8b",
    "prompt_path": "prompts/kio1_system_prompt_ver1.txt",
    "chat_directory": "chats",
    "temperature": 0.1,
    "request_timeout": 120,
    "keep_alive": -1,
    "max_output_tokens": 4096,
    "telemetry": {
        "enabled": false,
        "service_name": "kio1-orchestrator",
        "otlp_http_endpoint": "http://localhost:4318",
        "otlp_bearer_token": "",
        "metric_export_interval_ms": 5000,
        "trace_sample_ratio": 1.0
    },
    "provider_options": {
        "endpoint": "http://localhost:11434",
        "context_window_size": 16384
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

## API keys and `.env`

Keys are read from the environment, and from a `.env` file at the project
root, which git ignores; `.env.example` lists the names. A variable already
set in the environment wins over the file. The console's Settings tab writes
a key there for you after checking that it works, so the steps below are only
needed when configuring by hand.

## OpenRouter

One key, several hundred models from many vendors. Create a key at
<https://openrouter.ai/keys>, then either pick **OpenRouter** on the
console's Settings tab and paste it, or put it in `.env`:

```bash
OPENROUTER_API_KEY=sk-or-...
```

```json
{
    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-4.5",
    "provider_options": {"json_mode": true}
}
```

| Option | Default | Meaning |
|--------|---------|---------|
| `provider_options.json_mode` | `true` | Ask the API to constrain the answer to JSON. Turn off for a model that rejects the option; the system prompt already demands JSON |
| `provider_options.openrouter_endpoint` | `https://openrouter.ai/api/v1` | API base URL. Separate from `endpoint`, which is Ollama's and stays in the file after a switch |

The model list is fetched from OpenRouter without a key, so it can be browsed
before one is set. Answers are streamed into the console's live view.

## OpenAI

Set the OpenAI API key in the terminal:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY
echo
export OPENAI_API_KEY
```

Example configuration:

```json
{
    "provider": "openai",
    "allowed_providers": ["ollama", "openai", "anthropic"],
    "model": "gpt-4o-mini",
    "prompt_path": "prompts/kio1_system_prompt_ver1.txt",
    "chat_directory": "chats",
    "temperature": 0.1,
    "request_timeout": 120,
    "max_output_tokens": 2048
}
```

The OpenAI provider uses the Chat Completions API and requests JSON output.

## Anthropic

Set the Anthropic API key in the terminal:

```bash
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY
echo
export ANTHROPIC_API_KEY
```

Example configuration:

```json
{
    "provider": "anthropic",
    "allowed_providers": ["ollama", "openai", "anthropic"],
    "model": "claude-haiku-4-5",
    "prompt_path": "prompts/kio1_system_prompt_ver1.txt",
    "chat_directory": "chats",
    "temperature": 0.1,
    "request_timeout": 120,
    "max_output_tokens": 2048
}
```

The formatter accepts both plain JSON and JSON wrapped in a Markdown code fence.

## Context Window

`provider_options.context_window_size` sets the total token budget for a request — the system prompt, the whole conversation so far, and the generated response all share it. It maps to Ollama's `num_ctx` option. Because every turn re-sends the full transcript, this is what limits how long a conversation can run.

`max_output_tokens` caps the generated response. It maps to Ollama's `num_predict`, OpenAI's `max_completion_tokens`, and Anthropic's `max_tokens`. For Ollama, it must be smaller than `context_window_size` to leave room for the prompt.

Ollama's own default is 4096 regardless of what the model supports (`ministral-3:8b` supports 262144), which is why an explicit value is required. Raising it costs RAM, since the cache is allocated when the model loads — 16384 comfortably fits roughly twenty turns.

Zero and negative values are not sentinels for "use the model maximum". Ollama clamps them to a roughly 4-token window and still returns HTTP 200, producing unrelated output from a prompt that was silently discarded, so they are rejected at startup instead.

## Telemetry

The `telemetry` object is optional. When it is omitted or `telemetry.enabled` is `false`, the application does not initialize OpenTelemetry exporters and can run without the local observability stack.

When `enabled` is `true`, the endpoint is printed once at startup, and a placeholder host (`YOUR_HOST`, `changeme`, or any `example.com` host) is reported as a warning in the session log. Neither is fatal: running against an unreachable collector is a legitimate way to exercise the export-failure path. The notice exists because export errors are deliberately kept off the console, so a wrong endpoint would otherwise be invisible for the whole session.

When enabled, `otlp_http_endpoint` is treated as the OTLP/HTTP base URL. The application automatically sends traces to `/v1/traces` and metrics to `/v1/metrics`.

`trace_sample_ratio` controls trace sampling:

- `1.0`: record every trace
- `0.5`: record approximately half of traces
- `0.0`: record no traces

Trace sampling does not disable metrics.

If the OTLP Collector requires authentication, set `telemetry.otlp_bearer_token` in `config.json`. When set, it is sent as `Authorization: Bearer <token>` on both the trace and metric exporters. When left empty (the default), requests are sent without an `Authorization` header; if the Collector requires one, exports fail with a timeout/retry error rather than a clear auth error — see [troubleshooting.md](troubleshooting.md).

`metric_export_interval_ms` controls how frequently metrics are sent. Smaller values update Prometheus more frequently but increase export activity.

For setup, querying, retention, and safe shutdown instructions, see the [Observability Guide](observability.md).

## Workflow Dispatch

The `dispatch` object is optional. When it is omitted or `dispatch.enabled` is `false`, KIO1 prints the generated workflow plan and stops, which is the behaviour it had before the dispatch layer existed.

When enabled, each plan step is turned into an execution message and posted to the agent configured under `dispatch.agents`. Steps addressed to agents that are not configured are reported as `skipped`; they are not errors, because a plan spanning KIO2-KIO13 will always name agents that are not deployed yet.

Enabling dispatch with an empty `dispatch.agents` object is rejected at startup, since there would be nowhere to send any step.

Entries are not limited to KIO agents. An `http` entry calls an ordinary REST API and an `cli` entry runs a local command; both are adapted into the same execution message and reply the executor already reads, so a single plan can mix all three. [Connecting your own module](connecting-a-tool.md) is the whole contract.

The same entries also generate the planner's catalog: a prompt containing `{{AGENT_CATALOG}}` has it replaced at startup with the registered targets, their descriptions, and their capabilities. Adding an entry is therefore the only step needed to make it both known to the model and routable.

An agent that analyses code has to be told which code. The plan's `task` is free text, so `dispatch.workspace` travels in the execution message's `data` field as `repository.path` and `target.entry_point`. Leaving it unset sends neither key, which is correct for agents that do not touch a repository. In a containerized deployment the path must be the one inside the agent's container, not KIO1's.

Switching `dispatch.transport` to `nats` changes only how a message is delivered. The message body is identical, so agents that already speak the HTTP contract need no schema change.

For the message format, reply contract, dependencies, execution modes, and telemetry, see [Workflow Dispatch](workflow-dispatch.md).

## Where the file lives, and who may write it

`KIO1_CONFIG_PATH` names the file everything above is read from. It is also
the file the [console](console.md) writes to when someone adds, edits, or
removes a target there — the reader and the writer take the same setting, so
they can never disagree about which file they mean.

```bash
KIO1_CONFIG_PATH=/etc/kio1/config.json
KIO1_CONFIG_WRITABLE=0        # console changes last until the next restart
```

Set `KIO1_CONFIG_WRITABLE` to `0`, `false`, `no`, or `off` where the
configuration is mounted read-only, or where changes must only arrive through
the repository. The console then says so up front rather than failing at the
moment someone presses Save.

When the console does write, only the `dispatch.agents` object changes: every
other key is read and written back as it was found, and the previous contents
are kept alongside as `config.json.bak`.

The write is atomic where it can be: a temporary file beside the target, then
a replace. `deploy/compose.yaml` mounts the configuration into the container
as a **single file**, which makes it a mount point, and a mount point cannot
be renamed over — the kernel answers `EBUSY`. There the contents are rewritten
in place instead. Saving works either way; the difference is only that the
in-place route is not atomic, which is what the backup covers.

## Security

Do not put API keys in `config.json`, source files, or committed shell scripts. The OpenAI and Anthropic SDKs automatically read the `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` environment variables. Variables set with the commands above last only until the terminal session is closed.


## Custom System Prompts

Create a new file under `prompts/` and update `prompt_path` in `config.json`:

```json
"prompt_path": "prompts/my_custom_prompt.txt"
```

The prompt file is plain text. It is sent as the `system` role message to the model on every request.
