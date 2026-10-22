"""Tests for the review loop: plan, carry out, read the results, answer.

The properties that matter: a run ends with an answer built only from what
the steps returned; a review that finds work missing gets one more plan for
only that work, within a bounded number of rounds; and nothing the model
does wrong in a review can fail a run that has already done its work.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.config_loader import Config, DispatchConfig, OrchestrationConfig, load_config
from core.run_events import RunEventBus
from dispatch.agent_registry import AgentEndpoint, load_agent_registry
from dispatch.directory import AgentDirectory
from dispatch.review import (
    DECISION_CONTINUE,
    DECISION_DONE,
    Round,
    digest,
    fallback_answer,
    follow_up_request,
    parse_review,
    prefix_plan,
    review_request,
)
from dispatch.scoreboard import Scoreboard
from dispatch.workflow_executor import StepResult, WorkflowResult
from dispatch.workflow_planner import ExecutionMessage, parse_plan
from services.workflow_api.runner import WorkflowRunner

REGISTRY = load_agent_registry(
    {
        "GitHistory": {
            "type": "cli",
            "command": ["git", "log"],
            "capabilities": ["history"],
        },
        "Python": {
            "type": "cli",
            "command": ["python", "-V"],
            "capabilities": ["version"],
        },
    }
)


def plan(*steps: tuple[str, str, str], workflow_id: str = "wf-1") -> dict[str, Any]:
    """Return a plan document from (step_id, agent, capability) triples."""
    return {
        "workflow_id": workflow_id,
        "execution_mode": "sequential",
        "steps": [
            {"step_id": sid, "agent_id": agent, "capability": cap, "task": f"Do {cap}"}
            for sid, agent, cap in steps
        ],
        "explanation": "test",
    }


def finished_round(number: int = 1, output: dict[str, Any] | None = None) -> Round:
    """Return a round with one successful step."""
    executed = parse_plan(plan(("s1", "Python", "version")))
    result = WorkflowResult(
        workflow_id="wf-1",
        execution_mode="sequential",
        results=(
            StepResult(
                step_id="s1",
                agent_id="Python",
                capability="version",
                status="ok",
                duration_ms=12,
                output=output if output is not None else {"stdout": "Python 3.11.7"},
            ),
        ),
    )
    return Round(number=number, plan=executed, result=result)


# ── the pure parts ──────────────────────────────────────────────────────────


def test_a_follow_up_plan_gets_step_ids_of_its_own() -> None:
    original = parse_plan(
        {
            **plan(("s1", "Python", "version"), ("s2", "GitHistory", "history")),
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "Python",
                    "capability": "version",
                    "task": "a",
                },
                {
                    "step_id": "s2",
                    "agent_id": "GitHistory",
                    "capability": "history",
                    "task": "b",
                    "depends_on": ["s1"],
                },
            ],
        }
    )

    renamed = prefix_plan(original, "r2-")

    assert [step.step_id for step in renamed.steps] == ["r2-s1", "r2-s2"]
    assert renamed.steps[1].depends_on == ("r2-s1",)


def test_the_digest_shows_what_each_step_returned() -> None:
    text = digest([finished_round()])

    assert "s1 Python.version [ok, 12 ms]: Do version" in text
    assert "Python 3.11.7" in text


def test_a_huge_output_is_cut_rather_than_sent_whole() -> None:
    text = digest([finished_round(output={"stdout": "x" * 50_000})])

    assert len(text) < 3000
    assert "more chars" in text


def test_the_last_review_is_told_to_answer() -> None:
    assert "last round" in review_request("q", [finished_round()], final=True)
    assert "last round" not in review_request("q", [finished_round()], final=False)


@pytest.mark.parametrize(
    "decoded, final, expected",
    [
        ({"decision": "done", "answer": "A"}, False, DECISION_DONE),
        (
            {"decision": "continue", "answer": "A", "next_request": "more"},
            False,
            DECISION_CONTINUE,
        ),
        # The last round cannot continue, whatever the model says.
        (
            {"decision": "continue", "answer": "A", "next_request": "more"},
            True,
            DECISION_DONE,
        ),
        # Continuing needs something to continue with.
        ({"decision": "continue", "answer": "A"}, False, DECISION_DONE),
        # An unknown decision is read as done rather than failing.
        ({"decision": "maybe", "answer": "A"}, False, DECISION_DONE),
    ],
)
def test_a_review_decision_is_read_leniently(
    decoded: dict[str, Any], final: bool, expected: str
) -> None:
    assert parse_review(decoded, final=final).decision == expected


@pytest.mark.parametrize("decoded", [[], {"decision": "done"}, {"answer": "  "}])
def test_a_review_without_an_answer_is_refused(decoded: Any) -> None:
    with pytest.raises(ValueError):
        parse_review(decoded, final=False)


def test_the_fallback_answer_uses_only_the_results() -> None:
    answer = fallback_answer([finished_round()])

    assert answer == "Results:\n- [Python] version: Python 3.11.7"


def test_an_answer_written_as_a_list_of_lines_is_accepted() -> None:
    # What ministral-3:3b actually returned in a live run.
    review = parse_review(
        {"decision": "done", "answer": ["Latest commits: …", "Python 3.11.7 [Deneme]"]},
        final=False,
    )

    assert review.answer == "Latest commits: …\nPython 3.11.7 [Deneme]"


def test_command_output_reaches_the_reviewer_as_lines() -> None:
    log = "\n".join(f"{index:07x} commit {index}" for index in range(20))

    text = digest(
        [finished_round(output={"stdout": log, "stderr": "", "exit_code": 0})]
    )

    assert "    0000000 commit 0\n    0000001 commit 1" in text
    assert "\\n" not in text


def test_a_long_output_in_the_fallback_keeps_its_first_lines() -> None:
    log = "\n".join(f"line {index}" for index in range(20))

    answer = fallback_answer([finished_round(output={"stdout": log})])

    assert answer.splitlines()[1:4] == [
        "- [Python] version: ",
        "    line 0",
        "    line 1",
    ]
    assert answer.endswith("(+14 more lines)")


def test_a_follow_up_asks_only_for_what_is_missing() -> None:
    request = follow_up_request(
        "Show history and version", [finished_round()], "Get the history"
    )

    assert "Still needed: Get the history" in request
    assert "do not repeat it" in request
    assert "Python 3.11.7" in request


# ── configuration ───────────────────────────────────────────────────────────


def test_the_shipped_configuration_reviews_in_two_rounds() -> None:
    settings = load_config("config.example.json").orchestration

    assert settings == OrchestrationConfig(review=True, max_rounds=2)


# ── whole runs ──────────────────────────────────────────────────────────────


class Model:
    """Answers planning and review requests from two scripted queues."""

    def __init__(self, plans: list[Any], reviews: list[Any]) -> None:
        self.plans = list(plans)
        self.reviews = list(reviews)
        self.review_messages: list[str] = []
        self.plan_messages: list[str] = []

    def send_request(
        self,
        config: Config,
        client: Any,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> dict[str, str]:
        message = messages[-1]["content"]

        if system_prompt.startswith("REVIEW"):
            self.review_messages.append(message)
            reply = self.reviews.pop(0)
        else:
            self.plan_messages.append(message)
            reply = self.plans.pop(0)

        if isinstance(reply, Exception):
            raise reply

        return {"content": reply if isinstance(reply, str) else json.dumps(reply)}

    @staticmethod
    def extract_content(response: dict[str, str]) -> str:
        return response["content"]


class Transport:
    """Answers every step, or fails the agents it is told to."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.sent: list[str] = []

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        self.sent.append(message.step_id)
        failed = message.agent_id in self.failing
        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "agent_id": message.agent_id,
            "status": "error" if failed else "ok",
            "output": {} if failed else {"stdout": f"{message.agent_id} says hi"},
            "error": "boom" if failed else None,
        }

    def close(self) -> None:
        """Nothing to close."""


