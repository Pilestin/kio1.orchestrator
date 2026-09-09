import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# What a registry entry is, and therefore how a step addressed to it runs.
#
# `kio` is the consortium's KIO1-KIO10 protocol: a JSON execution message
# posted to an agent that knows the contract. The other two exist because
# most of what an orchestrator usefully calls is not a KIO: an ordinary REST
# API, or a command line tool sitting on the same machine. They are handled
# by their own transports, so the planner and the executor never learn the
# difference.
KIND_KIO = "kio"
KIND_HTTP = "http"
KIND_CLI = "cli"

ALLOWED_KINDS = frozenset({KIND_KIO, KIND_HTTP, KIND_CLI})

_ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class HttpToolSpec:
    """How to call an ordinary REST API that knows nothing about KIO1.

    Attributes:
        url: Full request URL. May contain placeholders.
        method: HTTP method to use.
        headers: Header name to value. Values may contain placeholders and
            `${ENV_VAR}` references, so credentials stay out of `config.json`.
        body: JSON body to send, with placeholders substituted before the
            request. `None` sends the KIO1-KIO10 execution message unchanged,
            which is the useful default for an API that already speaks it.
        result_path: Keys to follow into the decoded response to find the part
            worth keeping. Empty keeps the whole response.
        health_url: URL that answers a liveness check. Empty means this API
            cannot be probed, and KIO1 will not guess a path: an arbitrary
            REST API has no convention it may assume.
    """

    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    result_path: tuple[str, ...] = ()
    health_url: str = ""


@dataclass(frozen=True)
class CliToolSpec:
    """How to run a local command line tool.

    The command is always an argument list run without a shell, so a task
    description carrying quotes, semicolons, or backticks becomes one
    argument rather than something the shell would interpret.

    Attributes:
        command: Program and arguments. Each entry may contain placeholders.
        cwd: Working directory. Empty runs in KIO1's own directory.
        env: Extra environment variables for the child process. Values may
            contain placeholders and `${ENV_VAR}` references.
    """

    command: tuple[str, ...]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEndpoint:
    """A single target KIO1 is able to dispatch work to.

    Attributes:
        agent_id: The identifier used in workflow plans, such as `KIO10` or
            `Pytest`.
        endpoint: Base URL of the agent service, without a trailing slash.
            For non-KIO entries this is a descriptive address such as
            `cli:Pytest`; it travels in the protocol message and is not
            dialled.
        path: Request path appended to `endpoint`, starting with a slash.
            Only meaningful for `kio` entries.
        capabilities: Capability values the target accepts. An empty set means
            the registry performs no capability check for this entry.
        subject: NATS subject this agent listens on. Unset lets the NATS
            transport derive one from its subject template. Ignored by every
            other transport.
        kind: One of `kio`, `http`, or `cli`. Decides which transport runs the
            step. Defaults to `kio`, so an entry written before this field
            existed keeps its behaviour.
        description: What this target does, in the words the planning model
            should see. Empty falls back to a line built from the
            capabilities.
        tool: The call details for `http` and `cli` entries. Always `None` for
            a `kio` entry, whose call details are `endpoint` and `path`.
    """

    agent_id: str
    endpoint: str
    path: str
    capabilities: frozenset[str]
    subject: str | None = None
    kind: str = KIND_KIO
    description: str = ""
    tool: HttpToolSpec | CliToolSpec | None = None

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

    def entries(self) -> tuple[AgentEndpoint, ...]:
        """Return every registered entry, in configuration order.

        Configuration order rather than sorted order, because this is what
        the planning model's catalog is built from and the person editing the
        configuration is the one who decided what belongs near what.
        """
        return tuple(self._agents.values())

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


def _looks_like_a_filesystem_path(value: str) -> bool:
    """Report whether a value reads as a directory rather than a URL path.

    Args:
        value: The configured value.

    Returns:
        True for Windows drive letters and backslash separators, the two
        shapes a copied directory arrives in.
    """
    return "\\" in value or (len(value) > 1 and value[1] == ":")


