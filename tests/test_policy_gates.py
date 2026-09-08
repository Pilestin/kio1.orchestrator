"""Tests for policy gates: what must have passed before a step may run.

The case these exist for is concrete. A plan runs tests, runs a security
scan, and deploys. Both checks report a defect. Without a gate the deployment
step still runs, because it consumes nothing from either check and no
dependency links them.
"""

import json
from typing import Any

import pytest

from core.config_loader import DispatchConfig, load_config
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.policy import Gate, Policy, apply_gates, load_policy
from dispatch.workflow_executor import (
    STATUS_NOT_RUN,
    STATUS_OK,
    dispatch_plan,
    execute_workflow,
)
from dispatch.workflow_planner import ExecutionMessage, parse_plan

REGISTRY = AgentRegistry(
    {
        agent_id: AgentEndpoint(
            agent_id=agent_id,
            endpoint=f"http://127.0.0.1:81{agent_id[3:]}",
            path="/execute",
            capabilities=frozenset(capabilities),
        )
        for agent_id, capabilities in {
            "KIO7": ["code_generation"],
            "KIO8": ["deployment"],
            "KIO11": ["test_automation"],
            "KIO12": ["cybersecurity_validation"],
        }.items()
    }
)


class ScriptedTransport:
    """Answers each agent with a reply the test chose in advance."""

    def __init__(self, replies: dict[str, dict[str, Any]]) -> None:
        self.replies = replies
        self.called: list[str] = []

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        self.called.append(message.step_id)
        reply = self.replies.get(message.agent_id, {"status": "ok", "output": {}})

        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            **reply,
        }

    def close(self) -> None:
        """Nothing to release."""


def lifecycle_plan(*, declare_dependencies: bool = False) -> dict[str, Any]:
    """Return a plan that generates code, checks it two ways, and deploys.

    The deployment step never declares a dependency on the two checks,
    because it consumes nothing from them. That is exactly the gap a gate
    closes.
    """
    depends = ["s1"] if declare_dependencies else None
    steps: list[dict[str, Any]] = [
        {
            "step_id": "s1",
            "agent_id": "KIO7",
            "capability": "code_generation",
            "task": "Generate the service",
        },
        {
            "step_id": "s2",
            "agent_id": "KIO11",
            "capability": "test_automation",
            "task": "Run the tests",
        },
        {
            "step_id": "s3",
            "agent_id": "KIO12",
            "capability": "cybersecurity_validation",
            "task": "Scan for vulnerabilities",
        },
        {
            "step_id": "s4",
            "agent_id": "KIO8",
            "capability": "deployment",
            "task": "Deploy to staging",
        },
    ]

    if depends is not None:
        steps[1]["depends_on"] = depends
        steps[2]["depends_on"] = depends

    return {
        "workflow_id": "wf-gate01",
        "execution_mode": "sequential",
        "steps": steps,
        "explanation": "Generate, check, deploy.",
    }


def run(
    plan_data: dict[str, Any],
    policy: Policy,
    replies: dict[str, dict[str, Any]],
) -> tuple[Any, ScriptedTransport]:
    """Execute a plan under a policy and return the result and transport."""
    transport = ScriptedTransport(replies)
    result = execute_workflow(
        parse_plan(plan_data),
        REGISTRY,
        transport,
        timeout=5,
        max_parallel_steps=4,
        policy=policy,
    )

    return result, transport


CLEAN = {"status": "ok", "output": {"verdict": "clean"}}
DEFECT = {"status": "ok", "output": {"verdict": "defect"}}
NO_VERDICT = {"status": "ok", "output": {"summary": "done"}}

DEPLOY_GATE = Policy(
    gates=(
        Gate(
            before=frozenset({"deployment"}),
            requires=frozenset({"test_automation", "cybersecurity_validation"}),
            verdict=frozenset({"clean"}),
        ),
    )
)


# ── the behaviour the gate exists to change ─────────────────────────────────


def test_without_a_gate_a_failing_check_does_not_stop_the_deployment() -> None:
    # This is what the orchestrator did before gates existed, kept as a test
    # so the difference the gate makes is visible rather than asserted.
    result, transport = run(
        lifecycle_plan(),
        Policy(),
        {"KIO11": DEFECT, "KIO12": DEFECT, "KIO7": CLEAN, "KIO8": CLEAN},
    )

    assert "s4" in transport.called
    assert result.results[3].status == STATUS_OK


