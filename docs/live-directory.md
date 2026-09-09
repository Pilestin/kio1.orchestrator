# The live directory

Discovery at startup answers "what existed when we booted". That is the wrong
question for something that runs for days. Agents come up after KIO1 does, go
away while it is running, and change what they offer without anyone
restarting anything.

The directory holds the answer to **what can we reach now**. Three things
keep it current, and one rule keeps it safe.

## What changes

Every workflow takes one **snapshot** of the directory and uses it for both
the catalog the model sees and the routing that follows. Planning against one
set of agents and dispatching against another would produce steps addressed
to agents that had just gone away, so the snapshot is fixed for the length of
a run even if the directory moves underneath it.

In the terminal application the snapshot is taken per turn. In the Workflow
API it is taken per request.

## 1. Probing: an agent that goes away stops being offered

```json
"dispatch": {
    "health": {
        "enabled": true,
        "interval": 30,
        "timeout": 3,
        "failures_before_unreachable": 2,
        "path": "/health"
    }
}
```

Each pass probes every target that *can* be probed:

| Kind | How it is probed |
|------|------------------|
| `kio` | `GET <endpoint><path>` — `/health` by default |
| `http` | `GET` its `health_url`, **only if the entry names one** |
| `cli` | The program is looked for on this machine |

A REST API with no `health_url` is left alone and marked `unchecked`. An
arbitrary API has no liveness path KIO1 may assume, and inventing one would
withdraw a working tool for answering 404 to a URL nobody promised.

After `failures_before_unreachable` consecutive failures the target is
**withdrawn**: it disappears from the snapshot, so the planner is no longer
told it exists. It is not deleted — the record stays, with the reason, and
one successful probe brings it straight back.

The threshold is above one on purpose. A single blip should not remove an
agent in the middle of someone's conversation.

## 2. Refresh: an agent that starts later joins on its own

Every pass also re-reads the [manifest sources](discovery.md). An agent that
was not running at startup joins as soon as it is; one that gained a
capability has it picked up without a restart.

A refresh never discards what probing learned. An agent whose manifest still
answers while its health check fails keeps accumulating failures and is
withdrawn normally.

## 3. Registration: an agent can announce itself

The Workflow API exposes a small registry API so an agent can push its own
manifest instead of waiting to be asked.

```bash
curl -X POST http://localhost:8000/registry/agents \
  -H "Authorization: Bearer $KIO1_REGISTRY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"manifest": {"agent_id": "KIO7", "endpoint": "http://10.0.0.5:8107",
                    "description": "Generates and explains code.",
                    "capabilities": ["code_generation"]}}'
```

| Endpoint | What it does |
|----------|--------------|
| `GET /registry/agents` | Every record, including withdrawn ones, with the reason each is where it is |
| `POST /registry/agents` | Adds or updates targets from a manifest, in any shape discovery accepts |
| `DELETE /registry/agents/{agent_id}` | Removes a discovered or registered target |

**Registering is a handshake, not an assertion.** The target is probed before
it is trusted, and the result comes back in the response:

```json
{"agents": [{"agent_id": "KIO7", "status": "ready", "origin": "registration", ...}]}
```

An agent that did not answer is recorded as `unreachable` rather than
rejected, so a slow starter joins on the next health pass instead of having
to post again.

### The registry API is closed by default

Registering an agent means task text and upstream results start flowing to an
address someone supplied over the network. That is not a thing to leave open.

All three endpoints return **503** until `KIO1_REGISTRY_TOKEN` is set, and
then require `Authorization: Bearer <token>`. The 503 body names the variable,
so nobody has to guess why it is closed.

This is a shared secret over whatever transport you terminate — put the API
behind TLS if it is reachable from anywhere you do not control.

## The rule: configuration always wins

A record from `config.json` is never overwritten by a manifest or a
registration, and `DELETE` refuses it with a 409.

Two reasons. It is explicit intent — if you wrote it down, you meant it. And
it is where a deployment records an address only it can know: the host an
agent answers on from *inside a container* is not something the agent can
tell you.

To change a configured agent, change the file and restart. Everything else is
live.

## Seeing the state

```bash
curl -s localhost:8000/registry/agents -H "Authorization: Bearer $KIO1_REGISTRY_TOKEN" | jq
```

Each record carries `status`, `origin`, `failures`, `last_checked` and
`last_error`, which together answer "why is this agent not being planned
for", plus a `history` of what it has recently done per capability (see
[Choosing by history](selection.md)). The terminal application prints a one-line version at startup:

```text
Targets: 13 available, 1 not answering (KIO5)
```

## What this costs

- **Startup does one synchronous pass.** The first plan is built against a
  checked directory rather than an optimistic one. With probes running
  concurrently this is roughly one timeout, not one per agent.
- **A background thread.** One, daemon, stopped on shutdown.
- **Traffic.** One probe per target per interval, plus one fetch per manifest
  source. At the default 30 seconds this is negligible; do not set it to 1.

Both probing and refreshing are off unless configured, so a deployment that
lists every agent by hand and never turns them on behaves exactly as it did
before the directory existed.

## Still missing

- **No capability-level verification.** A probe proves the agent answers, not
  that it really does what its manifest claims.
- **A withdrawn agent is routed around, not re-planned.** A step whose agent
  went away is reassigned to another target declaring the same capability
  (see [Failover](failover.md)). If nothing else declares it, the step still
  fails — the plan itself is never rewritten.
- **Registration is push-once.** An agent that registers and then dies is
  withdrawn by probing, but nothing tells it to re-register; it has to
  either be probeable or post again.
