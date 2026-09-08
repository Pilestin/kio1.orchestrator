"""Gates: rules about what must have passed before a step may run.

Declared dependencies say what a step *consumes*. That is not the same as
what must be *true* before it is allowed to happen. Deployment consumes
nothing from a security scan, so no dependency links them — and without one,
a plan happily deploys after the scan reported a defect.

A gate closes that. It names capabilities that may not run until other
capabilities have run and reported an acceptable result. Gates live in
configuration rather than in the planning prompt, because whether a security
scan is mandatory before deployment is a decision of whoever deployed KIO1,
not something a model should be trusted to remember.
"""

import logging
from dataclasses import dataclass, replace
from typing import Any

from dispatch.workflow_planner import WorkflowPlan, descendants_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gate:
    """One rule barring a set of capabilities until others have passed.

    Attributes:
        before: Capabilities this gate guards. A step asking for any of them
            is subject to the rule.
        requires: Capabilities that must have run and succeeded first. Every
            step in the plan providing one of these must have passed.
        verdict: Acceptable verdicts for the required steps. Empty accepts any
            verdict, including none at all, and checks only that the step
            succeeded. Non-empty means the required step must have reported a
            verdict and it must be in this set.
    """

    before: frozenset[str]
    requires: frozenset[str]
    verdict: frozenset[str] = frozenset()

    def guards(self, capability: str) -> bool:
        """Report whether this gate applies to a capability.

        Args:
            capability: The capability a step is asking for.

        Returns:
            True when the capability is guarded by this gate.
        """
        return capability in self.before


@dataclass(frozen=True)
class Policy:
    """The gates and review rules applied to every workflow.

    Attributes:
        gates: The configured gates, in configuration order.
        block_on_review_required: Whether a step asking for human review stops
            the rest of the workflow. Defaults to false, which keeps the
            behaviour a plan had before gates existed: the request is recorded
            and shown, and execution continues.
    """

    gates: tuple[Gate, ...] = ()
    block_on_review_required: bool = False

    def gates_for(self, capability: str) -> tuple[Gate, ...]:
        """Return the gates guarding a capability.

        Args:
            capability: The capability a step is asking for.

        Returns:
            Every gate that applies, in configuration order.
        """
        return tuple(gate for gate in self.gates if gate.guards(capability))

    @property
    def is_empty(self) -> bool:
        """Report whether this policy would change any workflow's behaviour."""
        return not self.gates and not self.block_on_review_required


def providers_of(plan: WorkflowPlan, capability: str) -> tuple[str, ...]:
    """Return the ids of the plan's steps that provide a capability.

    Args:
        plan: The workflow plan.
        capability: The capability being looked for.

    Returns:
        Matching step ids, in plan order.
    """
    return tuple(step.step_id for step in plan.steps if step.capability == capability)


def applies_to(plan: WorkflowPlan, policy: Policy) -> bool:
    """Report whether a policy has anything to say about a plan.

    Args:
        plan: The workflow plan.
        policy: The configured policy.

    Returns:
        True when at least one gate guards a capability the plan asks for, or
        review requests are set to stop the workflow.
    """
    if policy.block_on_review_required:
        return True

    return any(policy.gates_for(step.capability) for step in plan.steps)


