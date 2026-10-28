"""KIO1 against the stand-in KIOs in kio-mocks/, over real HTTP.

The stand-ins are only worth having if KIO1 cannot tell them from the real
services. So this drives them with KIO1's own executor and transports, not
with calls of their own: a plan of UC-UC1-03 — KIO11 runs the tests, KIO2
localises from what KIO11 reported, KIO7 fixes from what KIO2 found — goes
through `execute_workflow`, results passed on as `data.upstream`; so does
one of UC-UC1-01/02 — KIO3 writes requirements, KIO4 designs, KIO7 writes
the skeleton, KIO6 checks it against the requirements; and a job KIO is
polled by KIO1's `KioJobTransport` exactly as KIO9 is.

kio-mocks imports nothing from KIO1; only this test reaches the other way.
"""

import shutil
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.config_loader import DispatchConfig
from dispatch.agent_registry import load_agent_registry
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import STATUS_OK, execute_workflow
from dispatch.workflow_planner import parse_plan

MOCKS = Path(__file__).resolve().parents[1] / "kio-mocks"
sys.path.insert(0, str(MOCKS))

from kio_mocks.protocol import Capability, Kio, serve


def start(kio: Kio) -> tuple[Any, str]:
    server = serve(kio, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def fleet() -> Iterator[dict[str, str]]:
    from kio_mocks.kio2 import KIO as KIO2
    from kio_mocks.kio3 import KIO as KIO3
    from kio_mocks.kio4 import KIO as KIO4
    from kio_mocks.kio6 import KIO as KIO6
    from kio_mocks.kio7 import KIO as KIO7
    from kio_mocks.kio11 import KIO as KIO11

    running = {kio.kio_id: start(kio) for kio in (KIO2, KIO3, KIO4, KIO6, KIO7, KIO11)}
    yield {kio_id: address for kio_id, (_, address) in running.items()}

    for server, _ in running.values():
        server.shutdown()
        server.server_close()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    copy = tmp_path / "shop-project"
    shutil.copytree(MOCKS / "sample", copy)
    return copy


def test_uc1_03_runs_through_kio1s_own_executor(
    fleet: dict[str, str], project: Path
) -> None:
    registry = load_agent_registry(
        {
            "KIO11": {"endpoint": fleet["KIO11"], "capabilities": ["test_automation"]},
            "KIO2": {"endpoint": fleet["KIO2"], "capabilities": ["diagnosis"]},
            "KIO7": {"endpoint": fleet["KIO7"], "capabilities": ["code_generation"]},
        }
    )
    plan = parse_plan(
        {
            "workflow_id": "wf-uc1-03",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "KIO11",
                    "capability": "test_automation",
                    "task": "Run the tests",
                },
                {
                    "step_id": "s2",
                    "agent_id": "KIO2",
                    "capability": "diagnosis",
                    "task": "Localise the failure",
                    "depends_on": ["s1"],
                },
                {
                    "step_id": "s3",
                    "agent_id": "KIO7",
                    "capability": "code_generation",
                    "task": "Propose a fix",
                    "depends_on": ["s2"],
                },
            ],
        }
    )

    result = execute_workflow(
        plan,
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=120,
        max_parallel_steps=1,
        workspace_data={"repository": {"path": str(project)}},
    )

    tests, diagnosis, fix = result.results
    assert [step.status for step in result.results] == [STATUS_OK] * 3
    assert tests.verdict == "defect"
    assert diagnosis.output is not None and diagnosis.output["fault"]["line"] == 25
    assert fix.output is not None and fix.output["verified"]["failed"] == []
    assert fix.verdict == "clean"


def test_uc1_01_02_runs_through_kio1s_own_executor(fleet: dict[str, str]) -> None:
    registry = load_agent_registry(
        {
            "KIO3": {
                "endpoint": fleet["KIO3"],
                "capabilities": ["requirements_engineering"],
            },
            "KIO4": {
                "endpoint": fleet["KIO4"],
                "capabilities": ["architecture_design"],
            },
            "KIO7": {"endpoint": fleet["KIO7"], "capabilities": ["code_generation"]},
            "KIO6": {"endpoint": fleet["KIO6"], "capabilities": ["quality_analysis"]},
        }
    )
    steps = [
        (
            "KIO3",
            "requirements_engineering",
            "Customers want bulk discounts: 5 or more items get 5%.",
        ),
        ("KIO4", "architecture_design", "Design the architecture"),
        ("KIO7", "code_generation", "Write the skeleton"),
        ("KIO6", "quality_analysis", "Check the code against the requirements"),
    ]
    plan = parse_plan(
        {
            "workflow_id": "wf-uc1-01",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": f"s{index}",
                    "agent_id": agent,
                    "capability": capability,
                    "task": task,
                    **({"depends_on": [f"s{index - 1}"]} if index > 1 else {}),
                }
                for index, (agent, capability, task) in enumerate(steps, 1)
            ],
        }
    )

    result = execute_workflow(
        plan,
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=120,
        max_parallel_steps=1,
    )

    _, design, skeleton, check = result.results
    assert [step.status for step in result.results] == [STATUS_OK] * 4
    assert design.output is not None
    assert design.output["traceability"]["untraced_requirements"] == []
    assert skeleton.output is not None
    assert "Traces: FR-01." in skeleton.output["files"][0]["content"]
    assert check.output is not None
    assert [row["status"] for row in check.output["alignment"]] == ["stub"]
    assert check.verdict == "inconclusive"


def slow_review(request: Any) -> dict[str, Any]:
    return {"summary": f"Reviewed: {request.task}", "gate": "pass"}


def test_a_job_kio_is_polled_by_kio1_as_kio9_is() -> None:
    server, address = start(
        Kio(
            kio_id="KIO9",
            title="A reviewer that takes its time",
            description="Reviews.",
            capabilities=[Capability("compliance_review", "Reviews.", slow_review)],
            jobs=True,
            job_delay=1.0,
        )
    )
    registry = load_agent_registry(
        {
            "KIO9": {
                "endpoint": address,
                "path": "/jobs",
                "protocol": "kio1-jobs",
                "poll": {"path": "/jobs/{job_id}", "interval_s": 0.2, "timeout_s": 30},
                "capabilities": ["compliance_review"],
            }
        }
    )
    plan = parse_plan(
        {
            "workflow_id": "wf-job",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "KIO9",
                    "capability": "compliance_review",
                    "task": "Review the report",
                }
            ],
        }
    )

    try:
        result = execute_workflow(
            plan,
            registry,
            build_transport(DispatchConfig(enabled=True, registry=registry)),
            timeout=60,
            max_parallel_steps=1,
        )
    finally:
        server.shutdown()
        server.server_close()

    step = result.results[0]
    assert step.status == STATUS_OK
    assert (
        step.output is not None
        and step.output["summary"] == "Reviewed: Review the report"
    )
    assert step.verdict == "clean"  # gate: pass
