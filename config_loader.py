import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from session_logger import measure_duration

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


@dataclass
class Config:
    """Application configuration."""

    model: str
    prompt_path: str
    chat_directory: str
    temperature: float
    request_timeout: float
    keep_alive: int
    max_output_tokens: int
    endpoint: str
    context_window_size: int
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)


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


def _load_endpoint(data: dict[str, Any]) -> str:
    """Read and validate the Ollama endpoint.

    Args:
        data: Complete decoded configuration object.

    Returns:
        The normalized Ollama endpoint without a trailing slash.

    Raises:
        ValueError: If `endpoint` is missing or not a non-empty string.
    """
    endpoint = data.get("endpoint")

    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("Config requires a non-empty 'endpoint' string.")

    return endpoint.strip().rstrip("/")


def _load_context_window_size(data: dict[str, Any], max_output_tokens: int) -> int:
    """Read and validate the Ollama context window size.

    Args:
        data: Complete decoded configuration object.
        max_output_tokens: Already-validated `max_output_tokens` value.

    Returns:
        The configured context window size in tokens.

    Raises:
        ValueError: If `context_window_size` is missing or not a positive
            integer, or if `max_output_tokens` leaves no room for the prompt.
            Ollama clamps zero and negative values to a roughly 4-token
            window and returns HTTP 200 rather than rejecting them, so
            invalid values must be caught here.
    """
    context_window_size = data.get("context_window_size")

    if type(context_window_size) is not int or context_window_size <= 0:
        raise ValueError("Config requires 'context_window_size' as a positive integer.")

    if max_output_tokens >= context_window_size:
        raise ValueError(
            f"max_output_tokens ({max_output_tokens}) must be smaller than "
            f"context_window_size ({context_window_size}) to leave room for "
            "the prompt"
        )

    return context_window_size


def load_config(config_path: str = "config.json") -> Config:
    """Load configuration from a JSON file.

    Args:
        config_path: Path to the configuration JSON file.

    Returns:
        A `Config` instance populated from the file.
    """
    path = Path(config_path)
    with measure_duration() as elapsed:
        data: dict[str, Any] = json.loads(path.read_text())

        telemetry = _load_telemetry_config(data)

        keep_alive = data.get("keep_alive", -1)
        if type(keep_alive) is not int:
            raise ValueError("keep_alive must be an integer.")

        max_output_tokens = data.get("max_output_tokens", 4096)
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer.")

        endpoint = _load_endpoint(data)
        endpoint_override = os.environ.get("KIO1_OLLAMA_ENDPOINT")
        if endpoint_override:
            endpoint = endpoint_override.strip().rstrip("/")

        context_window_size = _load_context_window_size(data, max_output_tokens)

    logger.info(
        "Config loaded: model=%s prompt_path=%s endpoint=%s duration_ms=%d",
        data["model"],
        data["prompt_path"],
        endpoint,
        elapsed(),
    )

    return Config(
        model=data["model"],
        prompt_path=data["prompt_path"],
        chat_directory=data["chat_directory"],
        temperature=data["temperature"],
        request_timeout=data["request_timeout"],
        keep_alive=keep_alive,
        max_output_tokens=max_output_tokens,
        endpoint=endpoint,
        context_window_size=context_window_size,
        telemetry=telemetry,
    )
