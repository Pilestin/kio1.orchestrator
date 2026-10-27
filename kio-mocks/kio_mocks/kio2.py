"""KIO2 — bug localisation by reverse execution and dynamic slicing.

D2.6 §4.1.2: "debugging, bug identification, localisation". Its requirements
are a trace recorder (FR-KIO2-07), replay and comparison of traces
(FR-KIO2-02/03), and fault localisation from traces and slice context
(FR-KIO2-05). It **does not fix bugs**: in UC-UC1-03 KIO2 localises and KIO7
proposes the patch, so `fix_recommendation` is refused with that reason, as
the real KIO2 refuses it.

What to run is found in this order: `arguments.test` or `arguments.target`, a
test id in the task, a failing test an earlier step (KIO11) reported, the
workspace's entry point, and last the first test that fails.
"""

from __future__ import annotations

from typing import Any

from kio_mocks import tracing
from kio_mocks.errors import KioError
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.pytest_runner import NODE_ID, first_failing_test


def _target(request: Request) -> tuple[str, str]:
    """(mode, target): what to record for this request."""
    chosen = str(
        request.arguments.get("test") or request.arguments.get("target") or ""
    ).strip()
    if not chosen:
        found = NODE_ID.search(request.task)
        chosen = found.group(0) if found else ""

    if not chosen:
        failed = request.find_upstream("failed")
        if isinstance(failed, list) and failed and isinstance(failed[0], dict):
            chosen = str(failed[0].get("test") or "")

    if chosen:
        return (
            (
                "pytest"
                if "::" in chosen or "/test" in chosen or chosen.startswith("test")
                else "script"
            ),
            chosen,
        )

    if request.workspace.entry_point:
        return "script", request.workspace.entry_point

    failing = first_failing_test(request.workspace)
    if failing is None:
        raise KioError(
            "Nothing fails here: every test passes and no entry point is set to run."
        )
    return "pytest", failing


def _record(request: Request) -> tuple[tracing.Trace, dict[str, Any]]:
    mode, target = _target(request)
    trace = tracing.record(request.workspace, mode, target)
    return trace, tracing.localise(request.workspace, trace)


def _upstream_trace(request: Request) -> tracing.Trace | None:
    ref = request.arguments.get("trace_ref") or request.find_upstream("trace_ref")
    return tracing.load(ref) if ref else None


def _clean(trace: tracing.Trace) -> dict[str, Any]:
    target = trace.data["target"]
    return {
        "summary": f"Ran {target}: it did not fail, so there is nothing to localise.",
        "verdict": "clean",
        "trace_ref": trace.ref,
        "target": target,
        "findings": [],
    }


def _findings(found: dict[str, Any]) -> list[dict[str, Any]]:
    """The raising line first, then the lines that set the values it used."""
    fault = found["fault"]
    findings = [
        {
            "file": fault["file"],
            "line": fault["line"],
            "function": fault["function"],
            "code": fault["code"],
            "confidence": 0.9,
            "reason": f"{found['exception'].get('type')} is raised here.",
        }
    ]

    for entry in found["slice"]:
        if (entry["file"], entry["line"]) == (
            fault["file"],
            fault["line"],
        ) or not entry["defines"]:
            continue
        values = ", ".join(
            f"{name} = {value}" for name, value in entry["defines"].items()
        )
        findings.append(
            {
                "file": entry["file"],
                "line": entry["line"],
                "function": entry["function"],
                "code": entry["code"],
                "confidence": 0.5,
                "reason": f"Sets {values}, which the failing line uses.",
            }
        )

    return findings


def bug_localization(request: Request) -> dict[str, Any]:
    trace, found = _record(request)
    if not found["failed"]:
        return _clean(trace)

    fault, error = found["fault"], found["exception"]
    values = ", ".join(f"{name} = {value}" for name, value in fault["values"].items())

    return {
        "summary": (
            f"{error.get('type')} raised at {fault['file']}:{fault['line']} in {fault['function']}: "
            f"`{fault['code']}`" + (f" with {values}." if values else ".")
        ),
        "verdict": "defect",
        "trace_ref": trace.ref,
        "target": trace.data["target"],
        "exception": error,
        "findings": _findings(found),
        "stack": found["stack"],
        "slice_context": found["slice"],
    }


