import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from services.session_manager.models import (
    Conversation,
    ConversationDetail,
    LineageEdge,
    RecordStepRequest,
    Session,
    SessionDetail,
    Step,
    UpdateSessionRequest,
    conversation_title,
)

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Execution-order lineage is only meaningful when the plan actually ran in
# order. In parallel mode the recording order carries no dependency
# information, so no edges are inferred.
_ORDERED_EXECUTION_MODES = frozenset({"sequential", "mixed"})

_SESSION_COLUMNS = (
    "session_id, request_text, status, workflow_id, execution_mode, error, "
    "answer, conversation_id, clarifications, attachments, created_at, updated_at"
)

_CONVERSATION_COLUMNS = "conversation_id, title, created_at, updated_at"


class UnknownConversationError(LookupError):
    """A session was opened in a conversation that does not exist."""


class SessionRepository(Protocol):
    """Storage for conversations, sessions, steps, artifacts, and lineage."""

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> Session:
        """Open a new session and return it.

        Without a conversation, one is opened for the session. A conversation
        still without a title takes it from this request.

        Raises:
            UnknownConversationError: If the conversation does not exist.
        """
        ...

    def create_conversation(self, title: str = "") -> Conversation:
        """Open an empty conversation and return it."""
        ...

    def get_conversation(self, conversation_id: UUID) -> Conversation | None:
        """Return a conversation, or None if unknown."""
        ...

    def list_conversations(self, limit: int) -> list[Conversation]:
        """Return the conversations most recently added to, newest first."""
        ...

    def conversation_sessions(self, conversation_id: UUID) -> list[Session]:
        """Return a conversation's sessions in the order they were opened."""
        ...

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Delete a conversation and its sessions; False if it was unknown."""
        ...

    def get_session(self, session_id: UUID) -> SessionDetail | None:
        """Return a session with its steps and lineage, or None if unknown."""
        ...

    def list_sessions(self, limit: int) -> list[Session]:
        """Return the most recent sessions, newest first."""
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
        self._conversations: dict[UUID, Conversation] = {}
        self._sessions: dict[UUID, Session] = {}
        self._steps: dict[UUID, list[Step]] = {}
        self._lineage: dict[UUID, list[LineageEdge]] = {}
        self._artifacts: dict[UUID, dict[str, int]] = {}
        self._next_artifact_id = 1

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> Session:
        """Open a new session and return it. See `SessionRepository`."""
        now = datetime.now(timezone.utc)

        if conversation_id is None:
            conversation = self.create_conversation()
        else:
            found = self._conversations.get(conversation_id)

            if found is None:
                raise UnknownConversationError(str(conversation_id))

            conversation = found

        # Re-inserted, not reassigned, so insertion order — the tie-break in
        # `list_conversations` — also puts it last.
        self._conversations.pop(conversation.conversation_id, None)
        self._conversations[conversation.conversation_id] = conversation.model_copy(
            update={
                "title": conversation.title or conversation_title(request_text),
                "updated_at": now,
            }
        )
        session = Session(
            session_id=uuid4(),
            request_text=request_text,
            status="pending",
            conversation_id=conversation.conversation_id,
            created_at=now,
            updated_at=now,
        )
        self._sessions[session.session_id] = session
        self._steps[session.session_id] = []
        self._lineage[session.session_id] = []
        self._artifacts[session.session_id] = {}

        return session

    def create_conversation(self, title: str = "") -> Conversation:
        """Open an empty conversation and return it."""
        now = datetime.now(timezone.utc)
        conversation = Conversation(
            conversation_id=uuid4(), title=title, created_at=now, updated_at=now
        )
        self._conversations[conversation.conversation_id] = conversation

        return conversation

    def get_conversation(self, conversation_id: UUID) -> Conversation | None:
        """Return a conversation, or None if unknown."""
        return self._conversations.get(conversation_id)

    def list_conversations(self, limit: int) -> list[Conversation]:
        """Return the conversations most recently added to, newest first."""
        # Insertion order breaks ties, as in `list_sessions`.
        ordered = sorted(
            enumerate(self._conversations.values()),
            key=lambda item: (item[1].updated_at, item[0]),
            reverse=True,
        )

        return [conversation for _, conversation in ordered[:limit]]

    def conversation_sessions(self, conversation_id: UUID) -> list[Session]:
        """Return a conversation's sessions in the order they were opened."""
        return [
            session
            for session in self._sessions.values()
            if session.conversation_id == conversation_id
        ]

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Delete a conversation and its sessions; False if it was unknown."""
        if self._conversations.pop(conversation_id, None) is None:
            return False

        for session in self.conversation_sessions(conversation_id):
            key = session.session_id
            del self._sessions[key], self._steps[key], self._lineage[key]
            del self._artifacts[key]

        return True

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

    def list_sessions(self, limit: int) -> list[Session]:
        """Return the most recent sessions, newest first.

        Args:
            limit: How many to return.

        Returns:
            Sessions ordered by when they were opened, newest first. Steps are
            left out: this is the list a console renders, and each row is
            opened separately for its detail.
        """
        # Two sessions opened in the same instant carry the same timestamp, so
        # insertion order is part of the key rather than left to sort
        # stability: without it the older of the two comes back first.
        ordered = sorted(
            enumerate(self._sessions.values()),
            key=lambda item: (item[1].created_at, item[0]),
            reverse=True,
        )

        return [session for _, session in ordered[:limit]]

    def update_session(
        self, session_id: UUID, update: UpdateSessionRequest
    ) -> Session | None:
        """Apply the non-null fields of an update, or None if unknown."""
        session = self._sessions.get(session_id)

        if session is None:
            return None

        changes = update.model_dump(exclude_none=True)

        # `model_copy` does not validate, so the models go in, not their dumps.
        if update.clarifications is not None:
            changes["clarifications"] = list(update.clarifications)

        if update.attachments is not None:
            changes["attachments"] = list(update.attachments)

        updated = session.model_copy(
            update={**changes, "updated_at": datetime.now(timezone.utc)}
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
            self._artifacts[session_id][step.step_id] = artifact_id

        edges = self._lineage[session_id]

        if step.depends_on:
            # The plan said what this step consumed, so no inference is needed.
            for dependency in step.depends_on:
                upstream_artifact = self._artifacts[session_id].get(dependency)

                if upstream_artifact is None:
                    continue

                edges.append(
                    LineageEdge(
                        produced_by_step_id=dependency,
                        consumed_by_step_id=step.step_id,
                        artifact_id=upstream_artifact,
                        basis="declared_dependency",
                    )
                )
        elif (
            step.output is not None
            and previous is not None
            and session.execution_mode in _ORDERED_EXECUTION_MODES
        ):
            edges.append(
                LineageEdge(
                    produced_by_step_id=previous.step_id,
                    consumed_by_step_id=step.step_id,
                    artifact_id=self._artifacts[session_id][step.step_id],
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
        """Create the tables if they do not exist yet.

        Sessions recorded before conversations existed are each given one of
        their own, so an upgraded history still shows every run.
        """
        statements = SCHEMA_PATH.read_text(encoding="utf-8")

        with self._pool.connection() as connection:
            connection.execute(statements)
            orphans = connection.execute("""
                SELECT session_id, request_text, created_at, updated_at
                FROM sessions
                WHERE conversation_id IS NULL
                """).fetchall()

            for session_id, request_text, created_at, updated_at in orphans:
                conversation_id = uuid4()
                connection.execute(
                    """
                    INSERT INTO conversations (conversation_id, title,
                                               created_at, updated_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        conversation_id,
                        conversation_title(request_text),
                        created_at,
                        updated_at,
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET conversation_id = %s WHERE session_id = %s",
                    (conversation_id, session_id),
                )

        logger.info("Session Manager schema applied")

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> Session:
        """Open a new session and return it. See `SessionRepository`."""
        title = conversation_title(request_text)

        with self._pool.connection() as connection:
            if conversation_id is None:
                conversation_id = uuid4()
                connection.execute(
                    "INSERT INTO conversations (conversation_id, title) "
                    "VALUES (%s, %s)",
                    (conversation_id, title),
                )
            else:
                touched = connection.execute(
                    """
                    UPDATE conversations
                    SET title = CASE WHEN title = '' THEN %s ELSE title END,
                        updated_at = now()
                    WHERE conversation_id = %s
                    RETURNING conversation_id
                    """,
                    (title, conversation_id),
                ).fetchone()

                if touched is None:
                    raise UnknownConversationError(str(conversation_id))

            row = connection.execute(
                f"""
                INSERT INTO sessions (session_id, request_text, status,
                                      conversation_id)
                VALUES (%s, %s, 'pending', %s)
                RETURNING {_SESSION_COLUMNS}
                """,
                (uuid4(), request_text, conversation_id),
            ).fetchone()

        return _to_session(row)

    def create_conversation(self, title: str = "") -> Conversation:
        """Open an empty conversation and return it."""
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""
                INSERT INTO conversations (conversation_id, title)
                VALUES (%s, %s)
                RETURNING {_CONVERSATION_COLUMNS}
                """,
                (uuid4(), title),
            ).fetchone()

        return _to_conversation(row)

    def get_conversation(self, conversation_id: UUID) -> Conversation | None:
        """Return a conversation, or None if unknown."""
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM conversations "
                "WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()

        return _to_conversation(row) if row is not None else None

    def list_conversations(self, limit: int) -> list[Conversation]:
        """Return the conversations most recently added to, newest first."""
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_CONVERSATION_COLUMNS}
                FROM conversations
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

        return [_to_conversation(row) for row in rows]

    def conversation_sessions(self, conversation_id: UUID) -> list[Session]:
        """Return a conversation's sessions in the order they were opened."""
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_SESSION_COLUMNS}
                FROM sessions
                WHERE conversation_id = %s
                ORDER BY created_at
                """,
                (conversation_id,),
            ).fetchall()

        return [_to_session(row) for row in rows]

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Delete a conversation and its sessions; False if it was unknown."""
        with self._pool.connection() as connection:
            # Sessions go with it by `ON DELETE CASCADE`, and their steps,
            # artifacts, and lineage with them.
            row = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = %s "
                "RETURNING conversation_id",
                (conversation_id,),
            ).fetchone()

        return row is not None

    def get_session(self, session_id: UUID) -> SessionDetail | None:
        """Return a session with its steps and lineage, or None if unknown."""
        with self._pool.connection() as connection:
            session_row = connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = %s",
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

    def list_sessions(self, limit: int) -> list[Session]:
        """Return the most recent sessions, newest first.

        Args:
            limit: How many to return.

        Returns:
            Sessions ordered by when they were opened, newest first. Steps are
            left out: this is the list a console renders, and each row is
            opened separately for its detail.
        """
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_SESSION_COLUMNS}
                FROM sessions
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

        return [_to_session(row) for row in rows]

    def update_session(
        self, session_id: UUID, update: UpdateSessionRequest
    ) -> Session | None:
        """Apply the non-null fields of an update, or None if unknown."""
        changes = storable_changes(update)

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
                RETURNING {_SESSION_COLUMNS}
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

            artifact_row = None

            if step.output is not None:
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

            if step.depends_on:
                # The plan said what this step consumed, so no inference is
                # needed. Only dependencies that actually left an artifact can
                # be linked.
                connection.execute(
                    """
                    INSERT INTO lineage (session_id, produced_by_step_id,
                                         consumed_by_step_id, artifact_id, basis)
                    SELECT %s, a.step_id, %s, a.id, 'declared_dependency'
                    FROM artifacts a
                    WHERE a.session_id = %s AND a.step_id = ANY(%s)
                    ON CONFLICT (session_id, produced_by_step_id,
                                 consumed_by_step_id) DO NOTHING
                    """,
                    (
                        session_id,
                        step.step_id,
                        session_id,
                        list(step.depends_on),
                    ),
                )
            elif (
                artifact_row is not None
                and previous_row is not None
                and execution_mode in _ORDERED_EXECUTION_MODES
            ):
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


