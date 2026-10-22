import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from services.session_manager.app import app, get_repository
from services.session_manager.models import (
    TITLE_MAX_CHARS,
    Clarification,
    RecordStepRequest,
    UpdateSessionRequest,
    conversation_title,
)
from services.session_manager.repository import (
    InMemorySessionRepository,
    PostgresSessionRepository,
    SessionRepository,
    UnknownConversationError,
)
from services.session_manager.sqlite_repository import SqliteSessionRepository

DATABASE_URL_ENV = "KIO1_TEST_DATABASE_URL"


def make_step(
    step_id: str = "s1",
    agent_id: str = "KIO2",
    capability: str = "bug_localization",
    status: str = "ok",
    output: dict[str, Any] | None = None,
) -> RecordStepRequest:
    """Build a step-record request for tests."""
    return RecordStepRequest(
        step_id=step_id,
        agent_id=agent_id,
        capability=capability,
        task=f"Task for {step_id}",
        status=status,  # type: ignore[arg-type]
        duration_ms=12,
        output=output if output is not None else {"summary": f"done {step_id}"},
    )


# ── repository behaviour, exercised against every implementation ─────────────


@pytest.fixture(scope="session")
def postgres_repository() -> Iterator[PostgresSessionRepository | None]:
    """Yield a Postgres-backed repository, or None when none is configured.

    One connection pool is opened for the whole test session and closed at the
    end. Opening a pool per test leaves pools to be garbage-collected, which
    psycopg reports as an unraisable exception.
    """
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()

    if not database_url:
        yield None
        return

    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(database_url, min_size=1, max_size=4, open=True)

    try:
        repository = PostgresSessionRepository(pool)
        repository.apply_schema()

        yield repository
    finally:
        pool.close()


@pytest.fixture(
    params=["memory", "sqlite", "postgres"],
    ids=["memory", "sqlite", "postgres"],
)
def repository(
    request: pytest.FixtureRequest,
    postgres_repository: PostgresSessionRepository | None,
    tmp_path: Path,
) -> SessionRepository:
    """Return each repository implementation in turn.

    The Postgres case is skipped unless `KIO1_TEST_DATABASE_URL` points at a
    reachable database, so the suite stays green without Docker while still
    covering real SQL when one is available. Every test opens its own session,
    so the shared database needs no cleanup between them.
    """
    if request.param == "memory":
        return InMemorySessionRepository()

    if request.param == "sqlite":
        # A real file rather than `:memory:`, so WAL mode and directory
        # creation are exercised the way a first run exercises them.
        sqlite = SqliteSessionRepository(tmp_path / "nested" / "kio1.db")
        request.addfinalizer(sqlite.close)
        return sqlite

    if postgres_repository is None:
        pytest.skip(f"{DATABASE_URL_ENV} is not set")

    return postgres_repository


def test_create_session_starts_pending(repository: SessionRepository) -> None:
    session = repository.create_session("Build a scheduling app")

    assert session.status == "pending"
    assert session.request_text == "Build a scheduling app"
    assert session.workflow_id is None


def test_get_session_returns_none_for_unknown_id(
    repository: SessionRepository,
) -> None:
    assert repository.get_session(uuid4()) is None


