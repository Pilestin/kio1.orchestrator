"""Choosing who carries out a step, and who else could.

A plan names an agent. That name is the model's guess at the moment it
planned, and by the time the step runs it can be wrong for reasons the model
could not have known: the agent went away, or it answered and failed.

What the plan actually asked for is the *capability*. Any target declaring it
can carry the step out, so a named agent that cannot is a reason to look for
another one rather than to give up. The order below is what makes that
substitution predictable instead of arbitrary.
"""

import logging
from dataclasses import dataclass
from typing import Any

from dispatch.agent_registry import AgentEndpoint, AgentRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailoverConfig:
    """When a step may be carried out by someone other than who was named.

    Attributes:
        enabled: Whether substitution happens at all. Off by default, so a
            plan runs exactly where it said it would until a deployment
            decides otherwise.
        max_attempts: How many targets one step may be tried against,
            including the first. Two means one substitution. This bounds the
            cost of a task that every agent refuses.
    """

    enabled: bool = False
    max_attempts: int = 2


def candidates_for(
    agent_id: str,
    capability: str,
    registry: AgentRegistry,
    failover: FailoverConfig,
) -> tuple[AgentEndpoint, ...]:
    """Return the targets that could carry out a step, best first.

    The named agent comes first when it exists and declares the capability:
    the model chose it, and nothing here second-guesses a working choice.
    Everything else declaring the same capability follows, in registry order,
    which puts hand-written entries ahead of discovered ones.

    Args:
        agent_id: The agent the plan named.
        capability: The capability the step asked for.
        registry: The targets reachable for this run.
        failover: Whether and how far to look past the named agent.

    Returns:
        Candidates in the order they should be tried, capped at
        `max_attempts`. Empty when nothing can carry the step out, which is
        the only case that still counts as unroutable.
    """
    named = registry.resolve(agent_id)
    ordered: list[AgentEndpoint] = []

    if named is not None and named.accepts(capability):
        ordered.append(named)

    if failover.enabled:
        ordered.extend(
            entry
            for entry in registry.entries()
            if entry.agent_id != agent_id and entry.accepts(capability)
        )

    if not ordered:
        return ()

    return tuple(ordered[: max(1, failover.max_attempts)])


def load_failover_config(failover_data: Any) -> FailoverConfig:
    """Build a `FailoverConfig` from the `dispatch.failover` block.

    Args:
        failover_data: Decoded `dispatch.failover` object, or an absent value.

    Returns:
        The configured settings.

    Raises:
        ValueError: If the block is malformed.
    """
    if failover_data is None:
        return FailoverConfig()

    if not isinstance(failover_data, dict):
        raise ValueError("dispatch.failover must be a JSON object.")

    enabled = failover_data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("dispatch.failover.enabled must be a boolean.")

    max_attempts = failover_data.get("max_attempts", 2)
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError(
            "dispatch.failover.max_attempts must be a positive integer, "
            "counting the first attempt."
        )

    if enabled:
        logger.info("Failover configured: max_attempts=%d", max_attempts)

    return FailoverConfig(enabled=enabled, max_attempts=max_attempts)
