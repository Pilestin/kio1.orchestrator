"""Tests for choosing the planning model from the console.

Someone who is not a developer should be able to switch from a local model
to OpenRouter by picking it, pasting a key, and choosing a model — no
environment variables, no editing files, no restart. And a key that does not
work must be refused at that moment, not discovered by the next run.
"""

import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as service
from app import _State, app, get_state
from core.config_loader import Config
from core.dotenv import load_dotenv, parse_dotenv, save_dotenv_value
from core.session_client import NullSessionClient
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from providers import openrouter_client
from providers.provider_client import describe_provider
from services.workflow_api.runner import WorkflowRunner
from tests.test_settings_api import DOCUMENT, Provider, Recorder, make_config

# ── .env ────────────────────────────────────────────────────────────────────


def test_a_dotenv_file_is_read_the_usual_way() -> None:
    text = (
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=yes\n"
        'QUOTED="has # inside"\n'
        "SINGLE='x y'\n"
        "TRAILING=abc  # note\n"
        "not a line\n"
    )

    assert parse_dotenv(text) == {
        "PLAIN": "value",
        "EXPORTED": "yes",
        "QUOTED": "has # inside",
        "SINGLE": "x y",
        "TRAILING": "abc",
    }


def test_the_environment_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("KIO1_TEST_A=from-file\nKIO1_TEST_B=from-file\n", encoding="utf-8")
    monkeypatch.setenv("KIO1_TEST_A", "from-environment")
    monkeypatch.delenv("KIO1_TEST_B", raising=False)

    applied = load_dotenv(path)

    assert applied == ["KIO1_TEST_B"]
    assert os.environ["KIO1_TEST_A"] == "from-environment"
    assert os.environ["KIO1_TEST_B"] == "from-file"
    monkeypatch.delenv("KIO1_TEST_B")


def test_saving_a_key_replaces_its_line_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keep me\nOTHER=1\nexport KEY=old\n", encoding="utf-8")

    save_dotenv_value("KEY", "new value", path)
    save_dotenv_value("ADDED", "2", path)

    assert path.read_text(encoding="utf-8") == (
        '# keep me\nOTHER=1\nKEY="new value"\nADDED="2"\n'
    )
    assert parse_dotenv(path.read_text(encoding="utf-8"))["KEY"] == "new value"


@pytest.mark.parametrize("value", ['a"b', "two\nlines"])
def test_a_value_that_would_corrupt_the_file_is_refused(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ValueError):
        save_dotenv_value("KEY", value, tmp_path / ".env")


# ── OpenRouter ──────────────────────────────────────────────────────────────


def openrouter_config(**options: Any) -> Config:
    """Return a configuration selecting OpenRouter."""
    config = make_config()
    config.provider = "openrouter"
    config.model = "vendor/model"
    config.provider_options = dict(options)
    return config


def test_openrouter_needs_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        openrouter_client.create_client(openrouter_config())


def test_openrouter_never_borrows_the_ollama_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `endpoint` is Ollama's and stays in the file after a switch.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    client = openrouter_client.create_client(
        openrouter_config(endpoint="http://127.0.0.1:11434")
    )

    assert str(client.base_url).startswith("https://openrouter.ai/api/v1")


def test_openrouter_lists_its_models(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    body = {"data": [{"id": "z/last"}, {"id": "a/first"}, {"name": "no id"}]}
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(body).encode()),
    )

    assert openrouter_client.list_models(openrouter_config()) == ["a/first", "z/last"]


def test_the_console_is_told_what_each_provider_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "set")

    openrouter = describe_provider("openrouter")
    ollama = describe_provider("ollama")

    assert openrouter["key_env"] == "OPENROUTER_API_KEY"
    assert openrouter["key_set"] is True
    assert openrouter["lists_models"] is True
    assert ollama["key_env"] is None
    assert describe_provider("nonexistent")["available"] is False


# ── switching from the console ──────────────────────────────────────────────


class Cloud:
    """A provider module that needs a key, as OpenRouter does."""

    API_KEY_ENV = "KIO1_TEST_CLOUD_KEY"
    DISPLAY_NAME = "Cloud"

    def __init__(self) -> None:
        self.preloaded: list[str] = []

    def create_client(self, config: Config) -> str:
        key = os.environ.get(self.API_KEY_ENV, "")

        if key != "good-key":
            raise ValueError(f"{self.API_KEY_ENV} is missing or wrong")

        return f"client-for-{config.model}"

    def list_models(self, config: Config) -> list[str]:
        return ["cloud/large", "cloud/small"]

    def preload(self, config: Config, client: Any) -> None:
        self.preloaded.append(config.model)


