"""KIO7 — the AI-SysDev tool: code understanding, and code generation.

D2.6 §4.1.7: code generation (FR-KIO7-05), code understanding and debugging
support with suggested fixes (FR-KIO7-06), proactive suggestions (FR-KIO7-07)
and test suites for generated code (FR-KIO7-11). In UC-UC1-03 it is KIO7, not
KIO2, that "proposes a fix patch and regenerates verifying tests".

Without a model, generation is rule-based and says so:

- **A fix** from KIO2's localisation (`fault`, `slice_context`): a guard for
  the edge the slice shows — a zero divisor, an empty sequence, a None, a
  missing key — returning what the failing test expects when the test says
  so. The patch is applied to a *copy* of the workspace and the tests are run
  there; the workspace itself is only changed with `arguments.apply: true`,
  which is the person's approval UC-UC1-03 asks for.
- **Code from an architecture** (KIO4's `components`): a module per component
  with its interfaces as stubs, and a test file per module, written to an
  output folder rather than into the workspace.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.pytest_runner import run_tests
from kio_mocks.workspace import SKIPPED_DIRS, Workspace

OUTPUT_DIR = Path(tempfile.gettempdir()) / "kio-mocks-output"


# ── understanding ────────────────────────────────────────────────────────────


def _risks(tree: ast.AST, source: str) -> list[dict[str, Any]]:
    """What a careful reviewer would ask about, found by walking the tree."""
    risks = []

    for function in [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]:
        guarded = {
            ast.unparse(test)
            for node in ast.walk(function)
            if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert))
            for test in [node.test]
        }
        for node in ast.walk(function):
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Div, ast.FloorDiv, ast.Mod)
            ):
                divisor = ast.unparse(node.right)
                constant = isinstance(node.right, ast.Constant)
                if not constant and not any(divisor in test for test in guarded):
                    risks.append(
                        {
                            "line": node.lineno,
                            "function": function.name,
                            "risk": "possible division by zero",
                            "detail": f"`{divisor}` is never checked before dividing by it.",
                        }
                    )
        for default in function.args.defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                risks.append(
                    {
                        "line": function.lineno,
                        "function": function.name,
                        "risk": "mutable default argument",
                        "detail": "The default is shared between calls.",
                    }
                )

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            risks.append(
                {
                    "line": node.lineno,
                    "function": "",
                    "risk": "bare except",
                    "detail": "Catches everything, KeyboardInterrupt included.",
                }
            )
        if (
            isinstance(node, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
            and any(
                isinstance(item, ast.Constant) and item.value is None
                for item in node.comparators
            )
        ):
            risks.append(
                {
                    "line": node.lineno,
                    "function": "",
                    "risk": "comparison with None",
                    "detail": "Use `is None`.",
                }
            )

    return sorted(risks, key=lambda risk: risk["line"])


def _describe(workspace: Workspace, path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    functions, classes = [], []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            branches = sum(
                isinstance(inner, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp))
                for inner in ast.walk(node)
            )
            functions.append(
                {
                    "name": node.name,
                    "signature": f"{node.name}({', '.join(argument.arg for argument in node.args.args)})",
                    "line": node.lineno,
                    "doc": (ast.get_docstring(node) or "").split("\n")[0],
                    "complexity": 1 + branches,
                }
            )
        elif isinstance(node, ast.ClassDef):
            classes.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "methods": [
                        item.name
                        for item in node.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ],
                    "doc": (ast.get_docstring(node) or "").split("\n")[0],
                }
            )

    return {
        "file": workspace.relative(path),
        "doc": (ast.get_docstring(tree) or "").split("\n")[0],
        "lines": len(source.splitlines()),
        "functions": functions,
        "classes": classes,
        "imports": sorted(
            {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            | {
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
        ),
        "risks": _risks(tree, source),
    }


def code_understanding(request: Request) -> dict[str, Any]:
    workspace = request.workspace
    named = re.findall(r"[\w./\\-]+\.py", request.task)
    fault = request.find_upstream("fault")
    if isinstance(fault, dict) and fault.get("file"):
        named.append(fault["file"])

    paths = [
        workspace.resolve(name)
        for name in dict.fromkeys(named)
        if workspace.resolve(name).is_file()
    ]
    if not paths:
        paths = [
            path
            for path in workspace.python_files(200)
            if not path.name.startswith("test_")
            and "/tests/" not in workspace.relative(path) + "/"
        ][:40]
    if not paths:
        raise KioError("No Python source to read in the workspace.")

    modules = []
    for path in paths:
        try:
            modules.append(_describe(workspace, path))
        except SyntaxError as error:
            modules.append(
                {
                    "file": workspace.relative(path),
                    "error": f"Does not parse: {error.msg} at line {error.lineno}.",
                }
            )

    risks = [
        {"file": module["file"], **risk}
        for module in modules
        for risk in module.get("risks", [])
    ]
    functions = sum(len(module.get("functions", [])) for module in modules)

    return {
        "summary": f"{len(modules)} modules, {functions} functions read; {len(risks)} things worth a second look."
        + (
            f" First: {risks[0]['risk']} in {risks[0]['file']}:{risks[0]['line']}."
            if risks
            else ""
        ),
        "verdict": "defect" if risks else "clean",
        "modules": modules,
        "suggestions": risks,
    }


# ── fixes ────────────────────────────────────────────────────────────────────


def _expected_value(slice_context: list[dict[str, Any]]) -> str | None:
    """What the failing test expects, when its assertion says: `== 0.0`."""
    for entry in slice_context:
        found = re.search(r"assert .*?==\s*(.+?)\s*$", entry.get("code", ""))
        if found:
            return found.group(1)
    return None


def _guard(
    fault: dict[str, Any], exception: str, default: str
) -> tuple[str, str] | None:
    """(condition, explanation) for the edge that broke the faulting line."""
    code = fault["code"]
    try:
        tree = ast.parse(
            code.removeprefix("return ").strip() if code.startswith("return ") else code
        )
    except SyntaxError:
        return None

    if exception == "ZeroDivisionError":
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Div, ast.FloorDiv, ast.Mod)
            ):
                divisor = ast.unparse(node.right)
                return (
                    f"not {divisor}"
                    if isinstance(node.right, (ast.Name, ast.Call, ast.Attribute))
                    else f"{divisor} == 0"
                ), f"`{divisor}` can be zero"
    if exception == "IndexError":
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                return (
                    f"not {ast.unparse(node.value)}",
                    f"`{ast.unparse(node.value)}` can be empty",
                )
    if exception in ("AttributeError", "TypeError") and "None" in fault.get(
        "message", ""
    ) + str(fault.get("values")):
        for name, value in fault.get("values", {}).items():
            if value == "None":
                return f"{name} is None", f"`{name}` can be None"
    if exception == "KeyError":
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                key = ast.unparse(node.slice)
                return (
                    f"{key} not in {ast.unparse(node.value)}",
                    f"`{key}` can be missing from `{ast.unparse(node.value)}`",
                )
    return None


def _patched(source: str, line: int, condition: str, default: str) -> str:
    lines = source.splitlines(keepends=True)
    target = lines[line - 1]
    indent = target[: len(target) - len(target.lstrip())]
    step = "    " if "\t" not in indent else "\t"
    guard = f"{indent}if {condition}:\n{indent}{step}return {default}\n"
    return "".join(lines[: line - 1]) + guard + "".join(lines[line - 1 :])


def _sandbox(workspace: Workspace) -> Path:
    root = workspace.require()
    copy = Path(tempfile.mkdtemp(prefix="kio7-sandbox-")) / root.name
    shutil.copytree(root, copy, ignore=shutil.ignore_patterns(*SKIPPED_DIRS))
    return copy


def _fix(request: Request, fault: dict[str, Any]) -> dict[str, Any]:
    workspace = request.workspace
    exception = (request.find_upstream("exception") or {}).get("type", "")
    slice_context = request.find_upstream("slice_context") or []
    default = _expected_value(slice_context) or "None"
    guard = _guard(
        {
            **fault,
            "message": (request.find_upstream("exception") or {}).get("message", ""),
        },
        exception,
        default,
    )

    if guard is None or not fault["code"].startswith("return"):
        return {
            "summary": f"No rule-based fix for {exception} at {fault['file']}:{fault['line']}; a model or a person is needed.",
            "verdict": "inconclusive",
            "hitl_required": True,
            "fault": fault,
        }

    condition, why = guard
    source = workspace.read(fault["file"])
    patched = _patched(source, fault["line"], condition, default)
    patch = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{fault['file']}",
            tofile=f"b/{fault['file']}",
        )
    )

    # Verify on a copy: the tests decide, not the rule.
    sandbox = _sandbox(workspace)
    (sandbox / fault["file"]).write_text(patched, encoding="utf-8")
    run = run_tests(Workspace(root=sandbox))

    applied = False
    if request.arguments.get("apply") is True and not run.failed:
        target = workspace.resolve(fault["file"])
        target.with_suffix(target.suffix + ".orig").write_text(source, encoding="utf-8")
        target.write_text(patched, encoding="utf-8")
        applied = True

    return {
        "summary": (
            f"Guard {why} at {fault['file']}:{fault['line']}: return {default} when {condition}. "
            f"With it, {len(run.passed)} of {run.total} tests pass"
            + (
                " — every one."
                if not run.failed
                else f"; {len(run.failed)} still fail."
            )
        ),
        "verdict": "clean" if not run.failed else "defect",
        "patch": patch,
        "files_changed": [fault["file"]],
        "verified": run.to_output(),
        "sandbox": str(sandbox),
        "applied": applied,
        "hitl_required": not applied,
        "rationale": f"{exception} at `{fault['code']}` because {why}; the failing test expects {default}.",
        "method": "rule-based (no model)",
    }


# ── generation from an architecture ──────────────────────────────────────────


def _snake(text: str) -> str:
    return (
        re.sub(
            r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()
        ).strip("_")
        or "module"
    )


def _from_components(
    request: Request, components: list[dict[str, Any]]
) -> dict[str, Any]:
    folder = (
        OUTPUT_DIR
        / hashlib.sha256(
            f"{request.workflow_id}{request.step_id}{request.task}".encode()
        ).hexdigest()[:12]
    )
    files = []

    for component in components:
        name = _snake(str(component.get("name", "component")))
        interfaces = component.get("interfaces") or [f"{name}_run(request)"]
        stubs = []
        tests = []
        for interface in interfaces:
            signature = re.sub(r"\s*->.*$", "", str(interface)).strip()
            function = re.match(r"[A-Za-z_][\w]*", signature.split(".")[-1])
            function_name = _snake(function.group(0)) if function else f"{name}_run"
            parameters = re.search(r"\((.*)\)", signature)
            stubs.append(
                f"def {function_name}({parameters.group(1) if parameters else ''}):\n"
                f'    """{component.get("responsibility", "")}"""\n'
                f'    raise NotImplementedError("{function_name}: generated skeleton")\n'
            )
            tests.append(
                f"def test_{function_name}_exists():\n    assert callable({name}.{function_name})\n"
            )

        files.append(
            {
                "path": f"{name}.py",
                "content": f'"""{component.get("responsibility", component.get("name", ""))}"""\n\n\n'
                + "\n\n".join(stubs),
            }
        )
        files.append(
            {
                "path": f"tests/test_{name}.py",
                "content": f"import {name}\n\n\n" + "\n\n".join(tests),
            }
        )

    for file in files:
        path = folder / file["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file["content"], encoding="utf-8")

    return {
        "summary": f"Skeleton for {len(components)} components: {len(files)} files with stubs and tests, in {folder}.",
        "verdict": "clean",
        "output_dir": str(folder),
        "files": files,
        "method": "skeleton from the architecture (no model)",
    }


def code_generation(request: Request) -> dict[str, Any]:
    fault = request.find_upstream("fault")
    if isinstance(fault, dict) and fault.get("file"):
        return _fix(request, fault)

    findings = request.find_upstream("findings")
    if (
        isinstance(findings, list)
        and findings
        and isinstance(findings[0], dict)
        and findings[0].get("code")
    ):
        return _fix(request, {**findings[0], "values": {}})

    components = request.find_upstream("components")
    if isinstance(components, list) and components:
        return _from_components(
            request, [item for item in components if isinstance(item, dict)]
        )

    raise KioError(
        "Nothing to generate from: give KIO2's diagnosis (for a fix) or KIO4's architecture (for code). "
        "Free-form code from a sentence needs a model (KIO_MOCKS_LLM)."
    )


KIO = Kio(
    kio_id="KIO7",
    title="AI-SysDev Tool: Code Generation, Code Understanding & Debugging",
    description=(
        "Code generation and code understanding: explains modules and flags risky code, "
        "proposes a fix from a bug localization and verifies it by running the tests on a "
        "copy, and generates code skeletons with tests from an architecture."
    ),
    capabilities=[
        Capability(
            "code_generation",
            "A verified fix from KIO2's diagnosis, or code from KIO4's architecture.",
            code_generation,
        ),
        Capability(
            "code_understanding",
            "Explain modules and flag what deserves a second look.",
            code_understanding,
        ),
    ],
)
