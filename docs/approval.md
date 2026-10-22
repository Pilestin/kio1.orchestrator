# Approving a plan before it runs

Some targets change things: a command that writes files, an API that
deploys. Mark one, and any plan that could reach it waits for a person to
approve it first.

```json
"Deploy": {
    "type": "cli",
    "command": ["./deploy.sh"],
    "capabilities": ["deployment"],
    "approval": true
}
```

The run stops after planning. Its detail in the console shows the plan, with
the marked targets highlighted, and two buttons: **Run plan** and
**Cancel**. Marked targets carry a lock in the Targets list.

"Could reach" is deliberate: with failover on, a step can move to any target
declaring its capability, so a plan naming an unmarked target still waits
when a marked one could end up running the step.

## Settings

```json
"orchestration": {
    "approval": "auto",
    "approval_timeout": 1800
}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `approval` | `auto` | `auto` asks only for plans that could reach a marked target; `always` asks for every plan; `never` never asks. With nothing marked, `auto` behaves exactly as before approval existed. |
| `approval_timeout` | `1800` | Seconds a plan waits. Nobody answering counts as **cancel**: a plan nobody looked at is not a plan anybody approved. |

## What happens when it is not approved

| When | Result |
|------|--------|
| The first plan is cancelled or times out | The run ends as `failed` — "The plan was not approved." — and no step runs. |
| A follow-up plan (round 2 or later) is cancelled | The rounds stop; the answer from the last review stands. |

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /workflow/{session_id}/approval` | `waiting: false`, or the plan waiting, the marked targets in it, and when the wait ends. |
| `POST /workflow/{session_id}/approval` | `{"approve": true}` or `false`. Requires the administration token: approving lets a plan run. `409` when nothing is waiting. |

Pending approvals are held in memory: a restart forgets them, and the runs
waiting on them end.
