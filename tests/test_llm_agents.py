"""Tests for agents that are a written role, run by a model inside KIO1.

The properties that matter: a name, a description, and instructions are
enough to make an agent the planner can use; a step reaches it with its task
and everything earlier steps produced; and whatever the model replies
becomes the step's output, nothing lost.
"""

import json
from typing import Any
from uuid import uuid4

import pytest

import dispatch.llm_transport as llm_module
from core.config_loader import Config, DispatchConfig
from core.prompt_loader import render_catalog
from core.run_events import RunEventBus, observing
from dispatch.agent_registry import (
    KIND_LLM,
    LlmToolSpec,
    load_agent_entry,
    load_agent_registry,
)
from dispatch.agent_transport import AgentTransportError
from dispatch.directory import HealthConfig, probe_target
from dispatch.llm_transport import LlmTransport, build_message, read_answer
from dispatch.transport_factory import build_transport
from dispatch.workflow_planner import ExecutionMessage

REVIEWER = {
    "type": "llm",
    "description": "Reviews code for bugs.",
    "instructions": "You are a careful code reviewer.",
    "capabilities": ["code_review"],
}


def config(**changes: Any) -> Config:
    """Return a configuration whose planning model is `planner-model`."""
    values: dict[str, Any] = {
        "provider": "fake",
        "allowed_providers": frozenset({"fake", "cloud"}),
        "model": "planner-model",
        "prompt_path": "p",
        "chat_directory": "c",
        "temperature": 0.2,
        "request_timeout": 30,
        "keep_alive": -1,
        "max_output_tokens": 1024,
    }
    return Config(**{**values, **changes})


def message(**data: Any) -> ExecutionMessage:
    return ExecutionMessage(
        workflow_id="wf-1",
        step_id="s2",
        agent_id="Reviewer",
        capability="code_review",
        endpoint="llm:Reviewer",
        task="Review the new payment code",
        data=data,
    )


class Provider:
    """A provider module that records what it was asked."""

    def __init__(
        self, reply: str = '{"result": "Two bugs found."}', fail: bool = False
    ) -> None:
        self.reply = reply
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.clients = 0

    def create_client(self, config: Config) -> str:
        self.clients += 1
        return "client"

    def send_request(
        self,
        config: Config,
        client: Any,
        system_prompt: str,
        messages: list[dict[str, str]],
        on_token: Any = None,
    ) -> str:
        self.calls.append(
            {
                "model": config.model,
                "temperature": config.temperature,
                "system": system_prompt,
                "user": messages[-1]["content"],
            }
        )
        if self.fail:
            raise TimeoutError("too slow")
        if on_token is not None:
            on_token(self.reply, "content")
        return self.reply

    @staticmethod
    def extract_content(response: str) -> str:
        return response


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Provider:
    fake = Provider()
    monkeypatch.setattr(llm_module, "load_provider", lambda name, allowed: fake)
    return fake


# ── defining one ────────────────────────────────────────────────────────────


def test_a_role_and_a_skill_make_an_agent() -> None:
    entry = load_agent_entry("Reviewer", REVIEWER)

    assert entry.kind == KIND_LLM
    assert entry.endpoint == "llm:Reviewer"
    assert isinstance(entry.tool, LlmToolSpec)
    assert entry.tool.instructions == "You are a careful code reviewer."


@pytest.mark.parametrize(
    "change, message",
    [
        ({"instructions": "  "}, "instructions must say what this agent is"),
        ({"capabilities": []}, "must name at least one thing it does"),
        ({"provider": "cloud"}, "model is required with provider"),
        ({"temperature": 3}, "temperature must be a number from 0 to 2"),
        ({"instructions": "x" * 9000}, "keep it under"),
    ],
)
def test_an_agent_that_cannot_work_is_refused(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_agent_entry("Reviewer", {**REVIEWER, **change})


def test_the_planner_sees_it_as_an_llm_agent() -> None:
    catalog = render_catalog(load_agent_registry({"Reviewer": REVIEWER}))

    assert "**Reviewer** _(LLM agent)_" in catalog
    assert "`code_review`" in catalog


def test_it_needs_no_network_to_be_ready() -> None:
    result = probe_target(
        load_agent_entry("Reviewer", REVIEWER), HealthConfig(enabled=True)
    )

    assert (result.reachable, result.detail) == (True, "")


# ── running one ─────────────────────────────────────────────────────────────


def test_the_step_gets_its_task_arguments_and_earlier_results() -> None:
    text = build_message(
        message(arguments={"path": "pay.py"}, upstream={"s1": {"stdout": "diff --git"}})
    )

    assert text.startswith("## Task\nReview the new payment code")
    assert '"path": "pay.py"' in text
    assert "diff --git" in text


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"result": "ok", "verdict": "clean"}', {"result": "ok", "verdict": "clean"}),
        ("Just prose, no JSON.", {"result": "Just prose, no JSON."}),
        ('{"something": "else"}', {"result": '{"something": "else"}'}),
    ],
)
def test_whatever_the_model_says_becomes_the_output(
    reply: str, expected: dict[str, Any]
) -> None:
    assert read_answer(reply) == expected


def test_the_role_is_the_system_prompt_and_the_planner_model_runs_it(
    provider: Provider,
) -> None:
    transport = LlmTransport(config())

    reply = transport.send(message(), load_agent_entry("Reviewer", REVIEWER), 5)

    assert reply["status"] == "ok"
    assert reply["output"] == {"result": "Two bugs found."}
    call = provider.calls[0]
    assert call["system"].startswith("You are a careful code reviewer.")
    assert '{"result":' in call["system"]
    assert call["model"] == "planner-model"


def test_an_agent_can_name_its_own_model(provider: Provider) -> None:
    transport = LlmTransport(config())
    entry = load_agent_entry(
        "Reviewer",
        {**REVIEWER, "provider": "cloud", "model": "big-model", "temperature": 0},
    )

    transport.send(message(), entry, 5)
    transport.send(message(), entry, 5)

    assert provider.calls[0]["model"] == "big-model"
    assert provider.calls[0]["temperature"] == 0
    assert provider.clients == 1  # started once, reused


def test_a_model_that_fails_fails_the_step_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_module, "load_provider", lambda name, allowed: Provider(fail=True)
    )

    with pytest.raises(AgentTransportError, match="Reviewer: the model did not answer"):
        LlmTransport(config()).send(
            message(), load_agent_entry("Reviewer", REVIEWER), 5
        )


def test_what_the_agent_writes_appears_in_the_live_view(provider: Provider) -> None:
    bus = RunEventBus()
    run_id = str(uuid4())

    with observing(bus, run_id):
        LlmTransport(config()).send(
            message(), load_agent_entry("Reviewer", REVIEWER), 5
        )

    written = "".join(
        e.data["text"] for e in bus.read(run_id) if e.kind == "model_output"
    )
    assert "— Reviewer —" in written
    assert "Two bugs found." in written


def test_the_transport_routes_llm_agents_when_it_knows_the_model(
    provider: Provider,
) -> None:
    routed = build_transport(DispatchConfig(enabled=True), config=config())

    reply = routed.send(message(), load_agent_entry("Reviewer", REVIEWER), 5)

    assert json.loads(json.dumps(reply))["output"]["result"] == "Two bugs found."
