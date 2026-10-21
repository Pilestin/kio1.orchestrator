"""Tests for changing what the planner uses, from the console.

The model, the temperature, the timeout, and the workspace are the settings a
run goes wrong on, and until now every one of them was a hand-edit into
`config.json` followed by a restart. Two things have to hold for the tab to be
worth having:

- a change reaches **both** the file and the running service, because writing
  only the file needs a restart and writing only memory is lost at one;
- saving a setting never disturbs the rest of the file, which also holds the
  agents, the telemetry block, and the policy.
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.config_loader import Config, DispatchConfig, Workspace
from core.config_writer import CONFIG_PATH_ENV
from core.session_client import NullSessionClient
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from services.workflow_api.runner import WorkflowRunner

DOCUMENT = {
    "provider": "ollama",
    "allowed_providers": ["ollama"],
    "model": "small-model",
    "prompt_path": "prompts/kio1.txt",
    "chat_directory": "chats",
    "temperature": 0.2,
    "request_timeout": 120,
    "keep_alive": -1,
    "max_output_tokens": 4096,
    "telemetry": {"enabled": False, "service_name": "kio1"},
    "dispatch": {
        "enabled": True,
        "agents": {"KIO2": {"endpoint": "http://localhost:8102"}},
        "workspace": {"path": "/old", "entry_point": "old.py"},
        "policy": {"gates": []},
    },
}


class Provider:
    """A provider module that can list its models, and counts preloads."""

    def __init__(self, models: list[str] | None = None, raises: bool = False) -> None:
        self.models = models if models is not None else ["small-model", "big-model"]
        self.raises = raises
        self.preloaded: list[str] = []

    def list_models(self, config: Config) -> list[str]:
        if self.raises:
            raise OSError("Ollama is not running")

        return list(self.models)

    def preload(self, config: Config, client: Any) -> None:
        self.preloaded.append(config.model)


class MuteProvider:
    """A provider that cannot be asked what models it has."""

    def preload(self, config: Config, client: Any) -> None:
        """Do nothing."""


class Recorder:
    """A session recorder that keeps nothing."""

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        """Do nothing."""

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Do nothing."""


def make_config() -> Config:
    """Return a configuration matching the document written to disk."""
    return Config(
        provider="ollama",
        allowed_providers=frozenset({"ollama"}),
        model="small-model",
        prompt_path="prompts/kio1.txt",
        chat_directory="chats",
        temperature=0.2,
        request_timeout=120.0,
        keep_alive=-1,
        max_output_tokens=4096,
        dispatch=DispatchConfig(
            enabled=True,
            registry=AgentRegistry({}),
            workspace=Workspace(path="/old", entry_point="old.py"),
        ),
    )


@pytest.fixture
def written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the writer at a throwaway configuration file, and open the API.

    These endpoints sit behind the administration token, which is absent by
    default so a deployment that never opted in stays closed. Every test here
    is about what happens once it *is* open; the gate itself is checked at the
    bottom of the file.
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps(DOCUMENT, indent=4), encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(path))
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "test-token")

    return path


@pytest.fixture
def state(written: Path) -> _State:
    """Build service state around a real, mutable configuration."""
    config = make_config()

    return _State(
        runner=WorkflowRunner(
            config=config,
            prompt_template="You are KIO1.",
            provider=Provider(),
            provider_client=None,
            recorder=Recorder(),  # type: ignore[arg-type]
            transport=None,  # type: ignore[arg-type]
            directory=AgentDirectory(AgentRegistry({})),
            scoreboard=Scoreboard(),
        ),
        session_client=NullSessionClient(),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
    )


@pytest.fixture
def client(state: _State) -> Iterator[TestClient]:
    """Return a client whose state is the fixture's, with no lifespan."""
    app.dependency_overrides[get_state] = lambda: state

    yield TestClient(app, headers={"Authorization": "Bearer test-token"})

    app.dependency_overrides.clear()


def saved(path: Path) -> dict[str, Any]:
    """Return the configuration file as it now stands."""
    return json.loads(path.read_text(encoding="utf-8"))


# ── reading ─────────────────────────────────────────────────────────────────


def test_the_current_settings_are_reported(client: TestClient) -> None:
    body = client.get("/settings").json()

    assert body["provider"] == "ollama"
    assert body["model"] == "small-model"
    assert body["temperature"] == 0.2
    assert body["request_timeout"] == 120.0
    assert body["workspace"] == {"path": "/old", "entry_point": "old.py"}


def test_the_installed_models_are_offered(client: TestClient) -> None:
    body = client.get("/settings").json()

    assert body["models"] == ["small-model", "big-model"]
    assert body["models_error"] == ""


def test_the_model_in_force_is_always_offerable(
    state: _State, client: TestClient
) -> None:
    # A picker that cannot show the value already in force reads as though
    # opening it changed something.
    state.runner = replace(state.runner, provider=Provider(["big-model"]))

    assert "small-model" in client.get("/settings").json()["models"]


def test_a_provider_that_cannot_be_asked_says_so(
    state: _State, client: TestClient
) -> None:
    # An empty list with no explanation reads as "there are no models", which
    # is a different problem from "I have no way to ask".
    state.runner = replace(state.runner, provider=MuteProvider())

    body = client.get("/settings").json()

    assert body["models"] == ["small-model"]
    assert "cannot list its models" in body["models_error"]


def test_a_provider_that_will_not_answer_says_why(
    state: _State, client: TestClient
) -> None:
    state.runner = replace(state.runner, provider=Provider(raises=True))

    assert "Ollama is not running" in client.get("/settings").json()["models_error"]


# ── writing ─────────────────────────────────────────────────────────────────