def diagnosis(request: Request) -> dict[str, Any]:
    trace = _upstream_trace(request)
    if trace is not None:
        found = tracing.localise(request.workspace, trace)
    else:
        trace, found = _record(request)

    if not found["failed"]:
        return _clean(trace)

    fault, error = found["fault"], found["exception"]
    origins = []
    for name, value in fault["values"].items():
        setter = next(
            (entry for entry in found["slice"] if name in entry["defines"]), None
        )
        if setter is not None:
            origins.append(
                f"{name} is {value}, set at {setter['file']}:{setter['line']} (`{setter['code']}`)"
            )

    return {
        "summary": f"Root cause at {fault['file']}:{fault['line']}: {error.get('type')} — {error.get('message')}.",
        "verdict": "defect",
        "root_cause": (
            f"`{fault['code']}` raises {error.get('type')} ({error.get('message')})"
            + (" because " + "; ".join(origins) + "." if origins else ".")
        ),
        "trace_ref": trace.ref,
        "exception": error,
        "fault": fault,
        "findings": _findings(found),
        "stack": found["stack"],
        # What KIO7 needs to propose a fix: the lines and the values that
        # led to the failure, nothing it would have to re-run to learn.
        "slice_context": found["slice"],
        "next": "KIO7 code_generation can propose a fix from slice_context; KIO2 does not fix.",
    }


def replay(request: Request) -> dict[str, Any]:
    trace = _upstream_trace(request)
    if trace is None:
        trace, _ = _record(request)

    start = request.arguments.get("from_step")
    steps = tracing.replay(
        request.workspace, trace, int(start) if start is not None else None
    )

    return {
        "summary": f"{len(steps)} recorded steps of {trace.data['target']}"
        + (", up to the failure." if trace.failed and start is None else "."),
        "verdict": "defect" if trace.failed else "clean",
        "trace_ref": trace.ref,
        "steps": steps,
        "recorded_steps": len(trace.events),
        "truncated": trace.data.get("truncated", False),
    }


def _a_passing_neighbour(request: Request, failing: str) -> str | None:
    """Another test in the failing test's file, to compare a good run with."""
    file = failing.split("::")[0]
    try:
        source = request.workspace.read(file)
    except KioError:
        return None
    import ast

    names = [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test")
        and node.name != failing.split("::")[-1]
    ]
    return f"{file}::{names[-1]}" if names else None


def trace_alignment(request: Request) -> dict[str, Any]:
    refs = request.arguments.get("trace_refs")
    if not isinstance(refs, list):
        refs = [
            output["trace_ref"]
            for output in request.upstream_outputs()
            if isinstance(output.get("trace_ref"), str)
        ]

    if len(refs) >= 2:
        first, second = tracing.load(refs[0]), tracing.load(refs[1])
    else:
        first = tracing.load(refs[0]) if refs else _record(request)[0]
        neighbour = (
            _a_passing_neighbour(request, first.data["target"])
            if first.data["mode"] == "pytest"
            else None
        )
        if neighbour is None:
            raise KioError(
                "Aligning needs two recordings; give trace_refs, or a failing test with a passing one beside it."
            )
        second = tracing.record(request.workspace, "pytest", neighbour)

    aligned = tracing.align(first, second)
    where = aligned["divergence"]

    return {
        "summary": (
            f"{first.data['target']} and {second.data['target']} agree for {aligned['common_prefix']} steps"
            + (
                f", then part: {where['first']} against {where['second']}."
                if where
                else " and do not part."
            )
        ),
        "verdict": "defect" if first.failed or second.failed else "clean",
        "trace_refs": [first.ref, second.ref],
        **aligned,
    }


KIO = Kio(
    kio_id="KIO2",
    title="Bug Locate: Reverse Execution & Dynamic Slicing",
    description=(
        "Bug localization, diagnosis, dynamic slicing, execution replay, and trace "
        "comparison on Python code. Records a failing run and names the faulty line "
        "and the values that led to it. Does NOT generate fixes."
    ),
    capabilities=[
        Capability(
            "bug_localization",
            "Record a failing run and name the faulty line.",
            bug_localization,
        ),
        Capability(
            "diagnosis",
            "Root cause and slice context for a fix, from a recording.",
            diagnosis,
        ),
        Capability("replay", "Step through a recording, up to the failure.", replay),
        Capability(
            "trace_alignment",
            "Align two recordings and find where they part.",
            trace_alignment,
        ),
    ],
    unsupported={
        "fix_recommendation": (
            "KIO2 localises faults; it does not generate fixes. Per D2.6 UC-UC1-03 the "
            "fix is KIO7's step: run diagnosis here and pass its slice_context to KIO7."
        ),
    },
)