def test_a_gate_stops_the_deployment_when_a_check_reports_a_defect() -> None:
    result, transport = run(
        lifecycle_plan(),
        DEPLOY_GATE,
        {"KIO11": DEFECT, "KIO12": CLEAN, "KIO7": CLEAN, "KIO8": CLEAN},
    )
    deployment = result.results[3]

    assert "s4" not in transport.called
    assert deployment.status == STATUS_NOT_RUN
    assert deployment.error is not None
    assert "test_automation" in deployment.error
    assert "'defect'" in deployment.error


def test_a_gate_lets_the_deployment_through_when_every_check_is_clean() -> None:
    result, transport = run(
        lifecycle_plan(),
        DEPLOY_GATE,
        {"KIO11": CLEAN, "KIO12": CLEAN, "KIO7": CLEAN, "KIO8": CLEAN},
    )

    assert transport.called == ["s1", "s2", "s3", "s4"]
    assert result.succeeded == 4


# ── how a gate can be missed ─────────────────────────────────────────────────


def test_a_plan_missing_a_required_check_entirely_is_blocked() -> None:
    # The dangerous case: not a failing scan, but no scan at all.
    plan = lifecycle_plan()
    plan["steps"] = [step for step in plan["steps"] if step["step_id"] != "s3"]

    result, transport = run(plan, DEPLOY_GATE, {"KIO11": CLEAN, "KIO7": CLEAN})
    deployment = result.results[-1]

    assert "s4" not in transport.called
    assert deployment.error is not None
    assert "no step in this plan provides it" in deployment.error


def test_a_check_that_failed_outright_also_blocks() -> None:
    # The gate's own dependency edge stops this one before the gate is even
    # evaluated, which is the same outcome by a shorter route.
    result, transport = run(
        lifecycle_plan(),
        DEPLOY_GATE,
        {
            "KIO11": {"status": "error", "error": "runner crashed"},
            "KIO12": CLEAN,
            "KIO7": CLEAN,
        },
    )
    deployment = result.results[3]

    assert "s4" not in transport.called
    assert deployment.status == STATUS_NOT_RUN


def test_a_check_reporting_a_failure_it_survived_blocks_with_the_gate_s_reason() -> (
    None
):
    # An agent that answers `status: error` for one capability while the plan
    # still reaches the deployment step: here the gate itself is what stops it.
    gate = Policy(
        gates=(
            Gate(
                before=frozenset({"deployment"}),
                requires=frozenset({"cybersecurity_validation"}),
                verdict=frozenset({"clean"}),
            ),
        )
    )
    result, transport = run(
        lifecycle_plan(),
        gate,
        {"KIO12": DEFECT, "KIO11": CLEAN, "KIO7": CLEAN},
    )
    deployment = result.results[3]

    assert "s4" not in transport.called
    assert deployment.error is not None
    assert "cybersecurity_validation" in deployment.error
    assert "'defect'" in deployment.error


def test_a_check_that_reports_no_verdict_blocks_when_one_is_required() -> None:
    # Asking for `clean` and being told nothing is not the same as being told
    # it is clean.
    result, _ = run(
        lifecycle_plan(),
        DEPLOY_GATE,
        {"KIO11": NO_VERDICT, "KIO12": CLEAN, "KIO7": CLEAN},
    )
    deployment = result.results[3]

    assert deployment.status == STATUS_NOT_RUN
    assert deployment.error is not None
    assert "reported no verdict" in deployment.error


def test_a_gate_without_a_verdict_requirement_only_checks_success() -> None:
    gate = Policy(
        gates=(
            Gate(
                before=frozenset({"deployment"}),
                requires=frozenset({"test_automation"}),
            ),
        )
    )
    plan = lifecycle_plan()
    plan["steps"] = [step for step in plan["steps"] if step["step_id"] != "s3"]

    _, transport = run(plan, gate, {"KIO11": NO_VERDICT, "KIO7": CLEAN})

    assert "s4" in transport.called


# ── ordering ─────────────────────────────────────────────────────────────────


def test_a_gate_adds_the_ordering_it_needs() -> None:
    # The plan never says the deployment comes after the checks. The gate
    # would be unenforceable if it ran alongside them.
    plan = parse_plan(lifecycle_plan())
    gated = apply_gates(plan, DEPLOY_GATE)
    deployment = next(step for step in gated.steps if step.step_id == "s4")

    assert set(deployment.depends_on) == {"s2", "s3"}
    assert plan.steps[3].depends_on == ()


