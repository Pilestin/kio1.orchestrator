import logging
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentEndpoint:
    """A single KIO agent that KIO1 is able to dispatch work to.

    Attributes:
        agent_id: The agent identifier used in workflow plans, such as `KIO10`.
        endpoint: Base URL of the agent service, without a trailing slash.
        path: Request path appended to `endpoint`, starting with a slash.
        capabilities: Capability values the agent accepts. An empty set means
            the registry performs no capability check for this agent.
    """

    agent_id: str
    endpoint: str
    path: str
    capabilities: frozenset[str]

    @property
    def url(self) -> str:
        """Return the full URL execution messages are posted to."""
        return f"{self.endpoint}{self.path}"

    def accepts(self, capability: str) -> bool:
        """Report whether this agent declares support for a capability.

        Args:
            capability: Capability value taken from a workflow step.

        Returns:
            True when the agent declares the capability, or when the agent
            declares no capabilities at all.
        """
        if not self.capabilities:
            return True

        return capability in self.capabilities


class AgentRegistry:
    """Maps agent identifiers from a workflow plan to reachable endpoints.

    The KIO1-KIO10 integration protocol puts an `endpoint` field inside every
    execution message but does not say who fills it in. KIO1 owns that
    mapping, so it is configured here rather than produced by the model.
    """

    def __init__(self, agents: dict[str, AgentEndpoint]) -> None:
        """Store the configured agents.

        Args:
            agents: Agent identifier to endpoint mapping, keyed by the exact
                identifier used in workflow plans.
        """
        self._agents = dict(agents)

    def __len__(self) -> int:
        """Return the number of registered agents."""
        return len(self._agents)

    def agent_ids(self) -> frozenset[str]:
        """Return the identifiers of every registered agent."""
        return frozenset(self._agents)

    def resolve(self, agent_id: str) -> AgentEndpoint | None:
        """Look up an agent by identifier.

        Args:
            agent_id: Agent identifier from a workflow step.

        Returns:
            The matching endpoint, or None when the agent has no configured
            endpoint. A missing agent is an expected condition: workflow plans
            routinely reference KIOs that are not deployed yet.
        """
        return self._agents.get(agent_id)


def load_agent_registry(agents_data: dict[str, object]) -> AgentRegistry:
    """Build an `AgentRegistry` from the `dispatch.agents` configuration block.

    Args:
        agents_data: Decoded `dispatch.agents` object, keyed by agent id.

    Returns:
        A registry containing every configured agent.

    Raises:
        ValueError: If an agent entry is malformed.
    """
    agents: dict[str, AgentEndpoint] = {}

    for agent_id, agent_data in agents_data.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("dispatch.agents keys must be non-empty strings.")

        if not isinstance(agent_data, dict):
            raise ValueError(f"dispatch.agents.{agent_id} must be a JSON object.")

        endpoint = agent_data.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError(
                f"dispatch.agents.{agent_id}.endpoint must be a non-empty "
                "HTTP(S) URL."
            )

        endpoint = endpoint.strip().rstrip("/")
        parsed_endpoint = urlparse(endpoint)
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.netloc
        ):
            raise ValueError(
                f"dispatch.agents.{agent_id}.endpoint must be a non-empty "
                "HTTP(S) URL."
            )

        path = agent_data.get("path", "/execute")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(
                f"dispatch.agents.{agent_id}.path must be a string starting "
                "with '/'."
            )

        capabilities_data = agent_data.get("capabilities", [])
        if not isinstance(capabilities_data, list) or not all(
            isinstance(capability, str) and capability.strip()
            for capability in capabilities_data
        ):
            raise ValueError(
                f"dispatch.agents.{agent_id}.capabilities must be a list of "
                "non-empty strings."
            )

        agents[agent_id.strip()] = AgentEndpoint(
            agent_id=agent_id.strip(),
            endpoint=endpoint,
            path=path,
            capabilities=frozenset(
                capability.strip() for capability in capabilities_data
            ),
        )

    logger.info("Agent registry loaded: agents=%s", sorted(agents))

    return AgentRegistry(agents)
