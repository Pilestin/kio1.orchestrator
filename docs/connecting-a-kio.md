# Connecting a KIO

This is the whole contract for making a KIO reachable from KIO1. If you own a
KIO and want KIO1 to call it, everything you need is on this page.

## 1. Accept an execution message

Expose one endpoint that accepts `POST` with a JSON body. `/execute` is the
default; any path works if you tell KIO1 which one.

This is what arrives, exactly as defined in the `KIO1 – KIO10 Integration
Strategy and Communication Protocol` document:

```json
{
  "workflow_id": "wf-bug-pay01",
  "step_id": "s2",
  "agent_id": "KIO2",
  "capability": "diagnosis",
  "endpoint": "http://127.0.0.1:8102",
  "task": "Provide detailed diagnostics on root causes",
  "data": {}
}
```

| Field | What to do with it |
|-------|--------------------|
| `workflow_id`, `step_id` | Echo them back. They are how KIO1 matches your reply to the right step. |
| `agent_id` | You. Useful as a sanity check. |
| `capability` | Which of your capabilities is being asked for. |
| `task` | The free-text instruction. |
| `endpoint` | Your own address, as KIO1 knows it. Nothing to do. |
| `data` | Extra structured input. See below. |

### `data`

Two things arrive here, and both may be present at once.

**The code under analysis**, when KIO1 has been told what it is:

```json
"data": {
  "repository": {"path": "/work/repo"},
  "target": {"entry_point": "reproduce.py"}
}
```

These come from `dispatch.workspace` in KIO1's `config.json`. The path is the
one *your* service can see, so in a containerized deployment it is the path
inside your container, not KIO1's. If the workspace is unset, neither key is
sent — a KIO that does not analyse code needs neither.

**Results from the steps you depend on**, keyed by step id:

```json
"data": {
  "upstream": {
    "s1": {"summary": "Located 1 candidate defect.", "findings": [...]}
  }
}
```

Whether you use either is up to you. Ignoring them is valid.

## 2. Reply

```json
{
  "workflow_id": "wf-bug-pay01",
  "step_id": "s2",
  "agent_id": "KIO2",
  "status": "ok",
  "output": {"summary": "...", "anything": "you like"},
  "error": null
}
```

| Field | Rule |
|-------|------|
| `workflow_id`, `step_id` | Echo what you received. A mismatch is logged, not rejected. |
| `status` | `"ok"` or `"error"`. Omitting it is treated as `"ok"`. |
| `output` | Any JSON object. Its shape is yours to define — KIO1 stores it as-is and passes it to dependent steps. Ignored when `status` is `"error"`. |
| `error` | A short reason when `status` is `"error"`. |

Answer with HTTP `200` even when `status` is `"error"`. A non-2xx status is
read as *your service is broken*, which is a different thing from *the task
failed*.

### Saying what you concluded

`status` says whether the call worked. It does not say what you found, and
those are different questions: an agent that inspects code and reports it is
fine has **succeeded**. Reporting that as an error would fail every workflow
run over healthy code.

Two optional fields inside `output` carry the conclusion. KIO1 reads both and
shows them next to the step:

| Field | Values | Meaning |
|-------|--------|---------|
| `verdict` | `"defect"`, `"clean"`, `"inconclusive"` | What you concluded. Anything else is ignored. |
| `hitl_required` | `true` | You want a human to look, usually because you were not confident. Only a real boolean counts. |

```json
{ "status": "ok",
  "output": { "verdict": "clean", "findings": [], "hitl_required": false } }
```

```text
  s1   KIO2   bug_localization   ok   911 ms  defect
  s1   KIO2   bug_localization   ok   995 ms  clean
```

Without `verdict` those two lines are identical. Say it explicitly rather than
leaving a reader to infer it from the length of a list.

Both fields are optional. An agent that sets neither behaves exactly as before
they existed.

> This reply format is **not** in the shared protocol document, which defines
> only the request. It is KIO1's current proposal and still needs consortium
> agreement. Raise it if it does not fit your KIO.

## 3. Get registered

Either KIO1's operator writes you into `config.json`, or you publish a
manifest and they point at it. The manifest is less work for both sides and
cannot drift — see [Discovery](discovery.md) for the four lines it takes.


Add your KIO to `dispatch.agents` in KIO1's `config.json`:

```json
"KIO2": {
    "endpoint": "http://127.0.0.1:8102",
    "path": "/execute",
    "capabilities": ["bug_localization", "diagnosis", "replay", "trace_alignment"]
}
```

`capabilities` must match the values in KIO1's system prompt for your agent. A
step asking for a capability you did not declare is skipped rather than sent,
which is a useful guard while things are being wired up.

Declare only what you actually do. If a capability is registered here but your
service refuses it, KIO1 dispatches the step and records the failure — the
guard above cannot help, because the mismatch is between the config and your
implementation rather than between the plan and the config.

Use `127.0.0.1`, not `localhost`, for anything on the same machine: on Windows
`localhost` tries IPv6 first and costs roughly two seconds per call against two
milliseconds.

## 4. Check it works

Confirm KIO1 can see you at all:

```bash
curl http://127.0.0.1:8102/health
```

Then send yourself a message shaped exactly like a real one:

```bash
curl -X POST http://127.0.0.1:8102/execute -H 'Content-Type: application/json' -d '{"workflow_id":"wf-test","step_id":"s1","agent_id":"KIO2","capability":"diagnosis","endpoint":"http://127.0.0.1:8102","task":"test","data":{}}'
```

