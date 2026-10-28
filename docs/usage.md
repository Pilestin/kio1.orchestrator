# Using KIO1

KIO1 turns a request written in plain language into a workflow, and runs that
workflow against whatever can carry it out — KIO agents, your own REST APIs,
or command line tools on the machine it runs on.

This page gets you from a clone to watching a full software lifecycle happen
in a browser, and then to pointing it at your own work.

---

## The quickest start

Double-click **`start.cmd`** on Windows, or run `./start.sh` (or
`python start.py`) anywhere else. On its first run it:

1. creates `.venv` and installs the runtime requirements,
2. creates `config.json` from `config.example.json`,
3. generates an administration token in `data/registry-token`,
4. starts Ollama if it is installed but not running, and names the
   `ollama pull` command if the configured model is missing,
5. starts KIO1 on the first free port from 8101 and opens the console with
   the token already filled in.

Later runs skip whatever is already done. History is kept in
`data/kio1.db` (SQLite), so nothing else — no Docker, no database server —
is needed. Options: `--port`, `--host`, `--no-browser`, `--skip-install`.

On a machine without a GPU a local model takes 30–90 seconds to plan; that is
the model, not a hang. See `request_timeout` in
[Configuration](configuration.md).

## Before you start, by hand

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt     # Windows
.venv/bin/pip install -r requirements.txt          # macOS, Linux
```

`requirements-dev.txt` adds the test and lint tools. Nothing else is required
for the walkthrough below. A model, PostgreSQL, and an OpenTelemetry collector
are all optional and all noted where they matter.

> There is a `Makefile` with shortcuts for all of this — `make install`,
> `make services`, `make test`. It is a convenience for people who have `make`,
> which Windows does not ship. Every command below is written out in full so
> you never need it.

---

## 1. See it work, in five minutes

The fastest honest answer to "what does this thing do":

```bash
python examples/uc1/serve.py
```

Open **http://127.0.0.1:8101/**. The banner in your terminal prints a
registry token; paste it under **Settings → Advanced → Administration token**.

### What you are looking at

**Agents** lists everything KIO1 can reach. Nothing was written into
`config.json` — each agent was found by fetching its `/manifest`, which is
the path an external deployment takes. Each card shows what it declares
and whether it is answering.

**Chat** is where you drive it. Type these requests in order, waiting for
each to finish:

| Type this | What happens |
|-----------|--------------|
| `Customers want bulk pricing discounts, write the requirements` | KIO3 derives requirements, acceptance criteria and a draft issue |
| `Generate the code and tests for the approved issue` | KIO4 plans modules, KIO7 **writes real files**, KIO6/KIO12/KIO9 check them |
| `Release it to staging` | **Refused.** The suite is red, and a policy gate will not let a release past it |
| `Fix the failing test` | KIO11 runs `pytest`, KIO2 reads the traceback, KIO7 patches the file, KIO11 re-runs — green |
| `Release it to staging` | Now it goes out |

The conversation then reads, newest first:

```text
COMPLETED  wf-uc1-04  Release it to staging
COMPLETED  wf-uc1-03  Fix the failing test
FAILED     wf-uc1-04  Release it to staging        ← the gate refused it
COMPLETED  wf-uc1-02  Generate the code and tests for the approved issue
COMPLETED  wf-uc1-01  Customers want bulk pricing discounts, write...
```

Click any plan to see its steps: which target ran each one, how long it took,
and what it concluded; the run details beside the chat show each agent's output.

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

### Making it write real code

The agents in that walkthrough do real work, but deterministically. To see
the configured model actually generate something from your own prompt:

```bash
python tools/codegen_agent.py --port 8107 --workspace ./generated
```

That is a `KIO7`-shaped agent offering `code_generation`. Any plan that
reaches that capability now leaves files on disk. Everything the model returns
is treated as untrusted: paths are resolved against the workspace and anything
escaping it is refused, file count and size are capped, and the result carries
`hitl_required` because nothing compiled or ran it.

### Stand-in KIOs that do their job

The walkthrough's agents live inside the example. To run KIO1 itself against
agents that behave like the KIOs it will meet, start the stand-ins:

```bash
cd kio-mocks && python -m kio_mocks
```

They answer on the usual ports (KIO2 on 8102, KIO11 on 8111, …), so the agents
already in `config.json` find them. Set the workspace to `kio-mocks/sample` —
a small shop whose average price divides by zero for an empty cart — and ask:

> Run the tests, find where the failing one breaks, propose a fix and measure
> its energy impact.

KIO11 runs the tests and reports the failure, KIO2 records it and names the
line and the values that broke it, KIO7 writes a guard and checks it against
every test on a copy of the project, and KIO10 compares the energy of the two.

Or ask for something new:

> Customers want bulk discounts: 5 or more items get 5%. Write the
> requirements, design it and check the code against them.

KIO3 writes the requirements and the criteria on both sides of the threshold,
KIO4 designs the parts and says which requirement each one serves, and KIO6
looks for them in the shop: the discount is there, but no test tries 4 items.

Every result comes from a real run; every output says `simulator: kio-mocks`
so it is never taken for the real KIO's. What each stand-in does is in
[their README](../kio-mocks/README.md).

`tools/dummy_fleet.py` is still there for testing the protocol alone; its
replies are canned.

### Without a browser

```bash
python examples/uc1/run.py           # the same scenarios, in the terminal
python examples/uc1/run.py --keep    # and keep the workspace to read
```

It exits non-zero if a scenario does not deliver, so it works in CI.

---

## 2. Run it on your own work

### Start it

There are two entry points at the root of the repository. `main.py` is the
terminal orchestrator; **`app.py` is the web service** — the Workflow API, the
registry API, and the console, all in one process.

**From source**, which is what you want while you are changing things:

```powershell
# PowerShell
$env:KIO1_REGISTRY_TOKEN = "choose-a-secret"
$env:KIO1_SESSION_MANAGER_URL = "http://localhost:8001"   # optional
.venv\Scripts\uvicorn app:app --port 8101 --reload
```

```bash
# bash
export KIO1_REGISTRY_TOKEN=choose-a-secret
export KIO1_SESSION_MANAGER_URL=http://localhost:8001     # optional
.venv/bin/uvicorn app:app --port 8101 --reload
```

Runs still execute without a Session Manager; nothing is written down, and
the console's Recent runs stays empty. To record them, start it too:

```bash
# PostgreSQL, or skip it: without KIO1_DATABASE_URL the Session Manager keeps
# everything in memory, which is fine for a look around.
docker compose -f deploy/compose.yaml up -d postgres

