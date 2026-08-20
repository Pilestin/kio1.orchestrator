import logging
from importlib import import_module
from types import ModuleType
from typing import Any, Protocol, cast

from core.config_loader import Config

logger = logging.getLogger(__name__)


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
