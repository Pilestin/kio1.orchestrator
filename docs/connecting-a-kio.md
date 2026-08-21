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

> This reply format is **not** in the shared protocol document, which defines
> only the request. It is KIO1's current proposal and still needs consortium
> agreement. Raise it if it does not fit your KIO.

## 3. Get registered

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

Then run the real thing: set `dispatch.enabled` to `true` in `config.json`,
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

## Things worth knowing

- **Timeouts.** KIO1 waits `dispatch.request_timeout` seconds (60 by default)
  and then records the step as failed. Long-running work should either finish
  inside that window or the timeout should be raised for everyone.
- **Trace context.** Requests carry a W3C `traceparent` header. Reading it
  puts your spans in the same trace as KIO1's; ignoring it costs you nothing
  except that correlation.
- **Parallel calls.** In a plan where several steps are independent, KIO1 may
  call you more than once at the same time. Handle concurrent requests.
- **No retries.** A failed call is reported, not retried.
