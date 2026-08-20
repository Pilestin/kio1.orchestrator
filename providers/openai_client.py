import logging
import os
from typing import Any, cast

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.config_loader import Config
from core.session_logger import measure_duration
from core.telemetry import record_gen_ai_response

logger = logging.getLogger(__name__)


def create_client(config: Config) -> OpenAI:
    """Create an OpenAI client.

    Args:
        config: Application configuration.

    Returns:
        An OpenAI client.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not api_key.strip():
        raise ValueError(
            "OPENAI_API_KEY environment variable is required when using the OpenAI provider"
        )
    logger.debug("Created OpenAI client with model: %s", config.model)
    return OpenAI(
        api_key=api_key,
        timeout=config.request_timeout,
    )


def preload(config: Config, client: Any) -> None:
    """Verify the configured model exists before starting the session.

    Args:
        config: Application configuration.
        client: OpenAI client.

    Returns:
        None.

    Raises:
        openai.NotFoundError: If config.model does not exist.
    """
    with measure_duration() as elapsed:
        client.models.retrieve(config.model)

    logger.info("Model verified: model=%s duration_ms=%d", config.model, elapsed())


def send_request(
    config: Config,
    client: OpenAI,
    system_prompt: str,
    messages: list[dict[str, str]],
) -> Any:
    """Send a request to the OpenAI Chat Completions API.

    Args:
        config: Application configuration.
        client: OpenAI client.
        system_prompt: System prompt text.
        messages: Conversation messages.

    Returns:
        An OpenAI response.
    """
    request_kwargs: dict[str, Any] = {
        "model": config.model,
        "messages": cast(
            list[ChatCompletionMessageParam],
            [{"role": "system", "content": system_prompt}, *messages],
        ),
        "temperature": config.temperature,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": config.max_output_tokens,
    }

    logger.debug("Request payload: %s", request_kwargs)

    with measure_duration() as elapsed:
        response = client.chat.completions.create(**request_kwargs)

    usage = getattr(response, "usage", None)
    finish_reason = response.choices[0].finish_reason

    record_gen_ai_response(
        provider=config.provider,
        request_model=config.model,
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        response_model=getattr(response, "model", None),
        response_id=getattr(response, "id", None),
        finish_reason=finish_reason,
        truncated=finish_reason == "length",
        truncation_reason="max_output_tokens",
    )

    logger.info(
        "Response received: model=%s duration_ms=%d input_tokens=%s output_tokens=%s",
        config.model,
        elapsed(),
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )

    if finish_reason == "length":
        raise ValueError(
            "OpenAI response reached max_output_tokens "
            f"({config.max_output_tokens}) before completion."
        )

    logger.debug("Response: %s", response)

    return response


def extract_content(response: Any) -> str:
    """Extract assistant text from an OpenAI response.

    Args:
        response: The response returned by the OpenAI Chat Completions API.

    Returns:
        The assistant message content with surrounding whitespace removed.
    """
    return response.choices[0].message.content.strip()
