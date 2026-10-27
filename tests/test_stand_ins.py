"""Tests for starting and stopping the stand-in KIOs from the console.

The properties that matter: KIO1 runs the stand-ins as a program of its own
and reads which KIOs came up from what it prints; one that dies at once —
its ports taken — is reported with why, not as running; stopping leaves
nothing behind; and only someone with the registry token can do either.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import REGISTRY_TOKEN_ENV, _State, app, get_state
from core.session_client import NullSessionClient
from core.stand_ins import StandInError, StandIns
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard

FLEET_PRINTS = (
    "import time; print('Starting 2 stand-in KIOs:'); "
    "print('  KIO2   http://127.0.0.1:8102  Bug Locate'); "
    "print('  KIO11  http://127.0.0.1:8111  Test Automation'); time.sleep(60)"
)


def stand_ins(tmp_path: Path, code: str = FLEET_PRINTS) -> StandIns:
    folder = tmp_path / "kio-mocks"
    (folder / "kio_mocks").mkdir(parents=True)
    (folder / "sample").mkdir()
    return StandIns(
        folder=folder,
        command=[sys.executable, "-u", "-c", code],
        log=tmp_path / "fleet.log",
    )


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator[StandIns]:
    managed = stand_ins(tmp_path)
    yield managed
    managed.stop()


def test_the_kios_that_came_up_are_read_from_what_the_fleet_prints(
    fleet: StandIns,
) -> None:
    status = fleet.start()

    assert status["running"] is True
    assert status["kios"] == [
        {"agent_id": "KIO2", "endpoint": "http://127.0.0.1:8102"},
        {"agent_id": "KIO11", "endpoint": "http://127.0.0.1:8111"},
    ]
    assert status["sample"].endswith("sample")


def test_stopping_leaves_nothing_running(fleet: StandIns) -> None:
    fleet.start()

    status = fleet.stop()

    assert status["running"] is False
    assert status["kios"] == []


def test_a_fleet_that_dies_at_once_says_why(tmp_path: Path) -> None:
    dying = stand_ins(
        tmp_path,
        "print('OSError: [Errno 98] Address already in use'); raise SystemExit(1)",
    )

    with pytest.raises(StandInError, match="Address already in use"):
        dying.start()

    assert dying.status()["running"] is False


def test_without_the_folder_there_is_nothing_to_start(tmp_path: Path) -> None:
    absent = StandIns(folder=tmp_path / "nowhere", log=tmp_path / "fleet.log")

    assert absent.status()["available"] is False
    with pytest.raises(StandInError):
        absent.start()


# ── the endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def client(fleet: StandIns, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    app.dependency_overrides[get_state] = lambda: _State(
        runner=None,  # type: ignore[arg-type]
        session_client=NullSessionClient(),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
        stand_ins=fleet,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_console_starts_and_stops_them(client: TestClient) -> None:
    started = client.post(
        "/stand-ins",
        json={"action": "start"},
        headers={"Authorization": "Bearer shared"},
    )
    stopped = client.post(
        "/stand-ins",
        json={"action": "stop"},
        headers={"Authorization": "Bearer shared"},
    )

    assert started.json()["running"] is True
    assert stopped.json()["running"] is False


def test_starting_them_needs_the_token(client: TestClient) -> None:
    assert client.post("/stand-ins", json={"action": "start"}).status_code == 401