def test_a_new_model_reaches_the_file(client: TestClient, written: Path) -> None:
    client.put("/settings", json={"model": "big-model"})

    assert saved(written)["model"] == "big-model"


def test_a_new_model_reaches_the_running_service(
    client: TestClient, state: _State
) -> None:
    # The point of the tab: trying a model against one request should not mean
    # restarting between attempts.
    client.put("/settings", json={"model": "big-model"})

    assert state.runner.config.model == "big-model"


def test_a_new_model_is_warmed(client: TestClient, state: _State) -> None:
    # A local model is read into memory on first use, and that read is charged
    # to whichever request triggers it — routinely the one that then times out.
    client.put("/settings", json={"model": "big-model"})

    assert state.runner.provider.preloaded == ["big-model"]


def test_nothing_is_warmed_when_the_model_did_not_change(
    client: TestClient, state: _State
) -> None:
    client.put("/settings", json={"temperature": 0.4})

    assert state.runner.provider.preloaded == []


def test_a_workspace_reaches_the_file_and_the_service(
    client: TestClient, written: Path, state: _State
) -> None:
    client.put(
        "/settings",
        json={"workspace_path": "/work", "workspace_entry_point": "main.py"},
    )

    assert saved(written)["dispatch"]["workspace"] == {
        "path": "/work",
        "entry_point": "main.py",
    }
    assert state.runner.config.dispatch.workspace.path == "/work"
    assert state.runner.config.dispatch.workspace.entry_point == "main.py"


def test_changing_one_half_of_the_workspace_keeps_the_other(
    client: TestClient, written: Path
) -> None:
    client.put("/settings", json={"workspace_path": "/work"})

    assert saved(written)["dispatch"]["workspace"] == {
        "path": "/work",
        "entry_point": "old.py",
    }


def test_a_timeout_reaches_the_file_and_the_service(
    client: TestClient, written: Path, state: _State
) -> None:
    client.put("/settings", json={"request_timeout": 600})

    assert saved(written)["request_timeout"] == 600
    assert state.runner.config.request_timeout == 600


def test_saving_disturbs_nothing_else_in_the_file(
    client: TestClient, written: Path
) -> None:
    # The same file holds the agents, the telemetry block, and the policy. A
    # settings save that dropped any of them would be discovered at the next
    # restart, which is far too late.
    client.put("/settings", json={"model": "big-model", "workspace_path": "/work"})

    document = saved(written)

    assert document["dispatch"]["agents"] == {
        "KIO2": {"endpoint": "http://localhost:8102"}
    }
    assert document["dispatch"]["policy"] == {"gates": []}
    assert document["telemetry"] == {"enabled": False, "service_name": "kio1"}
    assert document["allowed_providers"] == ["ollama"]


def test_an_empty_request_changes_nothing(client: TestClient, written: Path) -> None:
    before = written.read_text(encoding="utf-8")

    assert client.put("/settings", json={}).status_code == 200
    assert written.read_text(encoding="utf-8") == before


# ── refusals ────────────────────────────────────────────────────────────────


def test_an_empty_model_name_is_refused(client: TestClient) -> None:
    response = client.put("/settings", json={"model": "  "})

    assert response.status_code == 400
    assert "cannot be empty" in response.json()["detail"]


@pytest.mark.parametrize("temperature", [-0.1, 2.5])
def test_an_impossible_temperature_is_refused(
    temperature: float, client: TestClient
) -> None:
    assert client.put("/settings", json={"temperature": temperature}).status_code == 400


@pytest.mark.parametrize("timeout", [1, 4000])
def test_an_unusable_timeout_is_refused(timeout: float, client: TestClient) -> None:
    assert client.put("/settings", json={"request_timeout": timeout}).status_code == 400


def test_a_read_only_deployment_refuses_and_says_so(
    client: TestClient, state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_CONFIG_WRITABLE", "0")

    response = client.put("/settings", json={"model": "big-model"})

    assert response.status_code == 409
    assert "turned off" in response.json()["detail"]
    # And nothing was applied, so the page is not showing a change that did
    # not happen.
    assert state.runner.config.model == "small-model"


def test_a_read_only_deployment_is_visible_before_anyone_tries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_CONFIG_WRITABLE", "0")

    assert client.get("/settings").json()["writable"] is False


def test_the_settings_are_behind_the_administration_token(
    state: _State, written: Path
) -> None:
    # The model and the workspace are as much a part of administering this
    # service as the registry is: one points every agent at different code.
    app.dependency_overrides[get_state] = lambda: state
    anonymous = TestClient(app)

    try:
        assert anonymous.get("/settings").status_code == 401
        assert (
            anonymous.put("/settings", json={"model": "big-model"}).status_code == 401
        )

        headers = {"Authorization": "Bearer test-token"}

        assert anonymous.get("/settings", headers=headers).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_a_deployment_that_never_opened_the_api_stays_closed(
    state: _State, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIO1_REGISTRY_TOKEN", raising=False)
    app.dependency_overrides[get_state] = lambda: state

    try:
        response = TestClient(app).get("/settings")

        assert response.status_code == 503
        assert "which model plans a run" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ── the writer underneath ───────────────────────────────────────────────────


def test_a_dispatch_update_merges_rather_than_replaces(written: Path) -> None:
    from core.config_writer import save_settings

    save_settings({"dispatch": {"workspace": {"path": "/work"}}})

    document = saved(written)

    assert document["dispatch"]["workspace"] == {"path": "/work"}
    assert "agents" in document["dispatch"]
    assert "policy" in document["dispatch"]


def test_the_previous_contents_are_kept(written: Path) -> None:
    from core.config_writer import save_settings

    save_settings({"model": "big-model"})

    backup = written.with_suffix(written.suffix + ".bak")

    assert json.loads(backup.read_text(encoding="utf-8"))["model"] == "small-model"
