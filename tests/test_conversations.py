"""Tests for conversations over the APIs: KIO1's and the Session Manager's.

The properties that matter: a request without a conversation starts one and
says which; a request naming one joins it, and a name that does not exist is
a 404, never a silent new conversation; a follow-up is planned with what came
before it, briefly; a conversation reads back with every request in order;
and deleting history takes the registry token.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.session_client import SessionClient, SessionClientError
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from services.session_manager.app import app as session_manager_app
from services.session_manager.app import get_repository
from services.session_manager.models import UpdateSessionRequest
from services.session_manager.repository import InMemorySessionRepository
from services.session_manager.storage import EmbeddedSessionClient
from services.workflow_api.runner import conversation_context
from tests.test_review_loop import Model, make_runner, plan
from tests.test_services_integration import _serve
from tests.test_workflow_api import SpyRunner

AUTHORISED = {"Authorization": "Bearer test-token"}


@pytest.fixture
def repository() -> InMemorySessionRepository:
    return InMemorySessionRepository()


@pytest.fixture
def spy() -> SpyRunner:
    return SpyRunner()


@pytest.fixture
def client(
    repository: InMemorySessionRepository,
    spy: SpyRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """KIO1's API, keeping its history in a repository the test can read."""
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "test-token")
    app.dependency_overrides[get_state] = lambda: _State(
        runner=spy,  # type: ignore[arg-type]
        session_client=EmbeddedSessionClient(repository),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


def run(client: TestClient, request: str, conversation_id: Any = None) -> Any:
    body: dict[str, Any] = {"request": request}

    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)

    return client.post("/workflow/run", json=body)


# ── KIO1's API ──────────────────────────────────────────────────────────────


def test_a_request_without_a_conversation_starts_one(client: TestClient) -> None:
    response = run(client, "Why does average() crash?")

    assert response.status_code == 202
    conversation_id = response.json()["conversation_id"]
    listed = client.get("/conversations").json()["conversations"]
    assert [item["conversation_id"] for item in listed] == [conversation_id]
    assert listed[0]["title"] == "Why does average() crash?"


def test_a_request_naming_a_conversation_joins_it(client: TestClient) -> None:
    first = run(client, "Why does average() crash?").json()

    second = run(client, "Now add type hints", first["conversation_id"]).json()

    assert second["conversation_id"] == first["conversation_id"]
    assert len(client.get("/conversations").json()["conversations"]) == 1


def test_a_request_naming_an_unknown_conversation_is_a_404(
    client: TestClient,
) -> None:
    response = run(client, "Now add type hints", uuid4())

    assert response.status_code == 404
    assert client.get("/conversations").json()["conversations"] == []


def test_a_new_chat_is_listed_before_its_first_request(client: TestClient) -> None:
    created = client.post("/conversations")

    assert created.status_code == 201
    listed = client.get("/conversations").json()["conversations"]
    assert [item["conversation_id"] for item in listed] == [
        created.json()["conversation_id"]
    ]


def test_a_conversation_reads_back_with_each_request_and_answer(
    client: TestClient, repository: InMemorySessionRepository
) -> None:
    first = run(client, "Why does average() crash?").json()
    run(client, "Now add type hints", first["conversation_id"])
    repository.update_session(
        UUID(first["session_id"]),
        UpdateSessionRequest(status="completed", answer="An empty list [Reader]"),
    )

    detail = client.get(f"/conversations/{first['conversation_id']}").json()

    assert detail["conversation"]["title"] == "Why does average() crash?"
    assert [item["session"]["request_text"] for item in detail["sessions"]] == [
        "Why does average() crash?",
        "Now add type hints",
    ]
    assert detail["sessions"][0]["session"]["answer"] == "An empty list [Reader]"


def test_a_follow_up_is_run_with_the_answer_before_it(
    client: TestClient, repository: InMemorySessionRepository, spy: SpyRunner
) -> None:
    first = run(client, "Why does average() crash?").json()
    repository.update_session(
        UUID(first["session_id"]),
        UpdateSessionRequest(status="completed", answer="It divides by zero [Reader]"),
    )

    run(client, "Now add type hints", first["conversation_id"])

    assert spy.earlier[0] == ""
    assert "- Request: Why does average() crash?" in spy.earlier[1]
    assert "  Answer: It divides by zero [Reader]" in spy.earlier[1]
    assert "Now add type hints" not in spy.earlier[1]


def test_an_unknown_conversation_is_a_404(client: TestClient) -> None:
    assert client.get(f"/conversations/{uuid4()}").status_code == 404


def test_deleting_a_conversation_needs_the_token(client: TestClient) -> None:
    conversation_id = run(client, "Anything").json()["conversation_id"]

    assert client.delete(f"/conversations/{conversation_id}").status_code == 401
    assert client.get(f"/conversations/{conversation_id}").status_code == 200


