"""Tests for keeping the targets in the database, with their history.

The properties that matter: config.json's agents are imported exactly once;
every change is a version that can be looked at and brought back; order is
kept, because the catalog follows it; and one broken definition never stops
the others from loading.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.agent_store import (
    AGENT_STORE_ENV,
    FileAgentStore,
    SqliteAgentStore,
    open_agent_store,
    registry_from,
)
from core.config_writer import CONFIG_PATH_ENV
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard

PYTEST = {"type": "cli", "command": ["python", "-V"], "capabilities": ["version"]}
GIT = {"type": "cli", "command": ["git", "log"], "capabilities": ["history"]}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteAgentStore]:
    opened = SqliteAgentStore(tmp_path / "kio1.db")
    yield opened
    opened.close()


# ── the store ───────────────────────────────────────────────────────────────


def test_agents_are_imported_once_and_only_once(store: SqliteAgentStore) -> None:
    assert store.seed({"Pytest": PYTEST}) is True
    store.write({"Pytest": None})

    # A second start must not bring back what was removed since.
    assert store.seed({"Pytest": PYTEST}) is False
    assert store.read() == {}


def test_the_order_agents_were_added_in_is_kept(store: SqliteAgentStore) -> None:
    store.write({"Zeta": PYTEST})
    store.write({"Alpha": GIT})
    store.write({"Zeta": {**PYTEST, "description": "edited"}})

    assert list(store.read()) == ["Zeta", "Alpha"]


def test_every_change_is_a_version(store: SqliteAgentStore) -> None:
    store.seed({"Pytest": PYTEST})
    store.write({"Pytest": {**PYTEST, "description": "v2"}})
    store.write({"Pytest": None})

    history = store.history("Pytest")

    assert [item["action"] for item in history] == ["removed", "updated", "imported"]
    assert history[0]["entry"] is None
    assert history[1]["entry"]["description"] == "v2"
    assert history[0]["version"] > history[1]["version"]


def test_removing_something_unknown_records_nothing(store: SqliteAgentStore) -> None:
    store.write({"Nobody": None})

    assert store.history("Nobody") == []


def test_the_store_survives_a_restart(tmp_path: Path) -> None:
    first = SqliteAgentStore(tmp_path / "kio1.db")
    first.write({"Pytest": PYTEST})
    first.close()

    second = SqliteAgentStore(tmp_path / "kio1.db")

    try:
        assert second.read() == {"Pytest": PYTEST}
    finally:
        second.close()


# ── choosing one ────────────────────────────────────────────────────────────


def test_the_database_is_the_default_and_starts_from_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"dispatch": {"agents": {"Pytest": PYTEST}}}), encoding="utf-8"
    )
    monkeypatch.setenv(CONFIG_PATH_ENV, str(config))
    monkeypatch.delenv(AGENT_STORE_ENV, raising=False)

    opened = open_agent_store(f"sqlite:///{tmp_path.as_posix()}/kio1.db")

    try:
        assert isinstance(opened, SqliteAgentStore)
        assert opened.read() == {"Pytest": PYTEST}
    finally:
        opened.close()


@pytest.mark.parametrize(
    "choice, url",
    [("file", "sqlite:///x.db"), ("database", "postgresql://kio1@localhost/kio1")],
    ids=["asked-for", "postgres-not-supported-yet"],
)
def test_the_file_store_is_used_when_asked_or_needed(
    choice: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(AGENT_STORE_ENV, choice)

    assert isinstance(open_agent_store(url), FileAgentStore)


def test_one_broken_definition_does_not_stop_the_rest() -> None:
    registry = registry_from({"Pytest": PYTEST, "Broken": {"type": "cli"}})

    assert registry.agent_ids() == frozenset({"Pytest"})


# ── over the API ────────────────────────────────────────────────────────────


@pytest.fixture
def client(
    store: SqliteAgentStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "t")
    state = _State(
        runner=None,  # type: ignore[arg-type]
        session_client=None,  # type: ignore[arg-type]
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
        store=store,
    )
    app.dependency_overrides[get_state] = lambda: state
    yield TestClient(app, headers={"Authorization": "Bearer t"})
    app.dependency_overrides.clear()


def add(client: TestClient, agent_id: str, entry: dict[str, Any]) -> None:
    response = client.post(
        "/registry/agents", json={"manifest": {"agent_id": agent_id, **entry}}
    )
    assert response.status_code == 201, response.text


def test_an_agent_added_in_the_console_lands_in_the_database(
    client: TestClient, store: SqliteAgentStore
) -> None:
    add(client, "Pytest", PYTEST)

    assert store.read()["Pytest"]["command"] == ["python", "-V"]
    assert client.get("/registry/config").json()["store"] == "database"


def test_an_earlier_version_can_be_brought_back(
    client: TestClient, store: SqliteAgentStore
) -> None:
    add(client, "Pytest", PYTEST)
    client.put(
        "/registry/agents/Pytest",
        json={"entry": {**PYTEST, "description": "broken by an edit"}},
    )
    first = client.get("/registry/agents/Pytest/history").json()["versions"][-1]

    response = client.post(
        "/registry/agents/Pytest/restore", json={"version": first["version"]}
    )

    assert response.status_code == 200, response.text
    assert "description" not in store.read()["Pytest"]
    assert store.history("Pytest")[0]["action"] == "restored"


def test_a_removed_agent_can_be_brought_back(
    client: TestClient, store: SqliteAgentStore
) -> None:
    add(client, "Pytest", PYTEST)
    client.delete("/registry/agents/Pytest")
    versions = client.get("/registry/agents/Pytest/history").json()["versions"]

    response = client.post(
        "/registry/agents/Pytest/restore", json={"version": versions[1]["version"]}
    )

    assert response.status_code == 200
    assert "Pytest" in store.read()


def test_a_removal_cannot_be_restored_as_a_definition(client: TestClient) -> None:
    add(client, "Pytest", PYTEST)
    client.delete("/registry/agents/Pytest")
    removal = client.get("/registry/agents/Pytest/history").json()["versions"][0]

    response = client.post(
        "/registry/agents/Pytest/restore", json={"version": removal["version"]}
    )

    assert response.status_code == 400


def test_an_unknown_version_is_not_found(client: TestClient) -> None:
    assert (
        client.post("/registry/agents/Pytest/restore", json={"version": 99}).status_code
        == 404
    )


def test_every_agent_can_be_exported_in_config_form(client: TestClient) -> None:
    add(client, "Pytest", PYTEST)
    add(client, "Git", GIT)

    exported = client.get("/registry/export").json()["agents"]

    assert list(exported) == ["Pytest", "Git"]
    assert exported["Git"]["command"] == ["git", "log"]
