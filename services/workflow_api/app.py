import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config_loader import load_config
from core.prompt_loader import load_prompt
from core.service_telemetry import TraceContextMiddleware
from core.session_client import NullSessionClient, SessionClient, SessionClientError
from core.telemetry import init_telemetry, shutdown_telemetry, trace_provider_preload
from providers.provider_client import load_provider
from services.workflow_api.runner import WorkflowRunner, build_runner

logger = logging.getLogger(__name__)

SESSION_MANAGER_URL_ENV = "KIO1_SESSION_MANAGER_URL"


class RunWorkflowRequest(BaseModel):
    """A natural-language software-development request."""

    request: str = Field(min_length=1, max_length=100_000)


class RunWorkflowResponse(BaseModel):
    """Acknowledges that a workflow was accepted for execution."""

    session_id: UUID
    status: str
    status_url: str


@dataclass
class _State:
    """Everything resolved once at startup and reused per request."""

    runner: WorkflowRunner
    session_client: SessionClient | NullSessionClient


_state: _State | None = None


def _build_state() -> _State:
    """Load configuration, the prompt, and the provider, then assemble a runner.

    Returns:
        The service state.
    """
    config = load_config()
    init_telemetry(config.telemetry, llm=config.model)

    system_prompt = load_prompt(config.prompt_path)
    provider = load_provider(config.provider, config.allowed_providers)
    provider_client = provider.create_client(config)

    with trace_provider_preload(provider=config.provider, model=config.model):
        provider.preload(config, provider_client)

    session_manager_url = os.environ.get(SESSION_MANAGER_URL_ENV, "").strip()

    session_client: SessionClient | NullSessionClient
    if session_manager_url:
        session_client = SessionClient(session_manager_url)
        logger.info("Session Manager at %s", session_manager_url)
    else:
        session_client = NullSessionClient()
        logger.warning(
            "%s is not set; sessions will not be recorded and status lookups "
            "will fail",
            SESSION_MANAGER_URL_ENV,
        )

    return _State(
        runner=build_runner(
            config=config,
            system_prompt=system_prompt,
            provider=provider,
            provider_client=provider_client,
            recorder=session_client,
        ),
        session_client=session_client,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare the provider and telemetry on startup, flush them on shutdown."""
    global _state

    _state = _build_state()

    try:
        yield
    finally:
        state, _state = _state, None

        if state is not None:
            state.runner.transport.close()

        shutdown_telemetry()


app = FastAPI(
    title="KIO1 Workflow API",
    version="1.0.0",
    summary="Accepts software-development requests and orchestrates KIO agents.",
    lifespan=lifespan,
)
# Records the incoming request as the root of the trace, so the work the
# background runner does hangs off something rather than floating alone.
app.add_middleware(TraceContextMiddleware, service_name="workflow-api")


def get_state() -> _State:
    """Return the service state.

    Exposed as a dependency rather than read directly, so tests can supply
    their own state without starting a provider.

    Returns:
        The state built at startup.

    Raises:
        HTTPException: If the service is not ready.
    """
    if _state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow API is not ready",
        )

    return _state


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok", "service": "workflow-api"}


@app.post(
    "/workflow/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunWorkflowResponse,
)
def run_workflow(
    body: RunWorkflowRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    state: _State = Depends(get_state),
) -> RunWorkflowResponse:
    """Accept a request, open a session, and execute the workflow in the background.

    The session is opened before responding so the caller always receives an
    identifier it can poll, matching the platform's 202-with-session-id
    contract.

    Args:
        body: The natural-language request.
        background_tasks: Used to run the workflow after the response is sent.
        request: Used to build the absolute status URL.
        state: The provider, runner, and Session Manager client.

    Returns:
        The session identifier and where to poll for progress.

    Raises:
        HTTPException: If a session could not be opened.
    """
    try:
        session_id = state.session_client.create_session(body.request)
    except SessionClientError as error:
        logger.error("Could not open a session: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable, so the request was not accepted",
        ) from error

    background_tasks.add_task(state.runner.run, session_id, body.request)

    logger.info("Workflow accepted: session_id=%s", session_id)

    return RunWorkflowResponse(
        session_id=session_id,
        status="pending",
        status_url=str(request.url_for("get_workflow", session_id=session_id)),
    )


@app.get("/workflow/{session_id}", name="get_workflow")
def get_workflow(
    session_id: UUID,
    state: _State = Depends(get_state),
) -> JSONResponse:
    """Return everything recorded for one workflow run.

    Args:
        session_id: The session to read.
        state: The Session Manager client.

    Returns:
        The Session Manager's record for this session, passed through.

    Raises:
        HTTPException: If the session is unknown or unreachable.
    """
    try:
        detail: dict[str, Any] = state.session_client.get_session(session_id)
    except SessionClientError as error:
        logger.warning("Session lookup failed: session_id=%s", session_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No record for session {session_id}",
        ) from error

    return JSONResponse(content=detail)
