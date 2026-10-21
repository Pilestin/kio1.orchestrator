"""Tests for watching a run while it happens.

The property that matters: whoever is watching sees, in order, the model's
answer as it is written, the plan, each step starting and finishing, and the
end — and the stream always ends, whatever went wrong in between.
"""

import json
import logging
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core.config_loader import Config, DispatchConfig
from core.run_events import (
    FINISHED,
    RunEventBus,
    RunLogHandler,
    TokenRelay,
    emit,
    install_log_relay,
    observed,
    observing,
)
from core.session_client import NullSessionClient
from dispatch.agent_registry import AgentEndpoint, load_agent_registry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from dispatch.workflow_executor import execute_workflow
from dispatch.workflow_planner import ExecutionMessage, parse_plan
from providers.ollama_client import _read_stream
from providers.openai_client import _collect_stream
from providers.provider_client import supports_streaming
from services.workflow_api.runner import WorkflowRunner

PLAN = {
    "workflow_id": "wf-live",
    "execution_mode": "sequential",
    "steps": [
        {
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "diagnosis",
            "task": "Find the bug",
        }
    ],
    "explanation": "One step.",
}

REGISTRY = load_agent_registry(
    {"KIO2": {"endpoint": "http://127.0.0.1:1", "capabilities": ["diagnosis"]}}
)


class Transport:
    """Answers every step successfully without sending anything."""

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "agent_id": message.agent_id,
            "status": "ok",
            "output": {"summary": "found it"},
            "error": None,
        }

    def close(self) -> None:
        """Nothing to close."""


