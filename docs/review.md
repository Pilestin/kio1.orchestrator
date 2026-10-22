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

Each round costs a model call to plan and one to review. With a local model
on a CPU that is 30–90 seconds each, so the default allows one follow-up.

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

The loop is only as good as the model reading the results. In a live test,
`ministral-3:3b` (local, CPU) answered in the right language and cited its
sources, but picked older commits as "the latest" and stated a count it had
not counted. A larger model — locally, or through OpenRouter on the Settings
tab — reads results far more reliably.

---

See also: [Workflow Dispatch](workflow-dispatch.md) for how a plan is carried
out, and [The console](console.md) for the live view.
