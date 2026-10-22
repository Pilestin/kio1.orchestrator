# Reviewing a run and answering

A run no longer ends when its last step does. After the plan is carried out,
the model reads what every step returned and decides:

- **done**: the request is answered, or nothing registered can do the rest.
  It writes the answer for the person, in the language of the request,
  naming the target each fact came from: `Python 3.11.7 is installed [Deneme].`
- **continue**: part of the request is unanswered and a registered target
  could do it. KIO1 plans again for only that work, carries it out, and
  reviews again.

```
plan → carry out → review ─ done ─────────────→ answer
                     │
                     └ continue → plan the rest → carry out → review → …
```

The answer is saved with the run and shown at the top of its detail in the
console; `GET /workflow/{session_id}` returns it as `session.answer`.

## Configuration

```json
"orchestration": {
    "review": true,
    "max_rounds": 2,
    "review_prompt_path": "prompts/kio1_review_prompt.txt"
}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `review` | `true` | Off runs one plan and stops, with no answer — the behaviour before reviews existed. |
| `max_rounds` | `2` | Plans per run, the first included, from 1 to 10. The last review is told it must answer. |
| `review_prompt_path` | `prompts/kio1_review_prompt.txt` | The reviewer's system prompt. If it cannot be read, runs are not reviewed. |
| `review_provider` | `""` | The provider that reviews and answers. Empty uses the planning provider. |
| `review_model` | `""` | The model that reviews and answers. Empty uses the planning model; required with `review_provider`. |

Both can be set on the console's Settings tab, under **Answer model**,
without a restart. A key pasted there is tried before it is saved to `.env`.

Each round costs a model call to plan and one to review. With a local model
on a CPU that is 30–90 seconds each, so the default allows one follow-up.

## Follow-up requests

A request sent with a `conversation_id` continues that conversation. The
planner and the reviewer then read, before the request, the last
three runs of the conversation — each request, its answer (cut at 1 500
characters), and what the person clarified — at most 4 000 characters,
oldest dropped first:

```
Earlier in this conversation (context only):
- Request: This crashes on an empty list: def average(values): ...
  Answer: len([]) is 0, so it divides by zero [CodeReviewer]

Request now: Now add type hints.
```

The request comes last on purpose. With it first and the context after,
`ministral-3:3b` planned the previous request again; with it last, it planned
the new one and copied the function into the task. The limits are
`CONTEXT_TURNS` and `CONTEXT_MAX_CHARS` in `services/workflow_api/runner.py`.
If the conversation cannot be read, the request runs without it.

## What can go wrong, and what happens

| Situation | Result |
|-----------|--------|
| The review is not valid JSON, has no answer, or times out | The run still completes. The answer is written from the step results instead, one line per step, and the activity log marks it as a summary. |
| A follow-up plan cannot be made or carried out | The rounds stop; the answer from the last review stands. |
| The reviewer asks to continue on the last round | Treated as done. |
| The reviewer asks to continue without saying what is missing | Treated as done. |

Steps of later rounds are recorded with a prefix — `r2-s1`, `r2-s2` — so they
never overwrite the first round's. A run's status follows its **last** round:
a failure that a follow-up put right does not fail the run.

## Model quality

The loop is only as good as the model reading the results, and the two jobs
need different things: planning is a structured, repeated task a small model
manages; reading results and answering is where a larger one pays off. So
the answer model can differ from the planning model.

Measured on one request ("show the latest commits and the Python version"),
both local, CPU only:

| Planning | Answering | Answer | Time |
|----------|-----------|--------|------|
| `ministral-3:3b` | same | Older commits given as "the latest"; a count it had not counted | ~46 s |
| `ministral-3:3b` | `qwen3.5:4b` | The three latest commits, verbatim and cited; the right Python version | ~101 s |

A hosted model through OpenRouter answers better still, in seconds.

---

See also: [Workflow Dispatch](workflow-dispatch.md) for how a plan is carried
out, and [The console](console.md) for the live view.
