"""Tests for plans that wait for a person before they run.

The properties that matter: a plan that may reach a marked target does not
run until someone approves it; cancelling, or saying nothing until the
timeout, stops it without running a single step; and with nothing marked,
runs go exactly as they did before approval existed.
"""

import threading
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.approvals import ApprovalDesk
from core.config_loader import Config, DispatchConfig, OrchestrationConfig
from core.session_client import NullSessionClient
from dispatch.agent_registry import load_agent_entry, load_agent_registry
from dispatch.directory import AgentDirectory
from dispatch.failover import FailoverConfig
from dispatch.scoreboard import Scoreboard
from dispatch.workflow_planner import parse_plan
from services.workflow_api.runner import WorkflowRunner, guarded_targets
from tests.test_review_loop import Model, Recorder, Transport, plan

REGISTRY = load_agent_registry(
    {
        "Reader": {"type": "cli", "command": ["cat"], "capabilities": ["read"]},
        "Writer": {
            "type": "cli",
            "command": ["tee"],
            "capabilities": ["write"],
            "approval": True,
        },
        "Backup": {
            "type": "cli",
            "command": ["cp"],
            "capabilities": ["read"],
            "approval": True,
        },
    }
)


def make_runner(
    model: Model,
    desk: ApprovalDesk,
    *,
    approval: str = "auto",
    timeout: float = 5.0,
    review: bool = False,
    transport: Transport | None = None,
) -> tuple[WorkflowRunner, Recorder, Transport]:
    recorder = Recorder()
    transport = transport or Transport()
    config = Config(
        provider="fake",
        allowed_providers=frozenset({"fake"}),
        model="m",
        prompt_path="p",
        chat_directory="c",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        dispatch=DispatchConfig(enabled=True, registry=REGISTRY),
        orchestration=OrchestrationConfig(
            review=review, approval=approval, approval_timeout=timeout
        ),
    )
    runner = WorkflowRunner(
        config=config,
        prompt_template="PLAN {{AGENT_CATALOG}}",
        provider=model,
        provider_client=None,
        recorder=recorder,
        transport=transport,  # type: ignore[arg-type]
        directory=AgentDirectory(REGISTRY),
        scoreboard=Scoreboard(),
        review_template="REVIEW {{AGENT_CATALOG}}",
        approvals=desk,
    )
    return runner, recorder, transport


def answer_when_asked(desk: ApprovalDesk, approve: bool) -> threading.Thread:
    """Answer the first approval that appears, from another thread."""

    def wait_and_answer() -> None:
        for _ in range(500):
            for session in list(desk._pending):
                desk.decide(session, approve)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=wait_and_answer, daemon=True)
    thread.start()
    return thread


# ── the desk ────────────────────────────────────────────────────────────────


def test_an_answer_releases_the_waiting_run() -> None:
    desk = ApprovalDesk()
    answer_when_asked(desk, True)

    assert desk.ask("s", 1, {"steps": []}, timeout=5) is True
    assert desk.pending("s") is None


def test_silence_counts_as_no() -> None:
    assert ApprovalDesk().ask("s", 1, {}, timeout=0.05) is False


def test_answering_nothing_reports_so() -> None:
    assert ApprovalDesk().decide("nobody", True) is False


# ── which plans need it ─────────────────────────────────────────────────────


def test_a_target_asks_only_when_marked() -> None:
    assert load_agent_entry(
        "Writer", {"type": "cli", "command": ["x"], "approval": True}
    ).requires_approval
    assert not load_agent_entry(
        "Reader", {"type": "cli", "command": ["x"]}
    ).requires_approval

    with pytest.raises(ValueError, match="approval must be true or false"):
        load_agent_entry("Bad", {"type": "cli", "command": ["x"], "approval": "yes"})


def test_a_target_a_step_could_fail_over_to_counts() -> None:
    reading = parse_plan(plan(("s1", "Reader", "read")))

    # Backup also declares `read`, so with failover on the step may run there.
    assert guarded_targets(reading, REGISTRY, FailoverConfig(enabled=True)) == [
        "Backup"
    ]
    assert guarded_targets(reading, REGISTRY, FailoverConfig(enabled=False)) == []


# ── runs ────────────────────────────────────────────────────────────────────


def test_nothing_marked_runs_without_asking() -> None:
    desk = ApprovalDesk()
    runner, recorder, transport = make_runner(
        Model(plans=[plan(("s1", "Reader", "read"))], reviews=[]), desk
    )

    runner.run(uuid4(), "Read it")

    assert transport.sent == ["s1"]
    assert recorder.last("status") == "completed"


