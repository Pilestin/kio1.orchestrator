# Failover

A plan names an agent. What it actually asked for is a **capability**. When
the named agent cannot deliver, another target declaring the same capability
can.

## The problem

The model picks an agent when it plans. By the time the step runs, that
choice can be wrong for reasons the model could not have known — the agent
was withdrawn by [health checks](live-directory.md), or it answered and
failed. Without failover the whole chain stops:

```text
Workflow wf-fo-live (sequential): 0 ok, 1 error, 1 not dispatched
  s1 KIO7  code_generation error    KIO7 is unreachable at http://127.0.0.1:8157/execute
  s2 KIO11 test_automation not_run  An earlier step it depends on did not complete
```

Nothing was wrong with the request. `MyCodeGen` was running the whole time
and declares `code_generation`.

## Turning it on

```json
"dispatch": {
    "failover": {
        "enabled": true,
        "max_attempts": 2
    }
}
```

Same plan, same targets:

```text
Workflow wf-fo-live (sequential): 2 ok, 0 error, 0 not dispatched
  s1 MyCodeGen code_generation ok  5 ms  clean  planned KIO7
  s2 KIO11     test_automation ok  4 ms  defect
```

| Field | Meaning |
|-------|---------|
| `enabled` | Whether a step may run anywhere other than where it was planned. Off by default. |
| `max_attempts` | How many targets one step may be tried against, **including the first**. `2` means one substitution. |

## Who gets tried, in what order

1. **The agent the plan named**, when it exists and declares the capability.
   The model chose it; nothing here second-guesses a working choice.
2. **Every other target declaring the same capability**, in registry order —
   which puts hand-written entries ahead of discovered ones.

Capped at `max_attempts`. A target that does not declare the capability is
never a candidate, so failover cannot send code generation to a test runner.

## Two moments it applies

**Before dispatch.** If the named agent is not in the registry at all — the
usual reason being that health checks withdrew it — the step is reassigned
rather than skipped.

**After a failure.** A step that fails is tried on the next candidate. Both
kinds of failure count: the agent was unreachable, and the agent answered
`status: "error"`. An agent that ran and refused the work is a reason to ask
someone else, the same as one that never answered.

A `verdict` of `defect` is **not** a failure. An agent that inspected
something and reported a problem succeeded at its job; retrying that
elsewhere would be asking until someone says what you want to hear. Use a
[policy gate](policy-gates.md) if a defect should stop the workflow.

## A step that moved never looks like one that did not

Two things record it.

The summary line names the target that ran it and, separately, the one the
plan asked for:

```text
  s1 MyCodeGen code_generation ok  5 ms  clean  planned KIO7
```

And the result carries every attempt, in order:

```text
denemeler: KIO7=error → MyCodeGen=ok
```

`StepResult.attempts` holds one entry per try with its status, duration, and
error. `StepResult.planned_agent_id` is set **only** when the step moved, so
a reader never has to compare two fields to find out whether it did.

## What this costs

A task no one can do is now attempted `max_attempts` times instead of once.
That is the bound: with the default of 2, a plan that fails everywhere costs
one extra call per step, not one per registered agent.

Keep `max_attempts` small. Growing it does not make a broken task succeed; it
makes failure slower.

## Limits

- **No retry of the same agent.** A timeout that would have succeeded on a
  second try is not retried — the step moves to a different target or fails.
- **The session record names who ran it, not who was planned.** The Session
  Manager stores the final `agent_id`, which is truthful, but the attempt
  history and the planned agent do not reach the database. They are in the
  terminal summary and in `StepResult`.
- **Order is static.** Candidates are tried in registry order, not by which
  one has been succeeding lately. Choosing by past success, latency, or
  energy is the next step and the telemetry for it already exists.
- **No re-planning.** Failover substitutes *who* carries out a step. It never
  changes *what* the steps are: a plan that turned out to need a capability
  nobody has still fails at that step.
