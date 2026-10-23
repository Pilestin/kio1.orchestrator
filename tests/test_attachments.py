"""Tests for attaching files to a request and choosing targets with @Name.

The properties that matter: a file is stored where an agent can read it, and
never anywhere a name could steer it; its text reaches the planner and its
path reaches every step; a run names only files that were uploaded; and
`@Name` picks targets that exist, from the request alone.
"""

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.attachments import (
    MAX_BYTES,
    MAX_QUOTED_CHARS,
    AttachmentError,
    AttachmentStore,
    describe,
    safe_name,
)
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from services.session_manager.repository import InMemorySessionRepository
from services.session_manager.storage import EmbeddedSessionClient
from services.workflow_api.runner import mentioned_targets
from tests.test_review_loop import REGISTRY, Model, Transport, make_runner, plan
from tests.test_workflow_api import SpyRunner

# ── the store ───────────────────────────────────────────────────────────────


def test_a_stored_file_can_be_read_back(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)

    saved = store.save("stats.py", b"def average(values): ...")
    found = store.get(saved.attachment_id)

    assert found is not None
    assert found.name == "stats.py"
    assert found.path.read_bytes() == b"def average(values): ..."


def test_a_name_cannot_put_a_file_outside_the_store(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "store")

    saved = store.save("../../evil.py", b"x")

    assert saved.name == "evil.py"
    assert saved.path.parent.parent == (tmp_path / "store").resolve()


def test_only_an_id_the_store_made_is_looked_up(tmp_path: Path) -> None:
    assert AttachmentStore(tmp_path).get("../etc") is None


def test_a_file_over_the_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AttachmentError):
        AttachmentStore(tmp_path).save("big.log", b"x" * (MAX_BYTES + 1))


def test_a_name_that_is_only_dots_is_refused() -> None:
    with pytest.raises(AttachmentError):
        safe_name("..")


def test_text_is_quoted_for_the_planner(tmp_path: Path) -> None:
    saved = AttachmentStore(tmp_path).save("stats.py", b"def average(values): ...")

    section = describe([saved])

    assert section.startswith("Attached files:\n- stats.py at ")
    assert "```\ndef average(values): ...\n```" in section


def test_a_binary_file_is_named_but_not_quoted(tmp_path: Path) -> None:
    saved = AttachmentStore(tmp_path).save("image.png", b"\x89PNG\x00\x00")

    assert "image.png (6 bytes, not quoted)" in describe([saved])


def test_text_past_the_budget_is_named_but_not_quoted(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    small = store.save("stats.py", b"def average(values): ...")
    large = store.save("run.log", b"x" * (MAX_QUOTED_CHARS + 1))

    section = describe([large, small])

    assert "def average(values)" in section
    assert "run.log (" in section and "not quoted" in section


# ── @Name ───────────────────────────────────────────────────────────────────


def test_a_mentioned_target_is_found_whatever_its_case() -> None:
    assert mentioned_targets("ask @python which version", REGISTRY) == ["Python"]


def test_an_address_or_an_unknown_name_is_not_a_target() -> None:
    request = "mail me at a@Python.org, or ask @Nobody"

    assert mentioned_targets(request, REGISTRY) == []


def test_each_target_is_chosen_once_in_the_order_named() -> None:
    request = "@GitHistory then @Python, and @githistory again."

    assert mentioned_targets(request, REGISTRY) == ["GitHistory", "Python"]


# ── the run ─────────────────────────────────────────────────────────────────


class DataTransport(Transport):
    """Answers every step, and keeps the `data` each step was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.data: list[dict[str, Any]] = []

    def send(
        self, message: Any, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        self.data.append(dict(message.data))
        return super().send(message, agent, timeout)


def test_the_planner_reads_the_file_and_then_the_request(tmp_path: Path) -> None:
    saved = AttachmentStore(tmp_path).save("stats.py", b"def average(values): ...")
    model = Model(plans=[plan(("s1", "Python", "version"))], reviews=[])
    runner, _, _ = make_runner(model, review=False)

    runner.run(uuid4(), "Why does this crash?", "", [saved])

    message = model.plan_messages[0]
    assert message.index("def average(values)") < message.index("Request now: Why")


def test_every_step_is_told_where_the_files_are(tmp_path: Path) -> None:
    saved = AttachmentStore(tmp_path).save("stats.py", b"x = 1")
    transport = DataTransport()
    model = Model(plans=[plan(("s1", "Python", "version"))], reviews=[])
    runner, _, _ = make_runner(model, transport, review=False)

    runner.run(uuid4(), "Check it", "", [saved])

    assert transport.data[0]["attachments"] == [saved.to_dict()]


def test_a_chosen_target_is_named_to_the_planner() -> None:
    model = Model(plans=[plan(("s1", "Python", "version"))], reviews=[])
    runner, _, _ = make_runner(model, review=False)

    runner.run(uuid4(), "@python which version is installed?")

    assert model.plan_messages[0].endswith(
        "\n\nUse these targets for this request: Python."
    )


def test_a_name_in_an_earlier_turn_chooses_nothing_now() -> None:
    model = Model(plans=[plan(("s1", "Python", "version"))], reviews=[])
    runner, _, _ = make_runner(model, review=False)

    runner.run(uuid4(), "And now?", "Earlier in this conversation:\n- Request: @Python")

    assert "Use these targets" not in model.plan_messages[0]


# ── the API ─────────────────────────────────────────────────────────────────


@pytest.fixture
def served(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, SpyRunner, InMemorySessionRepository]]:
    repository = InMemorySessionRepository()
    spy = SpyRunner()
    app.dependency_overrides[get_state] = lambda: _State(
        runner=spy,  # type: ignore[arg-type]
        session_client=EmbeddedSessionClient(repository),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
        attachments=AttachmentStore(tmp_path),
    )

    yield TestClient(app), spy, repository

    app.dependency_overrides.clear()


def upload(client: TestClient, name: str, content: bytes) -> Any:
    return client.post(
        "/files",
        json={"name": name, "content_base64": base64.b64encode(content).decode()},
    )


def test_an_uploaded_file_is_given_an_id(served: Any) -> None:
    client, _, _ = served

    response = upload(client, "stats.py", b"x = 1")

    assert response.status_code == 201
    assert response.json()["name"] == "stats.py"
    assert response.json()["size"] == 5


def test_content_that_is_not_base64_is_refused(served: Any) -> None:
    client, _, _ = served

    response = client.post("/files", json={"name": "a.py", "content_base64": "%%%"})

    assert response.status_code == 400


def test_a_run_gets_the_files_it_names(served: Any) -> None:
    client, spy, repository = served
    attachment_id = upload(client, "stats.py", b"x = 1").json()["attachment_id"]

    response = client.post(
        "/workflow/run", json={"request": "Check it", "attachments": [attachment_id]}
    )

    assert response.status_code == 202
    assert [item.name for item in spy.attachments[0]] == ["stats.py"]
    detail = repository.get_session(UUID(response.json()["session_id"]))
    assert detail is not None
    assert [item.name for item in detail.session.attachments] == ["stats.py"]


def test_a_run_naming_a_file_that_was_not_uploaded_is_a_404(served: Any) -> None:
    client, spy, _ = served

    response = client.post(
        "/workflow/run", json={"request": "Check it", "attachments": [uuid4().hex]}
    )

    assert response.status_code == 404
    assert spy.runs == []
