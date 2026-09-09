import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from core.config_loader import Config, DispatchConfig
from core.session_client import NullSessionClient, SessionClientError
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.policy import Gate, Policy
from dispatch.scoreboard import Scoreboard
from services.workflow_api.app import _State, app, get_state
from services.workflow_api.runner import WorkflowRunner

PLAN = {
    "workflow_id": "wf-api01",
    "execution_mode": "sequential",
    "steps": [
        {
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "bug_localization",
            "task": "Locate the defect",
        },
        {
            "step_id": "s2",
            "agent_id": "KIO7",
            "capability": "code_generation",
            "task": "Write the fix",
        },
    ],
    "explanation": "Find and fix.",
}


class FakeProvider:
    """Stands in for a provider module, returning a canned model response."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def send_request(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"content": self.content}

    def extract_content(self, response: dict[str, Any]) -> str:
        return str(response["content"])


class FailingProvider:
    """Stands in for a provider whose request raises."""

    def send_request(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("model unreachable")

    def extract_content(self, response: dict[str, Any]) -> str:  # pragma: no cover
        return ""


class FakeRecorder:
    """Captures what a run recorded, in order."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        self.updates.append(fields)

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        self.steps.append(step)

    @property
    def statuses(self) -> list[str]:
        """Return every status this recorder was given, in order."""
        return [update["status"] for update in self.updates if "status" in update]


