import inspect
import logging
import os
from collections.abc import Callable
from importlib import import_module
from types import ModuleType
from typing import Any, Protocol, cast

from core.config_loader import Config

logger = logging.getLogger(__name__)

# Receives a fragment of model output as it is written, and which channel it
# belongs to: `content` for the answer, `thinking` for a reasoning model's
# thinking. A provider that can stream accepts one as `on_token`.
TokenCallback = Callable[[str, str], None]


def supports_streaming(provider: Any) -> bool:
    """Report whether a provider's `send_request` accepts `on_token`.

    Streaming is optional: a provider that cannot stream still plans, and the
    live view simply shows the plan when it arrives instead of as it is
    written.
    """
    try:
        parameters = inspect.signature(provider.send_request).parameters
    except (TypeError, ValueError, AttributeError):
        return False

    return "on_token" in parameters


class ProviderModule(Protocol):
    """Interface implemented by every provider module."""

    def create_client(self, config: Config) -> Any:
        """Create and return the provider client."""
        ...

    def preload(self, config: Config, client: Any) -> None:
        """Perform provider-specific initialization, such as local model preloading."""
        ...

    def send_request(
        self,
        config: Config,
        client: Any,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> Any:
        """Send a request to the provider."""
        ...

    def extract_content(self, response: Any) -> str:
        """Extract assistant text from a provider response."""
        ...


def describe_provider(provider_name: str) -> dict[str, Any]:
    """Return what the console needs to offer a provider.

    Args:
        provider_name: A provider module name, such as `openrouter`.

    Returns:
        Its name, display name, the environment variable holding its key
        (None when it needs none), whether that key is set, and whether it
        can list its own models. `available` is False, with the reason, when
        the module cannot be imported.
    """
    try:
        module = import_module(f"providers.{provider_name}_client")
    except Exception as error:  # noqa: BLE001 - reported to the console
        return {
            "name": provider_name,
            "label": provider_name,
            "available": False,
            "error": f"{type(error).__name__}: {error}",
        }

    key_env = getattr(module, "API_KEY_ENV", None)

    return {
        "name": provider_name,
        "label": getattr(module, "DISPLAY_NAME", provider_name),
        "available": True,
        "key_env": key_env,
        "key_set": bool(key_env and os.environ.get(key_env, "").strip()),
        "lists_models": callable(getattr(module, "list_models", None)),
    }


def load_provider(
    provider_name: str, allowed_providers: frozenset[str]
) -> ProviderModule:
    """Dynamically load a provider module.

    A provider named ``openai`` is loaded from ``providers/openai_client.py``.

    Args:
        provider_name: Provider name from configuration.
        allowed_providers: Set of allowed provider names.

    Returns:
        A module implementing the provider interface.
    """
    if (
        not provider_name
        or not provider_name.isidentifier()
        or provider_name not in allowed_providers
    ):
        raise ValueError(f"Invalid provider name: {provider_name!r}")

    module_name = f"providers.{provider_name}_client"

    try:
        module: ModuleType = import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            raise ValueError(
                f"Provider {provider_name!r} is not installed. "
                f"Expected module: {module_name.replace('.', '/')}.py"
            ) from error

        raise

    required_functions = (
        "create_client",
        "preload",
        "send_request",
        "extract_content",
    )

    missing_functions = [
        function_name
        for function_name in required_functions
        if not callable(getattr(module, function_name, None))
    ]

    if missing_functions:
        raise TypeError(
            f"Provider module {module_name!r} is missing functions: "
            f"{', '.join(missing_functions)}"
        )

    logger.info("Loaded provider module: %s", module_name)

    return cast(ProviderModule, module)
