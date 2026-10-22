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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from services.session_manager.models import (
    LineageEdge,
    RecordStepRequest,
    Session,
    SessionDetail,
    Step,
    UpdateSessionRequest,
)

logger = logging.getLogger(__name__)

_ORDERED_EXECUTION_MODES = frozenset({"sequential", "mixed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    request_text   TEXT NOT NULL,
    status         TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    workflow_id    TEXT,
    execution_mode TEXT,
    error          TEXT,
    answer         TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
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
    "answer, created_at, updated_at"
)


def _now() -> str:
    """Return the current time as sortable ISO-8601 text in UTC."""
    return datetime.now(timezone.utc).isoformat()


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
        """
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(sessions)")
        }

        if "answer" not in columns:
            self._connection.execute("ALTER TABLE sessions ADD COLUMN answer TEXT")
            logger.info("Session storage migrated: sessions.answer added")

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._connection.close()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        """Run one statement and return its rows."""
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def create_session(self, request_text: str) -> Session:
        """Open a new session and return it."""
        now = _now()
        rows = self._query(
            f"""
            INSERT INTO sessions (session_id, request_text, status,
                                  created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            RETURNING {_SESSION_COLUMNS}
            """,
            (str(uuid4()), request_text, now, now),
        )

        return _to_session(rows[0])

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
        changes = update.model_dump(exclude_none=True)

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
        created_at=row[7],
        updated_at=row[8],
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
