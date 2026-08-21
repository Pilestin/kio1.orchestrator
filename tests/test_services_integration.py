import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from core.config_loader import Config, DispatchConfig
from core.session_client import SessionClient
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.agent_transport import HttpAgentTransport
from services.session_manager.app import app as session_manager_app
from services.session_manager.app import get_repository
from services.session_manager.repository import InMemorySessionRepository
from services.workflow_api.app import _State
from services.workflow_api.app import app as workflow_app
from services.workflow_api.app import get_state
from services.workflow_api.runner import WorkflowRunner
from tools.dummy_agent import build_server, server_address

PLAN = {
    "workflow_id": "wf-int01",
    "execution_mode": "sequential",
    "steps": [
        {
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "bug_localization",
            "task": "Locate the null dereference",
        },
        {
            "step_id": "s2",
            "agent_id": "KIO2",
            "capability": "fix_recommendation",
            "task": "Recommend a fix",
        },
        {
            "step_id": "s3",
            "agent_id": "KIO4",
            "capability": "architecture_design",
            "task": "Not deployed anywhere",
        },
    ],
    "explanation": "Diagnose, fix, and design.",
}


class PlanningProvider:
    """Returns a fixed plan, standing in for a real model provider."""

    def send_request(self, **kwargs: Any) -> dict[str, Any]:
        return {"content": json.dumps(PLAN)}

    def extract_content(self, response: dict[str, Any]) -> str:
        return str(response["content"])


def _serve(app: Any) -> Iterator[str]:
    """Run a FastAPI app on an ephemeral port and yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15

    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)

    if not server.started:  # pragma: no cover - only on a very slow machine
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("Session Manager did not start in time")

    host, port = server.servers[0].sockets[0].getsockname()[:2]

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def session_manager_url() -> Iterator[str]:
    """Serve the Session Manager over real HTTP, backed by in-memory storage."""
    repository = InMemorySessionRepository()
    session_manager_app.dependency_overrides[get_repository] = lambda: repository

    try:
        yield from _serve(session_manager_app)
    finally:
        session_manager_app.dependency_overrides.clear()


@pytest.fixture
def kio2_endpoint() -> Iterator[AgentEndpoint]:
    """Serve a KIO2 stub agent on an ephemeral port."""
    server = build_server(agent_id="KIO2", host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server_address(server)

    try:
        yield AgentEndpoint(
            agent_id="KIO2",
            endpoint=f"http://{host}:{port}",
            path="/execute",
            capabilities=frozenset({"bug_localization", "fix_recommendation"}),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def client(
    session_manager_url: str,
    kio2_endpoint: AgentEndpoint,
) -> Iterator[TestClient]:
    """Wire the Workflow API to a live Session Manager and a live stub agent.

    Only the model provider is faked. Session recording and agent dispatch
    both go over real HTTP.
    """
    config = Config(
        provider="ollama",
        allowed_providers=frozenset({"ollama"}),
        model="test-model",
        prompt_path="prompts/test.txt",
        chat_directory="chats",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        dispatch=DispatchConfig(
            enabled=True,
            request_timeout=10.0,
            max_parallel_steps=2,
            registry=AgentRegistry({"KIO2": kio2_endpoint}),
        ),
    )
    session_client = SessionClient(session_manager_url)
    runner = WorkflowRunner(
        config=config,
        system_prompt="You are KIO1.",
        provider=PlanningProvider(),
        provider_client=None,
        recorder=session_client,
        transport=HttpAgentTransport(),
    )
    workflow_app.dependency_overrides[get_state] = lambda: _State(
        runner=runner,
        session_client=session_client,
    )

    try:
        yield TestClient(workflow_app)
    finally:
        workflow_app.dependency_overrides.clear()


def test_a_request_runs_end_to_end_and_is_fully_recorded(
    client: TestClient,
) -> None:
    accepted = client.post(
        "/workflow/run",
        json={"request": "There is a null pointer exception in the payment module"},
    )

    assert accepted.status_code == 202
    session_id = accepted.json()["session_id"]

    # TestClient runs background tasks before returning, so the workflow has
    # already finished by the time the response arrives.
    detail = client.get(f"/workflow/{session_id}")
    assert detail.status_code == 200
    body = detail.json()

    assert body["session"]["status"] == "completed"
    assert body["session"]["workflow_id"] == "wf-int01"
    assert body["session"]["execution_mode"] == "sequential"
    assert (
        body["session"]["request_text"]
        == "There is a null pointer exception in the payment module"
    )

    statuses = {step["step_id"]: step["status"] for step in body["steps"]}
    assert statuses == {"s1": "ok", "s2": "ok", "s3": "skipped"}

    # The stub agent echoes the task, which proves the dispatch actually
    # travelled over HTTP rather than being short-circuited anywhere.
    first_step = next(step for step in body["steps"] if step["step_id"] == "s1")
    assert first_step["output"]["received_task"] == "Locate the null dereference"

    # Only steps that produced an artifact take part in lineage, so the
    # undeployed KIO4 step does not appear.
    assert [
        (edge["produced_by_step_id"], edge["consumed_by_step_id"], edge["basis"])
        for edge in body["lineage"]
    ] == [("s1", "s2", "execution_order")]


def test_an_unreachable_agent_is_recorded_as_a_failed_session(
    session_manager_url: str,
) -> None:
    config = Config(
        provider="ollama",
        allowed_providers=frozenset({"ollama"}),
        model="test-model",
        prompt_path="prompts/test.txt",
        chat_directory="chats",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        dispatch=DispatchConfig(
            enabled=True,
            request_timeout=5.0,
            max_parallel_steps=2,
            registry=AgentRegistry(
                {
                    # Port 1 is reserved and never listening, so this fails fast.
                    "KIO2": AgentEndpoint(
                        "KIO2", "http://127.0.0.1:1", "/execute", frozenset()
                    )
                }
            ),
        ),
    )
    session_client = SessionClient(session_manager_url)
    workflow_app.dependency_overrides[get_state] = lambda: _State(
        runner=WorkflowRunner(
            config=config,
            system_prompt="You are KIO1.",
            provider=PlanningProvider(),
            provider_client=None,
            recorder=session_client,
            transport=HttpAgentTransport(),
        ),
        session_client=session_client,
    )

    try:
        client = TestClient(workflow_app)
        session_id = client.post(
            "/workflow/run", json={"request": "Fix the payment bug"}
        ).json()["session_id"]
        body = client.get(f"/workflow/{session_id}").json()
    finally:
        workflow_app.dependency_overrides.clear()

    assert body["session"]["status"] == "failed"

    steps = {step["step_id"]: step for step in body["steps"]}
    assert steps["s1"]["status"] == "error"
    assert "unreachable" in steps["s1"]["error"]
    # Sequential execution halts, so the step after the failure never ran.
    assert steps["s2"]["status"] == "not_run"
