import threading
from typing import Any

import pytest

from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.agent_transport import AgentTransportError
from dispatch.workflow_executor import (
    STATUS_ERROR,
    STATUS_NOT_RUN,
    STATUS_OK,
    STATUS_SKIPPED,
    execute_workflow,
)
from dispatch.workflow_planner import (
    ExecutionMessage,
    descendants_of,
    execution_levels,
    has_dependencies,
    parse_plan,
)
from services.session_manager.models import RecordStepRequest, UpdateSessionRequest
from services.session_manager.repository import InMemorySessionRepository

AGENTS = {
    agent_id: AgentEndpoint(
        agent_id, f"http://localhost:81{index:02d}", "/execute", frozenset()
    )
    for index, agent_id in enumerate(("KIO3", "KIO4", "KIO5", "KIO7"))
}


def build_plan(steps: list[dict[str, Any]], execution_mode: str = "mixed") -> Any:
    """Parse a plan from raw step objects."""
    return parse_plan(
        {
            "workflow_id": "wf-dep01",
            "execution_mode": execution_mode,
            "steps": steps,
            "explanation": "Dependency test.",
        }
    )


def step(
    step_id: str,
    agent_id: str,
    depends_on: list[str] | None = None,
    capability: str = "code_generation",
) -> dict[str, Any]:
    """Build a raw plan step, optionally with dependencies."""
    raw: dict[str, Any] = {
        "step_id": step_id,
        "agent_id": agent_id,
        "capability": capability,
        "task": f"Task {step_id}",
    }

    if depends_on is not None:
        raw["depends_on"] = depends_on

    return raw


DIAMOND = [
    step("s1", "KIO3"),
    step("s2", "KIO4", ["s1"]),
    step("s3", "KIO5", ["s1"]),
    step("s4", "KIO7", ["s2", "s3"]),
]


class RecordingTransport:
    """Answers dispatches and records the messages it received."""

    def __init__(self, failures: frozenset[str] = frozenset()) -> None:
        self.failures = failures
        self.received: list[ExecutionMessage] = []
        self.lock = threading.Lock()

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        with self.lock:
            self.received.append(message)

        if message.step_id in self.failures:
            raise AgentTransportError(f"{message.agent_id} is unreachable")

        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "status": "ok",
            "output": {"produced_by": message.step_id},
        }


def run(
    steps: list[dict[str, Any]],
    transport: RecordingTransport,
    execution_mode: str = "mixed",
    agents: dict[str, AgentEndpoint] | None = None,
) -> Any:
    """Execute a plan against the recording transport."""
    return execute_workflow(
        build_plan(steps, execution_mode),
        AgentRegistry(agents if agents is not None else AGENTS),
        transport,
        timeout=5.0,
        max_parallel_steps=4,
    )


# ── parsing ──────────────────────────────────────────────────────────────────


def test_a_plan_without_dependencies_is_unchanged() -> None:
    plan = build_plan([step("s1", "KIO3"), step("s2", "KIO4")], "sequential")

    assert not has_dependencies(plan)
    assert all(candidate.depends_on == () for candidate in plan.steps)


def test_declared_dependencies_are_read() -> None:
    plan = build_plan(DIAMOND)

    assert has_dependencies(plan)
    assert plan.steps[3].depends_on == ("s2", "s3")


def test_levels_group_independent_steps_together() -> None:
    assert execution_levels(build_plan(DIAMOND)) == (
        ("s1",),
        ("s2", "s3"),
        ("s4",),
    )


def test_a_plan_without_dependencies_is_a_single_level() -> None:
    plan = build_plan([step("s1", "KIO3"), step("s2", "KIO4")])

    assert execution_levels(plan) == (("s1", "s2"),)


def test_descendants_follow_the_graph_transitively() -> None:
    plan = build_plan(DIAMOND)

    assert descendants_of(plan, "s1") == frozenset({"s2", "s3", "s4"})
    assert descendants_of(plan, "s2") == frozenset({"s4"})
    assert descendants_of(plan, "s4") == frozenset()


# ── a bad optional field must never break a working plan ────────────────────


@pytest.mark.parametrize(
    "bad_value",
    ["s1", 1, [1, 2], [""], {"s1": True}],
)
def test_a_malformed_depends_on_is_ignored(bad_value: Any) -> None:
    plan = parse_plan(
        {
            "workflow_id": "wf-dep01",
            "execution_mode": "sequential",
            "steps": [
                step("s1", "KIO3"),
                {**step("s2", "KIO4"), "depends_on": bad_value},
            ],
        }
    )

    assert plan.steps[1].depends_on == ()


def test_a_dependency_on_an_unknown_step_is_dropped() -> None:
    plan = build_plan([step("s1", "KIO3"), step("s2", "KIO4", ["s1", "s99"])])

    assert plan.steps[1].depends_on == ("s1",)


def test_a_self_dependency_is_ignored() -> None:
    plan = build_plan([step("s1", "KIO3", ["s1"])])

    assert plan.steps[0].depends_on == ()


