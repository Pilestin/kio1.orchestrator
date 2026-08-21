import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status

from core.config_loader import load_config
from core.service_telemetry import (
    TraceContextMiddleware,
    init_service_tracing,
    shutdown_service_tracing,
)
from services.session_manager.models import (
    CreateSessionRequest,
    RecordStepRequest,
    Session,
    SessionDetail,
    Step,
    UpdateSessionRequest,
)
from services.session_manager.repository import (
    InMemorySessionRepository,
    PostgresSessionRepository,
    SessionRepository,
)

logger = logging.getLogger(__name__)

DATABASE_URL_ENV = "KIO1_DATABASE_URL"

_repository: SessionRepository | None = None
_pool: Any = None


def _build_repository() -> tuple[SessionRepository, Any]:
    """Create the repository the service will use.

    Returns:
        The repository and the connection pool backing it, if any. Without
        `KIO1_DATABASE_URL` the service falls back to in-memory storage so it
        can be run locally without Postgres; that storage is lost on restart.

    Raises:
        RuntimeError: If a database URL is set but psycopg is not installed.
    """
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()

    if not database_url:
        logger.warning(
            "%s is not set; using in-memory storage. Sessions will not "
            "survive a restart.",
            DATABASE_URL_ENV,
        )
        return InMemorySessionRepository(), None

    try:
        from psycopg_pool import ConnectionPool
    except ImportError as error:  # pragma: no cover - depends on environment
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is set but psycopg is not installed. "
            "Install psycopg[binary,pool] or unset the variable."
        ) from error

    pool = ConnectionPool(database_url, min_size=1, max_size=8, open=True)
    repository = PostgresSessionRepository(pool)
    repository.apply_schema()

    logger.info("Session Manager connected to PostgreSQL")

    return repository, pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open storage and tracing on startup, close both on shutdown."""
    global _pool
    global _repository

    _start_tracing()
    _repository, _pool = _build_repository()

    try:
        yield
    finally:
        if _pool is not None:
            _pool.close()
            _pool = None

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
    """Open a session for one workflow run."""
    session = repository.create_session(request.request_text)
    logger.info("Session created: session_id=%s", session.session_id)

    return session


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
