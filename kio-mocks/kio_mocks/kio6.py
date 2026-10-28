"""KIO6 — quality: does the code do what the requirements ask?

D2.6 §4.1.6 and UC-UC1-02: KIO6 validates that what was built is aligned with
the requirements. Here that means, for every requirement:

- **where it lives** — the functions that trace it: by the requirement id the
  code carries (KIO7 writes it into each module), by the interfaces KIO4
  gave the components that trace it, or, in code nobody generated, by the
  words the requirement and the function share;
- **whether it is written** — a function whose body only raises
  `NotImplementedError` is a stub, not an implementation;
- **whether it is tested** — a test that calls it and asserts more than that
  it exists;
- **whether the criteria hold** — an acceptance criterion with numbers in it
  ("given 5 items, a 5% discount applies") is checked against the constants
  in the code and the arguments in the tests, so a threshold that drifted, or
  a boundary nobody tests, shows up by name.

Then it runs the tests. Data validation (FR-KIO6-01…09) follows in a later
step of kio-mocks.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.kio3 import analyse
from kio_mocks.kio4 import _head
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.pytest_runner import run_tests
from kio_mocks.text import content_words, singular
from kio_mocks.workspace import SKIPPED_DIRS, Workspace

TRACE = re.compile(r"\b(?:N?FR)-\d+\b")


def _is_stub(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = function.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]
    if not body:
        return True
    if len(body) != 1:
        return False
    only = body[0]
    if isinstance(only, ast.Pass) or (
        isinstance(only, ast.Expr)
        and isinstance(only.value, ast.Constant)
        and only.value.value is Ellipsis
    ):
        return True
    if isinstance(only, ast.Raise) and only.exc is not None:
        raised = only.exc.func if isinstance(only.exc, ast.Call) else only.exc
        return isinstance(raised, ast.Name) and raised.id == "NotImplementedError"
    return False


def _calls(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            if isinstance(call.func, ast.Name):
                names.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                names.add(call.func.attr)
    return names


def _meaningful(test: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A test that asserts something beyond `callable(...)`."""
    for node in ast.walk(test):
        if isinstance(node, ast.Assert):
            check = node.test
            if not (
                isinstance(check, ast.Call)
                and isinstance(check.func, ast.Name)
                and check.func.id == "callable"
            ):
                return True
        if isinstance(node, ast.With) and "raises" in ast.unparse(
            node.items[0].context_expr
        ):
            return True
    return False


