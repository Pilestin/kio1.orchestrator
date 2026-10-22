import logging
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

from dispatch.mcp_client import McpTool

logger = logging.getLogger(__name__)

# What a registry entry is, and therefore how a step addressed to it runs.
#
# `kio` is the consortium's KIO1-KIO10 protocol: a JSON execution message
# posted to an agent that knows the contract. The other two exist because
# most of what an orchestrator usefully calls is not a KIO: an ordinary REST
# API, or a command line tool sitting on the same machine. They are handled
# by their own transports, so the planner and the executor never learn the
# difference.
# `mcp` is the one that describes itself: the entry says how to start the
# server and the server says what it can do, so its capabilities and the
# arguments of each are asked for rather than written down.
KIND_KIO = "kio"
KIND_HTTP = "http"
KIND_CLI = "cli"
KIND_MCP = "mcp"
# `llm` is the one nobody has to build: a role written in plain language,
# carried out by a language model inside KIO1. No server, no code.
KIND_LLM = "llm"

ALLOWED_KINDS = frozenset({KIND_KIO, KIND_HTTP, KIND_CLI, KIND_MCP, KIND_LLM})

# Long enough for a detailed role; short enough that a pasted document is
# refused rather than sent with every step.
_MAX_INSTRUCTIONS = 8000

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
        input_schema: JSON Schema for the named arguments the API takes,
            referenced as `{arguments.name}` in the URL, headers, or body.
            Empty for an API that is told what to do in prose alone. It is
            the same shape an MCP server reports for its tools, so the planner
            is shown both the same way and fills both in the same way.
    """

    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    result_path: tuple[str, ...] = ()
    health_url: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def arguments_tool(self, capability: str) -> McpTool | None:
        """Return the declared arguments as a tool description, or None.

        Args:
            capability: The capability being called, used as the tool name.

        Returns:
            A tool carrying `input_schema`, so the argument handling written
            for MCP servers applies unchanged. None when no arguments are
            declared.
        """
        if not self.input_schema:
            return None

        return McpTool(name=capability, input_schema=self.input_schema)


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
class McpToolSpec:
    """How to reach a Model Context Protocol server, and what it turned out to offer.

    The configured half is small on purpose — a command to run, or a URL to
    post to. Everything else about an MCP target is discovered: the server is
    asked for its tools, and each tool's arguments come back as a JSON Schema.
    That is the whole reason to prefer this kind over `cli` or `http` for
    something that speaks MCP.

    Attributes:
        command: Program and arguments for a server run as a child process.
            Empty means the server is reached over HTTP instead.
        cwd: Working directory for a child process. Empty runs in KIO1's own.
        env: Extra environment variables for the child. Values may contain
            `${ENV_VAR}` references, so a token stays out of `config.json`.
        url: Endpoint of a server reached over Streamable HTTP. Empty means
            the server is run as a child process instead.
        headers: Extra headers for an HTTP server, typically an Authorization
            line. Values may contain `${ENV_VAR}` references.
        discovered: The tools the server last said it had. Empty until it has
            been asked, which is why an unprobed MCP entry offers nothing
            specific to the planner rather than offering something invented.
    """

    command: tuple[str, ...] = ()
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    discovered: tuple[McpTool, ...] = ()

    @property
    def transport(self) -> str:
        """Return `stdio` for a child process, `http` for a URL."""
        return "stdio" if self.command else "http"

    @property
    def address(self) -> str:
        """Return what would be contacted, for probes and error messages."""
        return " ".join(self.command) if self.command else self.url


@dataclass(frozen=True)
class LlmToolSpec:
    """An agent that is a role, carried out by a language model in KIO1.

    The way someone who does not write code adds an agent: say what it is
    for, in plain language, and it exists. A "code reviewer" or a
    "requirements writer" is a set of instructions, not a service.

    Attributes:
        instructions: The role, as the system prompt the model is given.
        provider: The provider to run it on. Empty uses the planning
            provider.
        model: The model. Empty uses the planning model; needed with a
            provider other than the planner's, since model names differ.
        temperature: Sampling temperature, or None for the configured one.
    """

    instructions: str
    provider: str = ""
    model: str = ""
    temperature: float | None = None


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
        kind: One of `kio`, `http`, `cli`, or `mcp`. Decides which transport
            runs the step. Defaults to `kio`, so an entry written before this
            field existed keeps its behaviour.
        description: What this target does, in the words the planning model
            should see. Empty falls back to a line built from the
            capabilities.
        tool: The call details for everything that is not a KIO agent. Always
            `None` for a `kio` entry, whose call details are `endpoint` and
            `path`.
        enabled: Whether the planner may use this target at all. A disabled
            target stays registered — its definition, history, and place in
            the console are kept — but it is neither probed nor offered, so
            switching one off is reversible in a way removing it is not.
        requires_approval: Whether a plan that may run this target waits for
            a person to approve it first. For targets that change things — a
            command that writes files, an API that deploys.
    """

    agent_id: str
    endpoint: str
    path: str
    capabilities: frozenset[str]
    subject: str | None = None
    kind: str = KIND_KIO
    description: str = ""
    tool: HttpToolSpec | CliToolSpec | McpToolSpec | LlmToolSpec | None = None
    enabled: bool = True
    requires_approval: bool = False

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