def test_a_cycle_falls_back_to_the_declared_execution_mode() -> None:
    plan = build_plan(
        [
            step("s1", "KIO3", ["s2"]),
            step("s2", "KIO4", ["s1"]),
        ],
        "sequential",
    )

    assert not has_dependencies(plan)
    assert plan.execution_mode == "sequential"


def test_duplicate_dependencies_are_collapsed() -> None:
    plan = build_plan([step("s1", "KIO3"), step("s2", "KIO4", ["s1", "s1"])])

    assert plan.steps[1].depends_on == ("s1",)


# ── execution order ──────────────────────────────────────────────────────────


def test_dependencies_are_honoured_before_dependents_run() -> None:
    transport = RecordingTransport()

    result = run(DIAMOND, transport)

    order = [message.step_id for message in transport.received]
    assert order[0] == "s1"
    assert order[-1] == "s4"
    assert set(order[1:3]) == {"s2", "s3"}
    assert all(entry.status == STATUS_OK for entry in result.results)


def test_a_failed_dependency_blocks_only_its_dependents() -> None:
    transport = RecordingTransport(failures=frozenset({"s2"}))

    result = run(DIAMOND, transport)

    statuses = {entry.step_id: entry.status for entry in result.results}
    assert statuses["s1"] == STATUS_OK
    assert statuses["s2"] == STATUS_ERROR
    # s3 does not depend on s2, so it must still run.
    assert statuses["s3"] == STATUS_OK
    assert statuses["s4"] == STATUS_NOT_RUN
    assert "s4" not in {message.step_id for message in transport.received}


def test_a_skipped_dependency_blocks_its_dependents() -> None:
    # KIO5 is not deployed, so s3 is skipped and s4 can never get its input.
    transport = RecordingTransport()
    agents = {key: value for key, value in AGENTS.items() if key != "KIO5"}

    result = run(DIAMOND, transport, agents=agents)

    statuses = {entry.step_id: entry.status for entry in result.results}
    assert statuses["s3"] == STATUS_SKIPPED
    assert statuses["s4"] == STATUS_NOT_RUN


def test_sequential_mode_still_runs_one_step_at_a_time() -> None:
    transport = RecordingTransport()

    run(DIAMOND, transport, execution_mode="sequential")

    assert [message.step_id for message in transport.received] == [
        "s1",
        "s2",
        "s3",
        "s4",
    ]


# ── feeding results forward through the protocol's data field ───────────────


def test_upstream_output_travels_in_the_data_field() -> None:
    transport = RecordingTransport()

    run(DIAMOND, transport)

    sent = {message.step_id: message for message in transport.received}

    # A step with no dependencies keeps the empty data the protocol shows.
    assert sent["s1"].data == {}
    assert sent["s2"].data == {"upstream": {"s1": {"produced_by": "s1"}}}
    assert sent["s4"].data == {
        "upstream": {
            "s2": {"produced_by": "s2"},
            "s3": {"produced_by": "s3"},
        }
    }


def test_the_execution_message_gains_no_new_fields() -> None:
    # data is the protocol's own extension point, so feeding results forward
    # must not introduce any key outside the agreed message shape.
    transport = RecordingTransport()

    run(DIAMOND, transport)

    for message in transport.received:
        assert set(message.to_payload()) == {
            "workflow_id",
            "step_id",
            "agent_id",
            "capability",
            "endpoint",
            "task",
            "data",
        }


def test_plans_without_dependencies_still_send_empty_data() -> None:
    transport = RecordingTransport()

    run([step("s1", "KIO3"), step("s2", "KIO4")], transport, "sequential")

    assert all(message.data == {} for message in transport.received)


# ── lineage recorded from declared dependencies ─────────────────────────────


def test_declared_dependencies_are_recorded_as_such() -> None:
    repository = InMemorySessionRepository()
    session = repository.create_session("Build a service")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="mixed")
    )

    for step_id, depends_on in (
        ("s1", []),
        ("s2", ["s1"]),
        ("s3", ["s1"]),
        ("s4", ["s2", "s3"]),
    ):
        repository.record_step(
            session.session_id,
            RecordStepRequest(
                step_id=step_id,
                agent_id="KIO7",
                capability="code_generation",
                task=f"Task {step_id}",
                status="ok",
                output={"produced_by": step_id},
                depends_on=depends_on,
            ),
        )

    detail = repository.get_session(session.session_id)
    assert detail is not None
    edges = {
        (edge.produced_by_step_id, edge.consumed_by_step_id): edge.basis
        for edge in detail.lineage
    }

    assert edges == {
        ("s1", "s2"): "declared_dependency",
        ("s1", "s3"): "declared_dependency",
        ("s2", "s4"): "declared_dependency",
        ("s3", "s4"): "declared_dependency",
    }


def test_without_declared_dependencies_lineage_stays_order_derived() -> None:
    repository = InMemorySessionRepository()
    session = repository.create_session("Build a service")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="sequential")
    )

    for step_id in ("s1", "s2"):
        repository.record_step(
            session.session_id,
            RecordStepRequest(
                step_id=step_id,
                agent_id="KIO7",
                capability="code_generation",
                task=f"Task {step_id}",
                status="ok",
                output={"produced_by": step_id},
            ),
        )

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert [edge.basis for edge in detail.lineage] == ["execution_order"]
