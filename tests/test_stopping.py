"""Tests for stopping a run that is under way.

The properties that matter: a stopped run ends as "Stopped." and nothing
after the stop starts — no step, no review; a run waiting for a person stops
waiting at once; a model streaming its answer is cut off; and only a run
that is running can be stopped.
"""

import json
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.approvals import ApprovalDesk
from core.cancellation import STOPPED_REASON, StopBoard
from core.run_events import RunEventBus
from core.session_client import NullSessionClient
from dispatch.agent_registry import AgentEndpoint
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from dispatch.workflow_planner import ExecutionMessage
from services.workflow_api.runner import WorkflowRunner
from tests.test_approval import make_runner
from tests.test_review_loop import Model, Recorder, Transport, plan


def stoppable_runner(
    model: Any, *, approval: str = "auto", transport: Transport | None = None
) -> tuple[WorkflowRunner, Recorder, Transport, ApprovalDesk, StopBoard]:
    """A runner that can be stopped, waiting on a desk the test controls."""
    desk = ApprovalDesk()
    runner, recorder, transport = make_runner(
        model, desk, approval=approval, transport=transport
    )
    board = StopBoard()
    runner.stops = board

    return runner, recorder, transport, desk, board


def run_in_background(runner: WorkflowRunner, session_id: UUID) -> threading.Thread:
    worker = threading.Thread(
        target=runner.run, args=(session_id, "Do it"), daemon=True
    )
    worker.start()
    return worker


def wait_until_waiting(desk: ApprovalDesk, session_id: UUID) -> None:
    for _ in range(500):
        if desk.pending(str(session_id)) is not None:
            return
        time.sleep(0.01)
    raise AssertionError("the run never started waiting")


def test_a_run_waiting_for_approval_stops_without_running_anything() -> None:
    model = Model(plans=[plan(("s1", "Writer", "write"))], reviews=[])
    runner, recorder, transport, desk, board = stoppable_runner(
        model, approval="always"
    )
    session_id = uuid4()
    worker = run_in_background(runner, session_id)
    wait_until_waiting(desk, session_id)

    board.stop(str(session_id))
    desk.withdraw(str(session_id))
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert transport.sent == []
    assert recorder.last("status") == "failed"
    assert recorder.last("error") == STOPPED_REASON


def test_a_run_waiting_for_an_answer_stops() -> None:
    model = Model(plans=[{"question": "Which city?"}], reviews=[])
    runner, recorder, _, desk, board = stoppable_runner(model)
    session_id = uuid4()
    worker = run_in_background(runner, session_id)
    wait_until_waiting(desk, session_id)

    board.stop(str(session_id))
    desk.withdraw(str(session_id))
    worker.join(timeout=5)

    assert recorder.last("error") == STOPPED_REASON


class StreamingModel:
    """Writes a plan token by token, and is stopped after the first one."""

    def __init__(self, board: StopBoard, session_id: UUID) -> None:
        self.board = board
        self.session_id = session_id
        self.tokens_after_stop = 0

    def send_request(
        self,
        config: Any,
        client: Any,
        system_prompt: str,
        messages: list[dict[str, str]],
        on_token: Any = None,
    ) -> dict[str, str]:
        on_token("{")
        self.board.stop(str(self.session_id))
        on_token('"workflow_id"')
        self.tokens_after_stop += 1  # only reached if the stop was ignored
        return {"content": json.dumps(plan(("s1", "Reader", "read")))}

    @staticmethod
    def extract_content(response: dict[str, str]) -> str:
        return response["content"]


def test_a_streaming_model_is_cut_off_mid_answer() -> None:
    board = StopBoard()
    session_id = uuid4()
    model = StreamingModel(board, session_id)
    runner, recorder, transport, _, _ = stoppable_runner(model)
    runner.stops = board
    # Streaming is only asked for when someone is watching the run.
    runner.events = RunEventBus()

    runner.run(session_id, "Do it")

    assert model.tokens_after_stop == 0
    assert transport.sent == []
    assert recorder.last("error") == STOPPED_REASON


class StoppingTransport(Transport):
    """Answers the first step, and is told to stop the run while doing so."""

    def __init__(self) -> None:
        super().__init__()
        self.board: StopBoard | None = None
        self.session_id = ""

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        reply = super().send(message, agent, timeout)
        assert self.board is not None
        self.board.stop(self.session_id)
        return reply


def test_steps_not_yet_started_are_not_sent_after_a_stop() -> None:
    transport = StoppingTransport()
    model = Model(
        plans=[plan(("s1", "Reader", "read"), ("s2", "Reader", "read"))], reviews=[]
    )
    runner, recorder, _, _, board = stoppable_runner(model, transport=transport)
    session_id = uuid4()
    transport.board, transport.session_id = board, str(session_id)

    runner.run(session_id, "Do it")

    assert transport.sent == ["s1"]
    assert recorder.last("error") == STOPPED_REASON


def test_a_run_that_has_ended_cannot_be_stopped() -> None:
    model = Model(plans=[plan(("s1", "Reader", "read"))], reviews=[])
    runner, recorder, _, _, board = stoppable_runner(model)
    session_id = uuid4()

    runner.run(session_id, "Do it")

    assert board.stop(str(session_id)) is False
    assert recorder.last("status") == "completed"


# ── the endpoint ────────────────────────────────────────────────────────────


def client_with(board: StopBoard, desk: ApprovalDesk) -> TestClient:
    app.dependency_overrides[get_state] = lambda: _State(
        runner=None,  # type: ignore[arg-type]
        session_client=NullSessionClient(),
        directory=AgentDirectory.__new__(AgentDirectory),
        scoreboard=Scoreboard(),
        approvals=desk,
        stops=board,
    )
    return TestClient(app)


def test_stopping_a_run_that_is_not_running_is_refused() -> None:
    try:
        response = client_with(StopBoard(), ApprovalDesk()).post(
            f"/workflow/{uuid4()}/cancel"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_stopping_through_the_api_wakes_a_run_waiting_for_a_person() -> None:
    board, desk = StopBoard(), ApprovalDesk()
    session_id = str(uuid4())
    board.begin(session_id)
    answers: list[bool] = []
    waiter = threading.Thread(
        target=lambda: answers.append(desk.ask(session_id, 1, {}, timeout=30)),
        daemon=True,
    )
    waiter.start()
    for _ in range(500):
        if desk.pending(session_id):
            break
        time.sleep(0.01)

    try:
        response = client_with(board, desk).post(f"/workflow/{session_id}/cancel")
    finally:
        app.dependency_overrides.clear()

    waiter.join(timeout=5)

    assert response.status_code == 200
    assert answers == [False]
    assert board.stopping(session_id)
