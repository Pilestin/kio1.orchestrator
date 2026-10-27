# kio-mocks — stand-in KIOs that do their job

Stand-ins for the AI4SWEng KIOs that are not available yet, for running and
testing KIO1 end to end. Each one **does the work its KIO is defined to do in
D2.6** — on a small scale, without a model — and speaks the same protocol as
the real services, so KIO1 cannot tell the difference and a real KIO replaces
one by changing an address.

They are not part of KIO1: nothing here imports KIO1, and KIO1 imports
nothing from here (only its tests reach in). Standard library only; KIO11
and KIO7 run the project's tests with `pytest` where the project has it.

```bash
cd kio-mocks
python -m kio_mocks                    # every KIO written so far, on 81NN
python -m kio_mocks --only KIO2 KIO11  # some
python -m kio_mocks --print-config     # the dispatch.agents block for KIO1
```

Then point KIO1's workspace at the code to work on (Settings → Advanced →
Workspace). `sample/` is a small shop with one real defect to try it on.

Or do both from KIO1's console: **Agents → Start stand-ins**, then **Use the
sample project**. KIO1 runs this same command for you and stops it when it
stops.

## What each one does

| KIO | Capabilities | What it really does |
|---|---|---|
| KIO2 | `bug_localization`, `diagnosis`, `replay`, `trace_alignment` | Records the failing test under `sys.settrace`, names the line that raised and the values it saw, and slices back to where they came from. Replays and aligns recordings. **Does not fix** (D2.6: that is KIO7's step). |
| KIO7 | `code_generation`, `code_understanding` | Explains modules and flags risky code. Proposes a fix from KIO2's diagnosis and runs the tests on a copy; changes the workspace only with `arguments.apply: true`. Writes code skeletons with tests from KIO4's components. |
| KIO10 | `energy_efficiency`, `tinyml` | Measures the CPU time and energy of the tests (or entry point), and of KIO7's fix to compare. Places a model on an edge board within its limits, falling back to one that fits (simulated). |
| KIO11 | `test_automation`, `validation` | Runs the tests and reports what failed and where. Generates and runs edge-value tests for functions, to confirm a failure without being told where it is. Checks acceptance criteria against the tests. |

KIO3, KIO4, KIO5, KIO6, KIO8, KIO9, KIO12 and KIO13 follow; the plan is in
`../docs/plans/kio-mocks.md`.

Every output carries `"simulator": "kio-mocks"`, so no run's history
mistakes a stand-in's result for the real KIO's.

## The protocol

`GET /health`, `/manifest`, `/tasks`, `/schema`; `POST /execute` with the
KIO1–KIO10 execution message. A failed task is `status: "error"` inside HTTP
200; an unoffered capability is refused by name; work is bounded by a time
budget (`KIO_MOCKS_BUDGET`, 45 s). A KIO that answers through the job
interface (BITNET §3.5, as KIO9 does) also serves `POST /jobs` and
`GET /jobs/{id}`.

| Variable | Default | What it does |
|---|---|---|
| `KIO_MOCKS_BUDGET` | `45` | Seconds a direct answer may take |
| `KIO_MOCKS_TRACES` | temp folder | Where KIO2 keeps recordings |
| `KIO_MOCKS_CPU_WATTS` | `15` | The CPU power KIO10 converts CPU time with |
| `KIO_MOCKS_ENERGY_RUNS` | `3` | How many times KIO10 runs a workload |

## Tests

```bash
python -m pytest -q kio-mocks/tests     # the stand-ins themselves
python -m pytest -q tests/test_kio_mocks.py   # KIO1 driving them over HTTP
```
