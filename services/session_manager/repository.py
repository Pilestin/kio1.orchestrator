import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
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

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Execution-order lineage is only meaningful when the plan actually ran in
# order. In parallel mode the recording order carries no dependency
# information, so no edges are inferred.
_ORDERED_EXECUTION_MODES = frozenset({"sequential", "mixed"})


class SessionRepository(Protocol):
    """Storage for sessions, steps, artifacts, and lineage."""

    def create_session(self, request_text: str) -> Session:
        """Open a new session and return it."""
        ...

    def get_session(self, session_id: UUID) -> SessionDetail | None:
        """Return a session with its steps and lineage, or None if unknown."""
        ...

    def update_session(
        self, session_id: UUID, update: UpdateSessionRequest
    ) -> Session | None:
        """Apply the non-null fields of an update, or None if unknown."""
        ...

    def record_step(self, session_id: UUID, step: RecordStepRequest) -> Step | None:
        """Record one step and any artifact it produced, or None if unknown."""
        ...


class InMemorySessionRepository:
    """Non-persistent repository used by tests and local runs without Postgres."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, Session] = {}
        self._steps: dict[UUID, list[Step]] = {}
        self._lineage: dict[UUID, list[LineageEdge]] = {}
        self._next_artifact_id = 1

    def create_session(self, request_text: str) -> Session:
        """Open a new session and return it."""
        now = datetime.now(timezone.utc)
        session = Session(
            session_id=uuid4(),
            request_text=request_text,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self._sessions[session.session_id] = session
        self._steps[session.session_id] = []
        self._lineage[session.session_id] = []

        return session

    def get_session(self, session_id: UUID) -> SessionDetail | None:
        """Return a session with its steps and lineage, or None if unknown."""
        session = self._sessions.get(session_id)

        if session is None:
            return None

        return SessionDetail(
            session=session,
            steps=list(self._steps[session_id]),
            lineage=list(self._lineage[session_id]),
        )

    def update_session(
        self, session_id: UUID, update: UpdateSessionRequest
    ) -> Session | None:
        """Apply the non-null fields of an update, or None if unknown."""
        session = self._sessions.get(session_id)

        if session is None:
            return None

        updated = session.model_copy(
            update={
                **update.model_dump(exclude_none=True),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._sessions[session_id] = updated

        return updated

    def record_step(self, session_id: UUID, step: RecordStepRequest) -> Step | None:
        """Record one step and any artifact it produced, or None if unknown."""
        session = self._sessions.get(session_id)

        if session is None:
            return None

        steps = self._steps[session_id]
        steps[:] = [existing for existing in steps if existing.step_id != step.step_id]

        recorded = Step(
            **step.model_dump(),
            created_at=datetime.now(timezone.utc),
        )
        previous = _last_step_with_output(steps)
        steps.append(recorded)

        if step.output is not None:
            artifact_id = self._next_artifact_id
            self._next_artifact_id += 1

            if previous is not None and session.execution_mode in (
                _ORDERED_EXECUTION_MODES
            ):
                self._lineage[session_id].append(
                    LineageEdge(
                        produced_by_step_id=previous.step_id,
                        consumed_by_step_id=step.step_id,
                        artifact_id=artifact_id,
                        basis="execution_order",
                    )
                )

        return recorded


class PostgresSessionRepository:
    """PostgreSQL-backed repository."""

    def __init__(self, pool: Any) -> None:
        """Store the connection pool.

        Args:
            pool: An open `psycopg_pool.ConnectionPool`. Typed loosely so
                importing this module does not require psycopg to be
                installed for callers that only use the in-memory
                repository.
        """
        self._pool = pool

    def apply_schema(self) -> None:
        """Create the tables if they do not exist yet."""
        statements = SCHEMA_PATH.read_text(encoding="utf-8")

        with self._pool.connection() as connection:
            connection.execute(statements)

        logger.info("Session Manager schema applied")

    def create_session(self, request_text: str) -> Session:
        """Open a new session and return it."""
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO sessions (session_id, request_text, status)
                VALUES (%s, %s, 'pending')
                RETURNING session_id, request_text, status, workflow_id,
                          execution_mode, error, created_at, updated_at
                """,
                (uuid4(), request_text),
            ).fetchone()

        return _to_session(row)

    def get_session(self, session_id: UUID) -> SessionDetail | None:
        """Return a session with its steps and lineage, or None if unknown."""
        with self._pool.connection() as connection:
            session_row = connection.execute(
                """
                SELECT session_id, request_text, status, workflow_id,
                       execution_mode, error, created_at, updated_at
                FROM sessions
                WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()

            if session_row is None:
                return None

            step_rows = connection.execute(
                """
                SELECT s.step_id, s.agent_id, s.capability, s.task, s.status,
                       s.duration_ms, s.error, a.payload, s.created_at
                FROM steps s
                LEFT JOIN artifacts a
                    ON a.session_id = s.session_id AND a.step_id = s.step_id
                WHERE s.session_id = %s
                ORDER BY s.id
                """,
                (session_id,),
            ).fetchall()

            lineage_rows = connection.execute(
                """
                SELECT produced_by_step_id, consumed_by_step_id, artifact_id, basis
                FROM lineage
                WHERE session_id = %s
                ORDER BY id
                """,
                (session_id,),
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

    def update_session(
        self, session_id: UUID, update: UpdateSessionRequest
    ) -> Session | None:
        """Apply the non-null fields of an update, or None if unknown."""
        changes = update.model_dump(exclude_none=True)

        if not changes:
            detail = self.get_session(session_id)
            return detail.session if detail is not None else None

        assignments = ", ".join(f"{column} = %s" for column in changes)
        parameters = [*changes.values(), session_id]

        with self._pool.connection() as connection:
            row = connection.execute(
                f"""
                UPDATE sessions
                SET {assignments}, updated_at = now()
                WHERE session_id = %s
                RETURNING session_id, request_text, status, workflow_id,
                          execution_mode, error, created_at, updated_at
                """,
                parameters,
            ).fetchone()

        return _to_session(row) if row is not None else None

    def record_step(self, session_id: UUID, step: RecordStepRequest) -> Step | None:
        """Record one step and any artifact it produced, or None if unknown."""
        with self._pool.connection() as connection:
            session_row = connection.execute(
                "SELECT execution_mode FROM sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()

            if session_row is None:
                return None

            execution_mode = session_row[0]

            previous_row = connection.execute(
                """
                SELECT s.step_id
                FROM steps s
                JOIN artifacts a
                    ON a.session_id = s.session_id AND a.step_id = s.step_id
                WHERE s.session_id = %s AND s.step_id <> %s
                ORDER BY s.id DESC
                LIMIT 1
                """,
                (session_id, step.step_id),
            ).fetchone()

            row = connection.execute(
                """
                INSERT INTO steps (session_id, step_id, agent_id, capability,
                                   task, status, duration_ms, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, step_id) DO UPDATE
                SET agent_id = EXCLUDED.agent_id,
                    capability = EXCLUDED.capability,
                    task = EXCLUDED.task,
                    status = EXCLUDED.status,
                    duration_ms = EXCLUDED.duration_ms,
                    error = EXCLUDED.error
                RETURNING step_id, agent_id, capability, task, status,
                          duration_ms, error, created_at
                """,
                (
                    session_id,
                    step.step_id,
                    step.agent_id,
                    step.capability,
                    step.task,
                    step.status,
                    step.duration_ms,
                    step.error,
                ),
            ).fetchone()

            if step.output is None:
                return _to_step((*row[:7], None, row[7]))

            artifact_row = connection.execute(
                """
                INSERT INTO artifacts (session_id, step_id, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id, step_id) DO UPDATE
                SET payload = EXCLUDED.payload
                RETURNING id
                """,
                (session_id, step.step_id, json.dumps(step.output)),
            ).fetchone()

            if previous_row is not None and execution_mode in _ORDERED_EXECUTION_MODES:
                connection.execute(
                    """
                    INSERT INTO lineage (session_id, produced_by_step_id,
                                         consumed_by_step_id, artifact_id, basis)
                    VALUES (%s, %s, %s, %s, 'execution_order')
                    ON CONFLICT (session_id, produced_by_step_id,
                                 consumed_by_step_id) DO NOTHING
                    """,
                    (session_id, previous_row[0], step.step_id, artifact_row[0]),
                )

        return _to_step((*row[:7], step.output, row[7]))


def _last_step_with_output(steps: list[Step]) -> Step | None:
    """Return the most recently recorded step that produced an artifact.

    Args:
        steps: Steps recorded so far, in recording order.

    Returns:
        The last step carrying an output, or None when there is none.
    """
    for step in reversed(steps):
        if step.output is not None:
            return step

    return None


def _to_session(row: Any) -> Session:
    """Build a `Session` from a database row."""
    return Session(
        session_id=row[0],
        request_text=row[1],
        status=row[2],
        workflow_id=row[3],
        execution_mode=row[4],
        error=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _to_step(row: Any) -> Step:
    """Build a `Step` from a database row."""
    return Step(
        step_id=row[0],
        agent_id=row[1],
        capability=row[2],
        task=row[3],
        status=row[4],
        duration_ms=row[5],
        error=row[6],
        output=row[7],
        created_at=row[8],
    )
