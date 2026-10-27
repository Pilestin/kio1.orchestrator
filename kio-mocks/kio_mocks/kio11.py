"""KIO11 — AI-powered test automation.

D2.6 §4.1.11: test cases generated from code (FR-KIO11-01), turned into
executable scripts (FR-KIO11-03), run, and their results and artefacts
handed on (FR-KIO11-05/07). In UC-UC1-03 KIO11 confirms the failure before
KIO2 localises it, and runs the suite again after KIO7's fix.

`test_automation` runs the project's tests. When asked to generate tests —
or when there are none — it also writes edge-value tests for the functions
no test mentions and runs them (`testgen.py`): the dynamic check that
confirms a failure without being told where it is.

`validation` matches acceptance criteria from an earlier step (KIO3) with the
tests that exercise them, and says which are covered and passing.
"""

from __future__ import annotations

import re
from typing import Any

from kio_mocks import testgen
from kio_mocks.errors import KioError
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.pytest_runner import NODE_ID, run_tests

GENERATE = re.compile(
    r"\b(generat\w*|creat\w*|writ\w*|edge|fuzz\w*|üret\w*|oluştur\w*|yaz\w*)\b",
    re.IGNORECASE,
)

STOP_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "be",
    "to",
    "of",
    "and",
    "or",
    "in",
    "on",
    "for",
    "with",
    "given",
    "when",
    "then",
    "it",
    "its",
    "that",
    "this",
    "should",
    "must",
    "can",
    "test",
}


def test_automation(request: Request) -> dict[str, Any]:
    selection = request.arguments.get("tests")
    selection = (
        [str(item) for item in selection]
        if isinstance(selection, list)
        else NODE_ID.findall(request.task)
    )

    functions = testgen.untested_functions(request.workspace)
    run = run_tests(request.workspace, selection or None)
    wants_generation = (
        bool(request.arguments.get("generate"))
        or bool(GENERATE.search(request.task))
        or run.total == 0
    )

    probed: dict[str, Any] = {}
    if wants_generation:
        probed = testgen.probe(
            request.workspace, [f for f in functions if not f["tested"]] or functions
        )

    failed = run.failed
    defects = probed.get("defects", [])

    if run.total == 0 and not probed.get("probed"):
        raise KioError(
            "No tests were found, and no function here could be probed at its edges."
        )

    parts = [
        (
            f"{len(run.passed)} of {run.total} tests passed"
            if run.total
            else "No tests in the project"
        )
    ]
    if failed:
        first = failed[0]
        parts.append(
            f"{len(failed)} failed — first {first['test']} ({first['error']} at {first['where'] or 'unknown'})"
        )
    if probed.get("probed"):
        parts.append(
            f"edge tests for {len(probed['probed'])} functions: "
            + (f"{len(defects)} likely defects" if defects else "none broke")
        )

    return {
        "summary": "; ".join(parts) + ".",
        "verdict": "defect" if failed or defects else "clean",
        **run.to_output(),
        "untested_functions": [
            f"{f['file']}::{f['function']}" for f in functions if not f["tested"]
        ],
        **({"generated": probed} if probed else {}),
        "command": "python -m pytest"
        + (" " + " ".join(selection) if selection else ""),
    }


def _criteria(request: Request) -> list[dict[str, str]]:
    raw = (
        request.arguments.get("acceptance_criteria")
        or request.find_upstream("acceptance_criteria")
        or []
    )
    criteria = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, dict):
            text = " ".join(
                str(item.get(key, ""))
                for key in ("given", "when", "then", "text")
                if item.get(key)
            )
            criteria.append(
                {
                    "id": str(item.get("id") or f"AC-{index + 1}"),
                    "text": text or str(item),
                }
            )
        else:
            criteria.append({"id": f"AC-{index + 1}", "text": str(item)})
    return criteria


def _words(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-zçğıöşü0-9]+", text.lower().replace("_", " "))
        if len(word) > 2 and word not in STOP_WORDS
    }


def validation(request: Request) -> dict[str, Any]:
    criteria = _criteria(request)
    if not criteria:
        raise KioError(
            "No acceptance criteria to validate against: run KIO3 acceptance_criteria first, or pass them as arguments."
        )

    run = run_tests(request.workspace)
    outcomes = {test: "passed" for test in run.passed} | {
        f["test"]: "failed" for f in run.failed
    }
    report = []

    for criterion in criteria:
        wanted = _words(criterion["text"])
        matches = sorted(
            (
                test
                for test in outcomes
                if len(wanted & _words(test.split("::")[-1])) >= 2
            ),
            key=lambda test: -len(wanted & _words(test.split("::")[-1])),
        )[:3]
        status = (
            "uncovered"
            if not matches
            else "failing" if any(outcomes[t] == "failed" for t in matches) else "met"
        )
        report.append({**criterion, "status": status, "tests": matches})

    met = sum(1 for entry in report if entry["status"] == "met")

    return {
        "summary": f"{met} of {len(report)} acceptance criteria are met by passing tests.",
        "verdict": "clean" if met == len(report) else "defect",
        "criteria": report,
        "coverage": round(met / len(report), 2),
        **run.to_output(),
    }


KIO = Kio(
    kio_id="KIO11",
    title="AI-Powered Test Automation Tool (TAT)",
    description=(
        "Test automation and validation: runs the project's tests and reports what failed, "
        "generates edge-value tests for untested functions to confirm failures, and checks "
        "acceptance criteria against the tests."
    ),
    capabilities=[
        Capability(
            "test_automation",
            "Run the tests; generate and run edge tests when asked.",
            test_automation,
        ),
        Capability(
            "validation", "Match acceptance criteria with passing tests.", validation
        ),
    ],
)
