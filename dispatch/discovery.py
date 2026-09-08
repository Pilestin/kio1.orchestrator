"""Learning about agents from the agents themselves.

Writing an agent's capabilities into `config.json` means two people have to
agree and stay in agreement: whoever wrote the agent, and whoever wrote the
configuration. The agent is the one that actually knows, so it can say.

A source is a URL that answers with a manifest. KIO1 fetches each one at
startup and merges what it finds into the registry. Anything already named in
`config.json` wins: hand-written configuration is explicit intent, and it is
also where a deployment records an address only it can know, such as a host
reachable from inside a container.

A source that is unreachable is a warning, not a failure. An orchestrator
that refuses to start because one optional agent is down is worse than one
that starts without it.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_SOURCE_WORKERS = 8

# `${NAME}`, so a token for a private manifest stays out of config.json.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class DiscoveryError(Exception):
    """Raised when a manifest source could not be read or understood."""


@dataclass(frozen=True)
class ManifestSource:
    """One URL that describes one or more agents.

    Attributes:
        url: Where to GET the manifest.
        headers: Request headers. Values may carry `${ENV_VAR}` references so
            a token for a private manifest stays out of `config.json`.
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        """Return the scheme and host of this source, without a path.

        A manifest that does not name its own `endpoint` is assumed to be
        served by the agent it describes, which is the common case and saves
        repeating an address KIO1 just successfully dialled.
        """
        parsed = urlparse(self.url)

        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


@dataclass(frozen=True)
class DiscoveryConfig:
    """How agents are discovered at startup.

    Attributes:
        sources: Manifest URLs to fetch. Empty disables discovery entirely,
            which is how a deployment behaves that lists every agent by hand.
        timeout: Seconds to wait for one source.
        required: Whether an unreachable source stops startup. False keeps
            KIO1 usable when an optional agent is down.
    """

    sources: tuple[ManifestSource, ...] = ()
    timeout: float = 5.0
    required: bool = False


def load_discovery_config(discovery_data: Any) -> DiscoveryConfig:
    """Build a `DiscoveryConfig` from the `dispatch.discovery` block.

    Args:
        discovery_data: Decoded `dispatch.discovery` object, or an absent value.

    Returns:
        The configured discovery settings, empty when nothing is configured.

    Raises:
        ValueError: If the block is malformed.
    """
    if discovery_data is None:
        return DiscoveryConfig()

    if not isinstance(discovery_data, dict):
        raise ValueError("dispatch.discovery must be a JSON object.")

    sources_data = discovery_data.get("sources", [])
    if not isinstance(sources_data, list):
        raise ValueError("dispatch.discovery.sources must be a list of manifest URLs.")

    sources: list[ManifestSource] = []

    for index, entry in enumerate(sources_data):
        if isinstance(entry, str):
            sources.append(ManifestSource(url=_validated_url(entry, index)))
            continue

        if not isinstance(entry, dict):
            raise ValueError(
                f"dispatch.discovery.sources[{index}] must be a URL string or "
                "an object with a 'url'."
            )

        url = entry.get("url")
        if not isinstance(url, str):
            raise ValueError(
                f"dispatch.discovery.sources[{index}].url must be a string."
            )

        headers = entry.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError(
                f"dispatch.discovery.sources[{index}].headers must be an "
                "object mapping strings to strings."
            )

        sources.append(
            ManifestSource(url=_validated_url(url, index), headers=dict(headers))
        )

    timeout = discovery_data.get("timeout", 5)
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("dispatch.discovery.timeout must be a positive number.")

    required = discovery_data.get("required", False)
    if type(required) is not bool:
        raise ValueError("dispatch.discovery.required must be a boolean.")

    if sources:
        logger.info("Discovery configured: sources=%d", len(sources))

    return DiscoveryConfig(
        sources=tuple(sources), timeout=float(timeout), required=required
    )


def _validated_url(url: str, index: int) -> str:
    """Return a manifest URL, rejecting anything that cannot be fetched.

    Args:
        url: The configured value.
        index: Position in the sources list, for error messages.

    Returns:
        The trimmed URL.

    Raises:
        ValueError: If it is not an HTTP(S) URL.
    """
    trimmed = url.strip()
    parsed = urlparse(trimmed)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"dispatch.discovery.sources[{index}] must be an HTTP(S) URL, got "
            f"{url!r}."
        )

    return trimmed