def test_update_session_applies_only_supplied_fields(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("Build a scheduling app")

    updated = repository.update_session(
        session.session_id,
        UpdateSessionRequest(status="running", workflow_id="wf-1"),
    )

    assert updated is not None
    assert updated.status == "running"
    assert updated.workflow_id == "wf-1"
    assert updated.execution_mode is None
    assert updated.request_text == "Build a scheduling app"


def test_a_runs_answer_is_kept_with_it(repository: SessionRepository) -> None:
    session = repository.create_session("Which python?")

    repository.update_session(
        session.session_id,
        UpdateSessionRequest(status="completed", answer="Python 3.11.7 [Deneme]"),
    )

    detail = repository.get_session(session.session_id)
    listed = repository.list_sessions(10)

    assert detail is not None
    assert detail.session.answer == "Python 3.11.7 [Deneme]"
    assert (
        next(item for item in listed if item.session_id == session.session_id).answer
        == "Python 3.11.7 [Deneme]"
    )


def test_update_session_returns_none_for_unknown_id(
    repository: SessionRepository,
) -> None:
    assert (
        repository.update_session(uuid4(), UpdateSessionRequest(status="running"))
        is None
    )


def test_record_step_stores_the_step_and_its_output(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("Fix the payment bug")

    recorded = repository.record_step(session.session_id, make_step())

    assert recorded is not None
    assert recorded.step_id == "s1"
    assert recorded.output == {"summary": "done s1"}

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert [step.step_id for step in detail.steps] == ["s1"]


def test_record_step_returns_none_for_unknown_session(
    repository: SessionRepository,
) -> None:
    assert repository.record_step(uuid4(), make_step()) is None


def test_recording_a_step_twice_replaces_it(repository: SessionRepository) -> None:
    session = repository.create_session("Fix the payment bug")

    repository.record_step(session.session_id, make_step(status="error"))
    repository.record_step(session.session_id, make_step(status="ok"))

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert len(detail.steps) == 1
    assert detail.steps[0].status == "ok"


def test_sequential_runs_record_execution_order_lineage(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("Build a scheduling app")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="sequential")
    )

    repository.record_step(session.session_id, make_step(step_id="s1"))
    repository.record_step(session.session_id, make_step(step_id="s2"))
    repository.record_step(session.session_id, make_step(step_id="s3"))

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert [
        (edge.produced_by_step_id, edge.consumed_by_step_id, edge.basis)
        for edge in detail.lineage
    ] == [
        ("s1", "s2", "execution_order"),
        ("s2", "s3", "execution_order"),
    ]


def test_parallel_runs_record_no_lineage(repository: SessionRepository) -> None:
    # Recording order carries no dependency information when steps ran
    # concurrently, so inferring edges from it would be wrong.
    session = repository.create_session("Build a scheduling app")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="parallel")
    )

    repository.record_step(session.session_id, make_step(step_id="s1"))
    repository.record_step(session.session_id, make_step(step_id="s2"))

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert detail.lineage == []


def test_steps_without_output_produce_no_lineage(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("Build a scheduling app")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="sequential")
    )

    repository.record_step(session.session_id, make_step(step_id="s1"))
    repository.record_step(
        session.session_id,
        RecordStepRequest(
            step_id="s2",
            agent_id="KIO7",
            capability="code_generation",
            task="Not deployed",
            status="skipped",
            error="no_endpoint",
        ),
    )

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert detail.lineage == []
    assert detail.steps[1].output is None


# ── conversations, exercised against every implementation ───────────────────