Then run the real thing: make sure `dispatch.enabled` is `true` in `config.json`,
start `python main.py`, and ask something your KIO should handle. You will see
a per-step table under the plan:

```text
Workflow wf-bug01-pricing (sequential): 2 ok, 0 error, 0 not dispatched
  s1   KIO2   bug_localization             ok       854 ms
  s2   KIO2   diagnosis                    ok       869 ms
```

If your KIO is not ready yet, `tools/dummy_agent.py` stands in for it and
answers the same contract, so the rest of the chain can be tested without you.

## If you prefer NATS

The message and reply are identical. Instead of an HTTP endpoint, subscribe to
a subject — `kio.<your-agent-id-lowercased>.execute` by default — and answer
the request. `tools/dummy_agent.py --agent-id KIO2 --transport nats` is a
working example to copy from.

Set `dispatch.transport` to `nats` in KIO1's config to switch. Note that this
is core NATS request/reply, not JetStream: nothing is persisted, so your
service must be listening when the message is sent.

## If your work takes longer than one request: the job interface

A KIO that cannot answer inside one HTTP request can speak the KIO1 job
interface (BITNET §3.5) instead. KIO9 does: a compliance review takes 15–50
seconds, and it works through one at a time.

```json
"KIO9": {
    "endpoint": "https://cloud.erarge.com.tr/ai4sweng/kio9",
    "path": "/jobs",
    "protocol": "kio1-jobs",
    "poll": {"path": "/jobs/{job_id}", "interval_s": 2, "timeout_s": 240},
    "data": "{data.upstream.s1.data}",
    "headers": {"Authorization": "Bearer ${KIO9_TOKEN}"},
    "capabilities": ["compliance_review", "contract_specification"]
}
```

What KIO1 does with such an entry:

1. **Posts a job request**, not the execution message: `schema_version`
   `"1.0"`, `workflow_id`, `step_id`, `capability`, `task`, `data`, and an
   `idempotency_key`. `agent_id` and `endpoint` are not sent — the URL names
   the agent.
2. **Polls** `poll.path` every `interval_s` while the status is `accepted`
   (or anything else that is not final), until it is `success`,
   `needs_clarification` or `failure`, or `timeout_s` runs out. That timeout
   is the entry's own; `dispatch.request_timeout` does not apply.
3. **Reads the outcome** into the step:

   | Job | Step |
   |---|---|
   | `success`, `output.gate` `pass` | ok, verdict `clean` |
   | `success`, `output.gate` `block` | ok, verdict `defect` |
   | `success`, `output.gate` `warn` or a value KIO1 does not know | ok, `hitl_required` |
   | `success` with no `gate` (`contract_specification`) | ok |
   | `needs_clarification` | ok, verdict `inconclusive`, `hitl_required` |
   | `failure` | error, naming `failure_class` and the diagnostics message |
   | timed out, unreachable, refused | error |

   A negative review is a successful step with verdict `defect`, not a failed
   one. Nothing short of a final `pass` reads as clean. The job's `output`
   reaches later steps unchanged; the verdict is set beside it, so KIO9's own
   `output.verdict` (`conformant`, `non_conformant`, …) is kept.

**`data`.** Without it, the step's `data` is sent as it is. KIO9 needs a
payload in its own protocol's form — a contract reference, the report with its
`content_hash`, evidence pointers, attestations — which no planner can write.
So it comes from an earlier step, and `data` names where:
`"{data.upstream.s1.data}"` takes the `data` object out of what step `s1`
returned. The KIO9 step must `depends_on` that step; if the placeholder finds
nothing, the step fails saying so instead of sending the placeholder text.

**Ids.** The interface allows `[A-Za-z0-9._-]`, at most 64 characters. KIO1
also appends a digest of the request's content to `step_id`. KIO9 answers a
repeated `workflow_id` + `step_id` with the job it already has, and planners
reuse workflow ids across runs — so without the digest a second report could be
answered with the first one's review. With it, a retry of the same step finds
the same job, and a different request is a new one.

`headers` works on every `kio` entry, job or not; `${NAME}` is read from KIO1's
environment, and an unset one fails the step before anything is sent. A job
entry is reached over HTTP even when `dispatch.transport` is `nats`.

Trying it without ERGTECH: UC2's mock answers `accepted` once and then the
review, from `UC2-portal`:

```bash
python -m kio_mocks.server --port 8109
```

and `UC2-portal/docs/kio9-samples/` has a conformant and a defect request
recorded from the real pipeline.

## Things worth knowing

- **Timeouts.** KIO1 waits `dispatch.request_timeout` seconds (60 by default)
  and then records the step as failed. Long-running work should either finish
  inside that window or use the job interface above, whose entry carries its
  own timeout.
- **Trace context.** Requests carry a W3C `traceparent` header. Reading it
  puts your spans in the same trace as KIO1's; ignoring it costs you nothing
  except that correlation.
- **Parallel calls.** In a plan where several steps are independent, KIO1 may
  call you more than once at the same time. Handle concurrent requests.
- **No retries.** A failed call is reported, not retried. (A job is polled,
  which is not a retry: the job is submitted once.)
- **Work that needs an earlier step's output.** If your capability only makes
  sense after another step ran — replaying a recording, comparing two runs —
  say so, so KIO1's planning prompt can require a `depends_on` and hand you the
  earlier result through `data.upstream`. A capability that silently assumes a
  predecessor will be called first and fail.
