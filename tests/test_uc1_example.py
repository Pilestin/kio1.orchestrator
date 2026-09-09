"""The Use-Case 1 example, run as a test.

This is the widest thing in the suite: a real fleet over HTTP, discovery
instead of hand-written registration, a policy gate, a genuine failing test
found in a genuine traceback, and a build that reaches staging only after the
fix. If the orchestrator breaks in a way the unit tests do not notice, it
breaks here.

It is slower than everything else because two of its steps really do run
`pytest` in a subprocess. That is the point: the artefacts are real.
"""

import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import STATUS_NOT_RUN, STATUS_OK, dispatch_plan
from examples.uc1 import scenario
from examples.uc1.agents import Fleet
from examples.uc1.run import build_dispatch, check


@pytest.fixture(scope="module")
def run() -> Iterator[dict[str, Any]]:
    """Run every Use-Case 1 scenario once and yield what happened."""
    root = Path(tempfile.mkdtemp(prefix="kio1-uc1-test-"))
    workspace = root / "project"
    fleet = Fleet(workspace)
    endpoints = fleet.start()

    dispatch = build_dispatch(endpoints, workspace)
    directory = AgentDirectory(
        dispatch.registry, discovery=dispatch.discovery, health=dispatch.health
    )
    directory.refresh()
    scoreboard = Scoreboard(dispatch.selection)
    transport = build_transport(dispatch)

    results: dict[str, Any] = {}

    try:
        for _, _, build_plan in scenario.SCENARIOS:
            plan = build_plan()
            results[plan["workflow_id"]] = dispatch_plan(
                plan,
                replace(dispatch, registry=directory.snapshot()),
                transport,
                scoreboard,
            )

        yield {
            "results": results,
            "workspace": workspace,
            "root": root,
            "registry": directory.snapshot(),
            "scoreboard": scoreboard,
        }
    finally:
        transport.close()
        fleet.stop()
        shutil.rmtree(root, ignore_errors=True)


def test_every_agent_is_found_by_discovery_alone(run: dict[str, Any]) -> None:
    # Nothing is written into dispatch.agents; the fleet describes itself.
    assert run["registry"].agent_ids() >= {
        "KIO2",
        "KIO3",
        "KIO4",
        "KIO6",
        "KIO7",
        "KIO8",
        "KIO9",
        "KIO10",
        "KIO11",
        "KIO12",
    }


@pytest.mark.parametrize("workflow_id", list(scenario.EXPECTATIONS))
def test_each_scenario_delivers(run: dict[str, Any], workflow_id: str) -> None:
    result = run["results"][workflow_id]

    assert result is not None
    assert check(result, scenario.EXPECTATIONS[workflow_id], run["workspace"]) == []


def test_the_generated_code_really_exists(run: dict[str, Any]) -> None:
    source = (run["workspace"] / "pricing.py").read_text(encoding="utf-8")

    assert "def order_total" in source
    assert "DISCOUNT_TIERS" in source


def test_the_suite_failed_before_the_fix_and_passed_after(run: dict[str, Any]) -> None:
    steps = {step.step_id: step for step in run["results"]["wf-uc1-03"].results}

    assert steps["s1"].output["failed"] == 1
    assert steps["s5"].output["failed"] == 0
    assert steps["s5"].output["passed"] == 4


def test_the_fault_was_located_in_the_generated_file(run: dict[str, Any]) -> None:
    # KIO2 read a real traceback rather than being told the answer.
    localisation = {step.step_id: step for step in run["results"]["wf-uc1-03"].results}[
        "s2"
    ]

    assert localisation.output["findings"][0]["file"] == "pricing.py"


def test_the_diagnosis_asked_for_a_human(run: dict[str, Any]) -> None:
    diagnosis = {step.step_id: step for step in run["results"]["wf-uc1-03"].results}[
        "s3"
    ]

    assert diagnosis.review_required is True


def test_the_gate_refused_the_release_while_the_suite_was_red(
    run: dict[str, Any],
) -> None:
    deployment = {step.step_id: step for step in run["results"]["wf-uc1-04a"].results}[
        "s6"
    ]

    assert deployment.status == STATUS_NOT_RUN
    assert "test_automation" in (deployment.error or "")


def test_the_release_went_out_once_the_suite_was_green(run: dict[str, Any]) -> None:
    deployment = {step.step_id: step for step in run["results"]["wf-uc1-04"].results}[
        "s6"
    ]

    assert deployment.status == STATUS_OK
    assert Path(deployment.output["released"]).exists()


def test_the_scoreboard_learned_from_the_run(run: dict[str, Any]) -> None:
    scores = {score.capability: score for score in run["scoreboard"].summary()}

    # Four test runs happened: the refused release's, the red one, the green
    # one after the fix, and the release check. Every call succeeded — a
    # `defect` verdict is a successful call that found something.
    assert scores["test_automation"].attempts == 4
    assert scores["test_automation"].successes == 4
    assert scores["deployment"].successes == 1
