"""SQLite storage for sessions, so a single machine needs no database server.

PostgreSQL is the right home for a shared deployment, and it stays one. It is
the wrong first step for someone trying KIO1 on a laptop: it means Docker,
and Docker is the most common reason a first run does not happen. SQLite
ships with Python, keeps everything in one file, and survives a restart,
which is all a single person's history needs.

The behaviour is the PostgreSQL repository's, statement for statement where
the dialects allow, so a session recorded here reads back identically.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from services.session_manager.models import (
    Conversation,
    LineageEdge,
    RecordStepRequest,
    Session,
    SessionDetail,
    Step,
    UpdateSessionRequest,
    conversation_title,
)
from services.session_manager.repository import (
    UnknownConversationError,
    storable_changes,
)

logger = logging.getLogger(__name__)

_ORDERED_EXECUTION_MODES = frozenset({"sequential", "mixed"})

# Indexes on columns added after the first release are made in `_migrate`,
# once an older table has those columns.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS conversations_updated_idx ON conversations (updated_at);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    request_text    TEXT NOT NULL,
    status          TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    workflow_id     TEXT,
    execution_mode  TEXT,
    error           TEXT,
    answer          TEXT,
    conversation_id TEXT REFERENCES conversations (conversation_id) ON DELETE CASCADE,
    clarifications  TEXT,
    attachments     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_created_idx ON sessions (created_at);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    step_id     TEXT    NOT NULL,
    agent_id    TEXT    NOT NULL,
    capability  TEXT    NOT NULL,
    task        TEXT    NOT NULL,
    status      TEXT    NOT NULL
        CHECK (status IN ('ok', 'error', 'skipped', 'not_run')),
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT    NOT NULL,
    UNIQUE (session_id, step_id)
);

CREATE INDEX IF NOT EXISTS steps_session_idx ON steps (session_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    step_id    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, step_id)
);

CREATE TABLE IF NOT EXISTS lineage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT    NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    produced_by_step_id TEXT    NOT NULL,
    consumed_by_step_id TEXT    NOT NULL,
    artifact_id         INTEGER NOT NULL REFERENCES artifacts (id) ON DELETE CASCADE,
    basis               TEXT    NOT NULL
        CHECK (basis IN ('execution_order', 'declared_dependency')),
    created_at          TEXT    NOT NULL,
    UNIQUE (session_id, produced_by_step_id, consumed_by_step_id)
);

CREATE INDEX IF NOT EXISTS lineage_session_idx ON lineage (session_id);
"""

_SESSION_COLUMNS = (
    "session_id, request_text, status, workflow_id, execution_mode, error, "
    "answer, conversation_id, clarifications, attachments, created_at, updated_at"
)

_CONVERSATION_COLUMNS = "conversation_id, title, created_at, updated_at"


_clock_lock = threading.Lock()
_last_time = datetime.min.replace(tzinfo=timezone.utc)


def _now() -> str:
    """Return the current time as sortable ISO-8601 text in UTC.

    Never the same value twice: the clock can read the same in two calls a
    moment apart, and a conversation added to has only its `updated_at` to
    move it up the list — its `rowid`, the tie-break, stays where it was.
    """
    global _last_time

    with _clock_lock:
        now = datetime.now(timezone.utc)
        _last_time = max(now, _last_time + timedelta(microseconds=1))

        return _last_time.isoformat()