def test_gate_ordering_is_added_on_top_of_declared_dependencies() -> None:
    plan = parse_plan(lifecycle_plan(declare_dependencies=True))
    gated = apply_gates(plan, DEPLOY_GATE)
    checks = next(step for step in gated.steps if step.step_id == "s2")
    deployment = next(step for step in gated.steps if step.step_id == "s4")

    assert checks.depends_on == ("s1",)
    assert set(deployment.depends_on) == {"s2", "s3"}


def test_a_plan_with_no_gated_capability_is_returned_unchanged() -> None:
    plan = parse_plan(
        {
            "workflow_id": "wf-x",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "KIO7",
                    "capability": "code_generation",
                    "task": "Generate",
                }
            ],
            "explanation": "One step.",
        }
    )

    assert apply_gates(plan, DEPLOY_GATE) is plan


def test_steps_after_a_blocked_step_are_blocked_too() -> None:
    plan = lifecycle_plan()
    plan["steps"].append(
        {
            "step_id": "s5",
            "agent_id": "KIO7",
            "capability": "code_generation",
            "task": "Post-deployment smoke fix",
            "depends_on": ["s4"],
        }
    )

    result, transport = run(
        plan, DEPLOY_GATE, {"KIO11": DEFECT, "KIO12": CLEAN, "KIO7": CLEAN}
    )

    assert "s5" not in transport.called
    assert result.results[4].status == STATUS_NOT_RUN


# ── review requests ──────────────────────────────────────────────────────────


def test_a_review_request_can_stop_the_rest_of_the_workflow() -> None:
    result, transport = run(
        lifecycle_plan(),
        Policy(block_on_review_required=True),
        {
            "KIO7": {"status": "ok", "output": {"hitl_required": True}},
            "KIO11": CLEAN,
            "KIO12": CLEAN,
            "KIO8": CLEAN,
        },
    )

    assert transport.called == ["s1"]
    assert result.results[1].error is not None
    assert "asked for human review" in result.results[1].error


def test_a_review_request_is_only_recorded_by_default() -> None:
    # Changing this default would alter every existing deployment's behaviour.
    _, transport = run(
        lifecycle_plan(),
        Policy(),
        {"KIO7": {"status": "ok", "output": {"hitl_required": True}}},
    )

    assert len(transport.called) == 4


# ── configuration ────────────────────────────────────────────────────────────


def test_an_absent_policy_block_changes_nothing() -> None:
    assert load_policy(None).is_empty


def test_a_gate_is_read_from_configuration() -> None:
    policy = load_policy(
        {
            "gates": [
                {
                    "before": ["deployment"],
                    "requires": ["test_automation"],
                    "verdict": ["clean"],
                }
            ],
            "block_on_review_required": True,
        }
    )

    assert policy.block_on_review_required is True
    assert policy.gates_for("deployment")[0].requires == frozenset({"test_automation"})
    assert not policy.gates_for("code_generation")


def test_a_capability_written_as_a_bare_string_is_rejected() -> None:
    # Accepting it would iterate the characters and gate on "d", "e", "p"...
    with pytest.raises(ValueError, match=r"must be a list, not a string"):
        load_policy({"gates": [{"before": "deployment", "requires": ["x"]}]})


def test_a_gate_requiring_its_own_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="which nothing can satisfy"):
        load_policy({"gates": [{"before": ["deployment"], "requires": ["deployment"]}]})


def test_a_gate_must_name_what_it_guards_and_what_it_requires() -> None:
    with pytest.raises(ValueError, match="must name at least one capability"):
        load_policy({"gates": [{"before": [], "requires": ["x"]}]})


def test_the_repository_config_gates_deployment_on_the_checks() -> None:
    policy = load_config("config.json").dispatch.policy
    gates = policy.gates_for("deployment")

    assert gates
    assert gates[0].requires == frozenset(
        {"test_automation", "cybersecurity_validation"}
    )
    assert gates[0].verdict == frozenset({"clean"})


def test_the_policy_reaches_the_executor_through_dispatch_plan(
    tmp_path: Any,
) -> None:
    transport = ScriptedTransport({"KIO11": DEFECT, "KIO12": CLEAN, "KIO7": CLEAN})
    result = dispatch_plan(
        lifecycle_plan(),
        DispatchConfig(enabled=True, registry=REGISTRY, policy=DEPLOY_GATE),
        transport,
    )

    assert result is not None
    assert "s4" not in transport.called


def test_a_malformed_policy_is_rejected_when_the_config_loads(
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
                "dispatch": {"policy": {"gates": "everything"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="gates must be a list"):
        load_config(str(config_path))