def test_an_approved_plan_runs() -> None:
    desk = ApprovalDesk()
    runner, recorder, transport = make_runner(
        Model(plans=[plan(("s1", "Writer", "write"))], reviews=[]), desk
    )
    answer_when_asked(desk, True)

    runner.run(uuid4(), "Write it")

    assert transport.sent == ["s1"]
    assert recorder.last("status") == "completed"


def test_a_cancelled_plan_runs_nothing() -> None:
    desk = ApprovalDesk()
    runner, recorder, transport = make_runner(
        Model(plans=[plan(("s1", "Writer", "write"))], reviews=[]), desk
    )
    answer_when_asked(desk, False)

    runner.run(uuid4(), "Write it")

    assert transport.sent == []
    assert recorder.last("status") == "failed"
    assert recorder.last("error") == "The plan was not approved."


def test_a_plan_nobody_answers_is_cancelled_when_time_runs_out() -> None:
    desk = ApprovalDesk()
    runner, recorder, transport = make_runner(
        Model(plans=[plan(("s1", "Writer", "write"))], reviews=[]), desk, timeout=0.05
    )

    runner.run(uuid4(), "Write it")

    assert transport.sent == []
    assert recorder.last("status") == "failed"


def test_always_asks_even_with_nothing_marked() -> None:
    desk = ApprovalDesk()
    runner, _, transport = make_runner(
        Model(plans=[plan(("s1", "Reader", "read"))], reviews=[]),
        desk,
        approval="always",
        timeout=0.05,
    )

    runner.run(uuid4(), "Read it")

    assert transport.sent == []


def test_never_runs_marked_targets_without_asking() -> None:
    desk = ApprovalDesk()
    runner, _, transport = make_runner(
        Model(plans=[plan(("s1", "Writer", "write"))], reviews=[]),
        desk,
        approval="never",
        timeout=0.05,
    )

    runner.run(uuid4(), "Write it")

    assert transport.sent == ["s1"]


def test_a_cancelled_follow_up_keeps_the_answer_so_far() -> None:
    desk = ApprovalDesk()
    model = Model(
        plans=[plan(("s1", "Reader", "read")), plan(("s1", "Writer", "write"))],
        reviews=[
            {"decision": "continue", "answer": "Read it.", "next_request": "Write it"}
        ],
    )
    runner, recorder, transport = make_runner(model, desk, review=True)
    answer_when_asked(desk, False)

    runner.run(uuid4(), "Read and write")

    assert transport.sent == ["s1"]
    assert recorder.last("answer") == "Read it."
    assert recorder.last("status") == "completed"


# ── the API ─────────────────────────────────────────────────────────────────


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "test-token")
    desk = ApprovalDesk()
    runner, _, transport = make_runner(
        Model(plans=[plan(("s1", "Writer", "write"))], reviews=[]), desk
    )
    state = _State(
        runner=runner,
        session_client=NullSessionClient(),
        directory=runner.directory,
        scoreboard=Scoreboard(),
        approvals=desk,
    )
    app.dependency_overrides[get_state] = lambda: state
    yield TestClient(app), runner, transport
    app.dependency_overrides.clear()


def test_the_waiting_plan_can_be_read_and_approved_over_the_api(served: Any) -> None:
    client, runner, transport = served
    session_id = uuid4()
    worker = threading.Thread(
        target=runner.run, args=(session_id, "Write it"), daemon=True
    )
    worker.start()

    for _ in range(500):
        waiting = client.get(f"/workflow/{session_id}/approval").json()
        if waiting["waiting"]:
            break
        time.sleep(0.01)

    assert waiting["plan"]["guarded"] == ["Writer"]
    assert waiting["plan"]["steps"][0]["agent_id"] == "Writer"

    # Approving is an administrative act.
    assert (
        client.post(
            f"/workflow/{session_id}/approval", json={"approve": True}
        ).status_code
        == 401
    )

    approved = client.post(
        f"/workflow/{session_id}/approval",
        json={"approve": True},
        headers={"Authorization": "Bearer test-token"},
    )
    worker.join(timeout=5)

    assert approved.json() == {"session_id": str(session_id), "approved": True}
    assert transport.sent == ["s1"]
    assert client.get(f"/workflow/{session_id}/approval").json()["waiting"] is False


def test_answering_a_run_that_is_not_waiting_is_a_conflict(served: Any) -> None:
    client, _, _ = served

    response = client.post(
        f"/workflow/{uuid4()}/approval",
        json={"approve": True},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 409