_ARGUMENT_TYPES = frozenset(
    {"string", "number", "integer", "boolean", "array", "object"}
)


def _read_arguments_schema(agent_id: str, agent_data: dict[str, Any]) -> dict[str, Any]:
    """Read an entry's declared arguments as a JSON Schema.

    Two spellings are accepted. The short one is what a person writes:

        "arguments": {"latitude": {"type": "number", "required": true}}

    The long one is a JSON Schema object, which is what an OpenAPI document
    or an MCP server already has and should not need translating by hand:

        "arguments": {"type": "object", "properties": {...}, "required": [...]}

    Args:
        agent_id: Identifier of the entry, for error messages.
        agent_data: The raw entry object.

    Returns:
        A JSON Schema with `properties` and `required`, empty when the entry
        declares no arguments.

    Raises:
        ValueError: If the declaration is malformed.
    """
    declared = agent_data.get("arguments")

    if declared is None or declared == {}:
        return {}

    where = f"dispatch.agents.{agent_id}.arguments"

    if not isinstance(declared, dict):
        raise ValueError(f"{where} must be an object keyed by argument name.")

    if isinstance(declared.get("properties"), dict):
        required = declared.get("required", [])

        if not isinstance(required, list) or not all(
            isinstance(name, str) for name in required
        ):
            raise ValueError(f"{where}.required must be a list of argument names.")

        return {
            "type": "object",
            "properties": dict(declared["properties"]),
            "required": list(required),
        }

    properties: dict[str, Any] = {}
    required_names: list[str] = []

    for name, spec in declared.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"{where}.{name} must be an object such as "
                '{"type": "string", "required": true}.'
            )

        kind = spec.get("type", "string")

        if kind not in _ARGUMENT_TYPES:
            raise ValueError(
                f"{where}.{name}.type must be one of {sorted(_ARGUMENT_TYPES)}."
            )

        if spec.get("required", False) is True:
            required_names.append(name)

        properties[name] = {
            key: value for key, value in spec.items() if key != "required"
        } | {"type": kind}

    return {"type": "object", "properties": properties, "required": required_names}


def _read_enabled(agent_id: str, agent_data: dict[str, Any]) -> bool:
    """Read whether an entry is switched on.

    Args:
        agent_id: Identifier of the entry, for error messages.
        agent_data: The raw entry object.

    Returns:
        The flag, True when absent so every entry written before it existed
        keeps working.

    Raises:
        ValueError: If the value is not a boolean.
    """
    return _read_flag(agent_id, agent_data, "enabled", default=True)


def _read_flag(
    agent_id: str, agent_data: dict[str, Any], name: str, *, default: bool
) -> bool:
    """Read an optional boolean field of an entry.

    Args:
        agent_id: Identifier of the entry, for error messages.
        agent_data: The raw entry object.
        name: The field.
        default: The value when absent.

    Returns:
        The flag.

    Raises:
        ValueError: If the value is not a boolean — `"false"` as a string is
            truthy, and reading it as on would be exactly the wrong way round.
    """
    value = agent_data.get(name, default)

    if type(value) is not bool:
        raise ValueError(f"dispatch.agents.{agent_id}.{name} must be true or false.")

    return value


