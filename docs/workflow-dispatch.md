# Workflow Dispatch

KIO1 turns a user request into a workflow plan. This document describes the
layer that takes that plan and actually sends its steps to KIO agents.

Dispatch is disabled by default. With `dispatch.enabled` set to `false`,
KIO1 behaves exactly as before: it prints the plan and stops.

## Why this layer exists

The `KIO1 - KIO10 Integration Strategy and Communication Protocol` document
splits responsibility three ways:

- KIO1 generates and manages the overall workflow
- a planner translates each workflow step into an executable message
- each KIO executes its assigned task and returns a result

The first part already existed in this repository. The second and third did
not, so a generated plan was never delivered anywhere.

## Data flow

```text
model response (JSON workflow plan)
        |
        v
  parse_plan()            dispatch/workflow_planner.py   validate the plan
        |
        v
  route_plan()            dispatch/workflow_planner.py   plan step -> execution message
        |                 dispatch/agent_registry.py     agent_id  -> endpoint
        v
  execute_workflow()      dispatch/workflow_executor.py  sequential / parallel
        |
        v
  transport.send()        dispatch/agent_transport.py    HTTP POST to the agent
        |
        v
  StepResult per step     correlated by workflow_id + step_id
```

## Execution message

The message KIO1 posts to an agent follows the shared protocol exactly:

```json
{
  "workflow_id": "wf-medsch01",
  "step_id": "s7",
  "agent_id": "KIO10",
  "capability": "energy_efficiency",
  "endpoint": "http://localhost:8110",
  "task": "Analyse the energy and computational efficiency requirements",
  "data": {}
}
```

`data` is the protocol's extension point for structured inputs a specific KIO
needs beyond the free-text `task`. KIO1 currently always sends `{}`.

## Reply contract

The shared protocol defines the request but not the reply. The following
shape is what this implementation expects, and is a proposal for the
consortium rather than an agreed contract:

```json
{
  "workflow_id": "wf-medsch01",
  "step_id": "s7",
  "agent_id": "KIO10",
  "status": "ok",
  "output": {"summary": "..."},
  "error": null
}
```

Rules KIO1 applies when reading a reply:

| Situation | KIO1 behaviour |
|-----------|----------------|
| `status` missing | Treated as `ok`, so agents can be brought up incrementally |
| `status: "error"` | Step fails, `error` is surfaced to the user |
| `workflow_id` or `step_id` missing or mismatched | Warning logged, step still counted |
| Non-2xx HTTP status | Step fails with the status code and a truncated body |
| Agent unreachable or slow | Step fails, the workflow continues per execution mode |
| Reply is not a JSON object | Step fails |

Correlation is deliberately lenient. Rejecting replies outright would block
integration work whenever an agent is still being wired up, and the mismatch
is always visible in the logs and traces.

## Agent registry

The protocol puts an `endpoint` field inside every execution message but does
not say who fills it in. KIO1 owns that mapping, configured under
`dispatch.agents`:

```json
"dispatch": {
    "enabled": false,
    "request_timeout": 60,
    "max_parallel_steps": 4,
    "agents": {
        "KIO10": {
            "endpoint": "http://localhost:8110",
            "path": "/execute",
            "capabilities": ["tinyml", "energy_efficiency"]
        }
    }
}
```

| Field | Meaning |
|-------|---------|
| `endpoint` | Base URL of the agent service. Required. |
| `path` | Path execution messages are posted to. Defaults to `/execute`. |
| `capabilities` | Capability values the agent accepts. An empty list disables the check. |

An agent that is not listed is not an error. Plans routinely reference KIOs
that are not deployed yet; those steps are reported as `skipped` and the rest
of the workflow still runs.

## Execution modes

| Mode | Behaviour |
|------|-----------|
| `sequential` | One step at a time, halting after the first failure. Remaining steps are reported as `not_run`. |
| `parallel` | All steps dispatched concurrently, bounded by `max_parallel_steps`. One failure does not stop the others. |
| `mixed` | Runs sequentially, with a warning. |

`mixed` has no meaningful implementation today: workflow plans carry no
per-step dependency field, so there is nothing to derive a partial order
from. Making `mixed` real requires adding dependencies to the plan schema,
for example a `depends_on` list per step. This is a known gap, not an
oversight.

## Step statuses

| Status | Meaning |
|--------|---------|
| `ok` | The agent answered successfully |
| `error` | Transport failure, non-2xx reply, or `status: "error"` from the agent |
| `skipped` | No endpoint configured, or the agent does not declare the capability |
| `not_run` | An earlier step in a sequential workflow failed |

## Telemetry

The dispatch layer adds one span per workflow and one per step:

| Span | Description |
|------|-------------|
| `kio1.workflow.execute` | Dispatching a whole plan |
| `dispatch <agent_id>` | Delivering one execution message, a CLIENT span |

and four metrics:

| Metric | Type | Labels |
|--------|------|--------|
| `kio1.workflow.executions` | Counter | `kio1.workflow.execution_mode`, `kio1.status` |
| `kio1.workflow.execution.duration` | Histogram | same |
| `kio1.step.dispatches` | Counter | `kio1.agent.id`, `kio1.step.capability`, `kio1.status` |
| `kio1.step.duration` | Histogram | same |

`workflow_id` and `step_id` are unbounded, so they appear as span attributes
only, never as metric labels (observability contract section 2.1). `agent_id`
and `capability` come from the bounded sets defined in KIO1's system prompt
and are safe as labels.

Dispatch spans are CLIENT spans so that an agent's own server span becomes a
child once agents propagate trace context. KIO1 does not yet inject W3C
`traceparent` headers into execution messages; that is the next step for
end-to-end traces across KIOs.

## The stub agent

`tools/dummy_agent.py` is a dependency-free stand-in for a real KIO, used to
validate the protocol before any agent exists:

```bash
python tools/dummy_agent.py --agent-id KIO2 --port 8102
```

```bash
python tools/dummy_agent.py --agent-id KIO10 --port 8110
```

Useful flags:

| Flag | Purpose |
|------|---------|
| `--latency` | Delay replies, to exercise timeouts |
| `--fail-capability` | Answer `status: "error"` for a capability, repeatable |
| `--path` | Serve a path other than `/execute` |

It also answers `GET /health`, so reachability can be checked without
sending a workflow step.

## Known gaps

These are deliberate omissions, listed so they are not mistaken for
oversights:

- **No trace-context propagation.** Execution messages carry no `traceparent`,
  so an agent's spans are not yet children of KIO1's dispatch span.
- **No session id.** The observability guide expects a `session_id` produced
  by a Session Manager backed by PostgreSQL. Neither exists, so KIO1's local
  session id is not part of the execution message.
- **No retries.** A failed dispatch is reported, not retried.
- **No result feed-back into the plan.** Step outputs are reported to the
  user but not passed into later steps, and no lineage is persisted.
- **Transport is HTTP only.** The observability guide describes NATS
  JetStream carrying a `KIOEnvelope` instead. `AgentTransport` is the
  interface where that alternative would be added.