def test_a_session_without_a_conversation_opens_one(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("Why does average() crash?")

    assert session.conversation_id is not None
    conversation = repository.get_conversation(session.conversation_id)
    assert conversation is not None
    assert conversation.title == "Why does average() crash?"


def test_a_session_joins_the_conversation_it_names(
    repository: SessionRepository,
) -> None:
    first = repository.create_session("Why does average() crash?")
    second = repository.create_session("Now add type hints", first.conversation_id)

    assert second.conversation_id == first.conversation_id
    assert [
        session.session_id
        for session in repository.conversation_sessions(first.conversation_id)  # type: ignore[arg-type]
    ] == [first.session_id, second.session_id]


def test_a_new_chat_takes_its_title_from_its_first_request(
    repository: SessionRepository,
) -> None:
    conversation = repository.create_conversation()

    repository.create_session("Fix the payment bug", conversation.conversation_id)
    repository.create_session("And add a test", conversation.conversation_id)

    found = repository.get_conversation(conversation.conversation_id)
    assert found is not None
    assert found.title == "Fix the payment bug"


def test_a_session_in_an_unknown_conversation_is_refused(
    repository: SessionRepository,
) -> None:
    with pytest.raises(UnknownConversationError):
        repository.create_session("Anything", uuid4())


def test_the_conversation_last_added_to_is_listed_first(
    repository: SessionRepository,
) -> None:
    older = repository.create_conversation("older")
    newer = repository.create_conversation("newer")

    repository.create_session("A follow-up", older.conversation_id)

    listed = [item.conversation_id for item in repository.list_conversations(200)]
    assert listed.index(older.conversation_id) < listed.index(newer.conversation_id)


def test_deleting_a_conversation_deletes_its_sessions(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("Fix the payment bug")
    repository.record_step(session.session_id, make_step())
    conversation_id = session.conversation_id
    assert conversation_id is not None

    assert repository.delete_conversation(conversation_id) is True

    assert repository.get_conversation(conversation_id) is None
    assert repository.get_session(session.session_id) is None


def test_deleting_an_unknown_conversation_says_so(
    repository: SessionRepository,
) -> None:
    assert repository.delete_conversation(uuid4()) is False


def test_the_questions_a_run_asked_are_kept_with_it(
    repository: SessionRepository,
) -> None:
    session = repository.create_session("What is the weather?")

    repository.update_session(
        session.session_id,
        UpdateSessionRequest(
            clarifications=[Clarification(question="Which city?", answer="Istanbul")]
        ),
    )

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert detail.session.clarifications == [
        Clarification(question="Which city?", answer="Istanbul")
    ]


def test_a_long_request_is_cut_at_a_word_for_a_title() -> None:
    title = conversation_title("Please " + "explain this stack trace " * 10)

    assert len(title) <= TITLE_MAX_CHARS
    assert title == "Please explain this stack trace explain this stack trace…"


def test_line_breaks_do_not_reach_a_title() -> None:
    assert conversation_title("Fix\n\n  this   bug") == "Fix this bug"


def test_sessions_from_before_conversations_are_each_given_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, request_text TEXT NOT NULL,
            status TEXT NOT NULL, workflow_id TEXT, execution_mode TEXT,
            error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO sessions VALUES ('00000000-0000-0000-0000-000000000001',
            'Which python?', 'completed', NULL, NULL, NULL,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        """)
    old.close()

    upgraded = SqliteSessionRepository(path)

    try:
        [conversation] = upgraded.list_conversations(10)
        assert conversation.title == "Which python?"
        [session] = upgraded.conversation_sessions(conversation.conversation_id)
        assert session.request_text == "Which python?"
        assert session.answer is None
    finally:
        upgraded.close()


# ── HTTP surface ────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a client backed by a fresh in-memory repository."""
    repository = InMemorySessionRepository()
    app.dependency_overrides[get_repository] = lambda: repository

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_health_reports_the_service_name(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "session-manager"}


def test_post_sessions_returns_a_new_session(client: TestClient) -> None:
    response = client.post("/sessions", json={"request_text": "Build an app"})

    assert response.status_code == 201
    body = response.json()
    assert UUID(body["session_id"])
    assert body["status"] == "pending"


def test_post_sessions_rejects_an_empty_request(client: TestClient) -> None:
    assert client.post("/sessions", json={"request_text": ""}).status_code == 422


def test_full_session_lifecycle_over_http(client: TestClient) -> None:
    session_id = client.post(
        "/sessions", json={"request_text": "Fix the payment bug"}
    ).json()["session_id"]

    patched = client.patch(
        f"/sessions/{session_id}",
        json={
            "status": "running",
            "workflow_id": "wf-1",
            "execution_mode": "sequential",
        },
    )
    assert patched.status_code == 200
    assert patched.json()["execution_mode"] == "sequential"

    step = client.post(
        f"/sessions/{session_id}/steps",
        json={
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "bug_localization",
            "task": "Locate the defect",
            "status": "ok",
            "duration_ms": 21,
            "output": {"summary": "found it"},
        },
    )
    assert step.status_code == 201

    client.patch(f"/sessions/{session_id}", json={"status": "completed"})

    detail = client.get(f"/sessions/{session_id}").json()
    assert detail["session"]["status"] == "completed"
    assert len(detail["steps"]) == 1
    assert detail["steps"][0]["output"] == {"summary": "found it"}


def test_unknown_session_is_a_404_on_every_route(client: TestClient) -> None:
    unknown = uuid4()

    assert client.get(f"/sessions/{unknown}").status_code == 404
    assert (
        client.patch(f"/sessions/{unknown}", json={"status": "running"}).status_code
        == 404
    )
    assert (
        client.post(
            f"/sessions/{unknown}/steps",
            json={
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the defect",
                "status": "ok",
            },
        ).status_code
        == 404
    )


def test_a_malformed_session_id_is_rejected(client: TestClient) -> None:
    assert client.get("/sessions/not-a-uuid").status_code == 422


def test_an_unknown_step_status_is_rejected(client: TestClient) -> None:
    session_id = client.post(
        "/sessions", json={"request_text": "Fix the payment bug"}
    ).json()["session_id"]

    response = client.post(
        f"/sessions/{session_id}/steps",
        json={
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "bug_localization",
            "task": "Locate the defect",
            "status": "maybe",
        },
    )

    assert response.status_code == 422


def test_declared_dependencies_are_recorded_as_such(
    repository: SessionRepository,
) -> None:
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
    assert {
        (edge.produced_by_step_id, edge.consumed_by_step_id): edge.basis
        for edge in detail.lineage
    } == {
        ("s1", "s2"): "declared_dependency",
        ("s1", "s3"): "declared_dependency",
        ("s2", "s4"): "declared_dependency",
        ("s3", "s4"): "declared_dependency",
    }


def test_a_declared_dependency_that_left_no_artifact_is_not_linked(
    repository: SessionRepository,
) -> None:
    # A skipped upstream step produces nothing, so there is no artifact to
    # link to and inventing an edge would misrepresent the run.
    session = repository.create_session("Build a service")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="mixed")
    )

    repository.record_step(
        session.session_id,
        RecordStepRequest(
            step_id="s1",
            agent_id="KIO4",
            capability="architecture_design",
            task="Not deployed",
            status="skipped",
            error="no_endpoint",
        ),
    )
    repository.record_step(
        session.session_id,
        RecordStepRequest(
            step_id="s2",
            agent_id="KIO7",
            capability="code_generation",
            task="Depends on a skipped step",
            status="ok",
            output={"produced_by": "s2"},
            depends_on=["s1"],
        ),
    )

    detail = repository.get_session(session.session_id)
    assert detail is not None
    assert detail.lineage == []


def test_declared_dependencies_suppress_order_inference(
    repository: SessionRepository,
) -> None:
    # s3 declares only s1, so no edge from s2 may appear even though s2 was
    # recorded immediately before it.
    session = repository.create_session("Build a service")
    repository.update_session(
        session.session_id, UpdateSessionRequest(execution_mode="sequential")
    )

    for step_id, depends_on in (("s1", []), ("s2", []), ("s3", ["s1"])):
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
        ("s1", "s2"): "execution_order",
        ("s1", "s3"): "declared_dependency",
    }


def test_recent_sessions_are_listed_newest_first() -> None:
    repository = InMemorySessionRepository()
    first = repository.create_session("first")
    second = repository.create_session("second")

    listed = repository.list_sessions(10)

    assert [session.session_id for session in listed] == [
        second.session_id,
        first.session_id,
    ]


def test_listing_sessions_honours_its_limit() -> None:
    repository = InMemorySessionRepository()

    for index in range(5):
        repository.create_session(f"request {index}")

    assert len(repository.list_sessions(2)) == 2


def test_the_sessions_route_returns_the_recent_ones(client: TestClient) -> None:
    client.post("/sessions", json={"request_text": "a request"})

    response = client.get("/sessions?limit=5")

    assert response.status_code == 200
    assert [item["request_text"] for item in response.json()] == ["a request"]


def test_an_absurd_limit_is_clamped(client: TestClient) -> None:
    # One call must not be able to pull the whole table.
    assert client.get("/sessions?limit=100000").status_code == 200
