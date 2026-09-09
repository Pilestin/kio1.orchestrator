"""The live set of targets, kept current while KIO1 runs.

Discovery at startup answers "what existed when we booted". That is the wrong
question for a service: agents come up after KIO1 does, go down while it is
running, and gain capabilities without anyone restarting anything.

This holds the answer to "what can we reach *now*". Three things keep it
current:

- **Refresh.** Manifest sources are re-read on an interval, so an agent that
  starts later joins on its own.
- **Probing.** Every target that can be checked is checked, so one that went
  away stops being offered to the planner instead of failing mid-workflow.
- **Registration.** An agent can push its own manifest to a running KIO1
  rather than waiting to be asked.

Everything else in the system keeps taking an `AgentRegistry`. This produces
those as immutable snapshots: a workflow is routed against one consistent
view even if an agent disappears halfway through it.
"""

import logging
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from dispatch.agent_registry import (
    KIND_CLI,
    KIND_HTTP,
    KIND_KIO,
    AgentEndpoint,
    AgentRegistry,
    CliToolSpec,
    HttpToolSpec,
    load_agent_entry,
)
from dispatch.discovery import DiscoveryConfig, fetch_entries

logger = logging.getLogger(__name__)

# Where a record came from. Configuration is authoritative: a manifest never
# overwrites something a person wrote down, and nothing can delete it.
ORIGIN_CONFIG = "config"
ORIGIN_DISCOVERY = "discovery"
ORIGIN_REGISTRATION = "registration"

# Whether a target is offered to the planner.
STATUS_READY = "ready"
STATUS_UNREACHABLE = "unreachable"
# Nothing about this target can be probed, so it is trusted and used. A REST
# API with no health URL and no way to ask is not the same as one that failed.
STATUS_UNCHECKED = "unchecked"

_MAX_PROBE_WORKERS = 8


@dataclass(frozen=True)
class HealthConfig:
    """How the directory keeps itself current.

    Attributes:
        enabled: Whether probing and refreshing run at all. Off by default, so
            a deployment that lists every agent by hand behaves exactly as it
            did before the directory existed.
        interval: Seconds between background passes.
        timeout: Seconds to wait for one probe.
        failures_before_unreachable: Consecutive failed probes before a target
            stops being offered. Above one, so a single blip does not remove an
            agent mid-conversation.
        path: Liveness path appended to a KIO agent's endpoint.
    """

    enabled: bool = False
    interval: float = 30.0
    timeout: float = 3.0
    failures_before_unreachable: int = 2
    path: str = "/health"


@dataclass(frozen=True)
class AgentRecord:
    """One target and what is currently known about it.

    Attributes:
        endpoint: The routing details.
        origin: Where this record came from.
        status: Whether it is offered to the planner.
        failures: Consecutive failed probes.
        last_checked: Wall-clock time of the last probe, or 0 if never probed.
        last_error: Why the last probe failed, empty when it did not.
    """

    endpoint: AgentEndpoint
    origin: str = ORIGIN_CONFIG
    status: str = STATUS_UNCHECKED
    failures: int = 0
    last_checked: float = 0.0
    last_error: str = ""

    @property
    def agent_id(self) -> str:
        """Return the identifier plans address this target by."""
        return self.endpoint.agent_id

    @property
    def usable(self) -> bool:
        """Report whether this target should be offered to the planner."""
        return self.status != STATUS_UNREACHABLE

    def to_summary(self) -> dict[str, Any]:
        """Return a JSON-safe view for the registry API and the console."""
        return {
            "agent_id": self.agent_id,
            "kind": self.endpoint.kind,
            "origin": self.origin,
            "status": self.status,
            "description": self.endpoint.description,
            "capabilities": sorted(self.endpoint.capabilities),
            "endpoint": self.endpoint.endpoint,
            "failures": self.failures,
            "last_checked": self.last_checked or None,
            "last_error": self.last_error or None,
        }


