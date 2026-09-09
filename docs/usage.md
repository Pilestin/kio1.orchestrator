# Using KIO1

KIO1 turns a request written in plain language into a workflow, and runs that
workflow against whatever can carry it out — KIO agents, your own REST APIs,
or command line tools on the machine it runs on.

This page gets you from a clone to watching a full software lifecycle happen
in a browser, and then to pointing it at your own work.

---

## Before you start

```bash
make install          # creates .venv and installs dependencies
```

Nothing else is required for the walkthrough below. A model, PostgreSQL, and
an OpenTelemetry collector are all optional and all noted where they matter.

---

## 1. See it work, in five minutes

The fastest honest answer to "what does this thing do":

```bash
python examples/uc1/serve.py
```

Open **http://127.0.0.1:8000/**. The banner in your terminal prints a
registry token; click **Registry token** in the console and paste it.

### What you are looking at

**Targets** lists everything KIO1 can reach. Nothing was written into
`config.json` — each agent was found by fetching its `/manifest`, which is
the path an external deployment takes. Each row shows what it declares,
whether it is answering, and what it has recently done.

**Workflows** is where you drive it. Type these four requests in order,
waiting for each to finish:

| Type this | What happens |
|-----------|--------------|
| `Customers want bulk pricing discounts, write the requirements` | KIO3 derives requirements, acceptance criteria and a draft issue |
| `Generate the code and tests for the approved issue` | KIO4 plans modules, KIO7 **writes real files**, KIO6/KIO12/KIO9 check them |
| `Release it to staging` | **Refused.** The suite is red, and a policy gate will not let a release past it |
| `Fix the failing test` | KIO11 runs `pytest`, KIO2 reads the traceback, KIO7 patches the file, KIO11 re-runs — green |
| `Release it to staging` | Now it goes out |

Recent runs then reads:

```text
COMPLETED  wf-uc1-04  Release it to staging
COMPLETED  wf-uc1-03  Fix the failing test
FAILED     wf-uc1-04  Release it to staging        ← the gate refused it
COMPLETED  wf-uc1-02  Generate the code and tests for the approved issue
COMPLETED  wf-uc1-01  Customers want bulk pricing discounts, write...
```

Click any run to see its steps: which target ran each one, how long it took,
what it concluded, and the agent's output.

### What is real here

Everything except the planner. The agents genuinely write files, genuinely
run `pytest`, and genuinely parse the traceback it produces. The bug they
find is a real `ZeroDivisionError` in code that was really generated a moment
earlier.

The planner is **scripted** — it matches on what you type rather than calling
a model — so the walkthrough works with nothing installed. Pass `--provider`
to plan with whatever `config.json` configures instead.

This is [Use-Case 1](../examples/uc1/README.md) from the AI4SWEng
deliverable, with its four scenarios in the order the document specifies.

### Without a browser

```bash
python examples/uc1/run.py           # the same scenarios, in the terminal
python examples/uc1/run.py --keep    # and keep the workspace to read
```

It exits non-zero if a scenario does not deliver, so it works in CI.

---

## 2. Run it on your own work

### Start the services

```bash
make services                        # Workflow API, Session Manager, PostgreSQL
export KIO1_REGISTRY_TOKEN=$(openssl rand -hex 16)
```

The console is at http://localhost:8000/. Without `KIO1_REGISTRY_TOKEN` the
registry is closed and the console will tell you so — see
[the console](console.md).

### Tell it what it can use

Everything KIO1 can dispatch to lives in `dispatch.agents` in `config.json`.
There are three kinds:

```json
"agents": {
  "KIO2": {
    "endpoint": "http://127.0.0.1:8102",
    "description": "Bug localisation and diagnosis.",
    "capabilities": ["bug_localization", "diagnosis"]
  },

  "LogSearch": {
    "type": "http",
    "description": "Searches the internal log index.",
    "url": "https://logs.internal/api/v1/search",
    "headers": {"Authorization": "Bearer ${LOG_API_TOKEN}"},
    "body": {"query": "{task}", "repo": "{data.repository.path}"},
    "capabilities": ["log_search"]
  },

  "Pytest": {
    "type": "cli",
    "description": "Runs the project's test suite.",
    "command": ["python", "-m", "pytest", "-q", "{data.repository.path}"],
    "capabilities": ["test_execution"]
  }
}
```

- **`kio`** (the default) — a service speaking the KIO1–KIO10 protocol.
  [Connecting a KIO](connecting-a-kio.md).
- **`http`** — any REST API. The request is built from a template.
- **`cli`** — a program on this machine, run without a shell.
  [Connecting your own module](connecting-a-tool.md).

Write a real `description`. It is what the planning model reads when deciding
whether your tool fits a request.

**You do not have to write any of this.** If your service can serve one more
URL, publish a manifest and let KIO1 ask — see [Discovery](discovery.md). Or
register it at runtime from the console's **Register a target** box, or by
`POST /registry/agents`.

### Decide what must not happen

```json
"policy": {
  "gates": [
    {
      "before": ["deployment", "packaging"],
      "requires": ["test_automation", "cybersecurity_validation"],
      "verdict": ["clean"]
    }
  ],
  "block_on_review_required": false
}
```

That is the rule that refused the release in the walkthrough. It lives in
configuration rather than in the prompt because whether a security scan is
mandatory before deployment is your decision, not a model's.
[Policy gates](policy-gates.md).

