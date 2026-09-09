# Use-Case 1, end to end

```bash
python examples/uc1/run.py
```

Twenty-five seconds later you have a project that did not exist, built by ten
agents KIO1 discovered by asking them, with a real bug found in a real
traceback and a release that was refused until it was fixed.

```text
UC-UC1-01  Customer request to draft issue
  s1 KIO3 requirements_engineering ok  1 ms  clean
  s2 KIO3 acceptance_criteria      ok  1 ms  clean

UC-UC1-02  Code and test generation
  s2 KIO7 code_generation          ok  3 ms  clean     ← writes pricing.py
  ...

  (refused)  Release attempt on a red suite
  s3 KIO11 test_automation  ok  7428 ms  defect
  s6 KIO8  deployment       not_run  Blocked: deployment requires
       test_automation to report one of [clean], and s3 reported 'defect'.

UC-UC1-03  Bug localisation, fix, verification
  s1 KIO11 test_automation   ok  5816 ms  defect
  s2 KIO2  bug_localization  ok    13 ms  defect
  s3 KIO2  diagnosis         ok     2 ms  defect  review required
  s4 KIO7  fix_generation    ok     3 ms  clean
  s5 KIO11 test_automation   ok  4952 ms  clean     ← the same suite, now green

UC-UC1-04  End-to-end delivery with gates
  s6 KIO8  deployment  ok  14 ms  clean

Use-Case 1 delivered end to end.
  workspace holds: pricing.py, test_pricing.py
  released:        pricing-1.0.0.zip
```

## What is real here

This is not `tools/dummy_fleet.py`. Those agents answer with canned results,
which is right for proving the protocol and useless for showing a lifecycle:
nothing they "produce" exists, so nothing downstream can consume it.

Here:

| Agent | What it actually does |
|-------|----------------------|
| KIO3 | Reads the customer request and derives requirements from its numbers |
| KIO4 | Names the modules to build |
| KIO7 | **Writes `pricing.py` and `test_pricing.py` to disk** |
| KIO6 | Reads the written source and checks each requirement is traceable |
| KIO11 | **Runs `pytest` in the workspace** and reports what failed |
| KIO2 | **Parses the traceback** and names the file and line |
| KIO7 | **Rewrites the file** with the fix |
| KIO12 | Greps the real source for patterns nobody should ship |
| KIO9 | Checks whether personal data reached the pricing path |
| KIO10 | **Times 40,000 real invocations** |
| KIO8 | **Builds a zip** and copies it to a staging directory |

None of them uses a model — they are deterministic — but every artefact is
real. That is what makes UC-UC1-03 mean something: the failing test is a test
that genuinely fails, and the line KIO2 names is a line that genuinely exists.

The defect is planted on purpose in what KIO7 generates. `order_total`
divides by the item count to report an average, so it works for every cart
with items in it and raises `ZeroDivisionError` on an empty one — the kind of
thing that passes review and fails in production.

## What it exercises on the KIO1 side

Nothing is stubbed at the orchestrator. `dispatch.agents` is **empty**: every
agent is found through [discovery](../../docs/discovery.md) by fetching its
`/manifest`, which is the path an external deployment takes.

- [The live directory](../../docs/live-directory.md) — health probing and the
  catalog the planner is shown
- [Policy gates](../../docs/policy-gates.md) — the release refused on a red suite
- [Failover](../../docs/failover.md) and
  [selection](../../docs/selection.md) — enabled throughout
- The whole dispatch layer: dependency levels, upstream hand-off, verdicts,
  and `hitl_required`

## The four scenarios

They are the sequences written in D2.6's use-case tables, in the same order
and with the same KIOs:

| | |
|---|---|
| **UC-UC1-01** | A natural-language customer request becomes structured requirements, acceptance criteria, and a draft issue |
| **UC-UC1-02** | The approved issue becomes code, tests, traceability, a security scan, and a compliance check |
| **UC-UC1-03** | A failing test is confirmed, localised, diagnosed, patched, re-verified, and measured |
| **UC-UC1-04** | Final checks, packaging, and release — gated on the checks passing |

The release attempt between 02 and 03 is not in the deliverable. It is there
because a gate that never refuses anything proves nothing.

## Why the plans are written down

Each plan is the sequence D2.6 specifies, encoded rather than asked of a
model. The point of this example is KIO1's dispatch, gates, failover and
selection — and a run that only works when a model happens to be installed
*and* happens to plan correctly proves nothing about any of them.

The planner is exercised by `main.py` and the Workflow API, and it is tested
separately.

## Options

```bash
python examples/uc1/run.py --keep       # leave the workspace to read
python examples/uc1/run.py --workspace ./uc1-run   # build somewhere specific
python examples/uc1/run.py --verbose    # show what each agent is doing
```

It exits non-zero when a scenario does not deliver, so it is usable in CI.
Every expectation it checks is also a test:

```bash
python -m pytest tests/test_uc1_example.py -v
```

## Driving it from the console

`run.py` drives the dispatch layer directly, which is the right shape for a
test. To drive the same agents from a browser instead:

```bash
python examples/uc1/serve.py
```

That stands up the fleet, a Session Manager, the Workflow API, and the
[console](../../docs/console.md) at `http://127.0.0.1:8000/`. The banner
prints the registry token to paste in.

Requests are planned by a **scripted planner** that matches on what you type,
so it works with no model installed. Type these in order:

| Type this | Runs |
|-----------|------|
| `…requirements` | UC-UC1-01 |
| `…code…` | UC-UC1-02 |
| `Release it to staging` | UC-UC1-04 — **refused**, the suite is red |
| `Fix the failing test` | UC-UC1-03 |
| `Release it to staging` | UC-UC1-04 — now it goes out |

Recent runs then reads:

```text
COMPLETED  wf-uc1-04  Release it to staging
COMPLETED  wf-uc1-03  Fix the failing test
FAILED     wf-uc1-04  Release it to staging      ← the gate refused it
COMPLETED  wf-uc1-02  Generate the code and tests for the approved issue
COMPLETED  wf-uc1-01  Customers want bulk pricing discounts, write...
```

`--provider` plans with whatever `config.json` configures instead of the
script. `--workspace DIR` builds somewhere you can read afterwards.
