"""Tests for carrying a step out somewhere other than where it was planned.

The plan names an agent. What it asked for is a capability. These check that
the difference is used when the named agent cannot deliver, and that a step
which moved never reads like one that ran where it was sent.
"""

import json
from typing import Any

import pytest

from core.config_loader import DispatchConfig, load_config
from dispatch.agent_registry import AgentEndpoint, AgentRegistry, load_agent_registry
from dispatch.failover import FailoverConfig, candidates_for, load_failover_config
from dispatch.workflow_executor import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    dispatch_plan,
    execute_workflow,
    render_summary,
)
from dispatch.workflow_planner import ExecutionMessage, parse_plan, route_plan

ON = FailoverConfig(enabled=True, max_attempts=2)
OFF = FailoverConfig()


def registry_of(**agents: list[str]) -> AgentRegistry:
    """Return a registry of KIO-style agents from id to capability list."""
    return load_agent_registry(
        {
            agent_id: {
                "endpoint": f"http://127.0.0.1:8{index + 100}",
                "capabilities": capabilities,
            }
            for index, (agent_id, capabilities) in enumerate(agents.items())
        }
    )


TWO_CODERS = registry_of(
    KIO7=["code_generation"],
    MyCodeGen=["code_generation", "refactor"],
    KIO11=["test_automation"],
)


class ScriptedTransport:
    """Answers each agent the way the test told it to."""

    def __init__(self, replies: dict[str, dict[str, Any]]) -> None:
        self.replies = replies
        self.called: list[str] = []

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        self.called.append(agent.agent_id)
        reply = self.replies.get(agent.agent_id)

        if reply is None:
            from dispatch.agent_transport import AgentTransportError

            raise AgentTransportError(f"{agent.agent_id} is unreachable")

        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "agent_id": agent.agent_id,
            **reply,
        }

    def close(self) -> None:
        """Nothing to release."""


OK = {"status": "ok", "output": {"summary": "done"}}
REFUSED = {"status": "error", "error": "cannot do that"}


def one_step_plan(agent_id: str = "KIO7") -> dict[str, Any]:
    """Return a one-step plan asking for code generation."""
    return {
        "workflow_id": "wf-fo01",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": agent_id,
                "capability": "code_generation",
                "task": "Write the pricing module",
            }
        ],
        "explanation": "One step.",
    }


def run(
    plan_data: dict[str, Any],
    registry: AgentRegistry,
    failover: FailoverConfig,
    replies: dict[str, dict[str, Any]],
) -> tuple[Any, ScriptedTransport]:
    """Execute a plan and return the result and the transport that served it."""
    transport = ScriptedTransport(replies)
    result = execute_workflow(
        parse_plan(plan_data),
        registry,
        transport,
        timeout=5,
        max_parallel_steps=4,
        failover=failover,
    )

    return result, transport


# ── who is a candidate, and in what order ────────────────────────────────────


def test_the_named_agent_comes_first() -> None:
    candidates = candidates_for("KIO7", "code_generation", TWO_CODERS, ON)

    assert [entry.agent_id for entry in candidates] == ["KIO7", "MyCodeGen"]


def test_failover_off_offers_only_the_named_agent() -> None:
    candidates = candidates_for("KIO7", "code_generation", TWO_CODERS, OFF)

    assert [entry.agent_id for entry in candidates] == ["KIO7"]


def test_an_agent_that_does_not_declare_the_capability_is_not_a_candidate() -> None:
    candidates = candidates_for("KIO7", "code_generation", TWO_CODERS, ON)

    assert "KIO11" not in [entry.agent_id for entry in candidates]


def test_max_attempts_caps_the_list() -> None:
    registry = registry_of(A=["x"], B=["x"], C=["x"], D=["x"])
    capped = candidates_for(
        "A", "x", registry, FailoverConfig(enabled=True, max_attempts=2)
    )
    wider = candidates_for(
        "A", "x", registry, FailoverConfig(enabled=True, max_attempts=3)
    )

    assert len(capped) == 2
    assert len(wider) == 3


