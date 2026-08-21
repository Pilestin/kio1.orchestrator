# Services

KIO1 can be driven two ways. The terminal application in `main.py` is
unchanged. Alongside it there are now two HTTP services that give KIO1 the
role the observability guide assigns it: a request entry point, and a durable
record of every run.

```text
client
  |  POST /workflow/run
  v
Workflow API  ──────────────►  Session Manager  ──────►  PostgreSQL
  |   (opens a session first,      (sessions, steps,
  |    returns 202 + session_id)    artifacts, lineage)
  |
  |  execution messages
  v
KIO agents (KIO2 … KIO13)
```

## Workflow API

| Route | Purpose |
|-------|---------|
| `POST /workflow/run` | Accept a request, open a session, execute in the background |
| `GET /workflow/{session_id}` | Everything recorded for that run |
| `GET /health` | Liveness |

`POST /workflow/run` opens the session **before** answering, so the caller
always receives an identifier it can poll. If the Session Manager is
unreachable the request is refused with `503` and nothing runs: starting work
the caller cannot track would leave results no one can find.

```bash
curl -s -X POST http://localhost:8000/workflow/run -H 'Content-Type: application/json' -d '{"request":"there is a null pointer exception in the payment module"}'
```

```json
{
  "session_id": "0e4d1f0c-9a1e-4c22-9d20-6f3a5c8b1e77",
  "status": "pending",
  "status_url": "http://localhost:8000/workflow/0e4d1f0c-9a1e-4c22-9d20-6f3a5c8b1e77"
}
```

Polling that `status_url` returns the session, its steps, and its lineage.
The provider and system prompt are loaded once at startup and reused, so a
request does not pay model-loading cost.

## Session Manager

| Route | Purpose |
|-------|---------|
| `POST /sessions` | Open a session |
| `GET /sessions/{session_id}` | Session with steps and lineage |
| `PATCH /sessions/{session_id}` | Record status, workflow id, execution mode, or error |
| `POST /sessions/{session_id}/steps` | Record one step outcome and its output |
| `GET /health` | Liveness |

A session moves through `pending` → `running` → `completed` or `failed`. A run
is `failed` if any step failed, even when other steps succeeded, so a partial
result is never reported as a success.

Step output is stored verbatim as an artifact, and only for steps that
actually succeeded. An agent that reports an error leaves no artifact behind
even if it also returned a payload.

### Lineage

The `lineage` table records which artifact fed which step, and carries a
`basis` column saying how the link was established:

| Basis | Meaning |
|-------|---------|
| `execution_order` | Inferred from the order steps ran in |
| `declared_dependency` | Taken from a dependency the plan declared |

When a plan step declares `depends_on`, KIO1 sends those step ids along with
the step result and the edges are recorded as `declared_dependency`. When it
does not, the Session Manager falls back to inferring an edge from the
previous step that produced an artifact, and marks it `execution_order`. In
`parallel` runs the recording order carries no dependency information, so no
edges are inferred rather than wrong ones being invented.

Both kinds live in the same table, so a query can always tell what KIO1
actually knew from what it merely observed.

## Configuration

Both services read `config.json` for provider, dispatch, and telemetry
settings. Deployment-specific values come from the environment:

| Variable | Used by | Meaning |
|----------|---------|---------|
| `KIO1_DATABASE_URL` | Session Manager | PostgreSQL DSN. Unset falls back to in-memory storage, which is lost on restart. |
| `KIO1_SESSION_MANAGER_URL` | Workflow API | Session Manager base URL. Unset disables recording, and status lookups then fail. |
| `KIO1_TEST_DATABASE_URL` | Tests | Enables the PostgreSQL repository tests; they are skipped when unset. |

## Running

Everything at once, in containers:

```bash
make services
```

That brings up PostgreSQL, NATS, the Session Manager on `8001`, and the
Workflow API on `8000`. Stop it with `make services-down`; the database volume
survives.

NATS is started with JetStream enabled and its monitoring port on `8222`, so
the broker is ready even though dispatch defaults to HTTP. Switch transports
with `dispatch.transport` in `config.json`.

To run the services from source against a containerized database, start
PostgreSQL alone and then each service in its own terminal:

```bash
make postgres
```

```bash
make run-session-manager
```

```bash
make run-workflow-api
```

To exercise a full run without any real KIO agent, start a stub and register
it under `dispatch.agents` in `config.json`:

```bash
python tools/dummy_agent.py --agent-id KIO2 --port 8102
```

## Testing

The suite runs without Docker. Repository behaviour is verified against the
in-memory implementation, and the same tests run against real PostgreSQL when
a database is available:

```bash
make test-postgres
```

`tests/test_services_integration.py` wires the Workflow API to a live Session
Manager and a live stub agent over real HTTP, faking only the model provider.
It is the closest thing to a production run that does not need a model.

## Telemetry

The Workflow API initializes OpenTelemetry the same way the terminal
application does, and every run is wrapped in a `kio1.workflow.run` span
carrying `session.id`. Calls to the Session Manager and to KIO agents both
carry a W3C `traceparent` header, so those services join the same trace as
soon as they are instrumented.

`session.id` is a span attribute only, never a metric label: session
identifiers are unbounded and would degrade the metrics database (observability
contract section 2.1).

## Known gaps

- **The Session Manager is not instrumented.** It accepts `traceparent` but
  does not yet create spans of its own, so a trace currently stops at the
  Workflow API boundary.
- **No authentication.** Both services are open. They are meant to run on a
  private network or behind the platform's gateway.
- **Background execution is in-process.** A run is executed by the API process
  that accepted it. A restart mid-run leaves the session at `running` with no
  recovery, and load is not distributed. NATS JetStream addresses this and is
  the next phase.
- **No pagination or retention.** `GET /sessions/{id}` returns every step and
  artifact, and nothing prunes old sessions.
- **Lineage without declared dependencies is only an observation.** A plan
  that omits `depends_on` still produces `execution_order` edges, which record
  what ran after what, not what actually consumed what.
