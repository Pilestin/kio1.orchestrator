import logging
from dataclasses import dataclass, field
from typing import Any

from dispatch.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

_ALLOWED_EXECUTION_MODES = frozenset({"sequential", "parallel", "mixed"})

# Reasons a plan step cannot be turned into an execution message. These are
# expected outcomes, not failures: KIO1 plans across all of KIO2-KIO13 while
# only a few of them are deployed.
SKIP_NO_ENDPOINT = "no_endpoint"
SKIP_CAPABILITY_MISMATCH = "capability_mismatch"


@dataclass(frozen=True)
class PlanStep:
    """One step of the workflow plan produced by the model."""

    step_id: str
    agent_id: str
    capability: str
    task: str


@dataclass(frozen=True)
class WorkflowPlan:
    """A validated workflow plan.

    Attributes:
        workflow_id: Identifier the model assigned to the whole workflow.
        execution_mode: One of `sequential`, `parallel`, or `mixed`.
        steps: Plan steps in the order the model emitted them.
        explanation: The model's own summary of the plan.
    """

    workflow_id: str
    execution_mode: str
    steps: tuple[PlanStep, ...]
    explanation: str = ""


@dataclass(frozen=True)
class ExecutionMessage:
    """The message KIO1 sends to a single KIO agent.

    Field names follow the KIO1-KIO10 integration protocol exactly. `data` is
    the protocol's extension point for inputs a specific KIO needs beyond the
    free-text task.
    """

    workflow_id: str
    step_id: str
    agent_id: str
    capability: str
    endpoint: str
    task: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Return the message as the JSON object sent over the wire."""
        return {
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "capability": self.capability,
            "endpoint": self.endpoint,
            "task": self.task,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class SkippedStep:
    """A plan step that could not be routed to an agent."""

    step_id: str
    agent_id: str
    capability: str
    reason: str


@dataclass(frozen=True)
class RoutingResult:
    """The outcome of routing a whole plan against the agent registry."""

    messages: tuple[ExecutionMessage, ...]
    skipped: tuple[SkippedStep, ...]


def parse_plan(parsed_response: Any) -> WorkflowPlan:
    """Validate a decoded model response and return a workflow plan.

    Args:
        parsed_response: The already-decoded JSON response from the model.

    Returns:
        A validated `WorkflowPlan`.

    Raises:
        ValueError: If the response does not match the workflow plan schema.
    """
    if not isinstance(parsed_response, dict):
        raise ValueError("Workflow plan must be a JSON object.")

    workflow_id = parsed_response.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("Workflow plan requires a non-empty workflow_id.")

    execution_mode = parsed_response.get("execution_mode")
    if execution_mode not in _ALLOWED_EXECUTION_MODES:
        raise ValueError(
            "Workflow plan requires execution_mode to be one of "
            f"{sorted(_ALLOWED_EXECUTION_MODES)}."
        )

    steps_data = parsed_response.get("steps")
    if not isinstance(steps_data, list) or not steps_data:
        raise ValueError("Workflow plan requires a non-empty steps list.")

    steps: list[PlanStep] = []
    seen_step_ids: set[str] = set()

    for index, step_data in enumerate(steps_data):
        if not isinstance(step_data, dict):
            raise ValueError(f"Workflow step {index} must be a JSON object.")

        fields: dict[str, str] = {}
        for field_name in ("step_id", "agent_id", "capability", "task"):
            value = step_data.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Workflow step {index} requires a non-empty {field_name}."
                )
            # The shared protocol examples contain stray leading spaces inside
            # capability and task values, so every field is stripped here.
            fields[field_name] = value.strip()

        if fields["step_id"] in seen_step_ids:
            raise ValueError(f"Duplicate step_id: {fields['step_id']!r}")

        seen_step_ids.add(fields["step_id"])
        steps.append(
            PlanStep(
                step_id=fields["step_id"],
                agent_id=fields["agent_id"],
                capability=fields["capability"],
                task=fields["task"],
            )
        )

    explanation = parsed_response.get("explanation")

    return WorkflowPlan(
        workflow_id=workflow_id.strip(),
        execution_mode=execution_mode,
        steps=tuple(steps),
        explanation=explanation.strip() if isinstance(explanation, str) else "",
    )


def route_plan(plan: WorkflowPlan, registry: AgentRegistry) -> RoutingResult:
    """Turn plan steps into execution messages for the agents that exist.

    Steps addressed to agents without a configured endpoint are reported as
    skipped rather than raising, because a plan spanning KIO2-KIO13 will
    always reference agents that are not deployed yet.

    Args:
        plan: The validated workflow plan.
        registry: Registry of agents KIO1 can reach.

    Returns:
        Execution messages for routable steps and a record of the rest.
    """
    messages: list[ExecutionMessage] = []
    skipped: list[SkippedStep] = []

    for step in plan.steps:
        agent = registry.resolve(step.agent_id)

        if agent is None:
            logger.info(
                "Step skipped, no endpoint configured: step_id=%s agent_id=%s",
                step.step_id,
                step.agent_id,
            )
            skipped.append(
                SkippedStep(
                    step_id=step.step_id,
                    agent_id=step.agent_id,
                    capability=step.capability,
                    reason=SKIP_NO_ENDPOINT,
                )
            )
            continue

        if not agent.accepts(step.capability):
            logger.warning(
                "Step skipped, capability not declared: step_id=%s agent_id=%s "
                "capability=%s",
                step.step_id,
                step.agent_id,
                step.capability,
            )
            skipped.append(
                SkippedStep(
                    step_id=step.step_id,
                    agent_id=step.agent_id,
                    capability=step.capability,
                    reason=SKIP_CAPABILITY_MISMATCH,
                )
            )
            continue

        messages.append(
            ExecutionMessage(
                workflow_id=plan.workflow_id,
                step_id=step.step_id,
                agent_id=agent.agent_id,
                capability=step.capability,
                endpoint=agent.endpoint,
                task=step.task,
            )
        )

    logger.info(
        "Plan routed: workflow_id=%s dispatchable=%d skipped=%d",
        plan.workflow_id,
        len(messages),
        len(skipped),
    )

    return RoutingResult(messages=tuple(messages), skipped=tuple(skipped))