def apply_gates(plan: WorkflowPlan, policy: Policy) -> WorkflowPlan:
    """Add the dependencies a policy's gates imply.

    A gate is only meaningful if the step it guards runs *after* the steps it
    requires. Rather than trusting the plan to have ordered them, the edges
    are added here: a guarded step gains a dependency on every step providing
    a required capability.

    An edge that would create a cycle is dropped with a warning. That means
    the two steps require each other, which no ordering can satisfy; the
    runtime check then blocks the guarded step rather than letting it through.

    Args:
        plan: The validated workflow plan.
        policy: The configured policy.

    Returns:
        The plan, with gate dependencies added. Returned unchanged when no
        gate applies, so a deployment without gates behaves exactly as before.
    """
    if not policy.gates:
        return plan

    steps = list(plan.steps)
    changed = False

    for index, step in enumerate(steps):
        gates = policy.gates_for(step.capability)

        if not gates:
            continue

        required_steps: list[str] = []

        for gate in gates:
            for capability in sorted(gate.requires):
                required_steps.extend(providers_of(plan, capability))

        added = [
            step_id
            for step_id in dict.fromkeys(required_steps)
            if step_id != step.step_id and step_id not in step.depends_on
        ]

        # Reachability is read from the original plan: the model's own
        # dependencies are what could make an added edge circular, and gate
        # edges all point the same way (guarded step after required step).
        safe = []
        for step_id in added:
            if step_id in descendants_of(plan, step.step_id):
                logger.warning(
                    "Gate dependency %s -> %s would form a cycle; leaving it "
                    "to the runtime check",
                    step.step_id,
                    step_id,
                )
                continue
            safe.append(step_id)

        if not safe:
            continue

        steps[index] = replace(step, depends_on=(*step.depends_on, *safe))
        changed = True
        logger.info(
            "Gate added dependencies: step_id=%s capability=%s depends_on=%s",
            step.step_id,
            step.capability,
            ", ".join(safe),
        )

    if not changed:
        return plan

    return replace(plan, steps=tuple(steps))


def load_policy(policy_data: Any) -> Policy:
    """Build a `Policy` from the `dispatch.policy` configuration block.

    Args:
        policy_data: Decoded `dispatch.policy` object, or an absent value.

    Returns:
        The configured policy, empty when nothing is configured.

    Raises:
        ValueError: If the block is malformed.
    """
    if policy_data is None:
        return Policy()

    if not isinstance(policy_data, dict):
        raise ValueError("dispatch.policy must be a JSON object.")

    block_on_review = policy_data.get("block_on_review_required", False)
    if type(block_on_review) is not bool:
        raise ValueError("dispatch.policy.block_on_review_required must be a boolean.")

    gates_data = policy_data.get("gates", [])
    if not isinstance(gates_data, list):
        raise ValueError("dispatch.policy.gates must be a list of gate objects.")

    gates: list[Gate] = []

    for index, gate_data in enumerate(gates_data):
        if not isinstance(gate_data, dict):
            raise ValueError(f"dispatch.policy.gates[{index}] must be a JSON object.")

        before = _read_capability_list(gate_data, "before", index, required=True)
        requires = _read_capability_list(gate_data, "requires", index, required=True)
        verdict = _read_capability_list(gate_data, "verdict", index, required=False)

        overlap = before & requires
        if overlap:
            # A capability that gates itself can never run: every step
            # providing it would have to have passed before it starts.
            raise ValueError(
                f"dispatch.policy.gates[{index}] lists {sorted(overlap)} in both "
                "'before' and 'requires', which nothing can satisfy."
            )

        gates.append(Gate(before=before, requires=requires, verdict=verdict))

    policy = Policy(gates=tuple(gates), block_on_review_required=block_on_review)

    if not policy.is_empty:
        logger.info(
            "Policy loaded: gates=%d block_on_review_required=%s",
            len(policy.gates),
            policy.block_on_review_required,
        )

    return policy


def _read_capability_list(
    gate_data: dict[str, Any], name: str, index: int, *, required: bool
) -> frozenset[str]:
    """Read one of a gate's capability lists.

    Args:
        gate_data: The raw gate object.
        name: Field name to read.
        index: Position of the gate, for error messages.
        required: Whether an empty or absent list is an error.

    Returns:
        The declared values.

    Raises:
        ValueError: If the value is not a list of non-empty strings, or it is
            empty and the field is required.
    """
    value = gate_data.get(name, [])

    if isinstance(value, str):
        # One capability written without a list is the obvious mistake, and
        # accepting a bare string silently would iterate its characters.
        raise ValueError(
            f"dispatch.policy.gates[{index}].{name} must be a list, not a "
            f'string. Write ["{value}"].'
        )

    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(
            f"dispatch.policy.gates[{index}].{name} must be a list of "
            "non-empty capability names."
        )

    if required and not value:
        raise ValueError(
            f"dispatch.policy.gates[{index}].{name} must name at least one "
            "capability."
        )

    return frozenset(item.strip() for item in value)
