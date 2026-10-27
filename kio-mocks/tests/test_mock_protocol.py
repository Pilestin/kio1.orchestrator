"""Tests for the protocol every stand-in speaks.

The properties that matter: a reply always echoes the step's ids and says
its status; a task that fails is an error inside HTTP 200, never a broken
response; a capability the KIO does not offer is refused by name; a slow or
crashing handler costs one step, not the server; nothing outside the
workspace is read; and a job is accepted at once, then polled to its result.
"""

import json
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kio_mocks.errors import KioError
from kio_mocks.jobs import JobBoard
from kio_mocks.protocol import Capability, Kio, Request, serve


def count_files(request: Request) -> dict[str, Any]:
    files = request.workspace.python_files()
    return {
        "summary": f"{len(files)} Python files.",
        "verdict": "clean",
        "files": len(files),
    }


def refuse(request: Request) -> dict[str, Any]:
    raise KioError("Nothing to review in that request.")


def crash(request: Request) -> dict[str, Any]:
    raise ZeroDivisionError("bug in the stand-in")


def slow(request: Request) -> dict[str, Any]:
    time.sleep(2)
    return {"summary": "late"}


def sample(**changes: Any) -> Kio:
    settings: dict[str, Any] = {
        "kio_id": "KIO99",
        "title": "Sample",
        "description": "Counts files.",
        "capabilities": [
            Capability("counting", "Counts the workspace's Python files.", count_files),
            Capability("refusing", "Always refuses.", refuse),
            Capability("crashing", "Always crashes.", crash),
            Capability("slow", "Takes two seconds.", slow),
        ],
        "unsupported": {"fixing": "KIO99 finds things; it does not fix them."},
        "budget": 0.5,
    }
    settings.update(changes)
    return Kio(**settings)


def message(
    capability: str, workspace: Path | None = None, **extra: Any
) -> dict[str, Any]:
    data = {"repository": {"path": str(workspace)}} if workspace else {}
    return {
        "workflow_id": "wf-1",
        "step_id": "s1",
        "capability": capability,
        "task": "Do it",
        "data": data,
        **extra,
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "c.py").write_text("z = 3\n", encoding="utf-8")
    return tmp_path


def test_a_reply_echoes_the_step_and_says_it_worked(project: Path) -> None:
    reply = sample().execute(message("counting", project))

    assert (reply["workflow_id"], reply["step_id"], reply["agent_id"]) == (
        "wf-1",
        "s1",
        "KIO99",
    )
    assert reply["status"] == "ok"
    assert reply["output"]["files"] == 2  # the virtual environment is not the project
    assert reply["output"]["simulator"] == "kio-mocks"


def test_a_capability_not_offered_is_refused_by_name() -> None:
    reply = sample().execute(message("deploying"))

    assert reply["status"] == "error"
    assert "counting" in reply["error"]


def test_a_capability_deliberately_not_offered_says_why() -> None:
    assert (
        sample().execute(message("fixing"))["error"]
        == "KIO99 finds things; it does not fix them."
    )


def test_a_task_that_cannot_be_done_is_an_error_reply() -> None:
    reply = sample().execute(message("refusing"))

    assert reply["status"] == "error"
    assert reply["error"] == "Nothing to review in that request."


def test_a_bug_in_the_stand_in_costs_one_step() -> None:
    reply = sample().execute(message("crashing"))

    assert reply["status"] == "error"
    assert "ZeroDivisionError" in reply["error"]


def test_work_past_its_budget_ends_as_an_error() -> None:
    reply = sample().execute(message("slow"))

    assert reply["status"] == "error"
    assert "time budget" in reply["error"]


def test_no_workspace_says_where_to_set_one() -> None:
    reply = sample().execute(message("counting"))

    assert "Workspace" in reply["error"]


def test_nothing_outside_the_workspace_is_read(project: Path) -> None:
    request = Request.from_message(message("counting", project))

    with pytest.raises(KioError):
        request.workspace.read("../outside.txt")


# ── over HTTP ───────────────────────────────────────────────────────────────


@pytest.fixture
def server() -> Iterator[str]:
    httpd = serve(sample(), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_the_discovery_endpoints_describe_the_kio(server: str) -> None:
    assert get(server + "/health")["status"] == "ok"
    assert get(server + "/manifest")["capabilities"][0] == "counting"
    assert get(server + "/tasks")["unsupported_capabilities"] == ["fixing"]
    assert "request" in get(server + "/schema")


def test_a_failed_task_is_still_http_200(server: str) -> None:
    status, reply = post(server + "/execute", message("refusing"))

    assert status == 200
    assert reply["status"] == "error"


# ── jobs ────────────────────────────────────────────────────────────────────


def wait_for(board: JobBoard, job_id: str) -> dict[str, Any]:
    for _ in range(200):
        job = board.get(job_id)
        assert job is not None
        if job["status"] in ("success", "failure"):
            return job
        time.sleep(0.02)
    raise AssertionError("the job never finished")


def test_a_job_is_accepted_at_once_then_polled_to_its_result(project: Path) -> None:
    board = JobBoard(sample(jobs=True, job_delay=0.2))

    accepted = board.submit({**message("counting", project), "schema_version": "1.0"})
    done = wait_for(board, accepted["job_id"])

    assert accepted["status"] == "accepted"
    assert done["status"] == "success"
    assert done["output"]["files"] == 2


def test_the_same_request_is_the_same_job() -> None:
    board = JobBoard(sample(jobs=True))
    request = {**message("counting"), "idempotency_key": "kio1-abc"}

    assert board.submit(request)["job_id"] == board.submit(request)["job_id"]


def test_a_job_that_fails_says_why() -> None:
    board = JobBoard(sample(jobs=True))

    job = wait_for(board, board.submit(message("refusing"))["job_id"])

    assert job["status"] == "failure"
    assert job["diagnostics"]["message"] == "Nothing to review in that request."