def probe_target(endpoint: AgentEndpoint, health: HealthConfig) -> tuple[bool, str]:
    """Check whether one target can be reached right now.

    Only what can honestly be checked is checked. A local command is probed by
    looking for the program; a KIO agent by its liveness path; a REST API only
    when the configuration gave one, since an arbitrary API has no path KIO1
    may assume.

    Args:
        endpoint: The target to probe.
        health: Probe settings.

    Returns:
        A pair of (reachable, detail). `detail` is empty when reachable, and
        the reason when not. A target with nothing to probe reports reachable
        with the reason "not probeable", which the caller reads as unchecked
        rather than healthy.
    """
    if endpoint.kind == KIND_CLI:
        spec = endpoint.tool

        if not isinstance(spec, CliToolSpec) or not spec.command:
            return False, "no command configured"

        if shutil.which(spec.command[0]) is None:
            return False, f"{spec.command[0]!r} is not on this machine"

        return True, ""

    url = ""

    if endpoint.kind == KIND_KIO:
        url = f"{endpoint.endpoint}{health.path}"
    elif endpoint.kind == KIND_HTTP:
        spec = endpoint.tool
        if isinstance(spec, HttpToolSpec) and spec.health_url:
            url = spec.health_url

    if not url:
        return True, "not probeable"

    request = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=health.timeout) as response:
            if 200 <= response.status < 400:
                return True, ""

            return False, f"{url} answered HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return False, f"{url} answered HTTP {error.code}"
    except urllib.error.URLError as error:
        return False, f"{url} is unreachable: {error.reason}"
    except TimeoutError:
        return False, f"{url} did not answer within {health.timeout:g}s"
    except OSError as error:  # pragma: no cover - platform-specific socket errors
        return False, f"{url} could not be reached: {error}"