def merge_registries(
    configured: AgentRegistry, discovered: AgentRegistry
) -> AgentRegistry:
    """Combine hand-written and discovered agents, letting configuration win.

    A hand-written entry is explicit intent, and it is also where a deployment
    records an address only it can know — the host an agent answers on from
    inside a container, say. A manifest cannot know that, so it never
    overrides one.

    Args:
        configured: Agents read from `dispatch.agents`.
        discovered: Agents read from manifest sources.

    Returns:
        A registry holding both, in configured-then-discovered order.
    """
    merged = {entry.agent_id: entry for entry in configured.entries()}

    for entry in discovered.entries():
        if entry.agent_id in merged:
            logger.info(
                "Discovered agent %s is already configured by hand; keeping "
                "the configured entry",
                entry.agent_id,
            )
            continue

        merged[entry.agent_id] = entry

    return AgentRegistry(merged)


def load_agent_entry(agent_id: str, agent_data: dict[str, Any]) -> AgentEndpoint:
    """Build one registry entry, raising on anything malformed.

    Args:
        agent_id: The identifier the entry is keyed by.
        agent_data: The raw entry object.

    Returns:
        The validated entry.

    Raises:
        ValueError: If the entry is malformed.
    """
    return load_agent_registry({agent_id: agent_data}, announce=False).entries()[0]


def _read_capabilities(agent_id: str, agent_data: dict[str, Any]) -> frozenset[str]:
    """Read and validate an entry's declared capabilities.

    Args:
        agent_id: Identifier of the entry, for error messages.
        agent_data: The raw entry object.

    Returns:
        The declared capabilities.

    Raises:
        ValueError: If the value is not a list of non-empty strings.
    """
    capabilities_data = agent_data.get("capabilities", [])

    if not isinstance(capabilities_data, list) or not all(
        isinstance(capability, str) and capability.strip()
        for capability in capabilities_data
    ):
        raise ValueError(
            f"dispatch.agents.{agent_id}.capabilities must be a list of "
            "non-empty strings."
        )

    return frozenset(capability.strip() for capability in capabilities_data)


def _read_string_map(
    agent_id: str, agent_data: dict[str, Any], name: str
) -> dict[str, str]:
    """Read an optional object whose keys and values are both strings.

    Args:
        agent_id: Identifier of the entry, for error messages.
        agent_data: The raw entry object.
        name: Field name to read, used verbatim in error messages.

    Returns:
        The decoded mapping, empty when the field is absent.

    Raises:
        ValueError: If the field is present but not a string-to-string object.
    """
    value = agent_data.get(name, {})

    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(
            f"dispatch.agents.{agent_id}.{name} must be an object mapping "
            "strings to strings."
        )

    return dict(value)


