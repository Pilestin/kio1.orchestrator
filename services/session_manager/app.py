import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Response, status

from core.config_loader import load_config
from core.service_telemetry import (
    TraceContextMiddleware,
    init_service_tracing,
    shutdown_service_tracing,
)
from services.session_manager.models import (
    Conversation,
    ConversationDetail,
    CreateConversationRequest,
    CreateSessionRequest,
    RecordStepRequest,
    Session,
    SessionDetail,
    Step,
    UpdateSessionRequest,
)
from services.session_manager.repository import (
    SessionRepository,
    UnknownConversationError,
    conversation_detail,
)
from services.session_manager.storage import DATABASE_URL_ENV, open_repository

logger = logging.getLogger(__name__)


_repository: SessionRepository | None = None
_close: Callable[[], None] | None = None


def _build_repository() -> tuple[SessionRepository, Callable[[], None]]:
    """Create the repository the service will use.

    Returns:
        The repository and a function releasing it. Without
        `KIO1_DATABASE_URL` the service falls back to in-memory storage so it
        can be run locally without a database; that storage is lost on
        restart. `sqlite:///path` keeps history in a file with no server.

    Raises:
        RuntimeError: If a PostgreSQL URL is set but psycopg is not installed.
    """
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()

    if not database_url:
        logger.warning(
            "%s is not set; using in-memory storage. Sessions will not "
            "survive a restart.",
            DATABASE_URL_ENV,
        )

    return open_repository(database_url)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open storage and tracing on startup, close both on shutdown."""
    global _close
    global _repository

    _start_tracing()
    _repository, _close = _build_repository()

    try:
        yield
    finally:
        if _close is not None:
            _close()
            _close = None

        _repository = None
        shutdown_service_tracing()


def _start_tracing() -> None:
    """Initialize trace export, reusing the orchestrator's telemetry settings.

    A missing or unreadable `config.json` must not stop the service from
    serving: telemetry is an aid, not a dependency.

    Returns:
        None.
    """
    try:
        config = load_config()
    except (OSError, ValueError):
        logger.warning(
            "Could not read config.json; running without tracing", exc_info=True
        )
        return

    init_service_tracing(
        config.telemetry,
        service_name=f"{config.telemetry.kio_id}-session-manager",
    )


app = FastAPI(
    title="KIO1 Session Manager",
    version="1.0.0",
    summary="Records workflow sessions, step results, artifacts, and lineage.",
    lifespan=lifespan,
)
# Continues the caller's trace, so this service's work is part of the same
# end-to-end path rather than an unconnected trace of its own.
app.add_middleware(TraceContextMiddleware, service_name="session-manager")


def get_repository() -> Iterator[SessionRepository]:
    """Yield the active repository.

    Yields:
        The repository created at startup.

    Raises:
        HTTPException: If storage is not available, which means the service
            is being called before startup finished or after shutdown began.
    """
    if _repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is not available",
        )

    yield _repository


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok", "service": "session-manager"}


@app.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(
    request: CreateSessionRequest,
    repository: SessionRepository = Depends(get_repository),
) -> Session:
    """Open a session for one workflow run.

    It joins the conversation the request names, or opens one of its own.
    """
    try:
        session = repository.create_session(
            request.request_text, request.conversation_id
        )
    except UnknownConversationError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown conversation: {request.conversation_id}",
        ) from error

    logger.info("Session created: session_id=%s", session.session_id)

    return session


@app.post("/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(
    request: CreateConversationRequest,
    repository: SessionRepository = Depends(get_repository),
) -> Conversation:
    """Open an empty conversation."""
    return repository.create_conversation(request.title)


@app.get("/conversations")
def list_conversations(
    limit: int = 25,
    repository: SessionRepository = Depends(get_repository),
) -> list[Conversation]:
    """Return the conversations most recently added to, newest first.

    Args:
        limit: How many to return, clamped as for sessions.
        repository: Where conversations are stored.

    Returns:
        Conversations without their runs.
    """
    return repository.list_conversations(max(1, min(limit, 200)))


@app.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: UUID,
    repository: SessionRepository = Depends(get_repository),
) -> ConversationDetail:
    """Return a conversation with each of its runs, oldest first."""
    detail = conversation_detail(repository, conversation_id)

    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown conversation: {conversation_id}",
        )

    return detail


@app.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: UUID,
    repository: SessionRepository = Depends(get_repository),
) -> Response:
    """Delete a conversation and every run in it."""
    if not repository.delete_conversation(conversation_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown conversation: {conversation_id}",
        )

    logger.info("Conversation deleted: conversation_id=%s", conversation_id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/sessions")
def list_sessions(
    limit: int = 25,
    repository: SessionRepository = Depends(get_repository),
) -> list[Session]:
    """Return the most recent sessions, newest first.

    Args:
        limit: How many to return, clamped so one call cannot pull the whole
            table.
        repository: Where sessions are stored.

    Returns:
        Recent sessions without their steps.
    """
    return repository.list_sessions(max(1, min(limit, 200)))


@app.get("/sessions/{session_id}")
def get_session(
    session_id: UUID,
    repository: SessionRepository = Depends(get_repository),
) -> SessionDetail:
    """Return a session with its steps and lineage."""
    detail = repository.get_session(session_id)

    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown session: {session_id}",
        )

    return detail


@app.patch("/sessions/{session_id}")
def update_session(
    session_id: UUID,
    request: UpdateSessionRequest,
    repository: SessionRepository = Depends(get_repository),
) -> Session:
    """Record progress on a session."""
    session = repository.update_session(session_id, request)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown session: {session_id}",
        )

    return session


@app.post("/sessions/{session_id}/steps", status_code=status.HTTP_201_CREATED)
def record_step(
    session_id: UUID,
    request: RecordStepRequest,
    repository: SessionRepository = Depends(get_repository),
) -> Step:
    """Record the outcome of one workflow step."""
    step = repository.record_step(session_id, request)

    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown session: {session_id}",
        )

    logger.info(
        "Step recorded: session_id=%s step_id=%s status=%s",
        session_id,
        step.step_id,
        step.status,
    )

    return step
