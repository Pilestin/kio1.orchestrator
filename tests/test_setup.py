"""Tests for first-time setup: what this computer can run, and getting it.

The properties that matter: the suggestions fit the machine — nothing a CPU
would take minutes over, nothing the memory cannot hold — and a download
started from the console reports its progress and says plainly when it
failed.
"""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import app as service
from app import _State, app, get_state
from core.model_pull import PullManager
from core.session_client import NullSessionClient
from core.system_info import Hardware, ollama_status, suggest
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from tests.test_settings_api import make_config


def machine(ram: float | None, gpu_memory: float | None = None) -> Hardware:
    return Hardware(
        system="Test",
        cpus=8,
        ram_total_gb=ram,
        ram_free_gb=None,
        gpu="Card" if gpu_memory else "",
        gpu_memory_gb=gpu_memory,
    )


def recommended(ram: float | None, gpu: float | None = None) -> list[str]:
    return [s["name"] for s in suggest(machine(ram, gpu), []) if s["recommended"]]


# ── suggestions ─────────────────────────────────────────────────────────────


def test_a_laptop_without_a_gpu_gets_small_fast_models() -> None:
    # The machine KIO1 was measured on: 15 GB, no GPU.
    assert recommended(15.2) == ["ministral-3:3b", "qwen3.5:4b"]


def test_a_large_model_is_not_recommended_to_a_cpu_even_when_it_fits() -> None:
    assert "qwen2.5:14b" not in recommended(64)


def test_a_gpu_gets_the_strongest_answering_model_it_can_hold() -> None:
    assert recommended(32, gpu=24)[-1] == "qwen2.5:14b"


def test_too_little_memory_recommends_nothing() -> None:
    suggestions = suggest(machine(4), [])

    assert not any(s["recommended"] for s in suggestions)
    assert not any(s["fits"] for s in suggestions)


def test_installed_models_are_marked() -> None:
    suggestions = suggest(machine(16), ["qwen3.5:4b"])

    assert [s["installed"] for s in suggestions][:2] == [False, True]


def test_ollama_that_does_not_answer_is_reported_as_not_running() -> None:
    status = ollama_status("http://127.0.0.1:1")

    assert status["running"] is False
    assert status["models"] == []


# ── downloading ─────────────────────────────────────────────────────────────


class FakeOllama(BaseHTTPRequestHandler):
    """Streams `/api/pull` progress the way Ollama does."""

    lines: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for line in self.lines:
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        """Quiet."""


@pytest.fixture
def ollama() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def wait_until_finished(manager: PullManager) -> dict[str, Any]:
    for _ in range(300):
        latest = manager.status()[0]
        if latest["state"] != "running":
            return latest
        time.sleep(0.01)
    raise AssertionError("the download never finished")


def test_a_download_reports_progress_and_finishes(ollama: str) -> None:
    FakeOllama.lines = [
        {"status": "pulling manifest"},
        {"status": "pulling abc", "total": 1000, "completed": 250},
        {"status": "pulling abc", "total": 1000, "completed": 1000},
        {"status": "success"},
    ]
    manager = PullManager()

    manager.start(ollama, "tiny:1b")
    finished = wait_until_finished(manager)

    assert finished["state"] == "done"
    assert finished["percent"] == 100.0


def test_a_download_ollama_refuses_says_why(ollama: str) -> None:
    FakeOllama.lines = [{"error": "pull model manifest: file does not exist"}]
    manager = PullManager()

    manager.start(ollama, "nonexistent:1b")
    finished = wait_until_finished(manager)

    assert finished["state"] == "failed"
    assert "file does not exist" in finished["error"]


def test_starting_a_running_download_again_does_not_start_a_second() -> None:
    manager = PullManager()
    first = manager.start("http://127.0.0.1:1", "tiny:1b")
    first.state = "running"  # as if still downloading

    assert manager.start("http://127.0.0.1:1", "tiny:1b") is first


# ── the API ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "t")
    monkeypatch.setattr(service, "hardware", lambda: machine(15.2))
    monkeypatch.setattr(
        service,
        "ollama_status",
        lambda endpoint: {
            "installed": True,
            "running": False,
            "models": [],
            "version": None,
        },
    )
    config = make_config()
    state = _State(
        runner=type("Runner", (), {"config": config})(),  # type: ignore[arg-type]
        session_client=NullSessionClient(),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
    )
    app.dependency_overrides[get_state] = lambda: state
    yield TestClient(app, headers={"Authorization": "Bearer t"})
    app.dependency_overrides.clear()


def test_setup_describes_the_machine_and_what_suits_it(client: TestClient) -> None:
    system = client.get("/system").json()

    assert system["hardware"]["ram_total_gb"] == 15.2
    assert [s["name"] for s in system["suggestions"] if s["recommended"]] == [
        "ministral-3:3b",
        "qwen3.5:4b",
    ]
    assert system["planner"] == {"provider": "ollama", "model": "small-model"}


def test_a_download_needs_ollama_running(client: TestClient) -> None:
    response = client.post("/system/models/pull", json={"model": "qwen3.5:4b"})

    assert response.status_code == 409
    assert "not running" in response.json()["detail"]


def test_a_model_name_cannot_carry_anything_else(client: TestClient) -> None:
    response = client.post("/system/models/pull", json={"model": "x; rm -rf /"})

    assert response.status_code == 422


def test_setup_is_behind_the_token() -> None:
    assert TestClient(app).get("/system").status_code in {401, 503}