class SqliteSessionRepository:
    """SQLite-backed repository, safe to share between threads.

    One connection guarded by a lock. Workflows record from background
    threads, and SQLite serializes writers regardless; a pool would add
    nothing but `database is locked` errors to retry.
    """

    def __init__(self, path: str | Path) -> None:
        """Open, or create, the database file and its tables.

        Args:
            path: The database file, or `:memory:` for a throwaway one.
        """
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Readers do not block the writer, so the console polling a run does
        # not slow the run down.
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(_SCHEMA)
        self._migrate()

        logger.info("Session storage: SQLite at %s", path)

    def _migrate(self) -> None:
        """Bring a database made by an earlier version up to this schema.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table as it was, so
        a column added later has to be added here. SQLite has no
        `ADD COLUMN IF NOT EXISTS`; the table's columns are read instead.

        Sessions recorded before conversations existed are each given one of
        their own, so an upgraded history still shows every run.
        """
        connection = self._connection
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        added = {
            "answer": "answer TEXT",
            "conversation_id": "conversation_id TEXT "
            "REFERENCES conversations (conversation_id) ON DELETE CASCADE",
            "clarifications": "clarifications TEXT",
            "attachments": "attachments TEXT",
        }

        for column, definition in added.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE sessions ADD COLUMN {definition}")
                logger.info("Session storage migrated: sessions.%s added", column)

        connection.execute(
            "CREATE INDEX IF NOT EXISTS sessions_conversation_idx "
            "ON sessions (conversation_id, created_at)"
        )
        orphans = connection.execute("""
            SELECT session_id, request_text, created_at, updated_at
            FROM sessions
            WHERE conversation_id IS NULL
            """).fetchall()

        if not orphans:
            return

        connection.execute("BEGIN")

        for session_id, request_text, created_at, updated_at in orphans:
            conversation_id = str(uuid4())
            connection.execute(
                f"INSERT INTO conversations ({_CONVERSATION_COLUMNS}) "
                "VALUES (?, ?, ?, ?)",
                (
                    conversation_id,
                    conversation_title(request_text),
                    created_at,
                    updated_at,
                ),
            )
            connection.execute(
                "UPDATE sessions SET conversation_id = ? WHERE session_id = ?",
                (conversation_id, session_id),
            )

        connection.execute("COMMIT")
        logger.info(
            "Session storage migrated: %d session(s) given a conversation",
            len(orphans),
        )

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._connection.close()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        """Run one statement and return its rows."""
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> Session:
        """Open a new session and return it. See `SessionRepository`."""
        now = _now()
        title = conversation_title(request_text)

        with self._lock:
            connection = self._connection
            connection.execute("BEGIN")

            try:
                if conversation_id is None:
                    key = str(uuid4())
                    connection.execute(
                        f"INSERT INTO conversations ({_CONVERSATION_COLUMNS}) "
                        "VALUES (?, ?, ?, ?)",
                        (key, title, now, now),
                    )
                else:
                    key = str(conversation_id)
                    touched = connection.execute(
                        """
                        UPDATE conversations
                        SET title = CASE WHEN title = '' THEN ? ELSE title END,
                            updated_at = ?
                        WHERE conversation_id = ?
                        RETURNING conversation_id
                        """,
                        (title, now, key),
                    ).fetchone()

                    if touched is None:
                        raise UnknownConversationError(key)

                row = connection.execute(
                    f"""
                    INSERT INTO sessions (session_id, request_text, status,
                                          conversation_id, created_at, updated_at)
                    VALUES (?, ?, 'pending', ?, ?, ?)
                    RETURNING {_SESSION_COLUMNS}
                    """,
                    (str(uuid4()), request_text, key, now, now),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        return _to_session(row)

    def create_conversation(self, title: str = "") -> Conversation:
        """Open an empty conversation and return it."""
        now = _now()
        rows = self._query(
            f"""
            INSERT INTO conversations ({_CONVERSATION_COLUMNS})
            VALUES (?, ?, ?, ?)
            RETURNING {_CONVERSATION_COLUMNS}
            """,
            (str(uuid4()), title, now, now),
        )

        return _to_conversation(rows[0])

    def get_conversation(self, conversation_id: UUID) -> Conversation | None:
        """Return a conversation, or None if unknown."""
        rows = self._query(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations "
            "WHERE conversation_id = ?",
            (str(conversation_id),),
        )

        return _to_conversation(rows[0]) if rows else None

    def list_conversations(self, limit: int) -> list[Conversation]:
        """Return the conversations most recently added to, newest first."""
        rows = self._query(
            f"""
            SELECT {_CONVERSATION_COLUMNS}
            FROM conversations
            ORDER BY updated_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [_to_conversation(row) for row in rows]

    def conversation_sessions(self, conversation_id: UUID) -> list[Session]:
        """Return a conversation's sessions in the order they were opened."""
        rows = self._query(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM sessions
            WHERE conversation_id = ?
            ORDER BY created_at, rowid
            """,
            (str(conversation_id),),
        )

        return [_to_session(row) for row in rows]

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Delete a conversation and its sessions; False if it was unknown."""
        # Sessions go with it by `ON DELETE CASCADE`, and their steps,
        # artifacts, and lineage with them.
        rows = self._query(
            "DELETE FROM conversations WHERE conversation_id = ? "
            "RETURNING conversation_id",
            (str(conversation_id),),
        )

        return bool(rows)

    def get_session(self, session_id: UUID) -> SessionDetail | None:
        """Return a session with its steps and lineage, or None if unknown."""
        key = str(session_id)

        with self._lock:
            session_row = self._connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
                (key,),
            ).fetchone()

            if session_row is None:
                return None

            step_rows = self._connection.execute(
                """
                SELECT s.step_id, s.agent_id, s.capability, s.task, s.status,
                       s.duration_ms, s.error, a.payload, s.created_at
                FROM steps s
                LEFT JOIN artifacts a
                    ON a.session_id = s.session_id AND a.step_id = s.step_id
                WHERE s.session_id = ?
                ORDER BY s.id
                """,
                (key,),
            ).fetchall()

            lineage_rows = self._connection.execute(
                """
                SELECT produced_by_step_id, consumed_by_step_id, artifact_id, basis
                FROM lineage
                WHERE session_id = ?
                ORDER BY id
                """,
                (key,),
            ).fetchall()

        return SessionDetail(
            session=_to_session(session_row),
            steps=[_to_step(row) for row in step_rows],
            lineage=[
                LineageEdge(
                    produced_by_step_id=row[0],
                    consumed_by_step_id=row[1],
                    artifact_id=row[2],
                    basis=row[3],
                )
                for row in lineage_rows
            ],
        )

    def list_sessions(self, limit: int) -> list[Session]:
        """Return the most recent sessions, newest first."""
        rows = self._query(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM sessions
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [_to_session(row) for row in rows]

    def update_session(
        self, session_id: UUID, update: UpdateSessionRequest
    ) -> Session | None:
        """Apply the non-null fields of an update, or None if unknown."""
        changes = storable_changes(update)

        if not changes:
            detail = self.get_session(session_id)
            return detail.session if detail is not None else None

        # Column names come from the model's own fields, never from input.
        assignments = ", ".join(f"{column} = ?" for column in changes)
        rows = self._query(
            f"""
            UPDATE sessions
            SET {assignments}, updated_at = ?
            WHERE session_id = ?
            RETURNING {_SESSION_COLUMNS}
            """,
            (*changes.values(), _now(), str(session_id)),
        )

        return _to_session(rows[0]) if rows else None

    def record_step(self, session_id: UUID, step: RecordStepRequest) -> Step | None:
        """Record one step and any artifact it produced, or None if unknown."""
        key = str(session_id)
        now = _now()

        with self._lock:
            connection = self._connection
            session_row = connection.execute(
                "SELECT execution_mode FROM sessions WHERE session_id = ?", (key,)
            ).fetchone()

            if session_row is None:
                return None

            connection.execute("BEGIN")

            try:
                previous_row = connection.execute(
                    """
                    SELECT s.step_id
                    FROM steps s
                    JOIN artifacts a
                        ON a.session_id = s.session_id AND a.step_id = s.step_id
                    WHERE s.session_id = ? AND s.step_id <> ?
                    ORDER BY s.id DESC
                    LIMIT 1
                    """,
                    (key, step.step_id),
                ).fetchone()

                row = connection.execute(
                    """
                    INSERT INTO steps (session_id, step_id, agent_id, capability,
                                       task, status, duration_ms, error, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id, step_id) DO UPDATE
                    SET agent_id = excluded.agent_id,
                        capability = excluded.capability,
                        task = excluded.task,
                        status = excluded.status,
                        duration_ms = excluded.duration_ms,
                        error = excluded.error
                    RETURNING step_id, agent_id, capability, task, status,
                              duration_ms, error, created_at
                    """,
                    (
                        key,
                        step.step_id,
                        step.agent_id,
                        step.capability,
                        step.task,
                        step.status,
                        step.duration_ms,
                        step.error,
                        now,
                    ),
                ).fetchone()

                artifact_row = None

                if step.output is not None:
                    artifact_row = connection.execute(
                        """
                        INSERT INTO artifacts (session_id, step_id, payload, created_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (session_id, step_id) DO UPDATE
                        SET payload = excluded.payload
                        RETURNING id
                        """,
                        (key, step.step_id, json.dumps(step.output), now),
                    ).fetchone()

                if step.depends_on:
                    placeholders = ", ".join("?" for _ in step.depends_on)
                    connection.execute(
                        f"""
                        INSERT INTO lineage (session_id, produced_by_step_id,
                                             consumed_by_step_id, artifact_id,
                                             basis, created_at)
                        SELECT ?, a.step_id, ?, a.id, 'declared_dependency', ?
                        FROM artifacts a
                        WHERE a.session_id = ? AND a.step_id IN ({placeholders})
                        ON CONFLICT (session_id, produced_by_step_id,
                                     consumed_by_step_id) DO NOTHING
                        """,
                        (key, step.step_id, now, key, *step.depends_on),
                    )
                elif (
                    artifact_row is not None
                    and previous_row is not None
                    and session_row[0] in _ORDERED_EXECUTION_MODES
                ):
                    connection.execute(
                        """
                        INSERT INTO lineage (session_id, produced_by_step_id,
                                             consumed_by_step_id, artifact_id,
                                             basis, created_at)
                        VALUES (?, ?, ?, ?, 'execution_order', ?)
                        ON CONFLICT (session_id, produced_by_step_id,
                                     consumed_by_step_id) DO NOTHING
                        """,
                        (key, previous_row[0], step.step_id, artifact_row[0], now),
                    )

                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        return Step(
            step_id=row[0],
            agent_id=row[1],
            capability=row[2],
            task=row[3],
            status=row[4],
            duration_ms=row[5],
            error=row[6],
            output=step.output,
            created_at=row[7],
        )


def _to_session(row: Any) -> Session:
    """Build a `Session` from a row."""
    return Session(
        session_id=UUID(row[0]),
        request_text=row[1],
        status=row[2],
        workflow_id=row[3],
        execution_mode=row[4],
        error=row[5],
        answer=row[6],
        conversation_id=UUID(row[7]) if row[7] is not None else None,
        clarifications=json.loads(row[8]) if row[8] is not None else [],
        attachments=json.loads(row[9]) if row[9] is not None else [],
        created_at=row[10],
        updated_at=row[11],
    )


def _to_conversation(row: Any) -> Conversation:
    """Build a `Conversation` from a row."""
    return Conversation(
        conversation_id=UUID(row[0]),
        title=row[1],
        created_at=row[2],
        updated_at=row[3],
    )


def _to_step(row: Any) -> Step:
    """Build a `Step` from a row whose artifact payload is JSON text."""
    return Step(
        step_id=row[0],
        agent_id=row[1],
        capability=row[2],
        task=row[3],
        status=row[4],
        duration_ms=row[5],
        error=row[6],
        output=json.loads(row[7]) if row[7] is not None else None,
        created_at=row[8],
    )
