import json
import logging
import urllib.request
from typing import Any

from core.config_loader import Config
from core.session_logger import measure_duration
from core.telemetry import record_gen_ai_response

logger = logging.getLogger(__name__)


def create_client(config: Config) -> None:
    """Return None because Ollama uses direct HTTP requests."""
    logger.debug("Created ollama client (no-op, using direct HTTP requests)")
    return None


def preload(config: Config, client: Any) -> None:
    """Preload a model into Ollama's memory.

    The context window is sent here as well as on requests, because Ollama
    allocates its cache when the model loads. Loading with a smaller window
    than requests use would force a reload on the first request.

    Args:
        config: Application configuration.
        client: Not used for Ollama, included for interface consistency.

    Returns:
        None.
    """
    endpoint = _get_endpoint(config)
    url = f"{endpoint}/api/chat"
    payload = {
        "model": config.model,
        "messages": [],
        "keep_alive": config.keep_alive,
        "options": {
            "num_ctx": _get_context_window_size(config),
        },
    }

    request_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=request_data,
        headers={"Content-Type": "application/json"},
    )

    with measure_duration() as elapsed:
        with urllib.request.urlopen(req, timeout=config.request_timeout) as response:
            response.read()

    logger.info("Model preloaded: model=%s duration_ms=%d", config.model, elapsed())


def send_request(
    config: Config,
    client: Any,
    system_prompt: str,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """Send a chat completion request to the Ollama API.

    Args:
        config: Application configuration.
        client: Not used for Ollama, included for interface consistency.
        system_prompt: The system prompt text.
        messages: List of message dicts with `role` and `content` keys.

    Returns:
        The parsed JSON response from the model as a dictionary.

    Raises:
        ValueError: If the response was truncated by the context window or output limit.
    """
    endpoint = _get_endpoint(config)
    context_window_size = _get_context_window_size(config)
    url = f"{endpoint}/api/chat"

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *messages,
        ],
        "stream": False,
        "format": "json",
        "keep_alive": config.keep_alive,
        "options": {
            "temperature": config.temperature,
            "num_ctx": context_window_size,
            "num_predict": config.max_output_tokens,
        },
    }

    logger.debug("Request payload: %s", payload)

    request_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=request_data,
        headers={"Content-Type": "application/json"},
    )

    with measure_duration() as elapsed:
        with urllib.request.urlopen(req, timeout=config.request_timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))

    finish_reason = response_data.get("done_reason")
    input_tokens = response_data.get("prompt_eval_count")
    output_tokens = response_data.get("eval_count")

    record_gen_ai_response(
        provider=config.provider,
        request_model=config.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        response_model=response_data.get("model"),
        finish_reason=finish_reason,
        truncated=finish_reason == "length",
        truncation_reason="length",
    )

    logger.info(
        "Response received: model=%s duration_ms=%d input_tokens=%s output_tokens=%s",
        config.model,
        elapsed(),
        response_data.get("prompt_eval_count"),
        response_data.get("eval_count"),
    )
    logger.debug("Response: %s", response_data)

    if finish_reason == "length":
        raise ValueError(
            f"Response truncated before completion: "
            f"prompt={response_data.get('prompt_eval_count')} tokens, "
            f"output={response_data.get('eval_count')} tokens, "
            f"context_window_size={context_window_size}, "
            f"max_output_tokens={config.max_output_tokens}. "
            "Increase max_output_tokens if the output limit was reached; "
            "otherwise increase provider_options.context_window_size "
            "or start a new session."
        )

    return response_data


def extract_content(response: dict[str, Any]) -> str:
    """Extract the assistant message content from an Ollama response.

    Args:
        response: The full Ollama API response dictionary.

    Returns:
        The assistant's message content as a string.
    """
    return response["message"]["content"].strip()


def _get_endpoint(config: Config) -> str:
    """Read and validate the Ollama endpoint.

    Args:
        config: Application configuration containing provider options.

    Returns:
        The normalized Ollama endpoint without a trailing slash.

    Raises:
        ValueError: If provider_options.endpoint is missing or invalid.
    """
    endpoint = config.provider_options.get("endpoint")

    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("Ollama requires provider_options.endpoint")

    return endpoint.rstrip("/")


def _get_context_window_size(config: Config) -> int:
    """Read and validate the Ollama context window size.

    Args:
        config: Application configuration containing provider options.

    Returns:
        The configured context window size in tokens.

    Raises:
        ValueError: If provider_options.context_window_size is missing or not a
            positive integer, or if max_output_tokens leaves no room for the prompt.
            Ollama clamps zero and negative values to a roughly 4-token window
            and returns HTTP 200 rather than rejecting them, so invalid values
            must be caught here.
    """
    context_window_size = config.provider_options.get("context_window_size")

    if type(context_window_size) is not int or context_window_size <= 0:
        raise ValueError(
            "Ollama requires provider_options.context_window_size as a "
            "positive integer"
        )

    if config.max_output_tokens >= context_window_size:
        raise ValueError(
            f"max_output_tokens ({config.max_output_tokens}) must be smaller than "
            f"provider_options.context_window_size ({context_window_size}) to "
            f"leave room for the prompt"
        )

    return context_window_size