def test_an_unknown_agent_still_has_candidates_when_others_can_do_it() -> None:
    # The model named something that does not exist. The capability does.
    candidates = candidates_for("KIO99", "code_generation", TWO_CODERS, ON)

    assert [entry.agent_id for entry in candidates] == ["KIO7", "MyCodeGen"]


def test_nothing_declaring_the_capability_means_no_candidates() -> None:
    assert candidates_for("KIO7", "quantum_magic", TWO_CODERS, ON) == ()


# ── reassignment before dispatch ─────────────────────────────────────────────


def test_a_step_whose_agent_is_gone_is_reassigned_rather_than_skipped() -> None:
    # This is the case the live directory creates: KIO7 was withdrawn between
    # planning and dispatch, so it is not in the registry at all.
    without_kio7 = registry_of(MyCodeGen=["code_generation"])
    routed = route_plan(parse_plan(one_step_plan()), without_kio7, failover=ON)

    assert not routed.skipped
    assert routed.messages[0].agent_id == "MyCodeGen"


def test_the_same_step_is_skipped_when_failover_is_off() -> None:
    without_kio7 = registry_of(MyCodeGen=["code_generation"])
    routed = route_plan(parse_plan(one_step_plan()), without_kio7, failover=OFF)

    assert not routed.messages
    assert routed.skipped[0].step_id == "s1"


def test_a_step_nothing_can_do_is_still_skipped() -> None:
    routed = route_plan(
        parse_plan(
            {
                "workflow_id": "wf-x",
                "execution_mode": "sequential",
                "steps": [
                    {
                        "step_id": "s1",
                        "agent_id": "KIO7",
                        "capability": "quantum_magic",
                        "task": "Do the impossible",
                    }
                ],
                "explanation": "One step.",
            }
        ),
        TWO_CODERS,
        failover=ON,
    )

    assert routed.skipped[0].capability == "quantum_magic"


# ── moving after a failure ───────────────────────────────────────────────────


def test_a_step_that_fails_is_carried_out_by_the_next_candidate() -> None:
    result, transport = run(one_step_plan(), TWO_CODERS, ON, {"MyCodeGen": OK})
    step = result.results[0]

    assert transport.called == ["KIO7", "MyCodeGen"]
    assert step.status == STATUS_OK
    assert step.agent_id == "MyCodeGen"


def test_an_agent_that_refuses_the_work_also_hands_it_on() -> None:
    result, transport = run(
        one_step_plan(), TWO_CODERS, ON, {"KIO7": REFUSED, "MyCodeGen": OK}
    )

    assert transport.called == ["KIO7", "MyCodeGen"]
    assert result.results[0].status == STATUS_OK


def test_a_working_first_choice_is_never_second_guessed() -> None:
    result, transport = run(
        one_step_plan(), TWO_CODERS, ON, {"KIO7": OK, "MyCodeGen": OK}
    )
    step = result.results[0]

    assert transport.called == ["KIO7"]
    assert step.planned_agent_id is None
    assert len(step.attempts) == 1


def test_every_attempt_is_recorded() -> None:
    result, _ = run(one_step_plan(), TWO_CODERS, ON, {"MyCodeGen": OK})
    attempts = result.results[0].attempts

    assert [attempt.agent_id for attempt in attempts] == ["KIO7", "MyCodeGen"]
    assert attempts[0].status == STATUS_ERROR
    assert attempts[0].error is not None
    assert attempts[1].status == STATUS_OK


def test_a_step_that_moved_says_where_it_was_planned() -> None:
    result, _ = run(one_step_plan(), TWO_CODERS, ON, {"MyCodeGen": OK})
    step = result.results[0]

    assert step.agent_id == "MyCodeGen"
    assert step.planned_agent_id == "KIO7"


def test_every_candidate_failing_leaves_the_last_failure() -> None:
    result, transport = run(one_step_plan(), TWO_CODERS, ON, {})
    step = result.results[0]

    assert transport.called == ["KIO7", "MyCodeGen"]
    assert step.status == STATUS_ERROR
    assert len(step.attempts) == 2


def test_max_attempts_bounds_what_a_refused_task_costs() -> None:
    registry = registry_of(A=["x"], B=["x"], C=["x"])
    plan = {
        "workflow_id": "wf-x",
        "execution_mode": "sequential",
        "steps": [{"step_id": "s1", "agent_id": "A", "capability": "x", "task": "Try"}],
        "explanation": "One step.",
    }
    _, transport = run(plan, registry, FailoverConfig(enabled=True, max_attempts=2), {})

    assert transport.called == ["A", "B"]


def test_with_failover_off_a_failed_step_stays_failed() -> None:
    # The behaviour every existing deployment already has.
    result, transport = run(one_step_plan(), TWO_CODERS, OFF, {"MyCodeGen": OK})

    assert transport.called == ["KIO7"]
    assert result.results[0].status == STATUS_ERROR


# ── what a reader sees ───────────────────────────────────────────────────────


def test_the_summary_says_a_step_ran_somewhere_else() -> None:
    result, _ = run(one_step_plan(), TWO_CODERS, ON, {"MyCodeGen": OK})
    summary = render_summary(result)

    assert "MyCodeGen" in summary
    assert "planned KIO7" in summary


def test_the_summary_says_nothing_when_a_step_stayed_put() -> None:
    result, _ = run(one_step_plan(), TWO_CODERS, ON, {"KIO7": OK})

    assert "planned" not in render_summary(result)


# ── in a whole workflow ──────────────────────────────────────────────────────


def test_a_moved_step_still_feeds_the_step_that_depends_on_it() -> None:
    plan = {
        "workflow_id": "wf-fo02",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO7",
                "capability": "code_generation",
                "task": "Write it",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO11",
                "capability": "test_automation",
                "task": "Test it",
                "depends_on": ["s1"],
            },
        ],
        "explanation": "Two steps.",
    }
    result, transport = run(plan, TWO_CODERS, ON, {"MyCodeGen": OK, "KIO11": OK})

    assert transport.called == ["KIO7", "MyCodeGen", "KIO11"]
    assert result.succeeded == 2
    assert result.results[0].agent_id == "MyCodeGen"
    assert result.results[1].status == STATUS_OK


def test_a_step_nothing_can_do_is_reported_as_skipped_in_the_result() -> None:
    plan = {
        "workflow_id": "wf-fo03",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO99",
                "capability": "quantum_magic",
                "task": "Impossible",
            }
        ],
        "explanation": "One step.",
    }
    result, transport = run(plan, TWO_CODERS, ON, {})

    assert not transport.called
    assert result.results[0].status == STATUS_SKIPPED


def test_failover_reaches_the_executor_through_dispatch_plan() -> None:
    transport = ScriptedTransport({"MyCodeGen": OK})
    result = dispatch_plan(
        one_step_plan(),
        DispatchConfig(enabled=True, registry=TWO_CODERS, failover=ON),
        transport,
    )

    assert result is not None
    assert result.results[0].agent_id == "MyCodeGen"


# ── configuration ────────────────────────────────────────────────────────────


def test_failover_is_off_unless_configured() -> None:
    assert load_failover_config(None).enabled is False


def test_failover_settings_are_read() -> None:
    failover = load_failover_config({"enabled": True, "max_attempts": 4})

    assert failover.enabled is True
    assert failover.max_attempts == 4


def test_zero_attempts_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        load_failover_config({"max_attempts": 0})


def test_a_malformed_failover_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_failover_config([1, 2])


def test_the_repository_config_turns_failover_on() -> None:
    failover = load_config("config.json").dispatch.failover

    assert failover.enabled is True
    assert failover.max_attempts == 2


def test_a_malformed_failover_block_is_rejected_when_the_config_loads(
    tmp_path: Any,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "ollama",
                "allowed_providers": ["ollama"],
                "model": "qwen2.5:3b",
                "prompt_path": "prompts/kio1_system_prompt_ver1.txt",
                "chat_directory": "chats",
                "temperature": 0.2,
                "request_timeout": 60,
                "keep_alive": -1,
                "max_output_tokens": 1024,
                "dispatch": {"failover": {"max_attempts": -1}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="max_attempts must be a positive integer"):
        load_config(str(config_path))
