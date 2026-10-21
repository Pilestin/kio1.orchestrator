"""OpenRouter: hundreds of models from many vendors behind one key.

OpenRouter speaks the OpenAI Chat Completions API at its own address, so the
request itself is `openai_client.complete`. What differs is where it goes,
which key it uses, and that the model list is long enough to be worth
offering in the console rather than typing from memory.

Configuration:

    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-4.5",
    "provider_options": {"json_mode": true, "openrouter_endpoint": "https://openrouter.ai/api/v1"}

Both options are optional; the values shown are the defaults.

The key is read from `OPENROUTER_API_KEY`, never from `config.json`.
"""

import json
import logging
import os
import urllib.request
from typing import Any

from openai import OpenAI

from core.config_loader import Config
from providers.openai_client import complete
from providers.provider_client import TokenCallback

logger = logging.getLogger(__name__)

API_KEY_ENV = "OPENROUTER_API_KEY"
DISPLAY_NAME = "OpenRouter — hundreds of models, one key"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"

# OpenRouter's optional attribution headers: they name the application in
# its dashboard, and nothing else depends on them.
_ATTRIBUTION = {"X-Title": "KIO1 Orchestrator"}


def _endpoint(config: Config) -> str:
    """Return the API base URL.

    Read from `provider_options.openrouter_endpoint`, not `endpoint`: the
    latter is Ollama's address, and it stays in the file when the console
    switches provider. Reusing it would send OpenRouter requests to Ollama.
    """
    endpoint = config.provider_options.get("openrouter_endpoint")

    if isinstance(endpoint, str) and endpoint.strip().startswith("http"):
        return endpoint.strip().rstrip("/")

    return DEFAULT_ENDPOINT


def _json_mode(config: Config) -> bool:
    """Return whether to request JSON-constrained output.

    On by default. Not every model behind OpenRouter supports it; turning it
    off leaves the system prompt, which already demands JSON, to do the work.
    """
    value = config.provider_options.get("json_mode", True)

    if type(value) is not bool:
        raise ValueError("OpenRouter provider_options.json_mode must be true or false")

    return value


def create_client(config: Config) -> OpenAI:
    """Create an OpenAI client pointed at OpenRouter.

    Raises:
        ValueError: If `OPENROUTER_API_KEY` is not set.
    """
    api_key = os.getenv(API_KEY_ENV, "").strip()

    if not api_key:
        raise ValueError(
            f"{API_KEY_ENV} environment variable is required when using the "
            "OpenRouter provider. Create a key at https://openrouter.ai/keys "
            "and put it in .env."
        )

    return OpenAI(
        api_key=api_key,
        base_url=_endpoint(config),
        timeout=config.request_timeout,
        default_headers=_ATTRIBUTION,
    )


def preload(config: Config, client: Any) -> None:
    """Check the configured model exists, so a typo fails at startup.

    Raises:
        ValueError: If OpenRouter does not list the model.
    """
    models = list_models(config)

    if models and config.model not in models:
        raise ValueError(
            f"OpenRouter has no model named {config.model!r}. Pick one from "
            "the console's Settings tab or https://openrouter.ai/models."
        )


def send_request(
    config: Config,
    client: OpenAI,
    system_prompt: str,
    messages: list[dict[str, str]],
    on_token: TokenCallback | None = None,
) -> Any:
    """Send a request through OpenRouter, streaming when asked to."""
    return complete(
        config,
        client,
        system_prompt,
        messages,
        on_token,
        json_mode=_json_mode(config),
        label="OpenRouter",
    )


def extract_content(response: Any) -> str:
    """Extract assistant text from a response."""
    content = response.choices[0].message.content

    return (content or "").strip()


def list_models(config: Config) -> list[str]:
    """Return the model ids OpenRouter offers.

    The listing needs no key, so the console can show it before one is set.

    Raises:
        OSError: If OpenRouter could not be reached.
    """
    request = urllib.request.Request(f"{_endpoint(config)}/models", method="GET")

    with urllib.request.urlopen(request, timeout=10) as response:
        decoded = json.loads(response.read().decode("utf-8"))

    listed = decoded.get("data") if isinstance(decoded, dict) else None

    if not isinstance(listed, list):
        return []

    return sorted(
        entry["id"]
        for entry in listed
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )
