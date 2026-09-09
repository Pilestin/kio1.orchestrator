import logging
from dataclasses import dataclass, field, replace
from typing import Any

from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.failover import FailoverConfig, candidates_for

logger = logging.getLogger(__name__)

_ALLOWED_EXECUTION_MODES = frozenset({"sequential", "parallel", "mixed"})

# Reasons a plan step cannot be turned into an execution message. These are
# expected outcomes, not failures: KIO1 plans across all of KIO2-KIO13 while
# only a few of them are deployed.
SKIP_NO_ENDPOINT = "no_endpoint"
SKIP_CAPABILITY_MISMATCH = "capability_mismatch"


@dataclass(frozen=True)
class PlanStep:
    """One step of the workflow plan produced by the model.

    Attributes:
        step_id: Identifier of the step within the workflow.
        agent_id: Agent the step is addressed to.
        capability: Capability requested from the agent.
        task: Free-text description of the work.
        depends_on: Step ids whose output this step consumes. Optional and
            additive: a plan that omits it behaves exactly as it did before
            the field existed, and nothing is fed into its `data`.
    """

    step_id: str
    agent_id: str
    capability: str
    task: str
    depends_on: tuple[str, ...] = ()


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
    """The outcome of routing a whole plan against the agent registry.

    Attributes:
        messages: One message per routable step, addressed to the first
            candidate.
        skipped: Steps nothing could carry out.
        alternatives: Further candidates per step id, in the order they should
            be tried if the first one cannot do it. Empty for every step when
            failover is off, which is how routing behaved before it existed.
    """

    messages: tuple[ExecutionMessage, ...]
    skipped: tuple[SkippedStep, ...]
    alternatives: dict[str, tuple[AgentEndpoint, ...]] = field(default_factory=dict)


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
                depends_on=_read_depends_on(step_data, fields["step_id"]),
            )
        )

    explanation = parsed_response.get("explanation")
    steps = _sanitize_dependencies(steps)

    return WorkflowPlan(
        workflow_id=workflow_id.strip(),
        execution_mode=execution_mode,
        steps=tuple(steps),
        explanation=explanation.strip() if isinstance(explanation, str) else "",
    )


def _read_depends_on(step_data: dict[str, Any], step_id: str) -> tuple[str, ...]:
    """Read a step's optional dependency list.

    A malformed value is dropped rather than raised on. `depends_on` is an
    optional addition to the plan schema, so a model that emits it badly must
    not turn a plan that would otherwise run into a hard failure.

    Args:
        step_data: The raw step object.
        step_id: The step's own id, used for warnings and the self-reference check.

    Returns:
        The declared dependencies, deduplicated and order-preserving.
    """
    raw = step_data.get("depends_on")

    if raw is None:
        return ()

    if not isinstance(raw, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in raw
    ):
        logger.warning(
            "Ignoring malformed depends_on on step %s: expected a list of step ids",
            step_id,
        )
        return ()

    dependencies: list[str] = []

    for entry in raw:
        dependency = entry.strip()

        if dependency == step_id:
            logger.warning("Ignoring self-dependency on step %s", step_id)
            continue

        if dependency not in dependencies:
            dependencies.append(dependency)

    return tuple(dependencies)


def _sanitize_dependencies(steps: list[PlanStep]) -> list[PlanStep]:
    """Drop dependencies that cannot be honoured.

    References to steps the plan does not contain are dropped. A cycle makes
    the whole graph unusable, so every dependency is dropped and the plan
    falls back to its declared execution mode.

    Args:
        steps: Steps in plan order.

    Returns:
        The steps with unusable dependencies removed.
    """
    known = {step.step_id for step in steps}
    resolved: list[PlanStep] = []

    for step in steps:
        kept = tuple(
            dependency for dependency in step.depends_on if dependency in known
        )
        dropped = set(step.depends_on) - set(kept)

        if dropped:
            logger.warning(
                "Dropping dependencies on unknown steps from %s: %s",
                step.step_id,
                ", ".join(sorted(dropped)),
            )

        resolved.append(
            step if kept == step.depends_on else replace(step, depends_on=kept)
        )

    if _has_cycle(resolved):
        logger.warning(
            "Plan dependencies form a cycle; ignoring all of them and falling "
            "back to the declared execution mode"
        )
        return [replace(step, depends_on=()) for step in resolved]

    return resolved


def _has_cycle(steps: list[PlanStep]) -> bool:
    """Report whether the dependency graph contains a cycle.

    Args:
        steps: Steps whose dependencies reference only known step ids.

    Returns:
        True when no valid execution order exists.
    """
    remaining = {step.step_id: set(step.depends_on) for step in steps}

    while remaining:
        ready = [
            step_id for step_id, dependencies in remaining.items() if not dependencies
        ]

        if not ready:
            return True

        for step_id in ready:
            del remaining[step_id]

        for dependencies in remaining.values():
            dependencies.difference_update(ready)

    return False


def has_dependencies(plan: WorkflowPlan) -> bool:
    """Report whether any step declares a dependency."""
    return any(step.depends_on for step in plan.steps)


def execution_levels(plan: WorkflowPlan) -> tuple[tuple[str, ...], ...]:
    """Group step ids into levels that can run one after another.

    Every step in a level depends only on steps in earlier levels, so a level
    may run concurrently. A plan with no declared dependencies yields a single
    level containing every step, in plan order.

    Args:
        plan: A validated plan whose dependencies are already sanitized.

    Returns:
        Levels of step ids, in execution order.
    """
    pending = {step.step_id: set(step.depends_on) for step in plan.steps}
    order = [step.step_id for step in plan.steps]
    levels: list[tuple[str, ...]] = []

    while pending:
        ready = tuple(
            step_id for step_id in order if step_id in pending and not pending[step_id]
        )

        if not ready:  # pragma: no cover - cycles are removed by parse_plan
            break

        levels.append(ready)

        for step_id in ready:
            del pending[step_id]

        for dependencies in pending.values():
            dependencies.difference_update(ready)

    return tuple(levels)


def descendants_of(plan: WorkflowPlan, step_id: str) -> frozenset[str]:
    """Return every step that depends on a step, directly or transitively.

    Args:
        plan: A validated plan.
        step_id: The step whose dependents are wanted.

    Returns:
        The step ids that cannot run once this step has failed.
    """
    dependents: dict[str, set[str]] = {step.step_id: set() for step in plan.steps}

    for step in plan.steps:
        for dependency in step.depends_on:
            dependents[dependency].add(step.step_id)

    found: set[str] = set()
    queue = list(dependents.get(step_id, set()))

    while queue:
        current = queue.pop()

        if current in found:
            continue

        found.add(current)
        queue.extend(dependents.get(current, set()))

    return frozenset(found)


def route_plan(
    plan: WorkflowPlan,
    registry: AgentRegistry,
    *,
    workspace_data: dict[str, Any] | None = None,
    failover: FailoverConfig | None = None,
) -> RoutingResult:
    """Turn plan steps into execution messages for the targets that exist.

    Steps nothing can carry out are reported as skipped rather than raising,
    because a plan spanning KIO2-KIO13 will always reference agents that are
    not deployed yet.

    With failover on, a step whose named agent is missing or does not declare
    the capability is not skipped while another target does declare it: the
    plan asked for the capability, and the name was only the model's guess at
    who provides it.

    Args:
        plan: The validated workflow plan.
        registry: Registry of agents KIO1 can reach.
        workspace_data: The `data` fragment naming the code under analysis,
            attached to every message. Omitted means an empty `data`, which
            is how plans behaved before a workspace could be configured.
        failover: Whether a step may be carried out by a target other than the
            one the plan named. Omitted keeps every step where it was sent.

    Returns:
        Execution messages for routable steps, the further candidates for
        each, and a record of the steps nothing can carry out.
    """
    failover = failover if failover is not None else FailoverConfig()
    messages: list[ExecutionMessage] = []
    skipped: list[SkippedStep] = []
    alternatives: dict[str, tuple[AgentEndpoint, ...]] = {}

    for step in plan.steps:
        candidates = candidates_for(step.agent_id, step.capability, registry, failover)

        if not candidates:
            named = registry.resolve(step.agent_id)
            reason = SKIP_CAPABILITY_MISMATCH if named is not None else SKIP_NO_ENDPOINT
            logger.log(
                logging.WARNING if named is not None else logging.INFO,
                "Step skipped, nothing provides %s: step_id=%s agent_id=%s",
                step.capability,
                step.step_id,
                step.agent_id,
            )
            skipped.append(
                SkippedStep(
                    step_id=step.step_id,
                    agent_id=step.agent_id,
                    capability=step.capability,
                    reason=reason,
                )
            )
            continue

        agent = candidates[0]

        if agent.agent_id != step.agent_id:
            logger.info(
                "Step reassigned before dispatch: step_id=%s planned=%s "
                "running_on=%s capability=%s",
                step.step_id,
                step.agent_id,
                agent.agent_id,
                step.capability,
            )

        if len(candidates) > 1:
            alternatives[step.step_id] = candidates[1:]

        messages.append(
            ExecutionMessage(
                workflow_id=plan.workflow_id,
                step_id=step.step_id,
                agent_id=agent.agent_id,
                capability=step.capability,
                endpoint=agent.endpoint,
                task=step.task,
                data=dict(workspace_data) if workspace_data else {},
            )
        )

    logger.info(
        "Plan routed: workflow_id=%s dispatchable=%d skipped=%d",
        plan.workflow_id,
        len(messages),
        len(skipped),
    )

    return RoutingResult(
        messages=tuple(messages),
        skipped=tuple(skipped),
        alternatives=alternatives,
    )