def _read_tool_entry(
    agent_id: str,
    agent_data: dict[str, Any],
    *,
    kind: str,
    description: str,
    capabilities: frozenset[str],
) -> AgentEndpoint:
    """Build a registry entry for something that is not a KIO agent.

    Args:
        agent_id: Identifier of the entry.
        agent_data: The raw entry object.
        kind: Either `http` or `cli`.
        description: What the entry does, for the planning model.
        capabilities: The entry's declared capabilities.

    Returns:
        The registry entry, with its call details in `tool`.

    Raises:
        ValueError: If the entry is malformed for its kind.
    """
    if kind == KIND_HTTP:
        url = agent_data.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"dispatch.agents.{agent_id}.url must be a non-empty HTTP(S) "
                'URL. An entry with "type": "http" is called at this URL, '
                "so it replaces the endpoint/path pair a KIO entry uses."
            )

        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"dispatch.agents.{agent_id}.url must be a non-empty HTTP(S) URL."
            )

        method = agent_data.get("method", "POST")
        if not isinstance(method, str) or method.upper() not in _ALLOWED_HTTP_METHODS:
            raise ValueError(
                f"dispatch.agents.{agent_id}.method must be one of "
                f"{sorted(_ALLOWED_HTTP_METHODS)}."
            )

        health_url = agent_data.get("health_url", "")
        if not isinstance(health_url, str):
            raise ValueError(f"dispatch.agents.{agent_id}.health_url must be a string.")

        result_path_data = agent_data.get("result_path", [])
        if not isinstance(result_path_data, list) or not all(
            isinstance(key, str) and key for key in result_path_data
        ):
            raise ValueError(
                f"dispatch.agents.{agent_id}.result_path must be a list of "
                "non-empty strings naming the keys to follow into the response."
            )

        return AgentEndpoint(
            agent_id=agent_id,
            # The protocol message carries an `endpoint` field. For a plain
            # API the honest value is the URL itself; nothing dials it from
            # there, the transport uses the spec.
            endpoint=url,
            path="",
            capabilities=capabilities,
            kind=KIND_HTTP,
            description=description,
            tool=HttpToolSpec(
                url=url,
                method=method.upper(),
                headers=_read_string_map(agent_id, agent_data, "headers"),
                body=agent_data.get("body"),
                result_path=tuple(result_path_data),
                health_url=health_url.strip(),
            ),
        )

    command_data = agent_data.get("command")
    if (
        not isinstance(command_data, list)
        or not command_data
        or not all(isinstance(part, str) and part for part in command_data)
    ):
        raise ValueError(
            f"dispatch.agents.{agent_id}.command must be a non-empty list of "
            "strings, the program followed by its arguments. It is a list "
            "rather than one string because the command runs without a shell, "
            "so nothing in a task description can be interpreted as syntax."
        )

    cwd = agent_data.get("cwd", "")
    if not isinstance(cwd, str):
        raise ValueError(f"dispatch.agents.{agent_id}.cwd must be a string.")

    return AgentEndpoint(
        agent_id=agent_id,
        endpoint=f"cli:{agent_id}",
        path="",
        capabilities=capabilities,
        kind=KIND_CLI,
        description=description,
        tool=CliToolSpec(
            command=tuple(command_data),
            cwd=cwd.strip(),
            env=_read_string_map(agent_id, agent_data, "env"),
        ),
    )


def load_agent_registry(
    agents_data: dict[str, object], *, announce: bool = True
) -> AgentRegistry:
    """Build an `AgentRegistry` from the `dispatch.agents` configuration block.

    Args:
        agents_data: Decoded `dispatch.agents` object, keyed by agent id.
        announce: Whether to log what was loaded. Off for the single-entry
            path, which runs on every background refresh and would otherwise
            fill the log with a line per agent per pass.

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

        kind = agent_data.get("type", KIND_KIO)
        if kind not in ALLOWED_KINDS:
            raise ValueError(
                f"dispatch.agents.{agent_id}.type must be one of "
                f"{sorted(ALLOWED_KINDS)}."
            )

        description = agent_data.get("description", "")
        if not isinstance(description, str):
            raise ValueError(
                f"dispatch.agents.{agent_id}.description must be a string."
            )

        capabilities = _read_capabilities(agent_id, agent_data)

        if kind != KIND_KIO:
            agents[agent_id.strip()] = _read_tool_entry(
                agent_id.strip(),
                agent_data,
                kind=kind,
                description=description.strip(),
                capabilities=capabilities,
            )
            continue

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
            # Two different settings are called "path", and this is the one
            # people reach for by mistake. Say so, rather than repeating the
            # rule they just failed to guess.
            hint = ""
            if isinstance(path, str) and _looks_like_a_filesystem_path(path):
                hint = (
                    " This looks like a directory. The repository agents "
                    "should analyse goes in dispatch.workspace.path; "
                    f"dispatch.agents.{agent_id}.path is the HTTP request "
                    "path the agent serves, such as '/execute'."
                )

            raise ValueError(
                f"dispatch.agents.{agent_id}.path must be a string starting "
                f"with '/'.{hint}"
            )

        subject = agent_data.get("subject")
        if subject is not None and (
            not isinstance(subject, str) or not subject.strip()
        ):
            raise ValueError(
                f"dispatch.agents.{agent_id}.subject must be a non-empty string."
            )

        agents[agent_id.strip()] = AgentEndpoint(
            agent_id=agent_id.strip(),
            endpoint=endpoint,
            path=path,
            capabilities=capabilities,
            subject=subject.strip() if isinstance(subject, str) else None,
            kind=KIND_KIO,
            description=description.strip(),
        )

    if announce:
        logger.info("Agent registry loaded: agents=%s", sorted(agents))

    return AgentRegistry(agents)
