# KIO1: Orchestrator



KIO1 turns a software-development request, written in plain language, into a plan; runs
that plan on the agents that can carry it out; and answers in the language you asked in —
in a chat in your browser, or from a terminal.

<p align="center">
  <a href="https://www.youtube.com/watch?v=-p7JMi4_iGg ">
    <img width="1351" height="763" alt="image" src="https://github.com/user-attachments/assets/73af5be9-f67a-46c2-b1e1-4e30cf7013d5" />
  </a>
</p>

KIO1 supports multiple model providers:

- Ollama
- OpenAI
- Anthropic
- OpenRouter

Providers are loaded dynamically from `<provider>_client.py`, keeping the main application and configuration loader provider-neutral.

## Requirements

- Python 3.10 or newer
- An Ollama installation, or an OpenAI, Anthropic or OpenRouter API key
- Access to the model configured in `config.json`
- Docker with Docker Compose (optional, for local trace and metric storage)

## Quick start

Double-click **`start.cmd`** (Windows), or run:

```bash
./start.sh          # or: python start.py
```

That installs what is needed into `.venv`, creates `config.json`, starts
Ollama if it is installed, and opens the console in your browser. No Docker
and no database server are needed: history is kept in `data/kio1.db`.
See [Usage](docs/usage.md#the-quickest-start).

## Installation by hand

```bash
make install            # runtime plus test and lint tools
source .venv/bin/activate
```

Or manually:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # or requirements.txt for runtime only
```

Further information about the project is available in the docs folder. The
plan for what comes next is in [docs/roadmap.md](docs/roadmap.md).

## Configuration

The active provider and model are selected in `config.json`, which is created
from `config.example.json` on first run and is not tracked by git. API keys are read from environment variables, never from `config.json` itself. See [Configuration](docs/configuration.md) for the full field reference and per-provider setup.

## Usage

Start it as above and write your request on the **Chat** page. KIO1 shows the plan as it
builds it, asks when it needs something only you know, and ends with an answer that names
the agent each fact came from. **Agents** is where you add your own: describe one in a few
fields and try it before saving.

From a terminal instead:

```bash
python3 main.py
```

See [Usage Guide](docs/usage.md) for example sessions and sample requests.

## Observability

<!-- KIO1 can export OpenTelemetry traces and metrics to a local persistent Tempo and Prometheus stack. Telemetry is disabled by default.

Start the observability services with:

```bash
docker compose -f observability/compose.telemetry.yaml up -d
```

See the [Observability Guide](docs/observability.md) for configuration, querying, retention, and safe-shutdown instructions. -->

## Workflow Dispatch

KIO1 sends the steps of a generated plan to whatever carries them out. A target can be a KIO agent following the `KIO1 - KIO10` integration protocol, an ordinary REST API of your own, or a command line tool on this machine. All three are listed under `dispatch.agents` in `config.json`, each with a `type`, and a single plan can mix them.

Most KIOs are not running as services yet. Until they are, `kio-mocks/` stands in for
them — and each stand-in does its job rather than answering with a canned reply: KIO11 runs
the tests, KIO2 records the failing one and names the line that broke, KIO7 proposes a fix
and checks it on a copy, KIO10 measures what the fix costs. For a new feature, KIO3 writes
the requirements, KIO4 designs the parts, KIO7 writes the skeleton and KIO6 checks the code
against the requirements. They speak the same protocol as the real services, so a real KIO
takes a stand-in's place by changing an address.

```bash
cd kio-mocks && python -m kio_mocks
```

Point KIO1's workspace at `kio-mocks/sample` to try them on a small project with one real
bug. See [the stand-ins' README](kio-mocks/README.md) for what each one does.

For testing the protocol alone, `tools/dummy_fleet.py` starts a stub for every KIO with
canned answers, and `tools/dummy_agent.py --agent-id KIO2 --port 8102` starts one.

Steps can declare dependencies with an optional `depends_on` field, and a step's output is fed to the steps that depend on it through the protocol's own `data` field. Messages travel over HTTP by default, or over NATS with `dispatch.transport`; the message body is identical either way.

### Running it

```bash
python main.py                                        # terminal orchestrator
uvicorn app:app --port 8000                           # web service and console
docker compose -f deploy/compose.yaml up -d --build   # both, plus a database
```

`main.py` and `app.py` sit side by side at the root: one is how you drive KIO1
from a terminal, the other is how you drive it from a browser or another
program. [Usage](docs/usage.md) walks through both.

### Seeing it do real work

The stand-ins in `kio-mocks/` work without a model, so the code they generate
is a skeleton or a rule-based fix. For an agent that writes code with the model
`config.json` configures:

```bash
python tools/codegen_agent.py --port 8107 --workspace ./generated
```

Ask KIO1 for a FastAPI server and a runnable `main.py` appears in `./generated`.
It is a demonstration rather than KIO7: one model call, nothing compiled, no
tests run against the result — so it asks for a human to look.

If you own a KIO and want KIO1 to call it, [Connecting a KIO](docs/connecting-a-kio.md) is the whole contract on one page. To plug in something that is not a KIO — your own API, a script, a build tool — see [Connecting your own module](docs/connecting-a-tool.md). For anything that speaks the Model Context Protocol, [Connecting an MCP server](docs/mcp.md) is three lines of configuration: the server reports its own tools rather than being described.

See the [Workflow Dispatch Guide](docs/workflow-dispatch.md) for the message format, reply contract, dependencies, execution modes, transports, and known gaps.

## Services

Alongside the terminal application, KIO1 exposes a Workflow API and a Session Manager. The API accepts a request, opens a session, and executes the workflow in the background; the Session Manager records every session, step, artifact, and lineage edge in PostgreSQL.

```bash
make services
```

That starts PostgreSQL, the Session Manager on port 8001, and the Workflow API on port 8000.

There is no prompt in a container: `http://localhost:8000/docs` gives a browser form, or

```bash
curl -X POST http://localhost:8000/workflow/run -H 'Content-Type: application/json' -d '{"request":"fix the null pointer exception in the payment module"}'
```

which answers with a session id to poll for the result.

See the [Services Guide](docs/services.md) for the full route reference, configuration, and known gaps.

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
- [Services](docs/services.md) — Workflow API, Session Manager, and PostgreSQL
- [Connecting a KIO](docs/connecting-a-kio.md) — What a KIO must accept and reply to be reachable
- [Connecting your own module](docs/connecting-a-tool.md) — Registering a REST API or a local command as a workflow target
- [Connecting an MCP server](docs/mcp.md) — Pointing KIO1 at a Model Context Protocol server, which reports its own tools instead of being described
- [Policy gates](docs/policy-gates.md) — Stopping a step until the checks it depends on have passed
- [Failover](docs/failover.md) — Running a step on another target when the one the plan named cannot
- [Choosing by history](docs/selection.md) — Asking the target that has actually been working, not the one listed first
- [The console](docs/console.md) — The chat, the agents, and the settings, in your browser
- [First-time setup](docs/setup.md) — Choosing and downloading a model that suits the computer
- [Approving a plan](docs/approval.md) — Plans that wait for a person, and questions KIO1 asks first
- [Reviewing and answering](docs/review.md) — How a run reads its results and writes the answer
- [Agents made of instructions](docs/llm-agents.md) — An agent written in plain language, run by a model
- [Where agents are kept](docs/agent-store.md) — The agent database, its history, and export
- [Stand-in KIOs](kio-mocks/README.md) — KIO2–KIO13 stand-ins that do their job, for running KIO1 end to end
- [Usage](docs/usage.md) — From a clone to a lifecycle running in your browser
- [Use-Case 1, end to end](examples/uc1/README.md) — A runnable lifecycle: real files, a real failing test, a refused release
- [Discovery](docs/discovery.md) — Letting agents publish their own capabilities instead of listing them by hand
- [The live directory](docs/live-directory.md) — Health probing, background refresh, and the registration API
- [Workflow Dispatch](docs/workflow-dispatch.md) — Sending plan steps to KIO agents
- [Observability](docs/observability.md) — Tracing, metrics, storage, querying, and safe shutdown
