"""Where the registered targets are kept, and their history.

Until this existed the targets lived in `config.json` beside the model and
the telemetry settings. That worked for one person editing one file, and
not for much else: every console edit rewrote the configuration, nothing
remembered what an agent looked like yesterday, and a bad save was one
`.bak` file away from being lost for good.

The database is now where they live by default — the same SQLite file that
keeps the run history. Each change is a new version, so any earlier state of
an agent can be looked at and restored. On first start the agents in
`config.json` are imported once; after that the database is the source of
truth and the file's `dispatch.agents` is no longer read.

`KIO1_AGENT_STORE=file` keeps them in `config.json` as before — for a
deployment that versions its configuration in git, say.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.config_loader import rehost_entry
from core.config_writer import (
    ConfigWriteError,
    config_path,
    read_agents,
    save_agents,
    writing_enabled,
)
from dispatch.agent_registry import AgentEndpoint, AgentRegistry, load_agent_entry

logger = logging.getLogger(__name__)

AGENT_STORE_ENV = "KIO1_AGENT_STORE"
STORE_FILE = "file"
STORE_DATABASE = "database"

ACTION_IMPORTED = "imported"
ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_REMOVED = "removed"
ACTION_RESTORED = "restored"

# How many versions of one agent are kept. A long-lived agent edited daily
# for a year is a few hundred; past this the oldest go.
_VERSIONS_KEPT = 200


class AgentStoreError(Exception):
    """Raised when a change to the targets could not be saved."""


class AgentStore(Protocol):
    """Somewhere the targets are kept."""

    kind: str

    @property
    def location(self) -> str:
        """Where the targets are, for the console to show."""
        ...

    @property
    def writable(self) -> bool:
        """Whether changes can be saved."""
        ...

    def read(self) -> dict[str, Any]:
        """Return every target's definition, in catalog order."""
        ...

    def write(self, changes: dict[str, Any | None], *, action: str = "") -> None:
        """Apply changes: an id to its new definition, or to None to remove it."""
        ...

    def history(self, agent_id: str) -> list[dict[str, Any]]:
        """Return an agent's versions, newest first."""
        ...


class FileAgentStore:
    """Targets in `config.json`'s `dispatch.agents`, as before the database."""

    kind = STORE_FILE

    @property
    def location(self) -> str:
        """The configuration file."""
        return str(config_path())

    @property
    def writable(self) -> bool:
        """Whether the deployment allows the file to be written."""
        return writing_enabled()

    def read(self) -> dict[str, Any]:
        """Return `dispatch.agents` as the file holds it."""
        try:
            return read_agents()
        except ConfigWriteError as error:
            logger.warning("Could not read the configured agents: %s", error)
            return {}

    def write(self, changes: dict[str, Any | None], *, action: str = "") -> None:
        """Rewrite `dispatch.agents` with the changes applied."""
        try:
            agents = read_agents()

            for agent_id, entry in changes.items():
                if entry is None:
                    agents.pop(agent_id, None)
                else:
                    agents[agent_id] = entry

            save_agents(agents)
        except ConfigWriteError as error:
            raise AgentStoreError(str(error)) from error

    def history(self, agent_id: str) -> list[dict[str, Any]]:
        """A file keeps no history; its `.bak` is one step back at most."""
        return []


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id   TEXT PRIMARY KEY,
    entry      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    entry      TEXT,
    action     TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS agent_versions_agent_idx
    ON agent_versions (agent_id, id);

CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteAgentStore:
    """Targets in SQLite, each change kept as a version."""

    kind = STORE_DATABASE

    def __init__(self, path: str | Path) -> None:
        """Open, or create, the store.

        Args:
            path: The database file, or `:memory:`.
        """
        self._path = str(path)

        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(_SCHEMA)

    @property
    def location(self) -> str:
        """The database file."""
        return self._path

    @property
    def writable(self) -> bool:
        """Always: the database is KIO1's own."""
        return True

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._connection.close()

    def seed(self, entries: dict[str, Any]) -> bool:
        """Import targets once, the first time the store is opened.

        Args:
            entries: The targets to start with — `config.json`'s.

        Returns:
            True when they were imported; False when the store had been
            seeded before, so a later edit to the file does not reappear.
        """
        with self._lock:
            seeded = self._connection.execute(
                "SELECT value FROM store_meta WHERE key = 'seeded'"
            ).fetchone()

            if seeded is not None:
                return False

            self._apply(entries, ACTION_IMPORTED)
            self._connection.execute(
                "INSERT INTO store_meta (key, value) VALUES ('seeded', ?)", (_now(),)
            )

        logger.info("Imported %d target(s) into the database", len(entries))

        return True

    def read(self) -> dict[str, Any]:
        """Return every target, in the order they were added."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT agent_id, entry FROM agents ORDER BY position, agent_id"
            ).fetchall()

        return {agent_id: json.loads(entry) for agent_id, entry in rows}

    def write(self, changes: dict[str, Any | None], *, action: str = "") -> None:
        """Apply changes, each recorded as a version.

        Args:
            changes: Id to new definition, or to None to remove.
            action: What to call the change in the history; worked out from
                the change when omitted.

        Raises:
            AgentStoreError: If the database could not be written.
        """
        try:
            with self._lock:
                self._apply(changes, action)
        except sqlite3.Error as error:
            raise AgentStoreError(
                f"The agent database could not be written: {error}"
            ) from error

    def _apply(self, changes: dict[str, Any | None], action: str) -> None:
        """Write changes in one transaction. The caller holds the lock."""
        connection = self._connection
        now = _now()
        connection.execute("BEGIN")

        try:
            for agent_id, entry in changes.items():
                exists = connection.execute(
                    "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()

                if entry is None:
                    if exists is None:
                        continue

                    connection.execute(
                        "DELETE FROM agents WHERE agent_id = ?", (agent_id,)
                    )
                    recorded = action or ACTION_REMOVED
                    stored = None
                else:
                    stored = json.dumps(entry, ensure_ascii=False)

                    if exists is None:
                        position = connection.execute(
                            "SELECT COALESCE(MAX(position), -1) + 1 FROM agents"
                        ).fetchone()[0]
                        connection.execute(
                            "INSERT INTO agents (agent_id, entry, position, updated_at) "
                            "VALUES (?, ?, ?, ?)",
                            (agent_id, stored, position, now),
                        )
                        recorded = action or ACTION_CREATED
                    else:
                        connection.execute(
                            "UPDATE agents SET entry = ?, updated_at = ? WHERE agent_id = ?",
                            (stored, now, agent_id),
                        )
                        recorded = action or ACTION_UPDATED

                connection.execute(
                    "INSERT INTO agent_versions (agent_id, entry, action, changed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (agent_id, stored, recorded, now),
                )
                connection.execute(
                    "DELETE FROM agent_versions WHERE agent_id = ? AND id NOT IN ("
                    " SELECT id FROM agent_versions WHERE agent_id = ?"
                    " ORDER BY id DESC LIMIT ?)",
                    (agent_id, agent_id, _VERSIONS_KEPT),
                )

            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def history(self, agent_id: str) -> list[dict[str, Any]]:
        """Return an agent's versions, newest first.

        Each is `{version, action, changed_at, entry}`; `entry` is None for
        a removal.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, action, changed_at, entry FROM agent_versions "
                "WHERE agent_id = ? ORDER BY id DESC",
                (agent_id,),
            ).fetchall()

        return [
            {
                "version": version,
                "action": action,
                "changed_at": changed_at,
                "entry": json.loads(entry) if entry is not None else None,
            }
            for version, action, changed_at, entry in rows
        ]


def open_agent_store(database_url: str) -> AgentStore:
    """Open the store the environment chooses.

    Args:
        database_url: The run history's database URL. A SQLite URL holds the
            agents too; anything else — PostgreSQL, which the agent store
            does not support yet — falls back to the file.

    Returns:
        The store, seeded from `config.json` on its first use.
    """
    choice = os.environ.get(AGENT_STORE_ENV, STORE_DATABASE).strip().lower()

    if choice == STORE_FILE:
        return FileAgentStore()

    prefix = "sqlite:///"

    if not database_url.startswith(prefix):
        logger.warning(
            "Agents are kept in %s: the database store needs SQLite, and the "
            "database URL is not a SQLite one",
            config_path(),
        )
        return FileAgentStore()

    store = SqliteAgentStore(database_url[len(prefix) :] or ":memory:")
    store.seed(FileAgentStore().read())

    return store


def registry_from(entries: dict[str, Any]) -> AgentRegistry:
    """Build a registry from stored definitions, as this deployment reaches them.

    One malformed definition is logged and skipped rather than refusing to
    start: every other agent is still usable, and the broken one can be fixed
    from the console.

    Args:
        entries: Stored definitions, in catalog order.

    Returns:
        The registry, with loopback addresses rehosted as `KIO1_AGENT_HOST`
        says — exactly as `config.json` agents are.
    """
    agents: dict[str, AgentEndpoint] = {}

    for agent_id, entry in entries.items():
        try:
            agents[agent_id] = load_agent_entry(agent_id, rehost_entry(agent_id, entry))
        except ValueError as error:
            logger.warning(
                "Stored agent %s is not usable and was skipped: %s", agent_id, error
            )

    return AgentRegistry(agents)
