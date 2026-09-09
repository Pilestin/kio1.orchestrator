# The console

A web page for looking at what KIO1 can reach and what it has been doing,
served by the Workflow API itself at `/`.

```bash
make services
# then open http://localhost:8000/
```

## What it shows

**Targets.** Everything the [live directory](live-directory.md) knows: each
one's kind, where it came from, whether it is answering, what it declares,
and what it has recently done per capability.

```text
Id           Kind  Origin        Status        Capabilities              Recent
KIO3         kio   config        READY         requirements_engineering  1/1 · rate 0.667 · 23 ms
KIO7         kio   config        READY         code_generation           1/1 · rate 0.667 · 2 ms
KIO5         kio   config        UNREACHABLE   synthetic_data_generation —
             http://127.0.0.1:8105/health is unreachable: timed out
Pytest       cli   config        READY         test_execution            —
MyLogSearch  http  registration  UNCHECKED     log_search                —      [Remove]
```

Two things worth reading off that: `KIO5` says *why* it is not being planned
for, and `MyLogSearch` is `unchecked` rather than `ready` because it is an
HTTP tool with no `health_url` — nothing was probed, so nothing is claimed.

**Registering a target.** Paste the same manifest an agent would serve at
`/manifest`. It is probed before it is trusted, and the result comes straight
back: `MyAgent → ready`, or `MyAgent → unreachable` with the reason. Targets
that came from `config.json` show *in config.json* instead of a Remove
button, because the file is their source of truth.

**Workflows.** A box to send a request, the recent runs, and — for whichever
run you click — its steps with status, duration, error, and the agent's
output.

The page refreshes every four seconds, so a running workflow fills in as it
goes.

## The registry token

The [registry endpoints are closed](live-directory.md#the-registry-api-is-closed-by-default)
until `KIO1_REGISTRY_TOKEN` is set on the server. The console asks for it
with the **Registry token** button and keeps it in `sessionStorage` — this
browser tab only, gone when the tab closes, never written to disk and never
sent anywhere but this service.

Without a token the Targets tab shows the server's own explanation of what to
set. Running workflows does not need one.

## What it is not

It is one HTML file with no build step, no framework, and **no external
requests** — no CDN, no font host, no analytics. It talks only to the service
that served it, so it works on a machine with no route to the internet, which
is where an orchestrator like this usually lives. There is a test that keeps
it that way.

Everything a viewer sees that someone else wrote — an agent's description, an
error message, an output payload — is escaped before it reaches the page.

## Endpoints it uses

All of them are ordinary API endpoints; the console is just one caller.

| Endpoint | Purpose |
|----------|---------|
| `GET /registry/agents` | Targets, their status, and their history |
| `POST /registry/agents` | Register from a manifest |
| `DELETE /registry/agents/{id}` | Remove a discovered or registered target |
| `POST /workflow/run` | Start a run |
| `GET /workflows?limit=N` | Recent runs, newest first |
| `GET /workflow/{session_id}` | One run with its steps |

`GET /workflows` answers with an empty list when session recording is off —
a console asking what has run wants an empty answer when the answer is
nothing, not an error. It answers `503` when the Session Manager is
configured but unreachable, which is a different thing.

## Not here yet

- **No approval.** A workflow stopped by `block_on_review_required` shows as
  stopped, and there is no button to approve it and carry on. That needs the
  executor to be able to resume a plan part-way through, which it cannot do
  yet — see [Policy gates](policy-gates.md).
- **No lineage view.** The Session Manager records which artifact fed which
  step; the console does not draw it.
- **No authentication for running workflows.** Only the registry endpoints
  are token-gated. Anyone who can reach the service can start a run.
- **Polling, not streaming.** Four seconds is fine for a handful of
  operators and wrong for a wall display.
