"""Test cases from code: KIO11's FR-KIO11-01 and FR-KIO11-03, without a model.

What can be said about a function without knowing what it should return is
how it behaves at the edges: an empty list, a zero, an empty string. Each
untested top-level function gets a parametrised test calling it with the
edge values its parameter names suggest, and the test is run. A function
that divides by an empty list's length, indexes past an end, or reads an
attribute of None at such an edge is very likely wrong; one that rejects an
edge with a ValueError is doing its job — so the two are reported apart.

Names decide the values, because types are rarely annotated: `items`,
`values`, `rows` are collections; `quantity`, `count`, `price` numbers;
`name`, `text`, `path` strings. A parameter nothing suggests is not probed.
"""

from __future__ import annotations

import ast
import itertools
import os
import tempfile
from pathlib import Path
from typing import Any

from kio_mocks.pytest_runner import run_tests
from kio_mocks.workspace import Workspace

COLLECTION = (
    "items",
    "values",
    "list",
    "rows",
    "records",
    "data",
    "elements",
    "numbers",
    "entries",
    "xs",
    "lines",
    "orders",
    "cart",
)
NUMBER = (
    "quantity",
    "count",
    "n",
    "size",
    "amount",
    "price",
    "rate",
    "index",
    "total",
    "limit",
    "number",
    "age",
    "level",
    "x",
    "y",
)
TEXT = (
    "name",
    "text",
    "s",
    "string",
    "path",
    "query",
    "word",
    "message",
    "title",
    "key",
)

EDGES = {"collection": ["[]"], "number": ["0", "-1"], "text": ['""']}

# Raised at an edge, these almost always mean the code forgot the edge.
LIKELY_DEFECTS = (
    "ZeroDivisionError",
    "IndexError",
    "KeyError",
    "AttributeError",
    "UnboundLocalError",
    "RecursionError",
)


def _kind(name: str) -> str | None:
    lowered = name.lower()
    for kind, words in (("collection", COLLECTION), ("number", NUMBER), ("text", TEXT)):
        if lowered in words or any(
            lowered.endswith("_" + word) or lowered.startswith(word + "_")
            for word in words
        ):
            return kind
    if lowered.endswith("s") and len(lowered) > 3:
        return "collection"
    return None


def _module_name(workspace: Workspace, path: Path) -> str:
    return workspace.relative(path)[:-3].replace("/", ".").removesuffix(".__init__")


def _is_test(workspace: Workspace, path: Path) -> bool:
    relative = workspace.relative(path)
    return (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or relative.startswith("tests/")
    )


def untested_functions(workspace: Workspace, limit: int = 30) -> list[dict[str, Any]]:
    """Top-level functions no test file mentions by name."""
    files = workspace.python_files()
    tests_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in files
        if _is_test(workspace, path)
    )
    found = []

    for path in files:
        if _is_test(workspace, path) or path.name in ("setup.py", "conftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            if node.args.vararg or node.args.kwarg or node.args.posonlyargs:
                continue
            found.append(
                {
                    "module": _module_name(workspace, path),
                    "file": workspace.relative(path),
                    "function": node.name,
                    "line": node.lineno,
                    "parameters": [argument.arg for argument in node.args.args],
                    "defaults": len(node.args.defaults),
                    "tested": node.name in tests_text,
                }
            )
            if len(found) >= limit:
                return found

    return found


def edge_test(function: dict[str, Any]) -> str | None:
    """A parametrised pytest calling `function` at the edges its parameters suggest."""
    required = function["parameters"][
        : len(function["parameters"]) - function["defaults"]
    ]
    choices = []
    for name in required:
        kind = _kind(name)
        if kind is None:
            return None
        choices.append(EDGES[kind])

    cases = list(itertools.islice(itertools.product(*choices), 6)) if choices else [()]
    name = function["function"]
    module = function["module"]

    if not required:
        return (
            f"from {module} import {name}\n\n\ndef test_{name}_runs():\n    {name}()\n"
        )

    parameters = ", ".join(required)
    # One name takes the values themselves; several take a tuple each.
    if len(required) == 1:
        values = ", ".join(case[0] for case in cases)
    else:
        values = ", ".join("(" + ", ".join(case) + ")" for case in cases)

    return (
        f"import pytest\n\nfrom {module} import {name}\n\n\n"
        f'@pytest.mark.parametrize("{parameters}", [{values}])\n'
        f"def test_{name}_at_the_edges({parameters}):\n"
        f"    {name}({parameters})\n"
    )


def probe(workspace: Workspace, functions: list[dict[str, Any]]) -> dict[str, Any]:
    """Write the edge tests, run them against the workspace, and sort what broke."""
    sources = {
        f["function"]: code for f in functions if (code := edge_test(f)) is not None
    }

    if not sources:
        return {"generated_tests": "", "probed": [], "defects": [], "rejections": []}

    folder = Path(tempfile.mkdtemp(prefix="kio11-"))
    script = folder / "test_kio11_generated.py"
    script.write_text("\n\n".join(sources.values()), encoding="utf-8")

    # The tests live outside the workspace; its code is found through the path.
    previous = os.environ.get("PYTHONPATH")
    path = str(workspace.require()) + (os.pathsep + previous if previous else "")
    run = run_tests(
        workspace, [str(script), "--rootdir", str(folder)], env={"PYTHONPATH": path}
    )

    defects: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for failure in run.failed:
        entry = {
            "test": failure["test"].split("::")[-1],
            "error": failure["error"],
            "message": failure["message"],
            "where": failure["where"],
        }
        (defects if failure["error"] in LIKELY_DEFECTS else rejections).append(entry)

    return {
        "generated_tests": script.read_text(encoding="utf-8"),
        "probed": sorted(sources),
        "cases_run": run.total,
        "defects": defects,
        "rejections": rejections,
    }
