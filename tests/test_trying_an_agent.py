"""Tests for trying an agent: one task, no plan, nothing saved.

The properties that matter: a definition is carried out exactly as a plan's
step would be — the task, the workspace, and any arguments reach it — and
what came back is shown, failures included; a capability it does not offer
is refused before anything runs; nothing about trying changes the saved
targets or their record; and it is closed without the registry token.
"""

import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import REGISTRY_TOKEN_ENV, _State, app, get_state
from core.config_loader import Config, DispatchConfig, Workspace
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory, HealthConfig
from dispatch.scoreboard import Scoreboard
from tools.dummy_agent import build_server, server_address

AUTHORISED = {"Authorization": "Bearer shared"}


def config(workspace: Workspace | None = None) -> Config:
    """Return a configuration with no targets and a short timeout."""
    return Config(
        provider="fake",
        allowed_providers=frozenset({"fake"}),
        model="m",
        prompt_path="p",
        chat_directory="c",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        dispatch=DispatchConfig(
            enabled=True,
            registry=AgentRegistry({}),
            request_timeout=10,
            workspace=workspace or Workspace(),
        ),
    )


@pytest.fixture
def scoreboard() -> Scoreboard:
    return Scoreboard()


@pytest.fixture
def directory() -> AgentDirectory:
    return AgentDirectory(
        AgentRegistry({}), health=HealthConfig(enabled=True, timeout=1)
    )


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    directory: AgentDirectory,
    scoreboard: Scoreboard,
) -> Iterator[TestClient]:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    app.dependency_overrides[get_state] = lambda: _State(
        runner=SimpleNamespace(  # type: ignore[arg-type]
            config=config(Workspace(path="/work", entry_point="main.py"))
        ),
        session_client=None,  # type: ignore[arg-type]
        directory=directory,
        scoreboard=scoreboard,
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def kio() -> Iterator[str]:
    """A stand-in KIO that echoes the task it was given."""
    server = build_server(
        agent_id="Helper",
        host="127.0.0.1",
        port=0,
        capabilities=frozenset({"diagnosis"}),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server_address(server)

    yield f"http://{host}:{port}"

    server.shutdown()
    server.server_close()


class EchoApi(BaseHTTPRequestHandler):
    """An HTTP API that answers with the path it was called on."""

    def do_GET(self) -> None:
        body = json.dumps({"called": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def api() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    yield f"http://127.0.0.1:{server.server_address[1]}"

    server.shutdown()
    server.server_close()


def test_an_agent_is_asked_the_task_and_its_answer_is_shown(
    client: TestClient, kio: str
) -> None:
    body = client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={
            "entry": {
                "agent_id": "Helper",
                "endpoint": kio,
                "capabilities": ["diagnosis"],
            },
            "task": "Why does checkout crash?",
        },
    ).json()

    assert body["status"] == "ok"
    assert body["capability"] == "diagnosis"
    assert body["output"]["received_task"] == "Why does checkout crash?"
    assert body["verdict"] == "defect"


def test_a_command_is_run_and_what_it_printed_comes_back(client: TestClient) -> None:
    body = client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={
            "entry": {
                "agent_id": "Hello",
                "type": "cli",
                "command": [sys.executable, "-c", "print('hello from a draft')"],
                "capabilities": ["greeting"],
            },
            "task": "Say hello",
        },
    ).json()

    assert body["status"] == "ok"
    assert "hello from a draft" in json.dumps(body["output"])


def test_arguments_reach_an_http_api(client: TestClient, api: str) -> None:
    body = client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={
            "entry": {
                "agent_id": "Weather",
                "type": "http",
                "url": api + "/forecast?city={arguments.city}",
                "method": "GET",
                "arguments": {"city": {"type": "string", "required": True}},
                "capabilities": ["weather_check"],
            },
            "task": "Weather, please",
            "arguments": {"city": "Ankara"},
        },
    ).json()

    assert body["status"] == "ok", body["error"]
    assert "/forecast?city=Ankara" in json.dumps(body["output"])


def test_a_failure_is_shown_rather_than_raised(client: TestClient) -> None:
    response = client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={
            "entry": {
                "agent_id": "Gone",
                "endpoint": "http://127.0.0.1:1",
                "capabilities": ["diagnosis"],
            },
            "task": "Anything",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"]


def test_a_capability_it_does_not_offer_is_refused_before_anything_runs(
    client: TestClient, kio: str
) -> None:
    response = client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={
            "entry": {"endpoint": kio, "capabilities": ["diagnosis"]},
            "capability": "deployment",
            "task": "Ship it",
        },
    )

    assert response.status_code == 400
    assert "diagnosis" in response.json()["detail"]


def test_a_malformed_definition_is_refused(client: TestClient) -> None:
    response = client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={"entry": {"type": "cli", "capabilities": ["x"]}, "task": "Run"},
    )

    assert response.status_code == 400


def test_trying_saves_nothing_and_records_nothing(
    client: TestClient, kio: str, directory: AgentDirectory, scoreboard: Scoreboard
) -> None:
    client.post(
        "/registry/agents/try",
        headers=AUTHORISED,
        json={
            "entry": {
                "agent_id": "Helper",
                "endpoint": kio,
                "capabilities": ["diagnosis"],
            },
            "task": "Anything",
        },
    )

    assert directory.records() == ()
    assert scoreboard.scores_for_agent("Helper") == ()


def test_trying_needs_the_registry_token(client: TestClient) -> None:
    response = client.post(
        "/registry/agents/try",
        json={"entry": {"endpoint": "http://127.0.0.1:1"}, "task": "Anything"},
    )

    assert response.status_code == 401
