"""Round-trips against a real NATS broker.

Skipped unless `KIO1_TEST_NATS_SERVERS` names a reachable server, so the suite
stays green without Docker. Everything else about NATS is covered against a
fake connection in `test_nats_transport.py`; this file is what proves the two
agree with a real broker in between.

    KIO1_TEST_NATS_SERVERS=nats://localhost:4222 pytest tests/test_nats_live.py
"""

import os
from collections.abc import Iterator

import pytest

from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.nats_transport import NatsAgentTransport
from dispatch.workflow_executor import STATUS_ERROR, STATUS_OK, execute_workflow
from dispatch.workflow_planner import parse_plan
from tools.dummy_agent import NatsStubAgent

SERVERS_ENV = "KIO1_TEST_NATS_SERVERS"


@pytest.fixture(scope="module")
def servers() -> list[str]:
    """Return the configured NATS servers, skipping the module without them."""
    configured = os.environ.get(SERVERS_ENV, "").strip()

    if not configured:
        pytest.skip(f"{SERVERS_ENV} is not set")

    return [entry.strip() for entry in configured.split(",") if entry.strip()]


@pytest.fixture
def kio2_stub(servers: list[str]) -> Iterator[NatsStubAgent]:
    """Serve a KIO2 stub on the broker for the duration of a test."""
    stub = NatsStubAgent(
        agent_id="KIO2",
        servers=servers,
        fail_capabilities=frozenset({"diagnosis"}),
    )
    stub.start()

    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def transport(servers: list[str]) -> Iterator[NatsAgentTransport]:
    """Yield a transport pointed at the broker, closed afterwards."""
    instance = NatsAgentTransport(servers)

    try:
        yield instance
    finally:
        instance.close()


def build_plan(steps: list[dict[str, object]], mode: str = "sequential") -> object:
    """Parse a plan for these tests."""
    return parse_plan(
        {
            "workflow_id": "wf-live01",
            "execution_mode": mode,
            "steps": steps,
            "explanation": "Live NATS test.",
        }
    )


KIO2 = AgentEndpoint(
    agent_id="KIO2",
    endpoint="http://localhost:8102",
    path="/execute",
    capabilities=frozenset({"bug_localization", "fix_recommendation", "diagnosis"}),
)


def run(plan: object, transport: NatsAgentTransport) -> object:
    """Execute a plan over the live broker."""
    return execute_workflow(
        plan,  # type: ignore[arg-type]
        AgentRegistry({"KIO2": KIO2}),
        transport,
        timeout=10.0,
        max_parallel_steps=2,
    )


def test_a_step_round_trips_through_a_real_broker(
    kio2_stub: NatsStubAgent,
    transport: NatsAgentTransport,
) -> None:
    plan = build_plan(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the null dereference",
            }
        ]
    )

    result = run(plan, transport)

    assert result.succeeded == 1  # type: ignore[attr-defined]
    step = result.results[0]  # type: ignore[attr-defined]
    assert step.status == STATUS_OK
    assert step.output["received_task"] == "Locate the null dereference"


def test_declared_dependencies_feed_results_forward_over_nats(
    kio2_stub: NatsStubAgent,
    transport: NatsAgentTransport,
) -> None:
    plan = build_plan(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the defect",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO2",
                "capability": "fix_recommendation",
                "task": "Recommend a fix",
                "depends_on": ["s1"],
            },
        ],
        "mixed",
    )

    result = run(plan, transport)

    steps = {entry.step_id: entry for entry in result.results}  # type: ignore[attr-defined]
    assert steps["s1"].status == STATUS_OK
    assert steps["s2"].status == STATUS_OK
    # The stub echoes the upstream ids it was handed, so this proves the data
    # field survived the trip over a real broker.
    assert steps["s2"].output["received_upstream"] == ["s1"]


def test_an_agent_reported_error_survives_the_broker(
    kio2_stub: NatsStubAgent,
    transport: NatsAgentTransport,
) -> None:
    plan = build_plan(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "diagnosis",
                "task": "Diagnose the defect",
            }
        ]
    )

    result = run(plan, transport)

    assert result.results[0].status == STATUS_ERROR  # type: ignore[attr-defined]
    assert "Simulated failure" in result.results[0].error  # type: ignore[attr-defined]


def test_a_subject_nobody_listens_on_times_out(
    transport: NatsAgentTransport,
) -> None:
    # No stub is started here, so nothing answers and the timeout must be
    # reported as a failed step rather than hanging the workflow.
    plan = build_plan(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Nobody is listening",
            }
        ]
    )

    result = execute_workflow(
        plan,  # type: ignore[arg-type]
        AgentRegistry({"KIO2": KIO2}),
        transport,
        timeout=1.0,
        max_parallel_steps=1,
    )

    assert result.results[0].status == STATUS_ERROR  # type: ignore[attr-defined]
