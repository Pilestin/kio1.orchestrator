import logging
import os
from types import SimpleNamespace
from typing import Any, cast

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.config_loader import Config
from core.session_logger import measure_duration
from core.telemetry import record_gen_ai_response
from providers.provider_client import TokenCallback

logger = logging.getLogger(__name__)

DISPLAY_NAME = "OpenAI — GPT models"
API_KEY_ENV = "OPENAI_API_KEY"


def create_client(config: Config) -> OpenAI:
    """Create an OpenAI client.

    Args:
        config: Application configuration.

    Returns:
        An OpenAI client.
    """
    api_key = os.getenv(API_KEY_ENV)
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
    on_token: TokenCallback | None = None,
) -> Any:
    """Send a request to the OpenAI Chat Completions API.

    Args:
        config: Application configuration.
        client: OpenAI client.
        system_prompt: System prompt text.
        messages: Conversation messages.
        on_token: Called with each fragment of the answer as it is written.
            Omitted waits for the whole answer.

    Returns:
        An OpenAI response, or one assembled from the stream in the same
        shape.
    """
    return complete(config, client, system_prompt, messages, on_token)


def complete(
    config: Config,
    client: OpenAI,
    system_prompt: str,
    messages: list[dict[str, str]],
    on_token: TokenCallback | None = None,
    *,
    json_mode: bool = True,
    label: str = "OpenAI",
) -> Any:
    """Run one Chat Completions request against any compatible API.

    Shared with every provider that speaks the same API under a different
    address, such as OpenRouter, so streaming and truncation are handled once.

    Args:
        config: Application configuration.
        client: An `OpenAI` client, possibly pointed at another base URL.
        system_prompt: System prompt text.
        messages: Conversation messages.
        on_token: Receives each fragment when streaming.
        json_mode: Whether to ask the API to constrain output to JSON. Some
            models behind a gateway reject the option, so it can be turned
            off and the prompt left to ask for JSON on its own.
        label: Provider name for error messages.

    Returns:
        A response whose `choices[0].message.content` holds the answer.
    """
    request_kwargs: dict[str, Any] = {
        "model": config.model,
        "messages": cast(
            list[ChatCompletionMessageParam],
            [{"role": "system", "content": system_prompt}, *messages],
        ),
        "temperature": config.temperature,
        "max_completion_tokens": config.max_output_tokens,
    }

    if json_mode:
        request_kwargs["response_format"] = {"type": "json_object"}

    logger.debug("Request payload: %s", request_kwargs)

    with measure_duration() as elapsed:
        if on_token is None:
            response = client.chat.completions.create(**request_kwargs)
        else:
            response = _collect_stream(
                client.chat.completions.create(
                    **request_kwargs,
                    stream=True,
                    stream_options={"include_usage": True},
                ),
                on_token,
            )

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
            f"{label} response reached max_output_tokens "
            f"({config.max_output_tokens}) before completion."
        )

    logger.debug("Response: %s", response)

    return response


def _collect_stream(stream: Any, on_token: TokenCallback) -> Any:
    """Relay a streamed completion and assemble it into one response.

    Args:
        stream: The chunks the API returned.
        on_token: Receives each content fragment.

    Returns:
        An object shaped like a non-streamed response: `id`, `model`,
        `usage`, and `choices[0]` with `finish_reason` and `message.content`.
    """
    parts: list[str] = []
    finish_reason = None
    usage = None
    response_id = None
    model = None

    for chunk in stream:
        response_id = getattr(chunk, "id", None) or response_id
        model = getattr(chunk, "model", None) or model
        usage = getattr(chunk, "usage", None) or usage

        for choice in getattr(chunk, "choices", None) or []:
            text = getattr(choice.delta, "content", None)

            if text:
                parts.append(text)
                on_token(text, "content")

            finish_reason = choice.finish_reason or finish_reason

    message = SimpleNamespace(content="".join(parts))

    return SimpleNamespace(
        id=response_id,
        model=model,
        usage=usage,
        choices=[SimpleNamespace(finish_reason=finish_reason, message=message)],
    )


def extract_content(response: Any) -> str:
    """Extract assistant text from an OpenAI response.

    Args:
        response: The response returned by the OpenAI Chat Completions API.

    Returns:
        The assistant message content with surrounding whitespace removed.
    """
    return response.choices[0].message.content.strip()
