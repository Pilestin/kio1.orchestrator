# Discovery

An agent can describe itself. KIO1 asks at startup, and what comes back
reaches both the routing table and the planning prompt.

## Why

Writing an agent's capabilities into `config.json` means two people have to
agree and then stay in agreement: whoever wrote the agent, and whoever wrote
the configuration. When they drift, nothing complains — the model plans for a
capability the agent no longer has, or never plans for one it gained.

The agent is the one that actually knows. So it says.

## Turning it on

```json
"dispatch": {
    "discovery": {
        "sources": [
            "http://127.0.0.1:8103/manifest",
            {
                "url": "https://registry.internal/agents",
                "headers": {"Authorization": "Bearer ${REGISTRY_TOKEN}"}
            }
        ],
        "timeout": 5,
        "required": false
    }
}
```

| Field | Meaning |
|-------|---------|
| `sources` | URLs to GET. A bare string is a URL; an object may add `headers`. Empty disables discovery entirely. |
| `timeout` | Seconds to wait for one source. Sources are fetched concurrently, so one slow agent does not add its wait to every other. |
| `required` | Whether an unreachable source stops startup. Defaults to `false`. |

A source that is down logs a warning and is skipped. An orchestrator that
refuses to start because one optional agent is offline is worse than one that
starts without it — set `required: true` only when a source really is
load-bearing.

## What an agent publishes

Answer a `GET` with JSON. The smallest useful manifest:

```json
{
  "agent_id": "KIO7",
  "description": "Generates and explains code.",
  "capabilities": ["code_generation", "code_understanding"]
}
```

That is enough. `endpoint` is missing on purpose: KIO1 defaults it to the
address it just fetched the manifest from, which is the one address a
manifest cannot get wrong. `path` defaults to `/execute`.

Every field of a `dispatch.agents` entry is accepted here, so an `http` or
`cli` entry can be published the same way — see
[Connecting your own module](connecting-a-tool.md).

**Four shapes are accepted**, so nobody has to guess which one was wanted:

```json
{"agent_id": "KIO7", "capabilities": ["code_generation"]}

[{"agent_id": "KIO7", ...}, {"agent_id": "KIO11", ...}]

{"agents": [{"agent_id": "KIO7", ...}]}

{"agents": {"KIO7": {"capabilities": ["code_generation"]}}}
```

The last is exactly what `config.json` already looks like, so a deployment
can publish the block it already maintains.

`tools/dummy_agent.py` serves `/manifest`, so the whole path can be exercised
before any real agent does.

## Configuration wins

A hand-written entry is never overridden by a manifest. Two reasons:

1. It is explicit intent. If you wrote it down, you meant it.
2. It is where a deployment records an address only it can know — the host an
   agent answers on from *inside a container*, say. The agent cannot know
   that; you do.

Discovery therefore **adds** agents and never rewrites one. A discovered
agent whose id is already configured is logged and dropped.

A manifest entry that does not validate — a `cli` entry with no `command`,
say — is dropped with a warning rather than raised on. One agent publishing
nonsense must not stop KIO1 from starting without it.

## What it looks like

Starting from a `config.json` that lists **one local tool and no KIOs at
all**, pointed at five manifest URLs of which one is dead:

```text
=== configured by hand ===
Pytest

=== after discovery ===
KIO11, KIO3, KIO4, KIO7, Pytest

=== the catalog the model sees ===
- **Pytest** _(local command)_ — Runs this repository's own test suite.
  - capabilities: `test_execution`
- **KIO3** — Requirements, user stories, acceptance criteria.
  - capabilities: `acceptance_criteria`, `requirements_engineering`, `user_stories`
- **KIO4** — Software architecture planning and component design.
  - capabilities: `architecture_design`, `component_design`
...
```

and a plan then dispatches to agents that appear nowhere in the file:

```text
Workflow wf-disc01 (sequential): 3 ok, 0 error, 0 not dispatched
  s1 KIO3   requirements_engineering ok       16 ms  clean
  s2 KIO4   architecture_design      ok       14 ms  clean
  s3 Pytest test_execution           ok    12015 ms
```

## Startup is only the beginning

Everything above describes one fetch. On its own that would answer "what
existed when we booted", which goes stale the moment an agent restarts.

Sources are re-read on an interval, agents are probed so one that went away
stops being offered, and an agent can push its own manifest to a running
orchestrator instead of waiting to be asked. See
[The live directory](live-directory.md).

## Limits

- **Reachable at fetch time is the only verification.** A manifest that
  parses and an agent that answers a probe are both trusted; nothing confirms
  the agent really does what its capabilities claim.
- **One manifest per source.** A source that answers with several agents is
  fine, but there is no pagination for a registry with hundreds.