class FakeTransport:
    """Answers dispatches from a canned map of replies."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}

    def send(self, message: Any, agent: Any, timeout: float) -> dict[str, Any]:
        reply = self.replies.get(message.step_id)

        if isinstance(reply, Exception):
            raise reply

        return reply or {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "agent_id": message.agent_id,
            "status": "ok",
            "output": {"summary": f"did {message.step_id}"},
        }


def make_config(agents: dict[str, AgentEndpoint] | None = None) -> Config:
    """Build a configuration with dispatch enabled for the given agents."""
    if agents is None:
        agents = {
            "KIO2": AgentEndpoint(
                "KIO2", "http://localhost:8102", "/execute", frozenset()
            ),
            "KIO7": AgentEndpoint(
                "KIO7", "http://localhost:8107", "/execute", frozenset()
            ),
        }

    return Config(
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
            registry=AgentRegistry(agents),
        ),
    )


def make_runner(
    provider: Any,
    recorder: FakeRecorder,
    transport: Any | None = None,
    config: Config | None = None,
) -> WorkflowRunner:
    """Assemble a runner from fakes."""
    return WorkflowRunner(
        config=config if config is not None else make_config(),
        prompt_template="You are KIO1.",
        provider=provider,
        provider_client=None,
        recorder=recorder,
        transport=transport if transport is not None else FakeTransport(),
        directory=AgentDirectory(
            (config if config is not None else make_config()).dispatch.registry
        ),
        scoreboard=Scoreboard(),
    )


# ── runner ───────────────────────────────────────────────────────────────────


def test_a_successful_run_is_recorded_from_running_to_completed() -> None:
    recorder = FakeRecorder()
    runner = make_runner(FakeProvider(json.dumps(PLAN)), recorder)

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.statuses == ["running", "completed"]
    assert {
        "workflow_id": "wf-api01",
        "execution_mode": "sequential",
    } in recorder.updates
    assert [step["step_id"] for step in recorder.steps] == ["s1", "s2"]
    assert recorder.steps[0]["output"] == {"summary": "did s1"}


def test_recorded_steps_carry_the_task_text_from_the_plan() -> None:
    recorder = FakeRecorder()
    runner = make_runner(FakeProvider(json.dumps(PLAN)), recorder)

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.steps[0]["task"] == "Locate the defect"
    assert recorder.steps[1]["task"] == "Write the fix"


def test_a_provider_failure_fails_the_session_without_raising() -> None:
    recorder = FakeRecorder()
    runner = make_runner(FailingProvider(), recorder)

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.statuses == ["running", "failed"]
    assert "RuntimeError" in recorder.updates[-1]["error"]
    assert recorder.steps == []


def test_an_unusable_model_response_fails_the_session() -> None:
    recorder = FakeRecorder()
    runner = make_runner(FakeProvider('{"not": "a plan"}'), recorder)

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.statuses == ["running", "failed"]
    assert recorder.steps == []


def test_a_failed_step_fails_the_session_but_still_records_steps() -> None:
    recorder = FakeRecorder()
    runner = make_runner(
        FakeProvider(json.dumps(PLAN)),
        recorder,
        FakeTransport({"s1": Exception("KIO2 exploded")}),
    )

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.statuses == ["running", "failed"]
    assert recorder.steps[0]["status"] == "error"
    assert recorder.steps[1]["status"] == "not_run"


def test_output_is_only_recorded_for_successful_steps() -> None:
    recorder = FakeRecorder()
    runner = make_runner(
        FakeProvider(json.dumps(PLAN)),
        recorder,
        # An agent that reports an error should not leave an artifact behind.
        FakeTransport(
            {
                "s1": {
                    "workflow_id": "wf-api01",
                    "step_id": "s1",
                    "status": "error",
                    "error": "repository not found",
                    "output": {"leaked": True},
                }
            }
        ),
    )

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.steps[0]["status"] == "error"
    assert recorder.steps[0]["output"] is None


def test_steps_for_undeployed_agents_are_recorded_as_skipped() -> None:
    recorder = FakeRecorder()
    config = make_config(
        {
            "KIO2": AgentEndpoint(
                "KIO2", "http://localhost:8102", "/execute", frozenset()
            )
        }
    )
    runner = make_runner(FakeProvider(json.dumps(PLAN)), recorder, config=config)

    runner.run(uuid4(), "Fix the payment bug")

    assert recorder.statuses == ["running", "completed"]
    assert recorder.steps[1]["status"] == "skipped"
    assert recorder.steps[1]["error"] == "no_endpoint"


# ── HTTP surface ─────────────────────────────────────────────────────────────


class FakeSessionClient:
    """Stands in for the Session Manager client."""

    def __init__(
        self,
        session_id: UUID | None = None,
        detail: dict[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        self.session_id = session_id or uuid4()
        self.detail = detail
        self.fail = fail
        self.created: list[str] = []
        self.sessions: list[dict[str, Any]] = [
            {"session_id": str(self.session_id), "status": "completed"}
        ]

    def create_session(self, request_text: str) -> UUID:
        if self.fail:
            raise SessionClientError("Session Manager is unreachable")

        self.created.append(request_text)

        return self.session_id

    def get_session(self, session_id: UUID) -> dict[str, Any]:
        if self.detail is None:
            raise SessionClientError("unknown session")

        return self.detail

    def list_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        if self.fail:
            raise SessionClientError("Session Manager is unreachable")

        return self.sessions[:limit]


class SpyRunner:
    """Records the arguments a background run was started with."""

    def __init__(self) -> None:
        self.runs: list[tuple[UUID, str]] = []

    def run(self, session_id: UUID, request_text: str) -> None:
        self.runs.append((session_id, request_text))


def build_client(
    session_client: FakeSessionClient,
    runner: SpyRunner | None = None,
) -> tuple[TestClient, SpyRunner]:
    """Build a test client whose state is entirely fake.

    TestClient is constructed without a context manager on purpose: that
    skips the lifespan, so no provider is loaded and no model is contacted.
    """
    spy = runner if runner is not None else SpyRunner()
    app.dependency_overrides[get_state] = lambda: _State(
        runner=spy,  # type: ignore[arg-type]
        session_client=session_client,  # type: ignore[arg-type]
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
    )

    return TestClient(app), spy


@pytest.fixture(autouse=True)
def clear_overrides() -> Iterator[None]:
    """Remove dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


def test_health_reports_the_service_name() -> None:
    client, _ = build_client(FakeSessionClient())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "workflow-api"}


def test_run_returns_202_with_a_session_id_and_status_url() -> None:
    session_id = uuid4()
    client, _ = build_client(FakeSessionClient(session_id=session_id))

    response = client.post("/workflow/run", json={"request": "Fix the payment bug"})

    assert response.status_code == 202
    body = response.json()
    assert body["session_id"] == str(session_id)
    assert body["status"] == "pending"
    assert body["status_url"].endswith(f"/workflow/{session_id}")


def test_run_starts_the_workflow_in_the_background() -> None:
    session_id = uuid4()
    client, spy = build_client(FakeSessionClient(session_id=session_id))

    client.post("/workflow/run", json={"request": "Fix the payment bug"})

    assert spy.runs == [(session_id, "Fix the payment bug")]


