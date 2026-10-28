"""Tests for planning with a model on another machine — over Tailscale, say.

The properties that matter: an address is judged by where it points, so a
tailnet machine counts as private and plain HTTP across the internet is
flagged; start.py looks where the service will look, and never tries to
start Ollama on this machine for a server that lives on another; and the
console will not fill a shared model machine with downloads.
"""

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app as service
import start
from app import _State, app, get_state
from core.endpoints import exposed, is_local, scope
from core.session_client import NullSessionClient
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from tests.test_settings_api import make_config

TAILNET = "http://100.101.102.103:11434"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434", "loopback"),
        ("http://localhost:11434", "loopback"),
        (TAILNET, "private"),
        ("http://gpu-box:11434", "private"),
        ("http://gpu.tail1234.ts.net:11434", "private"),
        ("http://192.168.1.5:11434", "private"),
        ("http://164.90.10.10:11434", "public"),
        ("http://models.example.com", "public"),
    ],
)
def test_an_address_is_judged_by_where_it_points(url: str, expected: str) -> None:
    assert scope(url) == expected


def test_plain_http_across_the_internet_is_flagged() -> None:
    assert exposed("http://164.90.10.10:11434")
    assert not exposed("https://164.90.10.10:11434")
    assert not exposed(TAILNET)


def test_only_this_machine_is_local() -> None:
    assert is_local("http://127.0.0.1:11434")
    assert not is_local(TAILNET)


# ── start.py ────────────────────────────────────────────────────────────────


def test_start_checks_the_endpoint_the_service_will_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", TAILNET)
    monkeypatch.setattr(start, "say", lambda text: None)
    monkeypatch.setattr(
        start, "_get_json", lambda url, timeout: asked.append(url) or {"models": []}
    )

    start.check_ollama({"provider_options": {"endpoint": "http://127.0.0.1:11434"}})

    assert asked == [f"{TAILNET}/api/tags"]


def test_start_never_starts_ollama_here_for_a_server_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    said: list[str] = []
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", TAILNET)
    monkeypatch.setattr(start, "say", said.append)
    monkeypatch.setattr(start, "_get_json", lambda url, timeout: None)
    monkeypatch.setattr(
        start.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("started Ollama on this machine"),
    )

    start.check_ollama({"provider_options": {}})

    assert any("Tailscale" in line for line in said)


# ── the console ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "t")
    monkeypatch.setattr(
        service,
        "ollama_status",
        lambda endpoint: {
            "installed": False,
            "running": True,
            "models": [],
            "version": None,
        },
    )
    config = replace(make_config(), provider_options={"endpoint": TAILNET})
    state = _State(
        runner=type("Runner", (), {"config": config})(),  # type: ignore[arg-type]
        session_client=NullSessionClient(),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
    )
    app.dependency_overrides[get_state] = lambda: state
    yield TestClient(app, headers={"Authorization": "Bearer t"})
    app.dependency_overrides.clear()


def test_the_console_will_not_download_onto_a_shared_model_machine(
    client: TestClient,
) -> None:
    response = client.post("/system/models/pull", json={"model": "qwen3.5:4b"})

    assert response.status_code == 409
    assert "another machine" in response.json()["detail"]