def test_a_deleted_conversation_is_gone(client: TestClient) -> None:
    conversation_id = run(client, "Anything").json()["conversation_id"]

    response = client.delete(f"/conversations/{conversation_id}", headers=AUTHORISED)

    assert response.status_code == 200
    assert client.get(f"/conversations/{conversation_id}").status_code == 404


def test_deleting_an_unknown_conversation_is_a_404(client: TestClient) -> None:
    response = client.delete(f"/conversations/{uuid4()}", headers=AUTHORISED)

    assert response.status_code == 404


# ── the context a follow-up is planned with ─────────────────────────────────


def conversation(*sessions: dict[str, Any]) -> dict[str, Any]:
    """A conversation as `GET /conversations/{id}` returns it."""
    return {
        "conversation": {"title": "t"},
        "sessions": [
            {"session": {"session_id": str(uuid4()), **session}} for session in sessions
        ],
    }


def test_the_planner_and_the_reviewer_both_see_the_conversation() -> None:
    model = Model(
        plans=[plan(("s1", "Python", "version"))],
        reviews=[{"decision": "done", "answer": "Done [Python].", "reason": "ok"}],
    )
    runner, _, _ = make_runner(model)

    runner.run(uuid4(), "Now add type hints", "Earlier in this conversation: X")

    assert model.plan_messages[0].startswith("Earlier in this conversation: X")
    assert model.plan_messages[0].endswith("\n\nRequest now: Now add type hints")
    assert "Earlier in this conversation: X" in model.review_messages[0]


def test_only_the_last_three_turns_are_kept() -> None:
    detail = conversation(*({"request_text": f"turn {n}"} for n in range(1, 6)))

    context = conversation_context(detail, uuid4())

    assert "turn 2" not in context
    assert [line for line in context.splitlines() if "Request" in line] == [
        "- Request: turn 3",
        "- Request: turn 4",
        "- Request: turn 5",
    ]


def test_the_run_being_started_and_anything_after_it_are_left_out() -> None:
    detail = conversation(
        {"request_text": "first"}, {"request_text": "now"}, {"request_text": "later"}
    )
    current = UUID(detail["sessions"][1]["session"]["session_id"])

    context = conversation_context(detail, current)

    assert "first" in context
    assert "now" not in context and "later" not in context


def test_a_first_request_has_no_context() -> None:
    detail = conversation({"request_text": "only"})
    current = UUID(detail["sessions"][0]["session"]["session_id"])

    assert conversation_context(detail, current) == ""


def test_what_the_person_clarified_is_kept() -> None:
    detail = conversation(
        {
            "request_text": "What is the weather?",
            "answer": "Sunny [Weather]",
            "clarifications": [{"question": "Which city?", "answer": "Istanbul"}],
        }
    )

    context = conversation_context(detail, uuid4())

    assert "  Clarified: Which city? → Istanbul" in context


def test_a_long_conversation_drops_its_oldest_turns_first() -> None:
    detail = conversation(
        {"request_text": "old", "answer": "a" * 1_400},
        {"request_text": "recent", "answer": "b" * 1_400},
    )

    context = conversation_context(detail, uuid4(), max_chars=2_000)

    assert len(context) <= 2_000
    assert "- Request: recent" in context
    assert "- Request: old" not in context


def test_one_turn_longer_than_the_limit_is_cut() -> None:
    detail = conversation({"request_text": "x" * 5_000})

    assert len(conversation_context(detail, uuid4(), max_chars=1_000)) <= 1_000


# ── the Session Manager, over real HTTP ─────────────────────────────────────


@pytest.fixture
def session_manager() -> Iterator[SessionClient]:
    """A Session Manager served on a real port, and a client talking to it."""
    repository = InMemorySessionRepository()
    session_manager_app.dependency_overrides[get_repository] = lambda: repository

    try:
        for url in _serve(session_manager_app):
            yield SessionClient(url)
    finally:
        session_manager_app.dependency_overrides.clear()


def test_a_conversation_round_trips_through_the_session_manager(
    session_manager: SessionClient,
) -> None:
    conversation_id = UUID(session_manager.create_conversation()["conversation_id"])
    session_manager.create_session("Why does average() crash?", conversation_id)
    session_manager.create_session("Now add type hints", conversation_id)

    detail = session_manager.get_conversation(conversation_id)

    assert detail["conversation"]["title"] == "Why does average() crash?"
    assert len(detail["sessions"]) == 2
    assert [
        item["conversation_id"] for item in session_manager.list_conversations()
    ] == [str(conversation_id)]


def test_the_session_manager_refuses_an_unknown_conversation_as_a_404(
    session_manager: SessionClient,
) -> None:
    with pytest.raises(SessionClientError) as raised:
        session_manager.create_session("Anything", uuid4())

    assert raised.value.status == 404


def test_the_session_manager_says_whether_a_deletion_found_anything(
    session_manager: SessionClient,
) -> None:
    conversation_id = UUID(session_manager.create_conversation()["conversation_id"])

    assert session_manager.delete_conversation(conversation_id) is True
    assert session_manager.delete_conversation(conversation_id) is False
