"""Run a project's tests and read the result from pytest's own report.

The console summary is for people and changes between versions; the JUnit
report is for machines, and with the `xunit1` family it carries each test's
file and line, which is what a node id like `tests/test_x.py::test_y` is
made of.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

from kio_mocks.errors import KioError
from kio_mocks.workspace import Workspace, python

NODE_ID = re.compile(r"[\w./\\-]+\.py::[\w\[\].:-]+")


@dataclass
class TestRun:
    passed: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    output: str = ""

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.skipped)

    def to_output(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": len(self.passed),
            "failed": self.failed,
            "skipped": len(self.skipped),
            "duration_s": self.seconds,
        }


def run_tests(
    workspace: Workspace,
    selection: list[str] | None = None,
    *,
    timeout: float = 40.0,
    env: dict[str, str] | None = None,
) -> TestRun:
    """Run the project's tests, or the ones selected, and say how each did."""
    root = workspace.require()
    handle, report = tempfile.mkstemp(suffix=".xml", prefix="kio-mocks-")
    os.close(handle)

    try:
        completed = workspace.run(
            [
                python(),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--tb=short",
                "-o",
                "junit_family=xunit1",
                f"--junitxml={report}",
                *(selection or []),
            ],
            timeout=timeout,
            cwd=root,
            env=env,
        )

        if "No module named pytest" in completed.stderr:
            raise KioError(
                "pytest is not installed for the interpreter this service runs."
            )

        try:
            tree = ElementTree.parse(report)
        except (ElementTree.ParseError, FileNotFoundError) as error:
            tail = (completed.stdout + completed.stderr).strip().splitlines()[-3:]
            raise KioError("The tests did not run: " + " ".join(tail)) from error
    finally:
        try:
            os.remove(report)
        except OSError:
            pass

    run = TestRun(
        seconds=completed.seconds,
        cpu_seconds=completed.cpu_seconds,
        output=completed.stdout[-4000:],
    )

    for case in tree.iter("testcase"):
        node = _node_id(case)
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")

        if problem is not None:
            text = problem.text or ""
            run.failed.append(
                {
                    "test": node,
                    "error": (
                        (problem.get("message") or "").split(":")[0].split()[-1:][0]
                        if problem.get("message")
                        else ""
                    ),
                    "message": (problem.get("message") or "").strip()[:300],
                    "where": _deepest_location(text),
                    "traceback": text.strip()[-1500:],
                }
            )
        elif case.find("skipped") is not None:
            run.skipped.append(node)
        else:
            run.passed.append(node)

    return run


def _node_id(case: ElementTree.Element) -> str:
    file = (case.get("file") or "").replace("\\", "/")
    name = case.get("name") or ""
    classname = case.get("classname") or ""

    if not file:
        file = classname.replace(".", "/") + ".py"
    # A test method: the class is the last part of classname.
    module = file[:-3].replace("/", ".")
    owner = classname[len(module) + 1 :] if classname.startswith(module + ".") else ""

    return f"{file}::{owner + '::' if owner else ''}{name}"


def _deepest_location(traceback: str) -> str:
    """The last `file.py:line` of a short traceback: where it was raised."""
    found = re.findall(r"([\w./\\-]+\.py):(\d+)", traceback)
    return f"{found[-1][0].replace(chr(92), '/')}:{found[-1][1]}" if found else ""


def first_failing_test(workspace: Workspace) -> str | None:
    """The first test that fails, found by running them all."""
    run = run_tests(workspace)
    return run.failed[0]["test"] if run.failed else None
