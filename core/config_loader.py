import ipaddress
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.config_writer import config_path
from dispatch.agent_registry import AgentRegistry, load_agent_registry
from dispatch.directory import HealthConfig, load_health_config
from dispatch.discovery import DiscoveryConfig, load_discovery_config
from dispatch.failover import FailoverConfig, load_failover_config
from dispatch.policy import Policy, load_policy
from dispatch.scoreboard import SelectionConfig, load_selection_config

logger = logging.getLogger(__name__)

# Placeholder hosts shipped in the example configuration. Exporter errors are
# filtered off the console so they cannot corrupt the interactive prompt, so a
# placeholder host would otherwise fail invisibly for a whole session. It is
# reported once at startup instead of rejected: running against an unreachable
# collector is a legitimate way to exercise the export-failure path.
_ALLOWED_DISPATCH_TRANSPORTS = frozenset({"http", "nats"})

# Addresses that differ between running on a developer machine and running in
# a container. `config.json` is committed and shared; these are per-deployment,
# so the environment wins over the file.
PROVIDER_ENDPOINT_ENV = "KIO1_PROVIDER_ENDPOINT"
AGENT_ENDPOINT_ENV_PREFIX = "KIO1_AGENT_ENDPOINT_"
# Every agent usually moves together — from a laptop's loopback to the Docker
# host, say — so one variable rehosts all of them. Naming an agent explicitly
# still wins, and adding an agent to the file needs no new variable.
AGENT_HOST_ENV = "KIO1_AGENT_HOST"

_PLACEHOLDER_OTLP_HOSTS = ("your_host", "your-host", "changeme", "example.com")


def _agent_endpoint_overrides() -> dict[str, str]:
    """Return per-agent endpoint overrides taken from the environment.

    `KIO1_AGENT_ENDPOINT_KIO2=http://host.docker.internal:8102` redirects KIO2
    without editing the committed configuration. Agent ids are matched
    case-insensitively, since environment variables are conventionally
    uppercase.

    Returns:
        Uppercased agent id mapped to the endpoint to use instead.
    """
    overrides: dict[str, str] = {}

    for name, value in os.environ.items():
        if not name.startswith(AGENT_ENDPOINT_ENV_PREFIX) or not value.strip():
            continue

        overrides[name[len(AGENT_ENDPOINT_ENV_PREFIX) :].upper()] = value.strip()

    return overrides


def _is_loopback_host(hostname: str | None) -> bool:
    """Report whether an address means "this machine, wherever it is running".

    Only these are ambiguous between a laptop and a container, and only these
    are what the shared-host variable exists to repair. A public address such
    as `api.open-meteo.com` means the same thing in both places, so rewriting
    it does not fix anything — it breaks a target that worked.

    Args:
        hostname: Host parsed from the configured address, if any.

    Returns:
        True for loopback and wildcard addresses, false for everything else.
    """
    if not hostname:
        return False

    hostname = hostname.lower().strip("[]")

    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    # `0.0.0.0` is not loopback, but it is what a service binds to in order to
    # be reachable from outside; as a *destination* it is the same mistake.
    return address.is_loopback or address.is_unspecified


def _rehost(endpoint: str, host: str) -> str:
    """Return an endpoint pointing at a different host, port and scheme intact.

    Args:
        endpoint: The configured endpoint.
        host: The host to substitute.

    Returns:
        The endpoint with its host replaced. Unchanged if it cannot be parsed,
        or if it already names a host that is reachable from anywhere.
    """
    parsed = urlparse(endpoint)

    if not parsed.scheme or not parsed.netloc:
        return endpoint

    if not _is_loopback_host(parsed.hostname):
        return endpoint

    netloc = host if parsed.port is None else f"{host}:{parsed.port}"

    return parsed._replace(netloc=netloc).geturl()


def _is_placeholder_host(hostname: str | None) -> bool:
    """Report whether a hostname is an unedited example value.

    Args:
        hostname: Host parsed from the configured endpoint, if any.

    Returns:
        True for the example hosts and for any `.example.com` subdomain,
        which RFC 2606 reserves for documentation.
    """
    if not hostname:
        return False

    hostname = hostname.lower()

    return hostname in _PLACEHOLDER_OTLP_HOSTS or hostname.endswith(".example.com")


