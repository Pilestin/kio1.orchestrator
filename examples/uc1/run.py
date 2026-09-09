"""Run Use-Case 1 end to end against a real workspace.

    python examples/uc1/run.py

Starts the KIO fleet, points KIO1 at it by discovery, and runs the four
UC-UC1 scenarios in order. Nothing is stubbed at the KIO1 end: the real
registry, the real gates, the real failover, the real scoreboard.

Each scenario is checked against what has to be true of the world afterwards,
so this is a test rather than a demonstration. It exits non-zero when a
scenario does not deliver.
"""

import argparse
import json
import logging
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config_loader import DispatchConfig, Workspace
from core.prompt_loader import render_catalog
from core.session_logger import configure_console_encoding
from dispatch.agent_registry import load_agent_registry
from dispatch.directory import AgentDirectory, HealthConfig
from dispatch.discovery import DiscoveryConfig, ManifestSource
from dispatch.failover import FailoverConfig
from dispatch.policy import Gate, Policy
from dispatch.scoreboard import Scoreboard, SelectionConfig
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import STATUS_OK, dispatch_plan, render_summary
from examples.uc1.agents import Fleet
from examples.uc1.scenario import EXPECTATIONS, SCENARIOS

logger = logging.getLogger("uc1")

# Nothing reaches staging until the tests and the security scan have both run
# and come back clean. UC-UC1-02 leaves a failing suite behind on purpose, so
# this is the rule that has to hold for UC-UC1-04 to mean anything.
RELEASE_GATE = Policy(
    gates=(
        Gate(
            before=frozenset({"deployment"}),
            requires=frozenset({"test_automation", "cybersecurity_validation"}),
            verdict=frozenset({"clean"}),
        ),
    )
)


def build_dispatch(endpoints: dict[str, str], workspace: Path) -> DispatchConfig:
    """Assemble the dispatch settings KIO1 runs this scenario with.

    Only the manifest URLs are configured by hand — every agent's capabilities
    are discovered from the agent itself, which is the path an external
    deployment would take.

    Args:
        endpoints: Agent id to base URL, from the running fleet.
        workspace: The project the agents work on.

    Returns:
        Dispatch settings with an empty hand-written registry.
    """
    return DispatchConfig(
        enabled=True,
        request_timeout=180,
        max_parallel_steps=4,
        registry=load_agent_registry({}),
        workspace=Workspace(path=str(workspace), entry_point="test_pricing.py"),
        discovery=DiscoveryConfig(
            sources=tuple(
                ManifestSource(url=f"{base}/manifest") for base in endpoints.values()
            ),
            timeout=5,
        ),
        health=HealthConfig(enabled=True, interval=15, timeout=3),
        failover=FailoverConfig(enabled=True, max_attempts=2),
        selection=SelectionConfig(enabled=True, window=20, explore_every=0),
        policy=RELEASE_GATE,
    )


def check(result: Any, expected: dict[str, Any], workspace: Path) -> list[str]:
    """Return the ways a scenario's outcome fell short.

    Args:
        result: The executed workflow result.
        expected: What has to be true afterwards.
        workspace: The project directory, checked for produced files.

    Returns:
        One line per failed expectation, empty when the scenario delivered.
    """
    problems: list[str] = []

    if expected.get("all_steps_ok"):
        for step in result.results:
            if step.status != STATUS_OK:
                problems.append(
                    f"{step.step_id} ({step.agent_id}) is {step.status}: {step.error}"
                )

    for name in expected.get("files_present", []):
        if not (workspace / name).exists():
            problems.append(f"{name} was not produced")

    for step_id in expected.get("blocked_steps", []):
        step = next((item for item in result.results if item.step_id == step_id), None)

        if step is None or step.status == STATUS_OK:
            problems.append(f"{step_id} ran when the gate should have stopped it")

    for step_id, verdict in (expected.get("verdicts") or {}).items():
        actual = next(
            (step.verdict for step in result.results if step.step_id == step_id), None
        )
        if actual != verdict:
            problems.append(f"{step_id} reported {actual!r}, expected {verdict!r}")

    return problems


def main() -> int:
    """Run every scenario and report. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Run Use-Case 1 end to end.")
    parser.add_argument(
        "--workspace",
        help="Where the project is built. A temporary directory by default.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave the workspace behind so you can read what was produced.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show what each agent is doing."
    )
    arguments = parser.parse_args()

    # The summaries carry em dashes and arrows, which a default Windows
    # console cannot encode.
    configure_console_encoding()

    logging.basicConfig(
        level=logging.INFO if arguments.verbose else logging.WARNING,
        format="   %(name)s: %(message)s",
    )

    root = (
        Path(arguments.workspace).resolve()
        if arguments.workspace
        else Path(tempfile.mkdtemp(prefix="kio1-uc1-"))
    )
    workspace = root / "project"
    fleet = Fleet(workspace)
    endpoints = fleet.start()

    print(f"Workspace: {workspace}")
    print(f"Fleet:     {len(endpoints)} agents on 127.0.0.1\n")

    dispatch = build_dispatch(endpoints, workspace)
    directory = AgentDirectory(
        dispatch.registry, discovery=dispatch.discovery, health=dispatch.health
    )
    directory.start()
    scoreboard = Scoreboard(dispatch.selection)
    transport = build_transport(dispatch)

    print("Discovered from the agents themselves:")
    print(render_catalog(directory.snapshot()))

    failures: list[str] = []

    try:
        for identifier, name, build_plan in SCENARIOS:
            plan = build_plan()
            print(f"\n{'=' * 72}\n{identifier}  {name}\n{'=' * 72}")

            result = dispatch_plan(
                plan,
                replace(dispatch, registry=directory.snapshot()),
                transport,
                scoreboard,
            )

            if result is None:
                failures.append(f"{identifier}: the plan was not dispatchable")
                continue

            print(render_summary(result))

            problems = check(result, EXPECTATIONS[plan["workflow_id"]], workspace)

            if problems:
                failures.extend(f"{identifier}: {problem}" for problem in problems)
                print("\n  NOT DELIVERED")
                for problem in problems:
                    print(f"    - {problem}")
            else:
                print(f"\n  delivered — {EXPECTATIONS[plan['workflow_id']]['note']}")
    finally:
        directory.stop()
        transport.close()
        fleet.stop()

    print(f"\n{'=' * 72}")

    if failures:
        print(f"{len(failures)} expectation(s) not met:")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("Use-Case 1 delivered end to end.")
        produced = sorted(path.name for path in workspace.glob("*.py"))
        print(f"  workspace holds: {', '.join(produced)}")
        staged = root / "staging"
        if staged.exists():
            print(f"  released:        {', '.join(p.name for p in staged.iterdir())}")
        print("  scoreboard:")
        for score in scoreboard.summary():
            print(f"    {json.dumps(score.to_summary())}")

    if arguments.keep or arguments.workspace:
        print(f"\nWorkspace kept at {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