class StreamingProvider:
    """A provider that writes its answer in fragments, as a model does."""

    def __init__(self, answer: str, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.streamed = False

    def send_request(
        self,
        config: Config,
        client: Any,
        system_prompt: str,
        messages: list[dict[str, str]],
        on_token: Any = None,
    ) -> dict[str, str]:
        if self.fail:
            raise TimeoutError("timed out")

        if on_token is not None:
            self.streamed = True
            on_token("thinking about it", "thinking")

            for index in range(0, len(self.answer), 7):
                on_token(self.answer[index : index + 7], "content")

        return {"content": self.answer}

    @staticmethod
    def extract_content(response: dict[str, str]) -> str:
        return response["content"]


class Recorder:
    """Keeps the statuses a run reports."""

    def __init__(self) -> None:
        self.statuses: list[str] = []

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        if "status" in fields:
            self.statuses.append(fields["status"])

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Do nothing."""


def make_runner(provider: Any, bus: RunEventBus | None) -> WorkflowRunner:
    """Return a runner over one fake agent."""
    config = Config(
        provider="fake",
        allowed_providers=frozenset({"fake"}),
        model="m",
        prompt_path="p",
        chat_directory="c",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        dispatch=DispatchConfig(enabled=True, registry=REGISTRY),
    )

    return WorkflowRunner(
        config=config,
        prompt_template="Plan. {{AGENT_CATALOG}}",
        provider=provider,
        provider_client=None,
        recorder=Recorder(),
        transport=Transport(),  # type: ignore[arg-type]
        directory=AgentDirectory(REGISTRY),
        scoreboard=Scoreboard(),
        events=bus,
    )


def kinds(bus: RunEventBus, run_id: str) -> list[str]:
    """Return the kinds of a run's events, in order."""
    return [event.kind for event in bus.read(run_id)]


# ── the bus ─────────────────────────────────────────────────────────────────


def test_events_are_numbered_and_read_after_a_cursor() -> None:
    bus = RunEventBus()
    bus.publish("r", "a")
    bus.publish("r", "b", value=1)
    bus.publish("other", "x")

    assert [event.id for event in bus.read("r")] == [1, 2]
    assert [event.kind for event in bus.read("r", after=1)] == ["b"]
    assert bus.read("r", after=1)[0].to_dict()["value"] == 1


def test_a_run_keeps_only_its_latest_events() -> None:
    bus = RunEventBus(events_per_run=3)

    for index in range(5):
        bus.publish("r", "tick", n=index)

    assert [event.data["n"] for event in bus.read("r")] == [2, 3, 4]


def test_only_the_latest_runs_are_kept() -> None:
    bus = RunEventBus(runs=2)

    for run_id in ("a", "b", "c"):
        bus.publish(run_id, "status")

    assert bus.read("a") == []
    assert len(bus.read("c")) == 1


def test_a_waiting_reader_is_woken_by_a_new_event() -> None:
    bus = RunEventBus()
    timer = threading.Timer(0.05, lambda: bus.publish("r", "late"))
    timer.start()

    events = bus.wait("r", after=0, timeout=5)

    assert [event.kind for event in events] == ["late"]


def test_waiting_on_a_finished_run_returns_at_once() -> None:
    bus = RunEventBus()
    bus.publish("r", FINISHED)

    assert bus.wait("r", after=1, timeout=5) == []
    assert bus.is_finished("r")


# ── reporting from anywhere ────────────────────────────────────────────────


def test_emit_outside_a_run_does_nothing() -> None:
    assert not observed()
    emit("anything", detail=1)  # must not raise


def test_emit_inside_a_run_reaches_its_stream() -> None:
    bus = RunEventBus()

    with observing(bus, "r"):
        assert observed()
        emit("hello", to="you")

    assert not observed()
    assert bus.read("r")[0].data == {"to": "you"}


def test_model_output_is_gathered_rather_than_sent_per_token() -> None:
    bus = RunEventBus()

    with observing(bus, "r"):
        relay = TokenRelay(interval=60)

        for token in ("{", '"a"', ": ", "1", "}"):
            relay(token)

        relay("hmm", "thinking")
        relay.flush()

    events = bus.read("r")

    assert [(event.data["channel"], event.data["text"]) for event in events] == [
        ("content", '{"a": 1}'),
        ("thinking", "hmm"),
    ]


def test_log_lines_from_kio1_are_relayed_and_others_are_not() -> None:
    bus = RunEventBus()
    handler = RunLogHandler()

    with observing(bus, "r"):
        for name in ("dispatch.workflow_planner", "urllib3.connectionpool"):
            handler.emit(
                logging.LogRecord(name, logging.INFO, "", 0, "routed %s", ("s1",), None)
            )

    events = bus.read("r")

    assert len(events) == 1
    assert events[0].data["message"] == "routed s1"


def test_installing_the_relay_keeps_the_terminal_as_it_was() -> None:
    root = logging.getLogger()
    before_level, before_handlers = root.level, list(root.handlers)

    remove = install_log_relay()
    remove()

    assert root.level == before_level
    assert root.handlers == before_handlers


# ── a whole run ─────────────────────────────────────────────────────────────


def test_steps_report_as_they_start_and_finish() -> None:
    bus = RunEventBus()

    with observing(bus, "r"):
        execute_workflow(
            parse_plan(PLAN),
            REGISTRY,
            Transport(),  # type: ignore[arg-type]
            timeout=5,
            max_parallel_steps=2,
        )

    started, finished = bus.read("r")

    assert (started.kind, started.data["agent_id"], started.data["target_kind"]) == (
        "step_started",
        "KIO2",
        "kio",
    )
    assert finished.kind == "step_finished"
    assert finished.data["status"] == "ok"
    assert "found it" in finished.data["output"]


def test_a_run_reports_the_answer_as_it_is_written_then_the_plan() -> None:
    bus = RunEventBus()
    provider = StreamingProvider(json.dumps(PLAN))
    session_id = uuid4()

    make_runner(provider, bus).run(session_id, "Find the bug")

    run_kinds = kinds(bus, str(session_id))
    written = "".join(
        event.data["text"]
        for event in bus.read(str(session_id))
        if event.kind == "model_output" and event.data["channel"] == "content"
    )

    assert provider.streamed
    assert json.loads(written) == PLAN
    assert run_kinds[0] == "status"
    assert run_kinds.index("planning_started") < run_kinds.index("model_output")
    assert run_kinds.index("plan") < run_kinds.index("step_started")
    assert run_kinds[-2:] == ["status", FINISHED]
    assert bus.read(str(session_id))[-2].data["status"] == "completed"


def test_a_run_that_fails_to_plan_still_ends_its_stream() -> None:
    bus = RunEventBus()
    session_id = uuid4()

    make_runner(StreamingProvider("", fail=True), bus).run(session_id, "Anything")

    events = bus.read(str(session_id))

    assert events[-1].kind == FINISHED
    assert events[-2].data["status"] == "failed"
    assert "timed out" in events[-2].data["error"]


def test_nobody_watching_means_nothing_is_streamed() -> None:
    provider = StreamingProvider(json.dumps(PLAN))

    make_runner(provider, None).run(uuid4(), "Find the bug")

    assert not provider.streamed


def test_a_provider_that_cannot_stream_still_plans() -> None:
    class Plain:
        @staticmethod
        def send_request(
            config: Any, client: Any, system_prompt: str, messages: Any
        ) -> Any:
            return {"content": json.dumps(PLAN)}

        @staticmethod
        def extract_content(response: dict[str, str]) -> str:
            return response["content"]

    bus = RunEventBus()
    session_id = uuid4()

    assert not supports_streaming(Plain)

    make_runner(Plain, bus).run(session_id, "Find the bug")

    assert "model_answer" in kinds(bus, str(session_id))
    assert "model_output" not in kinds(bus, str(session_id))


# ── the stream over HTTP ────────────────────────────────────────────────────


@pytest.fixture
def served() -> Iterator[tuple[TestClient, RunEventBus, WorkflowRunner]]:
    """Return a client whose service holds a bus and a runner."""
    bus = RunEventBus()
    runner = make_runner(StreamingProvider(json.dumps(PLAN)), bus)
    state = _State(
        runner=runner,
        session_client=NullSessionClient(),
        directory=runner.directory,
        scoreboard=Scoreboard(),
        events=bus,
    )
    app.dependency_overrides[get_state] = lambda: state

    yield TestClient(app), bus, runner

    app.dependency_overrides.clear()


def frames(text: str) -> list[dict[str, Any]]:
    """Return the data of every Server-Sent Events frame."""
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def test_a_finished_run_is_replayed_and_the_stream_ends(
    served: tuple[TestClient, RunEventBus, WorkflowRunner],
) -> None:
    client, bus, runner = served
    session_id = uuid4()
    runner.run(session_id, "Find the bug")

    response = client.get(f"/workflow/{session_id}/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    received = frames(response.text)
    assert [frame["kind"] for frame in received] == kinds(bus, str(session_id))
    assert received[-1]["kind"] == FINISHED


def test_a_reader_can_resume_where_it_left_off(
    served: tuple[TestClient, RunEventBus, WorkflowRunner],
) -> None:
    client, bus, runner = served
    session_id = uuid4()
    runner.run(session_id, "Find the bug")
    total = len(bus.read(str(session_id)))

    resumed = frames(
        client.get(
            f"/workflow/{session_id}/events", headers={"Last-Event-ID": str(total - 1)}
        ).text
    )

    assert [frame["kind"] for frame in resumed] == [FINISHED]


def test_a_run_this_process_never_saw_ends_at_once(
    served: tuple[TestClient, RunEventBus, WorkflowRunner],
) -> None:
    client, _, _ = served

    response = client.get(f"/workflow/{uuid4()}/events")

    assert frames(response.text) == []


# ── providers ───────────────────────────────────────────────────────────────


def test_an_ollama_stream_is_relayed_and_reassembled() -> None:
    lines = [
        json.dumps({"message": {"thinking": "hm"}, "done": False}).encode(),
        json.dumps({"message": {"content": '{"a"'}, "done": False}).encode(),
        b"",
        json.dumps({"message": {"content": ": 1}"}, "done": False}).encode(),
        json.dumps(
            {"done": True, "done_reason": "stop", "eval_count": 5, "message": {}}
        ).encode(),
    ]
    seen: list[tuple[str, str]] = []

    response = _read_stream(
        lines, lambda text, channel: seen.append((channel, text)), deadline=1e12
    )

    assert response["message"]["content"] == '{"a": 1}'
    assert response["done_reason"] == "stop"
    assert response["eval_count"] == 5
    assert seen[0] == ("thinking", "hm")


def test_an_ollama_error_mid_stream_is_raised() -> None:
    with pytest.raises(ValueError, match="model not found"):
        _read_stream(
            [json.dumps({"error": "model not found"}).encode()],
            lambda text, channel: None,
            deadline=1e12,
        )


def test_an_ollama_stream_past_its_deadline_times_out() -> None:
    lines = [json.dumps({"message": {"content": "x"}, "done": False}).encode()] * 3

    with pytest.raises(TimeoutError):
        _read_stream(lines, lambda text, channel: None, deadline=0)


def test_an_openai_stream_is_reassembled_into_a_response() -> None:
    def chunk(text: str | None, finish: str | None = None) -> Any:
        delta = SimpleNamespace(content=text)
        return SimpleNamespace(
            id="c1",
            model="m",
            usage=None,
            choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
        )

    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2)
    stream = [
        chunk('{"a"'),
        chunk(": 1}", "stop"),
        SimpleNamespace(id="c1", model="m", usage=usage, choices=[]),
    ]
    seen: list[str] = []

    response = _collect_stream(stream, lambda text, channel: seen.append(text))

    assert response.choices[0].message.content == '{"a": 1}'
    assert response.choices[0].finish_reason == "stop"
    assert response.usage is usage
    assert seen == ['{"a"', ": 1}"]
