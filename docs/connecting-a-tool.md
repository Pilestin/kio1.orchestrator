# Connecting your own module

[Connecting a KIO](connecting-a-kio.md) covers services that implement the
consortium's KIO1–KIO10 protocol. This page covers everything else: an
internal REST API, a command line tool, a script — anything that does useful
work but has never heard of KIO1.

Nothing has to be rewritten to speak the protocol. You describe how to call
it in `config.json`, and KIO1 adapts in both directions.

## The three kinds of entry

Every entry in `dispatch.agents` has a `type`. It decides how a step
addressed to that entry runs.

| `type` | What it is | How the step runs |
|--------|-----------|-------------------|
| `kio` (default) | A service implementing the KIO1–KIO10 protocol | The execution message is posted to `endpoint` + `path`, or published on a NATS subject |
| `http` | Any REST API | The configured request is sent to `url`, and the response becomes the step's output |
| `cli` | A program on the machine KIO1 runs on | The command runs without a shell, and its exit code decides the step's status |

An entry without a `type` is a `kio` entry, so nothing written before this
existed needs changing.

## A REST API

```json
"LogSearch": {
    "type": "http",
    "description": "Searches the internal log index for a pattern.",
    "url": "https://logs.internal/api/v1/search",
    "method": "POST",
    "headers": {"Authorization": "Bearer ${LOG_API_TOKEN}"},
    "body": {"query": "{task}", "repo": "{data.repository.path}", "top_k": 20},
    "result_path": ["report"],
    "capabilities": ["log_search"]
}
```

| Field | Rule |
|-------|------|
| `url` | Required. May contain placeholders. |
| `method` | `GET`, `POST`, `PUT`, `PATCH`, or `DELETE`. Defaults to `POST`. |
| `headers` | Optional. Values may contain placeholders and `${ENV_VAR}` references. |
| `body` | Optional JSON body, with placeholders substituted. **Omit it** and KIO1 sends the standard execution message — the right default for an API that already speaks the protocol. Not sent for `GET` or `DELETE`. |
| `result_path` | Optional keys to follow into the response. `["report"]` on `{"report": {...}, "meta": {...}}` keeps only the report. |

**How the response is read.** A `2xx` with a JSON object becomes the step's
output as-is. A JSON value that is not an object, or a plain-text body, is
kept under `result`. A non-`2xx` fails the step and the body is quoted in the
error, so a rejection is readable without opening the logs.

**Credentials belong in the environment.** `${LOG_API_TOKEN}` is read from
the environment when the call is made. If it is unset, the step fails with a
message naming the variable — rather than calling the API with an empty
credential and reporting the resulting 401 as a tool failure.

## A command line tool

```json
"Pytest": {
    "type": "cli",
    "description": "Runs the project's test suite and reports what failed.",
    "command": ["python", "-m", "pytest", "-q", "{data.repository.path}"],
    "cwd": "/work/repo",
    "env": {"PYTHONWARNINGS": "ignore"},
    "capabilities": ["test_execution"]
}
```

| Field | Rule |
|-------|------|
| `command` | Required. A **list**: the program followed by its arguments, each a separate entry. May contain placeholders. |
| `cwd` | Optional working directory. Empty runs in KIO1's own directory. |
| `env` | Optional extra environment variables. Values may contain placeholders and `${ENV_VAR}` references. |

**Why a list and not a string.** The command runs with no shell involved. A
task description containing `;`, quotes, or backticks becomes one argument
and stays one argument. Accepting a single string would mean splitting it,
and splitting is where shell injection gets in. There is no option to pass a
string.

**How the result is read.** Exit code `0` succeeds; anything else fails the
step, with `stderr` (or `stdout`) quoted as the reason. A tool that ran and
answered "no" is a *result*, not a broken transport — the workflow records it
and blocks whatever depended on it, exactly as a KIO agent's failure would.

**Say what you concluded.** If the command prints a JSON object on stdout,
that object becomes the step's output directly. This is how a local tool gets
the same voice a KIO agent has:

```json
{"verdict": "defect", "hitl_required": true, "failed": ["test_pricing.py::test_empty_cart"]}
```

Otherwise the output is `{"stdout": "...", "stderr": "...", "exit_code": 0}`.

## Placeholders

Anything in `url`, `headers`, `body`, `command`, and `env` can carry these:

| Placeholder | Value |
|-------------|-------|
| `{task}` | The step's free-text instruction from the plan |
| `{capability}` | The capability the step asked for |
| `{workflow_id}`, `{step_id}`, `{agent_id}` | Step identity |
| `{data}` | The whole structured input object |
| `{data.repository.path}` | The code under analysis, from `dispatch.workspace` |
| `{data.upstream.s1}` | What step `s1` returned |

A string that is *exactly* one placeholder keeps the value's type, so
`"body": "{data}"` sends an object rather than its printed form. A
placeholder inside a longer string is substituted as text. A name that is not
a known field is **left untouched**, so a typo shows up in the request
instead of silently blanking a field.

## Registering it

Add the entry to `dispatch.agents`, with `dispatch.enabled` set to `true`. Two
things follow automatically:

**Routing.** A plan step naming that `agent_id` and one of its
`capabilities` is dispatched to it. A step asking for a capability the entry
does not declare is skipped rather than sent, the same guard KIO agents get.

**The planner's catalog.** The system prompt at
`prompts/kio1_system_prompt_ver2.txt` contains `{{AGENT_CATALOG}}`, which is
filled at startup from the same entries that decide routing. Adding an entry
is therefore the *only* step: the model is told about it, and the plan it
produces is routable. There is no second list to keep in sync.

Write a real `description`. It is what the model reads when deciding whether
your tool fits the request. Without one the catalog falls back to listing the
capability names, which is much weaker.

If your module can serve one more URL, it can publish this entry itself and
skip the configuration edit entirely. See [Discovery](discovery.md).

## Checking it works

```bash
python -c "from core.config_loader import load_config; from core.prompt_loader import render_catalog; print(render_catalog(load_config().dispatch.registry))"
```

That prints the catalog the model will see. If your entry is missing or its
description is wrong, fix it before running a plan.

Then start `python main.py` and ask for something only your tool can do. The
per-step table shows what happened:

```text
Workflow wf-tools01 (mixed): 2 ok, 1 error, 0 not dispatched
  s1 GitHistory repository_history ok     47 ms
  s2 LogSearch  log_search         ok      3 ms  clean
  s3 KIO2       bug_localization   error  KIO2 is unreachable at http://127.0.0.1:8102/execute
```

## What a tool cannot do yet

- **No retry of the same target.** A failed call is not retried on the same
  target, though it may move to another one declaring the same capability;
  see [Failover](failover.md).
- **One timeout for everyone.** `dispatch.request_timeout` applies to every
  target. A slow tool needs the timeout raised for all of them.
- **A `verdict` only stops things you gate on.** Reporting `defect` is
  recorded and shown, but it stops a later step only when a policy gate says
  it should. See [Policy gates](policy-gates.md).
- **Commands run wherever KIO1 runs.** A `cli` entry is local to the
  orchestrator process, including inside a container. A tool on another
  machine needs to be an `http` entry.
