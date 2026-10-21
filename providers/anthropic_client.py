import logging
import os
from typing import Any, cast

from anthropic import Anthropic
from anthropic.types import MessageParam

from core.config_loader import Config
from core.session_logger import measure_duration
from core.telemetry import record_gen_ai_response

logger = logging.getLogger(__name__)

DISPLAY_NAME = "Anthropic (Claude)"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Models that reject a custom `temperature` (400 if sent with a non-default value).
# Omitting the parameter entirely is always safe — the API falls back to its own default.
_NO_CUSTOM_TEMPERATURE = {
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
}


def create_client(config: Config) -> Anthropic:
    """Create an Anthropic client.

    Args:
        config: Application configuration.

    Returns:
        An Anthropic client.
    """
    api_key = os.getenv(API_KEY_ENV)
    if not api_key or not api_key.strip():
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable is required when using the Anthropic provider"
        )
    logger.debug("Created Anthropic client with model: %s", config.model)
    return Anthropic(
        api_key=api_key,
        timeout=config.request_timeout,
    )


def preload(config: Config, client: Any) -> None:
    """Verify the configured model exists before starting the session.

    Args:
        config: Application configuration.
        client: Anthropic client.

    Returns:
        None.

    Raises:
        anthropic.NotFoundError: If config.model does not exist.
    """
    with measure_duration() as elapsed:
        client.models.retrieve(config.model)
    logger.info("Model verified: model=%s duration_ms=%d", config.model, elapsed())


def send_request(
    config: Config,
    client: Anthropic,
    system_prompt: str,
    messages: list[dict[str, str]],
) -> Any:
    """Send a request to the Anthropic Messages API.

    Args:
        config: Application configuration.
        client: Anthropic client.
        system_prompt: System prompt text.
        messages: Conversation messages.

    Returns:
        An Anthropic response. `temperature` is omitted for models in
        `_NO_CUSTOM_TEMPERATURE` that reject a custom value.
    """
    request_kwargs: dict[str, Any] = {
        "model": config.model,
        "system": system_prompt,
        "messages": cast(list[MessageParam], messages),
        "max_tokens": config.max_output_tokens,
    }
    if config.model not in _NO_CUSTOM_TEMPERATURE:
        request_kwargs["temperature"] = config.temperature

    logger.debug("Request payload: %s", request_kwargs)

    with measure_duration() as elapsed:
        response = client.messages.create(**request_kwargs)

    usage = getattr(response, "usage", None)
    finish_reason = getattr(response, "stop_reason", None)

    record_gen_ai_response(
        provider=config.provider,
        request_model=config.model,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        response_model=getattr(response, "model", None),
        response_id=getattr(response, "id", None),
        finish_reason=finish_reason,
        truncated=finish_reason == "max_tokens",
        truncation_reason="max_output_tokens",
    )

    logger.info(
        "Response received: model=%s duration_ms=%d input_tokens=%s output_tokens=%s",
        config.model,
        elapsed(),
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )

    if finish_reason == "max_tokens":
        raise ValueError(
            "Anthropic response reached max_output_tokens "
            f"({config.max_output_tokens}) before completion."
        )

    logger.debug("Response: %s", response)

    return response


def extract_content(response: Any) -> str:
    """Extract assistant text from an Anthropic response.

    Args:
        response: The response returned by the Anthropic Messages API.

    Returns:
        The combined text content with surrounding whitespace removed.
    """
    text_parts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ]

    return "".join(text_parts).strip()