def _read_mcp_entry(
    agent_id: str,
    agent_data: dict[str, Any],
    *,
    description: str,
    capabilities: frozenset[str],
) -> AgentEndpoint:
    """Build a registry entry for a Model Context Protocol server.

    This is the shortest entry of any kind, and deliberately so: it says only
    how to reach the server. What the server can do, and what each of those
    things needs, is asked for at probe time rather than copied into
    `config.json` where it would go stale.

    `capabilities` keeps its usual meaning and becomes an allow-list: name
    some and only those tools are offered to the planner; name none and every
    tool the server reports is. Naming none is the common case, and it is the
    point of the whole kind.

    Args:
        agent_id: Identifier of the entry.
        agent_data: The raw entry object.
        description: What the entry does, for the planning model.
        capabilities: The entry's declared allow-list, possibly empty.

    Returns:
        The registry entry, with its connection details in `tool`.

    Raises:
        ValueError: If the entry names neither a command nor a URL, or names
            both.
    """
    command_data = agent_data.get("command", [])
    url = agent_data.get("url", "")

    if not isinstance(url, str):
        raise ValueError(f"dispatch.agents.{agent_id}.url must be a string.")

    url = url.strip()

    if command_data and url:
        raise ValueError(
            f"dispatch.agents.{agent_id} names both a command and a url. An "
            "MCP server is either run here as a child process (command) or "
            "reached over HTTP (url), and which one decides how it is "
            "contacted, so only one may be set."
        )

    if not command_data and not url:
        raise ValueError(
            f"dispatch.agents.{agent_id} needs either a command or a url. "
            'Use "command" for a server run here, such as '
            '["npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"], '
            'or "url" for one reached over HTTP.'
        )

    if url:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"dispatch.agents.{agent_id}.url must be a non-empty HTTP(S) URL."
            )

        return AgentEndpoint(
            agent_id=agent_id,
            endpoint=url,
            path="",
            capabilities=capabilities,
            kind=KIND_MCP,
            description=description,
            tool=McpToolSpec(
                url=url,
                headers=_read_string_map(agent_id, agent_data, "headers"),
            ),
        )

    if not isinstance(command_data, list) or not all(
        isinstance(part, str) and part for part in command_data
    ):
        raise ValueError(
            f"dispatch.agents.{agent_id}.command must be a non-empty list of "
            "strings, the program followed by its arguments. It is a list "
            "rather than one string because the server is started without a "
            "shell."
        )

    cwd = agent_data.get("cwd", "")

    if not isinstance(cwd, str):
        raise ValueError(f"dispatch.agents.{agent_id}.cwd must be a string.")

    return AgentEndpoint(
        agent_id=agent_id,
        endpoint=f"mcp:{agent_id}",
        path="",
        capabilities=capabilities,
        kind=KIND_MCP,
        description=description,
        tool=McpToolSpec(
            command=tuple(command_data),
            cwd=cwd.strip(),
            env=_read_string_map(agent_id, agent_data, "env"),
        ),
    )


def _read_llm_entry(
    agent_id: str,
    agent_data: dict[str, Any],
    *,
    description: str,
    capabilities: frozenset[str],
) -> AgentEndpoint:
    """Build a registry entry for an agent that is a written role.

    Args:
        agent_id: Identifier of the entry.
        agent_data: The raw entry object.
        description: What the entry does, for the planning model.
        capabilities: The entry's declared capabilities.

    Returns:
        The registry entry, with the role in `tool`.

    Raises:
        ValueError: If the role is missing or too long, or a model setting is
            malformed.
    """
    where = f"dispatch.agents.{agent_id}"
    instructions = agent_data.get("instructions")

    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError(
            f"{where}.instructions must say what this agent is and does, in plain "
            'language — for example "You review Python code for bugs and explain '
            'each one briefly."'
        )

    if len(instructions) > _MAX_INSTRUCTIONS:
        raise ValueError(
            f"{where}.instructions is {len(instructions)} characters; keep it under "
            f"{_MAX_INSTRUCTIONS}."
        )

    if not capabilities:
        raise ValueError(
            f"{where}.capabilities must name at least one thing it does, such as "
            '["code_review"]: the planner chooses agents by capability.'
        )

    provider = agent_data.get("provider", "")
    model = agent_data.get("model", "")

    for name, value in (("provider", provider), ("model", model)):
        if not isinstance(value, str):
            raise ValueError(f"{where}.{name} must be a string.")

    if provider.strip() and not model.strip():
        raise ValueError(
            f"{where}.model is required with provider: model names differ between "
            "providers."
        )

    temperature = agent_data.get("temperature")

    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not 0 <= temperature <= 2
    ):
        raise ValueError(f"{where}.temperature must be a number from 0 to 2.")

    return AgentEndpoint(
        agent_id=agent_id,
        endpoint=f"llm:{agent_id}",
        path="",
        capabilities=capabilities,
        kind=KIND_LLM,
        description=description,
        tool=LlmToolSpec(
            instructions=instructions.strip(),
            provider=provider.strip(),
            model=model.strip(),
            temperature=float(temperature) if temperature is not None else None,
        ),
    )


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
        kind: One of `http`, `cli`, or `mcp`.
        description: What the entry does, for the planning model.
        capabilities: The entry's declared capabilities.

    Returns:
        The registry entry, with its call details in `tool`.

    Raises:
        ValueError: If the entry is malformed for its kind.
    """
    if kind == KIND_MCP:
        return _read_mcp_entry(
            agent_id,
            agent_data,
            description=description,
            capabilities=capabilities,
        )

    if kind == KIND_LLM:
        return _read_llm_entry(
            agent_id,
            agent_data,
            description=description,
            capabilities=capabilities,
        )

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
                input_schema=_read_arguments_schema(agent_id, agent_data),
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
        enabled = _read_enabled(agent_id, agent_data)
        approval = _read_flag(agent_id, agent_data, "approval", default=False)

        if kind != KIND_KIO:
            entry = _read_tool_entry(
                agent_id.strip(),
                agent_data,
                kind=kind,
                description=description.strip(),
                capabilities=capabilities,
            )
            agents[agent_id.strip()] = replace(
                entry, enabled=enabled, requires_approval=approval
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
            enabled=enabled,
            requires_approval=approval,
        )

    if announce:
        logger.info("Agent registry loaded: agents=%s", sorted(agents))

    return AgentRegistry(agents)