class Recorder:
    """Keeps what a run records."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        self.updates.append(fields)

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        self.steps.append(step)

    def last(self, field: str) -> Any:
        values = [
            update[field] for update in self.updates if update.get(field) is not None
        ]
        return values[-1] if values else None


def make_runner(
    model: Model,
    transport: Transport | None = None,
    *,
    max_rounds: int = 2,
    review: bool = True,
) -> tuple[WorkflowRunner, Recorder, RunEventBus]:
    recorder = Recorder()
    bus = RunEventBus()
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
        orchestration=OrchestrationConfig(review=review, max_rounds=max_rounds),
    )
    runner = WorkflowRunner(
        config=config,
        prompt_template="PLAN {{AGENT_CATALOG}}",
        provider=model,
        provider_client=None,
        recorder=recorder,
        transport=transport or Transport(),  # type: ignore[arg-type]
        directory=AgentDirectory(REGISTRY),
        scoreboard=Scoreboard(),
        events=bus,
        review_template="REVIEW {{AGENT_CATALOG}}",
    )
    return runner, recorder, bus


def test_a_run_ends_with_the_reviewers_answer() -> None:
    model = Model(
        plans=[plan(("s1", "Python", "version"))],
        reviews=[
            {
                "decision": "done",
                "answer": "Python is installed [Python].",
                "reason": "ok",
            }
        ],
    )
    runner, recorder, bus = make_runner(model)
    session_id = uuid4()

    runner.run(session_id, "Which python?")

    assert recorder.last("answer") == "Python is installed [Python]."
    assert recorder.last("status") == "completed"
    assert "Python says hi" in model.review_messages[0]
    kinds = [event.kind for event in bus.read(str(session_id))]
    assert kinds.index("review_started") < kinds.index("review") < kinds.index("answer")


def test_missing_work_gets_one_more_plan_for_only_that() -> None:
    model = Model(
        plans=[
            plan(("s1", "Python", "version")),
            plan(("s1", "GitHistory", "history")),
        ],
        reviews=[
            {
                "decision": "continue",
                "answer": "Only the version so far.",
                "next_request": "Get the commit history",
            },
            {"decision": "done", "answer": "Both done [Python] [GitHistory]."},
        ],
    )
    transport = Transport()
    runner, recorder, bus = make_runner(model, transport)
    session_id = uuid4()

    runner.run(session_id, "Version and history")

    assert transport.sent == ["s1", "r2-s1"]
    assert [step["step_id"] for step in recorder.steps] == ["s1", "r2-s1"]
    assert "Still needed: Get the commit history" in model.plan_messages[1]
    assert recorder.last("answer") == "Both done [Python] [GitHistory]."
    assert "last round" in model.review_messages[1]
    assert "round_started" in [event.kind for event in bus.read(str(session_id))]


def test_rounds_are_bounded_whatever_the_reviewer_asks() -> None:
    keep_going = {"decision": "continue", "answer": "Not yet.", "next_request": "More"}
    model = Model(
        plans=[plan(("s1", "Python", "version"))] * 3,
        reviews=[keep_going, keep_going, keep_going],
    )
    transport = Transport()
    runner, recorder, _ = make_runner(model, transport, max_rounds=3)

    runner.run(uuid4(), "Anything")

    assert transport.sent == ["s1", "r2-s1", "r3-s1"]
    assert len(model.review_messages) == 3
    # The last review was told to answer, and its answer stands.
    assert recorder.last("answer") == "Not yet."


def test_a_review_that_cannot_be_read_falls_back_to_the_results() -> None:
    model = Model(
        plans=[plan(("s1", "Python", "version"))], reviews=["this is not json"]
    )
    runner, recorder, bus = make_runner(model)
    session_id = uuid4()

    runner.run(session_id, "Which python?")

    assert recorder.last("answer") == "Results:\n- [Python] version: Python says hi"
    assert recorder.last("status") == "completed"
    review = next(
        event for event in bus.read(str(session_id)) if event.kind == "review"
    )
    assert review.data["fallback"] is True


def test_a_review_that_times_out_still_answers() -> None:
    model = Model(
        plans=[plan(("s1", "Python", "version"))], reviews=[TimeoutError("slow")]
    )
    runner, recorder, _ = make_runner(model)

    runner.run(uuid4(), "Which python?")

    assert recorder.last("answer").startswith("Results:")


def test_a_failed_follow_up_plan_keeps_the_answer_so_far() -> None:
    model = Model(
        plans=[plan(("s1", "Python", "version")), ValueError("bad plan")],
        reviews=[
            {
                "decision": "continue",
                "answer": "Version only.",
                "next_request": "History",
            }
        ],
    )
    runner, recorder, bus = make_runner(model)
    session_id = uuid4()

    runner.run(session_id, "Version and history")

    assert recorder.last("answer") == "Version only."
    assert recorder.last("status") == "completed"
    assert "round_failed" in [event.kind for event in bus.read(str(session_id))]


def test_a_later_round_that_fixes_a_failure_completes_the_run() -> None:
    model = Model(
        plans=[
            plan(("s1", "GitHistory", "history")),
            plan(("s1", "Python", "version")),
        ],
        reviews=[
            {
                "decision": "continue",
                "answer": "History failed.",
                "next_request": "Get the version",
            },
            {"decision": "done", "answer": "Version found [Python]; history failed."},
        ],
    )
    runner, recorder, _ = make_runner(
        model, Transport(failing=frozenset({"GitHistory"}))
    )

    runner.run(uuid4(), "History or version")

    assert recorder.last("status") == "completed"


def test_with_review_off_a_run_is_one_plan_and_no_answer() -> None:
    model = Model(plans=[plan(("s1", "Python", "version"))], reviews=[])
    runner, recorder, _ = make_runner(model, review=False)

    runner.run(uuid4(), "Which python?")

    assert model.review_messages == []
    assert recorder.last("answer") is None
    assert recorder.last("status") == "completed"


def test_reviews_can_use_another_model_on_the_same_provider() -> None:
    seen: list[str] = []

    class Recording(Model):
        def send_request(
            self, config: Config, client: Any, system_prompt: str, messages: Any
        ) -> Any:
            seen.append(f"{system_prompt.split()[0]}:{config.model}")
            return super().send_request(config, client, system_prompt, messages)

    model = Recording(
        plans=[plan(("s1", "Python", "version"))],
        reviews=[{"decision": "done", "answer": "ok"}],
    )
    runner, _, bus = make_runner(model)
    runner.config.orchestration = OrchestrationConfig(review_model="big-model")
    session_id = uuid4()

    runner.run(session_id, "Which python?")

    assert seen == ["PLAN:m", "REVIEW:big-model"]
    started = next(e for e in bus.read(str(session_id)) if e.kind == "review_started")
    assert started.data["model"] == "big-model"


def test_reviews_can_use_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workflow_api.runner as runner_module

    planner = Model(plans=[plan(("s1", "Python", "version"))] * 2, reviews=[])
    reviewer = Model(
        plans=[],
        reviews=[
            {"decision": "done", "answer": "From the cloud."},
            {"decision": "done", "answer": "Again from the cloud."},
        ],
    )
    started: list[str] = []

    class CloudModule:
        @staticmethod
        def create_client(config: Config) -> str:
            started.append(config.model)
            return "cloud-client"

        send_request = reviewer.send_request
        extract_content = staticmethod(Model.extract_content)

    monkeypatch.setattr(
        runner_module, "load_provider", lambda name, allowed: CloudModule
    )

    runner, recorder, _ = make_runner(planner)
    runner.config.allowed_providers = frozenset({"fake", "cloud"})
    runner.config.orchestration = OrchestrationConfig(
        review_provider="cloud", review_model="cloud/large"
    )

    runner.run(uuid4(), "Which python?")
    runner.run(uuid4(), "Again")  # the second review reuses the client

    assert planner.review_messages == []
    assert len(reviewer.review_messages) == 2
    assert recorder.last("answer") == "Again from the cloud."
    assert started == ["cloud/large"]  # one client, reused by the second run


def test_a_reviewer_that_cannot_start_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.workflow_api.runner as runner_module

    def refuse(name: str, allowed: frozenset[str]) -> Any:
        raise ValueError("no such provider")

    monkeypatch.setattr(runner_module, "load_provider", refuse)
    model = Model(plans=[plan(("s1", "Python", "version"))], reviews=[])
    runner, recorder, _ = make_runner(model)
    runner.config.orchestration = OrchestrationConfig(
        review_provider="cloud", review_model="cloud/large"
    )

    runner.run(uuid4(), "Which python?")

    assert recorder.last("answer").startswith("Results:")
    assert recorder.last("status") == "completed"


@pytest.mark.parametrize(
    "block, message",
    [
        ({"review_provider": "openrouter"}, "review_model is required"),
        (
            {"review_provider": "elsewhere", "review_model": "x"},
            "not in allowed_providers",
        ),
        ({"review_model": 3}, "must be a string"),
    ],
)
def test_a_reviewer_that_cannot_work_is_refused_at_load(
    tmp_path: Any, block: dict[str, Any], message: str
) -> None:
    document = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
    document["orchestration"] = block
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_config(str(path))


def test_a_failed_first_plan_fails_the_run_without_a_review() -> None:
    model = Model(plans=[ValueError("bad plan")], reviews=[])
    runner, recorder, _ = make_runner(model)

    runner.run(uuid4(), "Anything")

    assert recorder.last("status") == "failed"
    assert "bad plan" in recorder.last("error")
    assert model.review_messages == []
