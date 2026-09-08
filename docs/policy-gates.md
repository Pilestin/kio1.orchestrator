# Policy gates

A gate says what must have **passed** before a step is allowed to run.

## The problem it solves

Take an ordinary lifecycle plan: generate code, run the tests, run a security
scan, deploy. Both checks report a defect. Here is what happened before gates
existed:

```text
Workflow wf-live02 (mixed): 6 ok, 0 error, 0 not dispatched
  s3 KIO7  code_generation          ok  15 ms  clean
  s4 KIO11 test_automation          ok  13 ms  defect
  s5 KIO12 cybersecurity_validation ok   1 ms  defect
  s6 KIO8  deployment               ok   2 ms  clean
```

The deployment ran. It even *waited* for both checks, because the plan
declared `depends_on: ["s4", "s5"]`. It waited and then ignored what they
said.

That is not a bug in the executor. A dependency describes what a step
**consumes**: the deployment needs the tested artefact, so it waits for the
step that produced it. Whether the tests passed is a different question, and
nothing was asking it.

## What a gate does

```json
"dispatch": {
    "policy": {
        "gates": [
            {
                "before": ["deployment", "packaging"],
                "requires": ["test_automation", "cybersecurity_validation"],
                "verdict": ["clean"]
            }
        ],
        "block_on_review_required": false
    }
}
```

Same plan, same agents, same replies:

```text
Workflow wf-live02 (mixed): 5 ok, 0 error, 1 not dispatched
  s4 KIO11 test_automation          ok       13 ms  defect
  s5 KIO12 cybersecurity_validation ok        2 ms  defect
  s6 KIO8  deployment               not_run  Blocked: deployment requires
           cybersecurity_validation to report one of [clean], and s5 reported 'defect'.
```

| Field | Meaning |
|-------|---------|
| `before` | Capabilities this gate guards. A step asking for any of them is subject to the rule. |
| `requires` | Capabilities that must have run and succeeded first. **Every** step in the plan providing one of these must have passed. |
| `verdict` | Optional. Acceptable verdicts for the required steps. Omit it to check only that they succeeded. |

## Four ways a gate stops a step

1. **A required check reported an unacceptable verdict.** The case above.
2. **A required check failed outright** — the agent was unreachable, timed
   out, or answered `status: "error"`.
3. **A required check reported nothing.** If you asked for `clean` and the
   agent said nothing at all, you do not know that it is clean. That blocks,
   and the message says so.
4. **The plan has no such check.** A plan that deploys without any security
   step is blocked, because the gate is about what must be true, not about
   grading whatever checks happened to be included. This is deliberate: the
   dangerous case is usually a missing scan, not a failing one.

## Gates fix the ordering themselves

A gate is unenforceable if the step it guards runs *alongside* the steps it
requires. So the required steps are added to the guarded step's `depends_on`
before the plan is routed — whether or not the model thought to order them.

You can see this: a plan whose deployment step declares no dependencies at
all still runs after the checks once a gate applies to it.

An added edge that would create a cycle is dropped with a warning, and the
runtime check then blocks the step rather than letting it through.

## Human review

```json
"block_on_review_required": true
```

An agent can set `hitl_required: true` in its output to say it wants a person
to look. By default that is recorded and shown, and the workflow continues —
the behaviour every existing deployment already has.

Set this to `true` and the first such request stops everything after it.
Note there is no way to *approve* and resume yet; the workflow simply ends
with the remaining steps marked `not_run`. That is still better than
proceeding past a step that asked for a human, but it is a stop, not a
handover.

There is one limit worth knowing: steps that share a level genuinely run at
the same time, so a review request cannot stop its own siblings. It stops
everything in later levels.

## Where gates belong

In configuration, not in the planning prompt. Whether a security scan is
mandatory before deployment is a decision of whoever deployed this
orchestrator — it differs between installations, and in a deployment with no
security agent at all the rule should simply not exist. A rule written into
the prompt is a suggestion the model may forget; a gate is checked by code
every time.

## What is not covered

- **Gates are capability-level, not step-level.** You cannot gate one
  particular deployment step and not another.
- **No conditional gates.** "Require a compliance review only for medical
  domains" is not expressible; the model still decides whether to include
  such a step, and a gate only checks what it decided.
- **No approval flow.** A blocked step stays blocked for that run.