@pytest.fixture
def switching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, _State, Cloud, Path]]:
    """Serve a state that may switch between `ollama` and a fake `cloud`."""
    config_file = tmp_path / "config.json"
    document = {**DOCUMENT, "allowed_providers": ["ollama", "cloud"]}
    config_file.write_text(json.dumps(document), encoding="utf-8")
    dotenv = tmp_path / ".env"

    monkeypatch.setenv("KIO1_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "test-token")
    monkeypatch.delenv(Cloud.API_KEY_ENV, raising=False)

    cloud = Cloud()
    real_load = service.load_provider

    def load(name: str, allowed: frozenset[str]) -> Any:
        return cloud if name == "cloud" else real_load(name, allowed)

    monkeypatch.setattr(service, "load_provider", load)
    monkeypatch.setattr(
        service,
        "save_dotenv_value",
        lambda name, value: save_dotenv_value(name, value, dotenv),
    )

    config = make_config()
    config.allowed_providers = frozenset({"ollama", "cloud"})
    state = _State(
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
    app.dependency_overrides[get_state] = lambda: state

    yield (
        TestClient(app, headers={"Authorization": "Bearer test-token"}),
        state,
        cloud,
        dotenv,
    )

    app.dependency_overrides.clear()
    os.environ.pop(Cloud.API_KEY_ENV, None)


def test_the_models_of_another_provider_can_be_listed_before_switching(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, state, _, _ = switching

    response = client.get("/settings/models", params={"provider": "cloud"})

    assert response.json()["models"] == ["cloud/large", "cloud/small"]
    assert state.runner.config.provider == "ollama"


def test_switching_with_a_key_saves_both_and_takes_effect_at_once(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, state, cloud, dotenv = switching

    response = client.put(
        "/settings",
        json={"provider": "cloud", "model": "cloud/large", "api_key": "good-key"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "cloud"
    assert state.runner.provider is cloud
    assert state.runner.provider_client == "client-for-cloud/large"
    assert state.runner.config.model == "cloud/large"
    assert parse_dotenv(dotenv.read_text(encoding="utf-8")) == {
        Cloud.API_KEY_ENV: "good-key"
    }
    saved = json.loads(Path(os.environ["KIO1_CONFIG_PATH"]).read_text("utf-8"))
    assert (saved["provider"], saved["model"]) == ("cloud", "cloud/large")
    assert cloud.preloaded == ["cloud/large"]


def test_a_key_that_does_not_work_changes_nothing(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, state, _, dotenv = switching

    response = client.put(
        "/settings",
        json={"provider": "cloud", "model": "cloud/large", "api_key": "bad-key"},
    )

    assert response.status_code == 400
    assert "cannot be used yet" in response.json()["detail"]
    assert state.runner.config.provider == "ollama"
    saved = json.loads(Path(os.environ["KIO1_CONFIG_PATH"]).read_text("utf-8"))
    assert saved["provider"] == "ollama"
    # Nor is the bad key kept, in the file or in this process.
    assert not dotenv.exists()
    assert Cloud.API_KEY_ENV not in os.environ


def test_a_new_provider_needs_a_model_to_go_with_it(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, _, _, _ = switching
    os.environ[Cloud.API_KEY_ENV] = "good-key"

    response = client.put("/settings", json={"provider": "cloud"})

    assert response.status_code == 400
    assert "Choose a cloud model" in response.json()["detail"]


def test_a_provider_the_deployment_does_not_allow_is_refused(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, _, _, _ = switching

    response = client.put("/settings", json={"provider": "openai", "model": "x"})

    assert response.status_code == 400


def test_the_answer_model_can_be_another_provider(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, state, _, dotenv = switching

    response = client.put(
        "/settings",
        json={
            "review_provider": "cloud",
            "review_model": "cloud/large",
            "review_api_key": "good-key",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["review"]["provider"] == "cloud"
    # The planner is untouched.
    assert state.runner.config.provider == "ollama"
    assert state.runner.config.orchestration.review_model == "cloud/large"
    saved = json.loads(Path(os.environ["KIO1_CONFIG_PATH"]).read_text("utf-8"))
    assert saved["orchestration"]["review_provider"] == "cloud"
    assert parse_dotenv(dotenv.read_text(encoding="utf-8")) == {
        Cloud.API_KEY_ENV: "good-key"
    }


def test_saving_the_answer_model_keeps_the_other_orchestration_settings(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, _, _, _ = switching
    path = Path(os.environ["KIO1_CONFIG_PATH"])
    document = json.loads(path.read_text("utf-8"))
    document["orchestration"] = {"review": True, "max_rounds": 3}
    path.write_text(json.dumps(document), encoding="utf-8")
    os.environ[Cloud.API_KEY_ENV] = "good-key"

    client.put(
        "/settings", json={"review_provider": "cloud", "review_model": "cloud/small"}
    )

    saved = json.loads(path.read_text("utf-8"))["orchestration"]
    assert saved == {
        "review": True,
        "max_rounds": 3,
        "review_provider": "cloud",
        "review_model": "cloud/small",
    }


def test_the_answer_model_can_go_back_to_the_planning_model(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, state, _, _ = switching
    os.environ[Cloud.API_KEY_ENV] = "good-key"
    client.put(
        "/settings", json={"review_provider": "cloud", "review_model": "cloud/large"}
    )

    response = client.put(
        "/settings", json={"review_provider": "", "review_model": "anything"}
    )

    assert response.json()["review"] == {"enabled": True, "provider": "", "model": ""}
    assert not state.runner.config.orchestration.separate_reviewer


@pytest.mark.parametrize(
    "body, message",
    [
        ({"review_provider": "cloud"}, "Choose a cloud model"),
        (
            {"review_provider": "cloud", "review_model": "x", "review_api_key": "bad"},
            "cannot be used yet",
        ),
        ({"review_provider": "openai", "review_model": "x"}, "Invalid provider"),
    ],
)
def test_an_answer_model_that_cannot_work_changes_nothing(
    switching: tuple[TestClient, _State, Cloud, Path],
    body: dict[str, Any],
    message: str,
) -> None:
    client, state, _, _ = switching

    response = client.put("/settings", json=body)

    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert not state.runner.config.orchestration.separate_reviewer


def test_the_settings_list_every_allowed_provider(
    switching: tuple[TestClient, _State, Cloud, Path],
) -> None:
    client, _, _, _ = switching

    providers = {item["name"] for item in client.get("/settings").json()["providers"]}

    assert providers == {"ollama", "cloud"}
