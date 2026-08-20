import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_registry import AgentRegistry, load_agent_registry

logger = logging.getLogger(__name__)


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
class DispatchConfig:
    """Workflow dispatch configuration.

    Attributes:
        enabled: Whether generated plans are sent to KIO agents. Disabled by
            default, so KIO1 keeps behaving as a plan-only terminal tool
            until agents are actually reachable.
        request_timeout: Seconds to wait for a single agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches in
            `parallel` execution mode.
        registry: Agents KIO1 is able to dispatch work to.
    """

    enabled: bool = False
    request_timeout: float = 60.0
    max_parallel_steps: int = 4
    registry: AgentRegistry = field(default_factory=lambda: AgentRegistry({}))


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

    agents_data = dispatch_data.get("agents", {})
    if not isinstance(agents_data, dict):
        raise ValueError("dispatch.agents must be a JSON object.")

    registry = load_agent_registry(agents_data)

    if enabled and not len(registry):
        raise ValueError(
            "dispatch.enabled is true but dispatch.agents is empty; there is "
            "nowhere to send workflow steps."
        )

    return DispatchConfig(
        enabled=enabled,
        request_timeout=float(request_timeout),
        max_parallel_steps=max_parallel_steps,
        registry=registry,
    )


def load_config(config_path: str = "config.json") -> Config:
    """Load configuration from a JSON file.

    Args:
        config_path: Path to the configuration JSON file.

    Returns:
        A `Config` instance populated from the file.
    """
    path = Path(config_path)
    data: dict[str, Any] = json.loads(path.read_text())

    provider_options = data.get("provider_options", {})

    if not isinstance(provider_options, dict):
        raise ValueError("provider_options must be a JSON object.")

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