def entries_from_manifest(payload: Any, source: ManifestSource) -> dict[str, Any]:
    """Turn a decoded manifest into `dispatch.agents`-shaped entries.

    Four shapes are accepted, because whoever writes a manifest should not
    have to guess which one KIO1 wanted: a single agent object, a list of
    them, `{"agents": [...]}`, and `{"agents": {"KIO2": {...}}}` — the last
    being exactly what `config.json` already looks like.

    An entry that does not name an `endpoint` or `url` gets the source's own
    origin, since the agent describing itself is normally the agent serving
    the manifest.

    Args:
        payload: The decoded manifest body.
        source: The source it came from, used for defaults and messages.

    Returns:
        Entries keyed by agent id.

    Raises:
        DiscoveryError: If the manifest's shape cannot be read at all.
    """
    if isinstance(payload, dict) and "agents" in payload:
        payload = payload["agents"]

    if isinstance(payload, dict) and "agent_id" not in payload:
        # Already keyed by agent id, the config.json shape.
        candidates = [
            {"agent_id": agent_id, **value}
            for agent_id, value in payload.items()
            if isinstance(value, dict)
        ]
    elif isinstance(payload, dict):
        candidates = [payload]
    elif isinstance(payload, list):
        candidates = [item for item in payload if isinstance(item, dict)]
    else:
        raise DiscoveryError(
            f"{source.url} answered with {type(payload).__name__}, expected an "
            "agent object or a list of them."
        )

    entries: dict[str, Any] = {}

    for candidate in candidates:
        agent_id = candidate.get("agent_id") or candidate.get("id")

        if not isinstance(agent_id, str) or not agent_id.strip():
            logger.warning("Ignoring an entry from %s with no agent_id", source.url)
            continue

        entry = {
            key: value
            for key, value in candidate.items()
            if key not in {"agent_id", "id"}
        }

        if entry.get("type", "kio") == "kio" and "endpoint" not in entry:
            entry["endpoint"] = source.origin

        entries[agent_id.strip()] = entry

    if not entries:
        raise DiscoveryError(f"{source.url} named no usable agents.")

    return entries


def fetch_source(source: ManifestSource, timeout: float) -> dict[str, Any]:
    """Fetch and decode one manifest source.

    Args:
        source: The source to read.
        timeout: Seconds to wait.

    Returns:
        Entries keyed by agent id.

    Raises:
        DiscoveryError: If the source could not be reached, decoded, or read.
    """
    headers = {"Accept": "application/json"}

    for name, value in source.headers.items():
        try:
            headers[name] = expand_environment(value)
        except KeyError as error:
            raise DiscoveryError(
                f"{source.url} needs environment variable {error.args[0]} for "
                f"its {name} header, and it is not set."
            ) from error

    request = urllib.request.Request(source.url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_MANIFEST_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise DiscoveryError(f"{source.url} returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise DiscoveryError(f"{source.url} is unreachable: {error.reason}") from error
    except TimeoutError as error:
        raise DiscoveryError(
            f"{source.url} did not answer within {timeout:g}s."
        ) from error

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise DiscoveryError(f"{source.url} did not answer JSON: {error}") from error

    return entries_from_manifest(payload, source)


def expand_environment(value: str) -> str:
    """Replace `${NAME}` references in a configured string.

    Args:
        value: The configured string.

    Returns:
        The string with every reference expanded.

    Raises:
        KeyError: If a referenced variable is unset.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)

        if resolved is None:
            raise KeyError(name)

        return resolved

    return _ENV_REFERENCE.sub(substitute, value)


def fetch_entries(discovery: DiscoveryConfig) -> dict[str, Any]:
    """Fetch every configured source and return what they declared.

    Sources are read concurrently: one unreachable agent would otherwise add
    its whole timeout to every startup.

    Args:
        discovery: The configured discovery settings.

    Returns:
        Entries keyed by agent id, ready to be validated by the registry
        loader. Empty when nothing is configured or every source failed.

    Raises:
        ValueError: If a source failed and `required` is set.
    """
    if not discovery.sources:
        return {}

    entries: dict[str, Any] = {}
    failures: list[str] = []

    with ThreadPoolExecutor(
        max_workers=min(_MAX_SOURCE_WORKERS, len(discovery.sources)),
        thread_name_prefix="kio1-discovery",
    ) as pool:
        futures = {
            pool.submit(fetch_source, source, discovery.timeout): source
            for source in discovery.sources
        }

        for future, source in futures.items():
            try:
                found = future.result()
            except DiscoveryError as error:
                failures.append(str(error))
                logger.warning("Discovery source skipped: %s", error)
                continue

            for agent_id, entry in found.items():
                if agent_id in entries:
                    logger.warning(
                        "Agent %s is declared by more than one source; keeping "
                        "the first",
                        agent_id,
                    )
                    continue

                entries[agent_id] = entry

            logger.info("Discovered from %s: %s", source.url, ", ".join(sorted(found)))

    if failures and discovery.required:
        raise ValueError(
            "dispatch.discovery.required is true and a source could not be "
            f"read: {failures[0]}"
        )

    return entries
