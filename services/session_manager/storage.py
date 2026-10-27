"""Choosing where sessions are stored, and using that store without a server.

Two things live here because both answer "where does a run's history go":

- `open_repository` turns a database URL into a repository. The Session
  Manager service uses it, and so does KIO1 itself when no service is set.
- `EmbeddedSessionClient` is the Session Manager without the HTTP in
  between: the same calls the web service makes to a remote Session Manager,
  answered by a repository in this process. One machine, one program, no
  second service to start — which is what a first run should be.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from core.session_client import SessionClientError
from services.session_manager.models import RecordStepRequest, UpdateSessionRequest
from services.session_manager.repository import (
    InMemorySessionRepository,
    PostgresSessionRepository,
    SessionRepository,
    UnknownConversationError,
    conversation_detail,
)
from services.session_manager.sqlite_repository import SqliteSessionRepository

logger = logging.getLogger(__name__)

SQLITE_PREFIX = "sqlite:///"

# The store, as a URL. Read by the Session Manager service, and by KIO1 when
# it keeps its own history.
DATABASE_URL_ENV = "KIO1_DATABASE_URL"

# Where a single machine keeps its history when nothing else is configured.
DEFAULT_DATABASE_PATH = Path("data") / "kio1.db"
DEFAULT_DATABASE_URL = f"{SQLITE_PREFIX}{DEFAULT_DATABASE_PATH.as_posix()}"

# What a run that was cut off by a stop says, in the console and to the
# reviewer of a later request in the same conversation.
INTERRUPTED_REASON = "KIO1 stopped while this request was running."

# How many of the newest runs are checked for ones left unfinished.
_INTERRUPTED_SCAN = 200


def open_repository(database_url: str) -> tuple[SessionRepository, Callable[[], None]]:
    """Open the store a database URL names.

    Args:
        database_url: `sqlite:///path/to/file.db`, a PostgreSQL URL, or empty
            for in-memory storage that is lost on restart.

    Returns:
        The repository, and a function that releases what it holds.

    Raises:
        RuntimeError: If a PostgreSQL URL is given but psycopg is not
            installed.
    """
    url = database_url.strip()

    if not url:
        return InMemorySessionRepository(), lambda: None

    if url.startswith(SQLITE_PREFIX):
        sqlite = SqliteSessionRepository(url[len(SQLITE_PREFIX) :] or ":memory:")
        return sqlite, sqlite.close

    try:
        from psycopg_pool import ConnectionPool
    except ImportError as error:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "A PostgreSQL database URL is set but psycopg is not installed. "
            "Install psycopg[binary,pool], or use sqlite:///data/kio1.db."
        ) from error

    pool = ConnectionPool(url, min_size=1, max_size=8, open=True)
    postgres = PostgresSessionRepository(pool)
    postgres.apply_schema()
    logger.info("Session storage: PostgreSQL")

    return postgres, pool.close


class EmbeddedSessionClient:
    """The Session Manager's client interface, served from this process.

    Mirrors `SessionClient` exactly, including which calls raise and which
    only log, so the web service cannot tell the difference: opening a
    session and reading one raise `SessionClientError`; recording progress
    never interrupts a workflow that is already running.
    """

    def __init__(
        self, repository: SessionRepository, close: Callable[[], None] = lambda: None
    ) -> None:
        """Wrap a repository.

        Args:
            repository: Where sessions are stored.
            close: Releases the repository's resources.
        """
        self._repository = repository
        self._close = close

    def close(self) -> None:
        """Release the store."""
        self._close()

    def close_interrupted(self, reason: str = INTERRUPTED_REASON) -> int:
        """Mark the runs a previous process left unfinished as failed.

        Only this process runs what this store records, so at startup
        anything still `pending` or `running` belongs to a process that is
        gone: it will never finish, and left alone it shows "working…"
        forever. A remote Session Manager may serve several KIO1 instances,
        which is why this lives here and not in the service.

        Orphans are always among the newest runs — a stop leaves at most the
        few that were in flight — so the recent list is enough to find them.

        Args:
            reason: Recorded as each run's error.

        Returns:
            How many runs were closed.
        """
        try:
            recent = self._repository.list_sessions(_INTERRUPTED_SCAN)
        except Exception:
            logger.warning("Could not look for interrupted runs", exc_info=True)
            return 0

        unfinished = [
            session for session in recent if session.status in ("pending", "running")
        ]

        for session in unfinished:
            self.update_session(session.session_id, status="failed", error=reason)

        if unfinished:
            logger.info(
                "Closed %d run(s) a previous stop left unfinished", len(unfinished)
            )

        return len(unfinished)

    def create_session(
        self, request_text: str, conversation_id: UUID | None = None
    ) -> UUID:
        """Open a session and return its identifier."""
        try:
            return self._repository.create_session(
                request_text, conversation_id
            ).session_id
        except UnknownConversationError as error:
            raise SessionClientError(
                f"No conversation {conversation_id}", status=404
            ) from error
        except Exception as error:
            raise SessionClientError(f"Could not open a session: {error}") from error

    def create_conversation(self) -> dict[str, Any]:
        """Open an empty conversation."""
        try:
            conversation = self._repository.create_conversation()
        except Exception as error:
            raise SessionClientError(
                f"Could not open a conversation: {error}"
            ) from error

        return conversation.model_dump(mode="json")

    def list_conversations(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return the conversations most recently added to, newest first."""
        return [
            conversation.model_dump(mode="json")
            for conversation in self._repository.list_conversations(limit)
        ]

    def get_conversation(self, conversation_id: UUID) -> dict[str, Any]:
        """Return a conversation with each of its runs, as JSON-ready data."""
        detail = conversation_detail(self._repository, conversation_id)

        if detail is None:
            raise SessionClientError(f"No conversation {conversation_id}", status=404)

        return detail.model_dump(mode="json")

    def delete_conversation(self, conversation_id: UUID) -> bool:
        """Delete a conversation and every run in it; False if unknown."""
        return self._repository.delete_conversation(conversation_id)

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        """Record progress on a session, logging rather than raising."""
        body = {key: value for key, value in fields.items() if value is not None}

        if not body:
            return

        try:
            self._repository.update_session(session_id, UpdateSessionRequest(**body))
        except Exception:
            logger.warning("Could not update session %s", session_id, exc_info=True)

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Record one step outcome, logging rather than raising."""
        try:
            self._repository.record_step(session_id, RecordStepRequest(**step))
        except Exception:
            logger.warning(
                "Could not record step %s of session %s",
                step.get("step_id"),
                session_id,
                exc_info=True,
            )

    def get_session(self, session_id: UUID) -> dict[str, Any]:
        """Return a session with its steps and lineage, as JSON-ready data."""
        detail = self._repository.get_session(session_id)

        if detail is None:
            raise SessionClientError(f"No session {session_id}")

        return detail.model_dump(mode="json")

    def list_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return the most recent sessions, newest first."""
        return [
            session.model_dump(mode="json")
            for session in self._repository.list_sessions(limit)
        ]