uvicorn services.session_manager.app:app --port 8001    # in one terminal
uvicorn app:app --port 8101 --reload                    # in another
```

**In containers**, which brings up everything at once — PostgreSQL, the
Session Manager, and `app.py`:

```bash
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml logs -f
docker compose -f deploy/compose.yaml down          # keeps the database
```

Put your `KIO1_REGISTRY_TOKEN` in the `workflow-api` service's `environment:`
block in `deploy/compose.yaml`, next to the other settings there.

Either way the console is at **http://localhost:8101/**. Without
`KIO1_REGISTRY_TOKEN` the registry is closed and the console says so — see
[the console](console.md).

| Address | What |
|---------|------|
| `http://localhost:8101/` | The console |
| `http://localhost:8101/docs` | The API, as OpenAPI |
| `http://localhost:8101/health` | Liveness |
| `http://localhost:8001/health` | The Session Manager, when it is running |

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

**You do not have to write any of this by hand.** The console's **Targets**
tab adds, edits, and removes targets, and writes the result back into
`config.json` — so what you do there survives a restart and you never touch
code. Its **How to use** tab has copy-paste examples for all three kinds.

If your service can serve one more URL, it can skip the form entirely:
publish a manifest and let KIO1 ask ([Discovery](discovery.md)), or push it to
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
curl -X POST http://localhost:8101/workflow/run \
  -H 'Content-Type: application/json' \
  -d '{"request": "The checkout total is wrong for empty carts. Find and fix it."}'
```

You get a `session_id` back immediately; the run continues in the background.
`GET /workflow/{session_id}` returns every step with its result.

### The registry token

The console's **Targets** tab reads and writes the agent registry, and those
endpoints are **closed until you choose a token**. Until then the tab shows:

```text
The agent registry API is closed, so this page cannot list or change targets.
Running workflows still works.
```

That is deliberate, not a misconfiguration. Registering an agent tells KIO1 to
start sending task text and step results to an address someone supplied over
the network; leaving that open to anyone who can reach the service would be a
poor default.

To open it, pick any secret, put it in `KIO1_REGISTRY_TOKEN` **when you start
the service**, then click **Registry token** in the console and paste the same
value. The console keeps it in that browser tab only — never on disk, never
sent anywhere but this service.

It is a shared secret over whatever transport you terminate. Put the service
behind TLS if it is reachable from anywhere you do not control.

### On a server

`start.py` listens on `127.0.0.1`, so only the machine it runs on can open
the console. The simplest way to reach a KIO1 on a server is still that
address, through SSH:

```bash
ssh -L 8101:127.0.0.1:8101 you@server     # then open http://127.0.0.1:8101
```

To open it to the network instead, set a console password first:

```bash
python start.py --set-password            # asks twice, keeps only a hash in .env
python start.py --host 0.0.0.0 --no-browser
```

Without a password `start.py` refuses any address but this machine's. With
one, the console asks for it, and nothing else answers someone who has not
signed in — no workflow, no conversation, no file — so a scanner looking for
`/.env` or `/config.json` gets a refusal, and nobody runs models on your bill.
A few wrong guesses and that address has to wait. Restarting KIO1 signs
everyone out. Scripts can keep using the administration token as a bearer
token.

The password travels as typed: put HTTPS in front (Caddy or nginx) before you
use it over the internet, and keep `.env` to yourself — `start.py` makes it
readable by its owner only.

[Running on a server](deploy.md) walks through a whole deployment: a
DigitalOcean droplet, HTTPS or Tailscale in front, and the model on another
machine over Tailscale (`KIO1_PROVIDER_ENDPOINT`).

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

`KIO1_CONFIG_PATH` says which file all of the above lives in, and which one
the console saves to. `KIO1_CONFIG_WRITABLE=0` stops it saving at all, for a
deployment whose configuration is read-only or must only change through the
repository.

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

**The console says the registry is closed.** That is the expected state until
you choose a token — see [The registry token](#the-registry-token) below.
Running workflows is unaffected.

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