### Send it a request

From the console, or:

```bash
curl -X POST http://localhost:8000/workflow/run \
  -H 'Content-Type: application/json' \
  -d '{"request": "The checkout total is wrong for empty carts. Find and fix it."}'
```

You get a `session_id` back immediately; the run continues in the background.
`GET /workflow/{session_id}` returns every step with its result.

---

## 3. The terminal application

`main.py` is the same orchestrator without the services — useful when you
want to watch plans being produced.

```bash
.venv/bin/python main.py
```

```text
Targets: 13 available, 1 not answering (KIO5)
KIO1 Orchestrator (type 'exit' to quit)
----------------------------------------

> There is a null pointer exception in the payment module. Diagnose and fix it.
```

It prints the JSON plan, then dispatches it and shows a per-step table:

```text
Workflow wf-bug01 (sequential): 4 ok, 0 error, 0 not dispatched
  s1 KIO2  bug_localization  ok   854 ms  defect
  s2 KIO2  diagnosis         ok   869 ms  defect  review required
  s3 KIO7  code_generation   ok  1204 ms  clean
  s4 KIO11 test_automation   ok  2010 ms  clean
```

This path **needs a model**: it plans with whatever `provider` and `model`
name in `config.json` (Ollama, OpenAI, or Anthropic — API keys come from the
environment, never from the file). Set `dispatch.enabled` to `false` to get
plans without dispatching them.

Each session writes `chats/chat_<timestamp>_<id>.jsonl` and
`logs/log_<timestamp>_<id>.log`.

---

## Reading a result table

```text
  s2 MyCodeGen code_generation ok  5 ms  clean  planned KIO7
```

| Column | Meaning |
|--------|---------|
| `s2` | The step's id in the plan |
| `MyCodeGen` | The target that **actually** ran it |
| `ok` | Whether the call worked — not whether it found a problem |
| `clean` | What the target **concluded**: `clean`, `defect`, or `inconclusive` |
| `review required` | The target asked for a person to look |
| `planned KIO7` | It ran somewhere other than where the plan said |

Two statuses are not failures. `skipped` means the plan named an agent that
is not configured — routine while a fleet is being brought up. `not_run`
means something stopped it: a gate refused it, or a step it depends on did
not complete. The reason is printed next to it.

---

## Where everything is configured

| Setting | What it decides | More |
|---------|-----------------|------|
| `provider`, `model` | Who plans | [Configuration](configuration.md) |
| `dispatch.enabled` | Whether plans are dispatched at all | |
| `dispatch.agents` | Everything KIO1 can reach | [KIOs](connecting-a-kio.md), [your modules](connecting-a-tool.md) |
| `dispatch.discovery` | Manifest URLs agents describe themselves through | [Discovery](discovery.md) |
| `dispatch.health` | How often targets are probed, and when one is withdrawn | [The live directory](live-directory.md) |
| `dispatch.policy` | What must have passed before a step may run | [Policy gates](policy-gates.md) |
| `dispatch.failover` | Whether a step may run somewhere other than planned | [Failover](failover.md) |
| `dispatch.selection` | Whether history reorders the candidates | [Choosing by history](selection.md) |
| `dispatch.workspace` | The code under analysis, passed to every agent | |
| `telemetry` | Trace and metric export | [Observability](observability.md) |

Addresses that differ per deployment come from the environment rather than
the file: `KIO1_AGENT_HOST`, `KIO1_AGENT_ENDPOINT_<AGENT>`,
`KIO1_PROVIDER_ENDPOINT`, `KIO1_SESSION_MANAGER_URL`, `KIO1_REGISTRY_TOKEN`.

---

## When something looks wrong

**A target says `unreachable`.** The console shows why next to it — usually
its `/health` path timing out. It comes back on its own once it answers; the
directory keeps probing.

**A target says `unchecked`.** Nothing about it can be probed. That is the
honest answer for an `http` entry with no `health_url`, not a problem.

**A step says `skipped`.** No configured target declares that capability.
Check the catalog the planner is shown:

```bash
python -c "from core.config_loader import load_config; from core.prompt_loader import render_catalog; print(render_catalog(load_config().dispatch.registry))"
```

**A run says `failed` but every step is `ok`.** Look for a `not_run` step — a
gate refused it. The reason names the capability and the verdict it wanted.

**The console says the registry is closed.** `KIO1_REGISTRY_TOKEN` is not set
on the server. The 503 body says exactly that.

**Nothing is recorded and status lookups fail.** `KIO1_SESSION_MANAGER_URL`
is not set, so nothing is being written down. Runs still execute.

More in [Troubleshooting](troubleshooting.md).

---

## What KIO1 cannot do yet

Worth knowing before you plan around it:

- **A stopped workflow cannot be resumed.** `block_on_review_required` stops
  a run when an agent asks for a person, and there is no way to approve it and
  carry on — the remaining steps end as `not_run`. It is a stop, not a
  handover.
- **Plans are never rewritten.** [Failover](failover.md) substitutes *who*
  carries out a step; nothing changes *what* the steps are.
- **History does not survive a restart.** The scoreboard that
  [chooses between targets](selection.md) lives in memory.
- **A probe proves an agent answers, not that it does what it claims.**
