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
needs beyond the free-text `task`. KIO1 uses it, and only it, to feed results
forward:

```json
"data": {
  "upstream": {
    "s2": {"summary": "..."},
    "s3": {"summary": "..."}
  }
}
```

A step that declares no dependencies receives `"data": {}`, exactly as before
the field was used. No key outside the seven agreed fields is ever added to an
execution message.

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

## Dependencies

A plan step may carry an optional `depends_on` array naming the steps whose
output it consumes:

```json
{
  "step_id": "s4",
  "agent_id": "KIO7",
  "capability": "code_generation",
  "task": "Generate the service",
  "depends_on": ["s2", "s3"]
}
```

The field is additive. A plan that omits it behaves exactly as it did before
the field existed, so nothing that worked before can start failing because of
it. That principle is enforced rather than assumed:

| Problem | KIO1 behaviour |
|---------|----------------|
| `depends_on` is not a list of step ids | Field ignored, warning logged |
| It names a step the plan does not contain | That reference dropped, warning logged |
| It names the step itself | That reference dropped, warning logged |
| The graph contains a cycle | All dependencies dropped, plan falls back to `execution_mode` |

A malformed optional field never turns a plan that would otherwise run into a
hard failure.

## Execution modes

When no step declares a dependency, nothing changes:

| Mode | Behaviour |
|------|-----------|
| `sequential` | One step at a time, halting after the first failure. Remaining steps are `not_run`. |
| `parallel` | All steps dispatched concurrently, bounded by `max_parallel_steps`. One failure does not stop the others. |
| `mixed` | Runs sequentially, with a warning: there is no partial order to derive. |

When dependencies **are** declared, they define the order. Steps are grouped
into levels, where every step in a level depends only on earlier levels:

```text
depends_on:  s2←s1,  s3←s1,  s4←s2,s3

level 1:  s1
level 2:  s2  s3      (independent, may run together)
level 3:  s4
```

`execution_mode` then only decides whether a level's steps run concurrently:
`sequential` runs them one at a time, `parallel` and `mixed` run them
together, bounded by `max_parallel_steps`. This is what makes `mixed`
meaningful.

Failure is scoped to the graph rather than the whole plan. If a step fails, or
was skipped because its agent is not deployed, only the steps that depend on
it become `not_run`; unrelated branches still run. Their input was never
produced, so dispatching them would spend a call to get a misleading answer.

## Step statuses

| Status | Meaning |
|--------|---------|
| `ok` | The agent answered successfully |
| `error` | Transport failure, non-2xx reply, or `status: "error"` from the agent |
| `skipped` | No endpoint configured, or the agent does not declare the capability |
| `not_run` | An earlier step in a sequential workflow failed |

## Transports

The same execution message travels over either transport, chosen with
`dispatch.transport`:

| Transport | Delivery |
|-----------|----------|
| `http` | `POST` to the agent's `endpoint` + `path` |
| `nats` | NATS request/reply on the agent's subject |

The NATS body is byte-identical to the HTTP body — a test asserts exactly
that, so the two cannot drift. Metadata with no place in the message schema
(`workflow_id`, `step_id`, `kio_id`, `traceparent`) rides in NATS headers
instead of being added to the body.

Subjects default to `kio.{agent}.execute` with the agent id lowercased, and
`dispatch.nats.subject_template` changes the pattern. An agent can also name
its own subject with `subject` in its registry entry.

`endpoint` stays populated in the message even on NATS, because it is part of
the agreed schema.

Delivery uses core NATS request/reply, not JetStream persistence. The
synchronous request/reply shape matches what the executor already does over
HTTP and keeps both transports behaving identically. Durable JetStream
delivery would change the execution model — the reply would arrive out of
band rather than as the return value — and is not implemented.

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

Dispatch spans are CLIENT spans, and KIO1 injects a W3C `traceparent` header
on both transports, so an agent that reads it continues this trace rather than
starting an unrelated one. Agents that ignore the header are unaffected.

## The stub agent

`tools/dummy_agent.py` is a dependency-free stand-in for a real KIO, used to
validate the protocol before any agent exists:

```bash
python tools/dummy_agent.py --agent-id KIO2 --port 8102
```

```bash
python tools/dummy_agent.py --agent-id KIO10 --transport nats
```

In `nats` mode the stub subscribes to the agent's subject and answers with the
same reply object it returns over HTTP, so the two transports can be compared
directly.

Useful flags:

| Flag | Purpose |
|------|---------|
| `--latency` | Delay replies, to exercise timeouts |
| `--fail-capability` | Answer `status: "error"` for a capability, repeatable |
| `--path` | Serve a path other than `/execute` |
| `--transport` | `http` (default) or `nats` |
| `--nats-server` | Broker URL in `nats` mode, repeatable |
| `--subject` | Listen on a subject other than `kio.<agent>.execute` |

It also answers `GET /health`, so reachability can be checked without
sending a workflow step.

## Known gaps

These are deliberate omissions, listed so they are not mistaken for
oversights:

- **No JetStream durability.** NATS delivery is core request/reply, so a
  message is lost if no agent is listening. Durable delivery needs an
  out-of-band reply path and a different execution model.
- **No retries.** A failed dispatch is reported, not retried.
- **The session id is not in the message.** It is carried in trace context and
  in the Session Manager's records, but the execution message schema has no
  field for it and none was added.
- **Nothing verifies what an agent does with `data.upstream`.** KIO1 hands
  results forward; whether an agent reads them is up to that agent.
- **No JetStream consumer on the agent side.** The stub agent's NATS mode is
  a plain subscriber, so it demonstrates the protocol rather than the durable
  delivery a production worker would want.
