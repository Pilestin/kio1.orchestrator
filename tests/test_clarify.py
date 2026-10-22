"""Tests for the planner asking the person a question before it plans.

The properties that matter: a request missing something only the person
knows gets a question rather than a plan with an invented value; the answer
reaches the planner and the reviewer; and a run never waits for ever or
asks without end.
"""

import threading
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.approvals import ApprovalDesk
from core.session_client import NullSessionClient
from dispatch.scoreboard import Scoreboard
from tests.test_approval import make_runner
from tests.test_review_loop import Model, plan

WHICH_CITY = {"question": "Which city?"}


def answer_when_asked(desk: ApprovalDesk, *replies: str) -> threading.Thread:
    """Answer each question that appears with the next reply."""

    def wait_and_answer() -> None:
        remaining = list(replies)
        for _ in range(1000):
            if not remaining:
                return
            for session in list(desk._pending):
                if desk.answer(session, remaining[0]):
                    remaining.pop(0)
            time.sleep(0.01)

    thread = threading.Thread(target=wait_and_answer, daemon=True)
    thread.start()
    return thread


# ── the desk ────────────────────────────────────────────────────────────────


def test_a_question_waits_for_its_answer() -> None:
    desk = ApprovalDesk()
    answer_when_asked(desk, "Istanbul")

    assert desk.ask_question("s", 1, "Which city?", timeout=5) == "Istanbul"


def test_an_unanswered_question_gives_nothing() -> None:
    assert ApprovalDesk().ask_question("s", 1, "Which city?", timeout=0.05) is None


def test_an_approval_cannot_answer_a_question() -> None:
    desk = ApprovalDesk()
    results: list[Any] = []
    worker = threading.Thread(
        target=lambda: results.append(desk.ask_question("s", 1, "Q?", timeout=0.3))
    )
    worker.start()
    time.sleep(0.05)

    assert desk.decide("s", True) is False
    assert desk.pending("s").kind == "question"
    worker.join()


# ── runs ────────────────────────────────────────────────────────────────────


def test_the_answer_is_planned_with() -> None:
    desk = ApprovalDesk()
    model = Model(plans=[WHICH_CITY, plan(("s1", "Reader", "read"))], reviews=[])
    runner, recorder, transport = make_runner(model, desk)
    answer_when_asked(desk, "Istanbul")

    runner.run(uuid4(), "What is the weather?")

    assert model.plan_messages[1].endswith("Q: Which city?\nA: Istanbul")
    assert transport.sent == ["s1"]
    assert recorder.last("status") == "completed"


def test_the_reviewer_sees_what_the_person_said() -> None:
    desk = ApprovalDesk()
    model = Model(
        plans=[WHICH_CITY, plan(("s1", "Reader", "read"))],
        reviews=[{"decision": "done", "answer": "Sunny in Istanbul."}],
    )
    runner, _, _ = make_runner(model, desk, review=True)
    answer_when_asked(desk, "Istanbul")

    runner.run(uuid4(), "What is the weather?")

    assert (
        "Clarified with the user:\n- Q: Which city?\n  A: Istanbul"
        in model.review_messages[0]
    )


def test_a_planner_that_keeps_asking_is_stopped() -> None:
    desk = ApprovalDesk()
    model = Model(
        plans=[
            WHICH_CITY,
            {"question": "Which district?"},
            {"question": "Which street?"},
        ],
        reviews=[],
    )
    runner, recorder, transport = make_runner(model, desk)
    runner.config.orchestration = type(runner.config.orchestration)(
        review=False, max_questions=2
    )
    answer_when_asked(desk, "Istanbul", "Kadikoy")

    runner.run(uuid4(), "What is the weather?")

    assert transport.sent == []
    assert recorder.last("status") == "failed"
    assert "still needs more after 2 answer(s): Which street?" in recorder.last("error")


def test_with_asking_off_the_run_says_what_was_missing() -> None:
    desk = ApprovalDesk()
    runner, recorder, _ = make_runner(Model(plans=[WHICH_CITY], reviews=[]), desk)
    runner.config.orchestration = type(runner.config.orchestration)(
        review=False, clarify=False
    )

    runner.run(uuid4(), "What is the weather?")

    assert (
        recorder.last("error")
        == "ValueError: The planner needs more information: Which city?"
    )


def test_an_unanswered_question_ends_the_run() -> None:
    desk = ApprovalDesk()
    runner, recorder, _ = make_runner(Model(plans=[WHICH_CITY], reviews=[]), desk)
    runner.config.orchestration = type(runner.config.orchestration)(
        review=False, question_timeout=10
    )
    # Shorter than the configured minimum, for the test's sake.
    original = desk.ask_question
    desk.ask_question = lambda s, r, q, timeout: original(s, r, q, 0.05)  # type: ignore[method-assign]

    runner.run(uuid4(), "What is the weather?")

    assert "The question was not answered: Which city?" in recorder.last("error")


def test_a_plan_is_not_mistaken_for_a_question() -> None:
    desk = ApprovalDesk()
    with_both = {**plan(("s1", "Reader", "read")), "question": "Anything else?"}
    runner, _, transport = make_runner(Model(plans=[with_both], reviews=[]), desk)

    runner.run(uuid4(), "Read it")

    assert transport.sent == ["s1"]


# ── the API ─────────────────────────────────────────────────────────────────


@pytest.fixture
def served() -> Any:
    desk = ApprovalDesk()
    runner, _, transport = make_runner(
        Model(plans=[WHICH_CITY, plan(("s1", "Reader", "read"))], reviews=[]), desk
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


def test_the_question_is_read_and_answered_over_the_api(served: Any) -> None:
    client, runner, transport = served
    session_id = uuid4()
    worker = threading.Thread(
        target=runner.run, args=(session_id, "Weather?"), daemon=True
    )
    worker.start()

    for _ in range(500):
        waiting = client.get(f"/workflow/{session_id}/waiting").json()
        if waiting["waiting"]:
            break
        time.sleep(0.01)

    assert waiting["kind"] == "question"
    assert waiting["plan"]["question"] == "Which city?"

    # Answering is not an administrative act: no token.
    response = client.post(f"/workflow/{session_id}/answer", json={"text": "Istanbul"})
    worker.join(timeout=5)

    assert response.json() == {"session_id": str(session_id), "answered": True}
    assert transport.sent == ["s1"]


def test_answering_a_run_that_asked_nothing_is_a_conflict(served: Any) -> None:
    client, _, _ = served

    response = client.post(f"/workflow/{uuid4()}/answer", json={"text": "Istanbul"})

    assert response.status_code == 409