class AgentDirectory:
    """Holds every known target and keeps what is known about it current.

    Safe to use from several threads: a background pass and a registration
    request routinely land at the same time. Readers get immutable snapshots
    rather than a live view, so nothing shifts underneath a running workflow.
    """

    def __init__(
        self,
        configured: AgentRegistry,
        *,
        discovery: DiscoveryConfig | None = None,
        health: HealthConfig | None = None,
    ) -> None:
        """Seed the directory with the hand-written registry.

        Args:
            configured: Agents read from `dispatch.agents`.
            discovery: Manifest sources to re-read on each pass.
            health: Probe settings.
        """
        self._lock = threading.RLock()
        self._discovery = discovery if discovery is not None else DiscoveryConfig()
        self._health = health if health is not None else HealthConfig()
        self._records: dict[str, AgentRecord] = {
            entry.agent_id: AgentRecord(endpoint=entry, origin=ORIGIN_CONFIG)
            for entry in configured.entries()
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── reading ─────────────────────────────────────────────────────────────

    def snapshot(self) -> AgentRegistry:
        """Return the targets currently worth offering, as a fixed registry.

        Returns:
            A registry holding every usable target. Unreachable ones are left
            out, so the planner stops proposing work for an agent that went
            away instead of producing a plan that fails at dispatch.
        """
        with self._lock:
            return AgentRegistry(
                {
                    agent_id: record.endpoint
                    for agent_id, record in self._records.items()
                    if record.usable
                }
            )

    def records(self) -> tuple[AgentRecord, ...]:
        """Return every record, usable or not, in insertion order."""
        with self._lock:
            return tuple(self._records.values())

    def record_for(self, agent_id: str) -> AgentRecord | None:
        """Return one record by identifier, or None when it is unknown."""
        with self._lock:
            return self._records.get(agent_id)

    # ── writing ─────────────────────────────────────────────────────────────

    def upsert(
        self, endpoint: AgentEndpoint, origin: str, *, probe: bool = False
    ) -> AgentRecord:
        """Add or update one target.

        A record written by configuration is never replaced: `config.json` is
        where a deployment records an address only it can know, and a manifest
        cannot know better.

        Args:
            endpoint: The target's routing details.
            origin: Where this came from.
            probe: Whether to check reachability before storing, which is what
                makes a registration a handshake rather than an assertion.

        Returns:
            The stored record.
        """
        status, failures, detail = STATUS_UNCHECKED, 0, ""

        if probe:
            reachable, detail = probe_target(endpoint, self._health)

            if not reachable:
                status = STATUS_UNREACHABLE
                failures = self._health.failures_before_unreachable
            else:
                status = STATUS_UNCHECKED if detail else STATUS_READY
                detail = ""

        with self._lock:
            existing = self._records.get(endpoint.agent_id)

            if existing is not None and existing.origin == ORIGIN_CONFIG:
                logger.info(
                    "Ignoring %s registration for %s: it is configured by hand",
                    origin,
                    endpoint.agent_id,
                )
                return existing

            if existing is not None and existing.endpoint == endpoint and not probe:
                # A refresh that learned nothing new must not discard what
                # probing knows. Otherwise an agent whose manifest still
                # answers while its health check fails resets its own failure
                # count every pass and is never withdrawn.
                return existing

            record = AgentRecord(
                endpoint=endpoint,
                origin=origin,
                status=status,
                failures=failures,
                last_checked=time.time() if probe else 0.0,
                last_error=detail,
            )
            self._records[endpoint.agent_id] = record

            if existing is None:
                logger.info(
                    "Target joined: agent_id=%s origin=%s status=%s",
                    record.agent_id,
                    origin,
                    status,
                )

            return record

    def register(
        self, agent_id: str, entry: dict[str, Any], origin: str = ORIGIN_REGISTRATION
    ) -> AgentRecord:
        """Validate a manifest entry and add it, probing it first.

        Args:
            agent_id: The identifier the entry is keyed by.
            entry: A `dispatch.agents`-shaped object.
            origin: Where this came from.

        Returns:
            The stored record, which may be `unreachable` when the probe
            failed. A target that did not answer is recorded rather than
            rejected, so the next pass can promote it once it is up.

        Raises:
            ValueError: If the entry is malformed.
        """
        return self.upsert(load_agent_entry(agent_id, entry), origin, probe=True)

    def forget(self, agent_id: str) -> bool:
        """Remove a target that was discovered or registered.

        Args:
            agent_id: The target to remove.

        Returns:
            True when it was removed. False when it is unknown, or when it
            came from `config.json` and therefore cannot be removed at
            runtime — the file is the source of truth for those.
        """
        with self._lock:
            record = self._records.get(agent_id)

            if record is None or record.origin == ORIGIN_CONFIG:
                return False

            del self._records[agent_id]
            logger.info("Target removed: agent_id=%s", agent_id)

            return True

    # ── keeping current ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-read manifest sources and re-probe every target.

        Failures anywhere are logged rather than raised: this runs on a timer
        in the background, and an exception escaping would end the loop and
        silently freeze the directory.
        """
        self._refresh_discovery()
        self._probe_all()

    def _refresh_discovery(self) -> None:
        """Add or update targets their manifests currently declare."""
        if not self._discovery.sources:
            return

        try:
            entries = fetch_entries(self._discovery)
        except Exception:
            logger.warning("Discovery refresh failed", exc_info=True)
            return

        for agent_id, entry in entries.items():
            try:
                endpoint = load_agent_entry(agent_id, entry)
            except ValueError as error:
                logger.warning(
                    "Discovered agent %s is not usable and was ignored: %s",
                    agent_id,
                    error,
                )
                continue

            self.upsert(endpoint, ORIGIN_DISCOVERY)

    def _probe_all(self) -> None:
        """Probe every target and update what is known about each."""
        if not self._health.enabled:
            return

        targets = [record.endpoint for record in self.records()]

        if not targets:
            return

        with ThreadPoolExecutor(
            max_workers=min(_MAX_PROBE_WORKERS, len(targets)),
            thread_name_prefix="kio1-probe",
        ) as pool:
            outcomes = list(
                pool.map(lambda target: probe_target(target, self._health), targets)
            )

        checked_at = time.time()

        with self._lock:
            for endpoint, (reachable, detail) in zip(targets, outcomes, strict=True):
                record = self._records.get(endpoint.agent_id)

                if record is None:  # removed while the pass was running
                    continue

                self._records[endpoint.agent_id] = self._apply_probe(
                    record, reachable, detail, checked_at
                )

    def _apply_probe(
        self, record: AgentRecord, reachable: bool, detail: str, checked_at: float
    ) -> AgentRecord:
        """Return the record a probe outcome produces.

        Args:
            record: The record as it stands.
            reachable: Whether the probe succeeded.
            detail: The reason it failed, or "not probeable".
            checked_at: When the probe ran.

        Returns:
            The updated record.
        """
        if reachable and detail:
            # Nothing to check. Leave it usable and say so, rather than
            # claiming a health it was never asked about.
            return replace(
                record,
                status=STATUS_UNCHECKED,
                failures=0,
                last_checked=checked_at,
                last_error="",
            )

        if reachable:
            if record.status == STATUS_UNREACHABLE:
                logger.info("Target is answering again: agent_id=%s", record.agent_id)

            return replace(
                record,
                status=STATUS_READY,
                failures=0,
                last_checked=checked_at,
                last_error="",
            )

        failures = record.failures + 1
        status = record.status

        if failures >= self._health.failures_before_unreachable:
            if status != STATUS_UNREACHABLE:
                logger.warning(
                    "Target withdrawn after %d failed probes: agent_id=%s reason=%s",
                    failures,
                    record.agent_id,
                    detail,
                )
            status = STATUS_UNREACHABLE

        return replace(
            record,
            status=status,
            failures=failures,
            last_checked=checked_at,
            last_error=detail,
        )

    # ── the background pass ─────────────────────────────────────────────────

    @property
    def watching(self) -> bool:
        """Report whether the background pass is running."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Run one pass now, then keep passing on the configured interval.

        The first pass is synchronous so the very first plan is built against
        a checked directory rather than an optimistic one. Nothing starts when
        there is nothing to do.
        """
        if not self._health.enabled and not self._discovery.sources:
            return

        self.refresh()

        if not self._health.enabled or self.watching:
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="kio1-directory", daemon=True
        )
        self._thread.start()
        logger.info("Directory watch started: interval=%gs", self._health.interval)

    def stop(self) -> None:
        """Stop the background pass and wait briefly for it to finish."""
        self._stop.set()
        thread, self._thread = self._thread, None

        if thread is not None:
            thread.join(timeout=self._health.interval + self._health.timeout)

    def _loop(self) -> None:
        """Refresh on the interval until stopped."""
        while not self._stop.wait(self._health.interval):
            try:
                self.refresh()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Directory refresh raised; the watch continues")


def load_health_config(health_data: Any) -> HealthConfig:
    """Build a `HealthConfig` from the `dispatch.health` block.

    Args:
        health_data: Decoded `dispatch.health` object, or an absent value.

    Returns:
        The configured settings.

    Raises:
        ValueError: If the block is malformed.
    """
    if health_data is None:
        return HealthConfig()

    if not isinstance(health_data, dict):
        raise ValueError("dispatch.health must be a JSON object.")

    enabled = health_data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("dispatch.health.enabled must be a boolean.")

    interval = health_data.get("interval", 30)
    if type(interval) not in (int, float) or interval <= 0:
        raise ValueError("dispatch.health.interval must be a positive number.")

    timeout = health_data.get("timeout", 3)
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("dispatch.health.timeout must be a positive number.")

    threshold = health_data.get("failures_before_unreachable", 2)
    if type(threshold) is not int or threshold < 1:
        raise ValueError(
            "dispatch.health.failures_before_unreachable must be a positive " "integer."
        )

    path = health_data.get("path", "/health")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("dispatch.health.path must be a string starting with '/'.")

    if enabled:
        logger.info(
            "Health checks configured: interval=%gs timeout=%gs threshold=%d",
            interval,
            timeout,
            threshold,
        )

    return HealthConfig(
        enabled=enabled,
        interval=float(interval),
        timeout=float(timeout),
        failures_before_unreachable=threshold,
        path=path,
    )