def test_run_rejects_an_empty_request() -> None:
    client, spy = build_client(FakeSessionClient())

    assert client.post("/workflow/run", json={"request": ""}).status_code == 422
    assert spy.runs == []


def test_run_is_refused_when_no_session_can_be_opened() -> None:
    client, spy = build_client(FakeSessionClient(fail=True))

    response = client.post("/workflow/run", json={"request": "Fix the payment bug"})

    assert response.status_code == 503
    assert "Session Manager" in response.json()["detail"]
    # Nothing must run if the caller has no identifier to track it by.
    assert spy.runs == []


def test_get_workflow_passes_the_session_record_through() -> None:
    session_id = uuid4()
    detail = {
        "session": {"session_id": str(session_id), "status": "completed"},
        "steps": [{"step_id": "s1", "status": "ok"}],
        "lineage": [],
    }
    client, _ = build_client(FakeSessionClient(session_id=session_id, detail=detail))

    response = client.get(f"/workflow/{session_id}")

    assert response.status_code == 200
    assert response.json() == detail


def test_get_workflow_returns_404_for_an_unrecorded_session() -> None:
    client, _ = build_client(FakeSessionClient())

    assert client.get(f"/workflow/{uuid4()}").status_code == 404


def test_get_workflow_rejects_a_malformed_session_id() -> None:
    client, _ = build_client(FakeSessionClient())

    assert client.get("/workflow/not-a-uuid").status_code == 422


# ── the operator console ─────────────────────────────────────────────────────


def test_the_console_is_served_from_the_service_itself() -> None:
    client, _ = build_client(FakeSessionClient())

    response = client.get("/")

    assert response.status_code == 200
    assert "KIO1 Console" in response.text


def test_the_console_loads_nothing_from_anywhere_else() -> None:
    # It has to work on a machine with no route to the internet, so no CDN,
    # no font host, no analytics. Only the placeholder inside the example
    # manifest names a host, and that one is a loopback address.
    client, _ = build_client(FakeSessionClient())

    page = client.get("/").text

    assert "https://" not in page
    assert page.count("http://") == page.count("http://127.0.0.1")


def test_recent_runs_are_listed() -> None:
    recorder = FakeSessionClient()
    client, _ = build_client(recorder)

    response = client.get("/workflows?limit=5")

    assert response.status_code == 200
    assert response.json()["workflows"] == recorder.sessions


def test_listing_runs_reports_an_unavailable_session_manager() -> None:
    client, _ = build_client(FakeSessionClient(fail=True))

    assert client.get("/workflows").status_code == 503


def test_recording_being_off_lists_nothing_rather_than_failing() -> None:
    # A console asking what has run wants an empty answer when the answer is
    # nothing, not an error.
    assert NullSessionClient().list_sessions() == []


def test_a_workflow_stopped_by_a_gate_is_not_reported_as_completed() -> None:
    # The console shows this word before anything else. A release a policy
    # refused has not completed, even though every call that ran succeeded.
    recorder = FakeRecorder()
    plan = json.dumps(
        {
            "workflow_id": "wf-gated",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "KIO11",
                    "capability": "test_automation",
                    "task": "Run the suite",
                },
                {
                    "step_id": "s2",
                    "agent_id": "KIO8",
                    "capability": "deployment",
                    "task": "Release it",
                },
            ],
            "explanation": "Check then ship.",
        }
    )
    config = make_config(
        {
            "KIO11": AgentEndpoint(
                "KIO11", "http://localhost:8111", "/execute", frozenset()
            ),
            "KIO8": AgentEndpoint(
                "KIO8", "http://localhost:8108", "/execute", frozenset()
            ),
        }
    )
    config.dispatch = replace(
        config.dispatch,
        policy=Policy(
            gates=(
                Gate(
                    before=frozenset({"deployment"}),
                    requires=frozenset({"test_automation"}),
                    verdict=frozenset({"clean"}),
                ),
            )
        ),
    )
    runner = make_runner(
        FakeProvider(plan),
        recorder,
        transport=FakeTransport(
            {
                "s1": {
                    "workflow_id": "wf-gated",
                    "step_id": "s1",
                    "agent_id": "KIO11",
                    "status": "ok",
                    "output": {"verdict": "defect"},
                }
            }
        ),
        config=config,
    )

    runner.run(uuid4(), "Ship the pricing change")

    assert recorder.statuses[-1] == "failed"