def inventory(root: Path) -> dict[str, Any]:
    """Every function and test in a folder, with what each one says about itself."""
    functions: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    for directory, folders, files in os.walk(root):
        folders[:] = sorted(
            name
            for name in folders
            if name not in SKIPPED_DIRS and not name.startswith(".")
        )
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            module_doc = ast.get_docstring(tree) or ""
            constants = {
                float(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
            }
            is_test_file = name.startswith("test_") or "tests/" in relative
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                entry: dict[str, Any] = {
                    "name": node.name,
                    "file": relative,
                    "line": node.lineno,
                    "doc": ast.get_docstring(node) or "",
                    "module_doc": module_doc,
                }
                if is_test_file and node.name.startswith("test"):
                    tests.append(
                        {
                            **entry,
                            "calls": _calls(node),
                            "meaningful": _meaningful(node),
                            "source": ast.get_source_segment(source, node) or "",
                        }
                    )
                elif not is_test_file:
                    functions.append(
                        {
                            **entry,
                            "stub": _is_stub(node),
                            "traces": set(
                                TRACE.findall(module_doc + " " + entry["doc"])
                            ),
                            "constants": constants,
                        }
                    )
    return {"functions": functions, "tests": tests}


def _terms(text: str) -> set[str]:
    return {singular(word) for word in content_words(text.replace("_", " "))}


def _place(
    requirement: dict[str, Any],
    functions: list[dict[str, Any]],
    components: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """The functions a requirement lives in, and how they were found."""
    rid = str(requirement.get("id", ""))
    head = _head(requirement)
    traced = [function for function in functions if rid and rid in function["traces"]]
    # The id in a module's docstring covers every function in it — an API
    # module traces every requirement — so the ones named for what the
    # requirement is about come first.
    if traced:
        named = [function for function in traced if head in _terms(function["name"])]
        return named or traced, "requirement id in the code"

    names = {
        str(interface).split("(")[0].split(".")[-1]
        for component in components
        if rid in (component.get("requirements") or [])
        for interface in component.get("interfaces") or []
    }
    by_interface = [function for function in functions if function["name"] in names]
    if by_interface:
        return by_interface, "the interfaces of the components that trace it"

    # What the requirement is about, not the detail after its colon: "bulk
    # pricing discounts", not the "items" its thresholds count.
    goal = str(requirement.get("goal") or requirement.get("text", "")).split(":")[0]
    wanted = _terms(goal) | {head}
    scored = []
    for function in functions:
        in_name = _terms(function["name"])
        in_doc = _terms(function["doc"])
        if head not in in_name | in_doc:
            continue
        scored.append((2 * len(wanted & in_name) + len(wanted & in_doc), function))
    scored.sort(key=lambda pair: -pair[0])
    return [
        function for _, function in scored[:3]
    ], "the words the requirement and the code share"


def _numbers(criterion: dict[str, Any]) -> tuple[int, float, bool] | None:
    """(count, rate, whether the rate should not apply) from a threshold criterion."""
    count = re.search(r"\b(\d+)\b", str(criterion.get("given", "")))
    rate = re.search(r"(\d+(?:\.\d+)?)\s*%", str(criterion.get("then", "")))
    if not (count and rate):
        return None
    return (
        int(count.group(1)),
        float(rate.group(1)),
        "not" in str(criterion.get("then", "")),
    )


def _check_criterion(
    criterion: dict[str, Any], places: list[dict[str, Any]], tests: list[dict[str, Any]]
) -> dict[str, Any]:
    found = _numbers(criterion)
    touching = [
        test
        for test in tests
        if test["meaningful"] and test["calls"] & {p["name"] for p in places}
    ]
    check: dict[str, Any] = {
        "id": criterion.get("id"),
        "criterion": (
            f"Given {criterion.get('given')}, when {criterion.get('when')}, then {criterion.get('then')}"
        ),
    }
    if places and all(place["stub"] for place in places):
        check["status"] = "not written yet"
        return check
    if found is None:
        check["tested"] = bool(touching)
        check["status"] = "tested" if touching else "untested"
        return check

    count, rate, negative = found
    threshold = count + 1 if negative else count
    constants = (
        set().union(*(place["constants"] for place in places)) if places else set()
    )
    in_code = float(threshold) in constants and (
        rate / 100 in constants or rate in constants
    )
    argument = re.compile(rf"[(\[, ]{count}[,)\] ]")
    tested = [test["name"] for test in touching if argument.search(test["source"])]
    check.update(
        {
            "in_code": in_code,
            "tested_by": tested,
            "status": (
                "holds and tested"
                if in_code and tested
                else "holds, untested" if in_code else "numbers not in the code"
            ),
        }
    )
    if in_code and not tested:
        check["detail"] = f"No test calls it with {count}" + (
            " — the boundary just below the threshold." if negative else "."
        )
    elif not in_code:
        check["detail"] = (
            f"{threshold} and {rate:g}% do not appear together in {', '.join(sorted({p['file'] for p in places})) or 'the code'}."
        )
    return check


def _inputs(
    request: Request,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    requirements = request.find_upstream("requirements")
    criteria = request.find_upstream("acceptance_criteria")
    components = request.find_upstream("components")
    if not (isinstance(requirements, list) and requirements):
        found = analyse(str(request.arguments.get("text") or request.task))
        requirements, criteria = found["requirements"], found["acceptance_criteria"]
    return (
        [item for item in requirements if isinstance(item, dict)],
        [item for item in criteria or [] if isinstance(item, dict)],
        [item for item in components or [] if isinstance(item, dict)],
    )


def quality_analysis(request: Request) -> dict[str, Any]:
    requirements, criteria, components = _inputs(request)
    generated = request.find_upstream("output_dir")
    root = (
        Path(str(generated))
        if generated and Path(str(generated)).is_dir()
        else request.workspace.require()
    )
    code = inventory(root)
    if not code["functions"]:
        raise KioError(
            f"There is no Python code in {root} to check against the requirements."
        )

    rows: list[dict[str, Any]] = []
    for requirement in requirements:
        places, how = _place(requirement, code["functions"], components)
        names = {place["name"] for place in places}
        tests = [test for test in code["tests"] if test["calls"] & names]
        written = [place for place in places if not place["stub"]]
        rid = requirement.get("id")
        rows.append(
            {
                "requirement": rid,
                "text": requirement.get("text"),
                "found_by": how if places else None,
                "where": [
                    f"{place['file']}:{place['line']} {place['name']}"
                    for place in places
                ],
                "status": (
                    "missing" if not places else "implemented" if written else "stub"
                ),
                "tests": [test["name"] for test in tests if test["meaningful"]],
                "criteria": [
                    _check_criterion(criterion, places, code["tests"])
                    for criterion in criteria
                    if criterion.get("requirement") == rid
                ],
            }
        )

    run = run_tests(Workspace(root), timeout=30.0) if code["tests"] else None
    missing = [row["requirement"] for row in rows if row["status"] == "missing"]
    stubs = [row["requirement"] for row in rows if row["status"] == "stub"]
    drifted = [
        c["id"]
        for row in rows
        for c in row["criteria"]
        if c["status"] == "numbers not in the code"
    ]
    untested = [
        c["id"]
        for row in rows
        for c in row["criteria"]
        if c["status"] in ("holds, untested", "untested")
    ]
    failed = len(run.failed) if run else 0

    if missing or drifted or failed:
        verdict = "defect"
    elif stubs or untested:
        verdict = "inconclusive"
    else:
        verdict = "clean"

    placed = len(rows) - len(missing)
    parts = [f"{placed}/{len(rows)} requirements have a place in the code"]
    if stubs:
        parts.append(f"{len(stubs)} only as stubs ({', '.join(stubs)})")
    if missing:
        parts.append(f"not found: {', '.join(missing)}")
    if drifted:
        parts.append(f"criteria the code contradicts: {', '.join(drifted)}")
    if untested:
        parts.append(f"criteria no test checks: {', '.join(untested)}")
    if run:
        parts.append(f"tests {len(run.passed)} passed, {failed} failed")

    return {
        "summary": "; ".join(parts) + ".",
        "verdict": verdict,
        "code": str(root),
        "alignment": rows,
        "tests": run.to_output() if run else None,
        "report": _report(rows, run),
        "method": "trace ids, interfaces and shared words; criteria against constants and test arguments (no model)",
    }


def _report(rows: list[dict[str, Any]], run: Any) -> str:
    out = [
        "# Requirements against the code",
        "",
        "| Requirement | Where | Status | Tests |",
        "|---|---|---|---|",
    ]
    for row in rows:
        where = "<br>".join(row["where"]) or "—"
        out.append(
            f"| {row['requirement']} | {where} | {row['status']} | {len(row['tests'])} |"
        )
    checks = [(row["requirement"], c) for row in rows for c in row["criteria"]]
    if checks:
        out += ["", "## Acceptance criteria", ""]
        out += [
            f"- **{c['id']}** ({rid}) {c['status']}"
            + (f" — {c['detail']}" if c.get("detail") else "")
            for rid, c in checks
        ]
    if run:
        out += ["", f"Tests: {len(run.passed)} passed, {len(run.failed)} failed."]
    return "\n".join(out) + "\n"


KIO = Kio(
    kio_id="KIO6",
    title="Data Validation and Quality Analysis",
    description=(
        "Quality analysis of code against requirements: traces each requirement to the "
        "functions that implement it, tells implemented code from stubs, finds the tests "
        "that exercise it, checks threshold acceptance criteria against the constants in the "
        "code and the arguments in the tests, and runs the tests."
    ),
    capabilities=[
        Capability(
            "quality_analysis",
            "Requirement-to-code alignment: where each requirement lives, whether it is written and tested, whether its criteria hold.",
            quality_analysis,
        ),
    ],
    unsupported={
        "data_validation": "KIO6's data validation is not written in kio-mocks yet (step 5 of docs/plans/kio-mocks.md).",
    },
)