def conversation_detail(
    repository: SessionRepository, conversation_id: UUID
) -> ConversationDetail | None:
    """Return a conversation with each of its runs, or None if unknown.

    Built from the repository's own calls, so every implementation answers
    alike; a conversation is a handful of runs, and one read per run costs
    nothing worth a query of its own.
    """
    conversation = repository.get_conversation(conversation_id)

    if conversation is None:
        return None

    details = (
        repository.get_session(session.session_id)
        for session in repository.conversation_sessions(conversation_id)
    )

    return ConversationDetail(
        conversation=conversation,
        sessions=[detail for detail in details if detail is not None],
    )


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


def storable_changes(update: UpdateSessionRequest) -> dict[str, Any]:
    """Return an update's non-null fields as column values.

    The questions and answers, and the attached files, are JSON columns,
    so they go in as JSON text; every other field is stored as it is.
    Shared with the SQLite repository, whose columns hold the same text.
    """
    changes = update.model_dump(exclude_none=True)

    for column in ("clarifications", "attachments"):
        if column in changes:
            changes[column] = json.dumps(changes[column])

    return changes


def _to_session(row: Any) -> Session:
    """Build a `Session` from a database row."""
    return Session(
        session_id=row[0],
        request_text=row[1],
        status=row[2],
        workflow_id=row[3],
        execution_mode=row[4],
        error=row[5],
        answer=row[6],
        conversation_id=row[7],
        clarifications=row[8] or [],
        attachments=row[9] or [],
        created_at=row[10],
        updated_at=row[11],
    )


def _to_conversation(row: Any) -> Conversation:
    """Build a `Conversation` from a database row."""
    return Conversation(
        conversation_id=row[0], title=row[1], created_at=row[2], updated_at=row[3]
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