def rehost_entry(agent_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Return an entry with its addresses pointed where this deployment can reach.

    `config.json` is committed and shared, so it names agents the way they are
    addressed from a developer machine — usually `127.0.0.1`. Inside a
    container that means the container itself. These variables redirect them
    without editing the file, which is why the file keeps the original and
    only what runs is rewritten.

    Every address an entry carries is considered, not just `endpoint`: an
    `http` tool reaches its service through `url`, and a loopback address in
    there is wrong in exactly the same way.

    Only loopback addresses move. `KIO1_AGENT_HOST` answers the question "what
    does *this machine* mean from in here", and a public address such as
    `https://api.open-meteo.com/...` was never asking it — rewriting one
    silently replaced a working target with the Docker host, and the failure
    surfaced as an unreachable agent nobody had misconfigured.

    Args:
        agent_id: The entry's identifier, used for the per-agent override.
        entry: The raw `dispatch.agents` entry.

    Returns:
        A copy with any address rewritten. The original is never modified, so
        a caller that also saves the entry saves what was actually written.
    """
    override = _agent_endpoint_overrides().get(agent_id.upper())
    shared_host = os.environ.get(AGENT_HOST_ENV, "").strip()
    rehosted = dict(entry)

    if override is not None:
        logger.info(
            "Agent endpoint overridden from the environment: agent_id=%s "
            "endpoint=%s",
            agent_id,
            override,
        )
        rehosted["endpoint"] = override

        return rehosted

    if not shared_host:
        return rehosted

    for key in ("endpoint", "url", "health_url"):
        value = rehosted.get(key)

        if not isinstance(value, str) or not value:
            continue

        moved = _rehost(value, shared_host)

        if moved != value:
            logger.info(
                "Agent rehosted from the environment: agent_id=%s %s=%s",
                agent_id,
                key,
                moved,
            )
            rehosted[key] = moved

    return rehosted


@dataclass(frozen=True)
class TelemetryConfig:
    """OpenTelemetry export configuration."""

    enabled: bool = False
    service_name: str = "kio1-orchestrator"
    otlp_http_endpoint: str = "http://localhost:4318"
    otlp_bearer_token: str = ""
    metric_export_interval_ms: int = 5000
    trace_sample_ratio: float = 1.0
    kio_id: str = "kio1"
    deployment_environment: str = "local"


@dataclass(frozen=True)
class Workspace:
    """The code a workflow is about.

    A KIO that analyses code needs to be told which code. The plan's `task`
    is free text, so this travels in the execution message's `data` field —
    the protocol's own extension point — under the keys KIO agents document.

    Attributes:
        path: Repository root, as the agents can see it. In a containerized
            deployment this is the path inside the agent's container, not
            KIO1's.
        entry_point: Runnable script that reproduces the behaviour under
            analysis, relative to `path`.
    """

    path: str = ""
    entry_point: str = ""

    def to_data(self) -> dict[str, Any]:
        """Return the `data` fragment describing this workspace.

        Returns:
            The fragment to merge into an execution message's `data`, or an
            empty object when nothing is configured, so a plan behaves
            exactly as it did before a workspace existed.
        """
        fragment: dict[str, Any] = {}

        if self.path:
            fragment["repository"] = {"path": self.path}

        if self.entry_point:
            fragment["target"] = {"entry_point": self.entry_point}

        return fragment


@dataclass(frozen=True)
class DispatchConfig:
    """Workflow dispatch configuration.

    Attributes:
        enabled: Whether generated plans are sent to KIO agents. Disabled by
            default, so KIO1 keeps behaving as a plan-only terminal tool
            until agents are actually reachable.
        transport: How execution messages travel, `http` or `nats`. The
            message body is identical either way.
        request_timeout: Seconds to wait for a single agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches in
            `parallel` execution mode.
        registry: Agents KIO1 is able to dispatch work to.
        nats_servers: NATS server URLs, required when transport is `nats`.
        nats_subject_template: Subject used for agents that do not name one
            themselves. `{agent}` is replaced with the lowercased agent id.
        workspace: The repository and entry point agents should analyse.
        policy: Gates deciding what must have passed before a step runs.
        discovery: Manifest sources agents describe themselves through.
        health: How the live directory probes targets and keeps current.
        failover: Whether a step may run on a target other than the one
            the plan named.
        selection: Whether recent outcomes reorder the candidates for a
            step.
    """

    enabled: bool = False
    transport: str = "http"
    request_timeout: float = 60.0
    max_parallel_steps: int = 4
    registry: AgentRegistry = field(default_factory=lambda: AgentRegistry({}))
    nats_servers: tuple[str, ...] = ()
    nats_subject_template: str = "kio.{agent}.execute"
    workspace: Workspace = field(default_factory=Workspace)
    policy: Policy = field(default_factory=Policy)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    failover: FailoverConfig = field(default_factory=FailoverConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)


@dataclass
class Config:
    """Application configuration."""

    provider: str
    allowed_providers: frozenset[str]
    model: str
    prompt_path: str
    chat_directory: str
    temperature: float
    request_timeout: float
    keep_alive: int
    max_output_tokens: int
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    provider_options: dict[str, Any] = field(default_factory=dict)


def _load_telemetry_config(data: dict[str, Any]) -> TelemetryConfig:
    """Load and validate the optional telemetry configuration.

    Args:
        data: Complete decoded configuration object.

    Returns:
        Validated telemetry settings.

    Raises:
        ValueError: If telemetry settings have invalid types or values.
    """
    telemetry_data = data.get("telemetry", {})

    if not isinstance(telemetry_data, dict):
        raise ValueError("telemetry must be a JSON object.")

    enabled = telemetry_data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("telemetry.enabled must be a boolean.")

    service_name = telemetry_data.get("service_name", "kio1-orchestrator")
    if not isinstance(service_name, str) or not service_name.strip():
        raise ValueError("telemetry.service_name must be a non-empty string.")

    endpoint = telemetry_data.get(
        "otlp_http_endpoint",
        "http://localhost:4318",
    )
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError(
            "telemetry.otlp_http_endpoint must be a non-empty HTTP(S) URL."
        )

    endpoint = endpoint.strip().rstrip("/")
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
        raise ValueError(
            "telemetry.otlp_http_endpoint must be a non-empty HTTP(S) URL."
        )

    if enabled and _is_placeholder_host(parsed_endpoint.hostname):
        logger.warning(
            "telemetry.otlp_http_endpoint host is still the example "
            "placeholder (%r); every export will fail until it is replaced "
            "with a real Collector endpoint",
            parsed_endpoint.hostname,
        )

    bearer_token = telemetry_data.get("otlp_bearer_token", "")
    if not isinstance(bearer_token, str):
        raise ValueError("telemetry.otlp_bearer_token must be a string.")

    export_interval = telemetry_data.get("metric_export_interval_ms", 5000)
    if type(export_interval) is not int or export_interval <= 0:
        raise ValueError(
            "telemetry.metric_export_interval_ms must be a positive integer."
        )

    sample_ratio = telemetry_data.get("trace_sample_ratio", 1.0)
    if type(sample_ratio) not in (int, float) or not 0.0 <= sample_ratio <= 1.0:
        raise ValueError(
            "telemetry.trace_sample_ratio must be a number between 0.0 and 1.0."
        )

    kio_id = telemetry_data.get("kio_id", "kio1")
    if not isinstance(kio_id, str) or not kio_id.strip():
        raise ValueError("telemetry.kio_id must be a non-empty string.")

    deployment_environment = telemetry_data.get("deployment_environment", "local")
    if (
        not isinstance(deployment_environment, str)
        or not deployment_environment.strip()
    ):
        raise ValueError("telemetry.deployment_environment must be a non-empty string.")

    return TelemetryConfig(
        enabled=enabled,
        service_name=service_name.strip(),
        otlp_http_endpoint=endpoint,
        otlp_bearer_token=bearer_token.strip(),
        metric_export_interval_ms=export_interval,
        trace_sample_ratio=float(sample_ratio),
        kio_id=kio_id.strip(),
        deployment_environment=deployment_environment.strip(),
    )


def _load_dispatch_config(data: dict[str, Any]) -> DispatchConfig:
    """Load and validate the optional dispatch configuration.

    Args:
        data: Complete decoded configuration object.

    Returns:
        Validated dispatch settings.

    Raises:
        ValueError: If dispatch settings have invalid types or values.
    """
    dispatch_data = data.get("dispatch", {})

    if not isinstance(dispatch_data, dict):
        raise ValueError("dispatch must be a JSON object.")

    enabled = dispatch_data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("dispatch.enabled must be a boolean.")

    request_timeout = dispatch_data.get("request_timeout", 60)
    if type(request_timeout) not in (int, float) or request_timeout <= 0:
        raise ValueError("dispatch.request_timeout must be a positive number.")

    max_parallel_steps = dispatch_data.get("max_parallel_steps", 4)
    if type(max_parallel_steps) is not int or max_parallel_steps <= 0:
        raise ValueError("dispatch.max_parallel_steps must be a positive integer.")

    transport = dispatch_data.get("transport", "http")
    if transport not in _ALLOWED_DISPATCH_TRANSPORTS:
        raise ValueError(
            "dispatch.transport must be one of "
            f"{sorted(_ALLOWED_DISPATCH_TRANSPORTS)}."
        )

    nats_data = dispatch_data.get("nats", {})
    if not isinstance(nats_data, dict):
        raise ValueError("dispatch.nats must be a JSON object.")

    servers_data = nats_data.get("servers", [])
    if not isinstance(servers_data, list) or not all(
        isinstance(server, str) and server.strip() for server in servers_data
    ):
        raise ValueError("dispatch.nats.servers must be a list of non-empty strings.")

    subject_template = nats_data.get("subject_template", "kio.{agent}.execute")
    if not isinstance(subject_template, str) or "{agent}" not in subject_template:
        raise ValueError(
            "dispatch.nats.subject_template must be a string containing " "'{agent}'."
        )

    if enabled and transport == "nats" and not servers_data:
        raise ValueError(
            "dispatch.transport is 'nats' but dispatch.nats.servers is empty; "
            "there is no broker to publish to."
        )

    agents_data = dispatch_data.get("agents", {})
    if not isinstance(agents_data, dict):
        raise ValueError("dispatch.agents must be a JSON object.")

    workspace_data = dispatch_data.get("workspace", {})
    if not isinstance(workspace_data, dict):
        raise ValueError("dispatch.workspace must be a JSON object.")

    workspace_path = workspace_data.get("path", "")
    entry_point = workspace_data.get("entry_point", "")

    for name, value in (("path", workspace_path), ("entry_point", entry_point)):
        if not isinstance(value, str):
            raise ValueError(f"dispatch.workspace.{name} must be a string.")

    for agent_id, agent_data in agents_data.items():
        if isinstance(agent_data, dict):
            agents_data[agent_id] = rehost_entry(str(agent_id), agent_data)

    policy = load_policy(dispatch_data.get("policy"))
    discovery = load_discovery_config(dispatch_data.get("discovery"))
    health = load_health_config(dispatch_data.get("health"))
    failover = load_failover_config(dispatch_data.get("failover"))
    selection = load_selection_config(dispatch_data.get("selection"))

    registry = load_agent_registry(agents_data)

    if enabled and not len(registry):
        raise ValueError(
            "dispatch.enabled is true but dispatch.agents is empty; there is "
            "nowhere to send workflow steps."
        )

    return DispatchConfig(
        enabled=enabled,
        transport=transport,
        request_timeout=float(request_timeout),
        max_parallel_steps=max_parallel_steps,
        registry=registry,
        nats_servers=tuple(server.strip() for server in servers_data),
        nats_subject_template=subject_template,
        workspace=Workspace(
            path=workspace_path.strip(),
            entry_point=entry_point.strip(),
        ),
        policy=policy,
        discovery=discovery,
        health=health,
        failover=failover,
        selection=selection,
    )


def load_config(path_override: str | None = None) -> Config:
    """Load configuration from a JSON file.

    Args:
        path_override: Path to read instead of the configured one.
            Mainly for tests; a deployment sets `KIO1_CONFIG_PATH`
            instead, so the console's writer and this reader always
            agree on which file they mean.

    Returns:
        A `Config` instance populated from the file.
    """
    path = Path(path_override) if path_override else config_path()
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    provider_options = data.get("provider_options", {})

    if not isinstance(provider_options, dict):
        raise ValueError("provider_options must be a JSON object.")

    provider_endpoint = os.environ.get(PROVIDER_ENDPOINT_ENV, "").strip()

    if provider_endpoint:
        logger.info(
            "Provider endpoint overridden from the environment: %s",
            provider_endpoint,
        )
        provider_options = {**provider_options, "endpoint": provider_endpoint}

    telemetry = _load_telemetry_config(data)
    dispatch = _load_dispatch_config(data)

    allowed_providers = frozenset(data["allowed_providers"])
    if data["provider"] not in allowed_providers:
        raise ValueError(f"Unknown provider: {data['provider']!r}")

    keep_alive = data.get("keep_alive", -1)
    if type(keep_alive) is not int:
        raise ValueError("keep_alive must be an integer.")

    max_output_tokens = data.get("max_output_tokens", 4096)
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer.")

    logger.info(
        "Config loaded: provider=%s model=%s prompt_path=%s",
        data["provider"],
        data["model"],
        data["prompt_path"],
    )

    return Config(
        provider=data["provider"],
        allowed_providers=allowed_providers,
        model=data["model"],
        prompt_path=data["prompt_path"],
        chat_directory=data["chat_directory"],
        temperature=data["temperature"],
        request_timeout=data["request_timeout"],
        keep_alive=keep_alive,
        max_output_tokens=max_output_tokens,
        telemetry=telemetry,
        dispatch=dispatch,
        provider_options=provider_options,
    )
