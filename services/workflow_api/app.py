import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hmac import compare_digest
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
from dispatch.directory import ORIGIN_REGISTRATION, AgentDirectory
from dispatch.discovery import DiscoveryError, ManifestSource, entries_from_manifest
from providers.provider_client import load_provider
from services.workflow_api.runner import WorkflowRunner, build_runner

logger = logging.getLogger(__name__)

SESSION_MANAGER_URL_ENV = "KIO1_SESSION_MANAGER_URL"
# Registering an agent means task text and upstream results start flowing
# to an address someone supplied over the network. That is not something to
# leave open by default, so the endpoints stay closed until a token is set.
REGISTRY_TOKEN_ENV = "KIO1_REGISTRY_TOKEN"


class RunWorkflowRequest(BaseModel):
    """A natural-language software-development request."""

    request: str = Field(min_length=1, max_length=100_000)


class RunWorkflowResponse(BaseModel):
    """Acknowledges that a workflow was accepted for execution."""

    session_id: UUID
    status: str
    status_url: str


class RegisterAgentRequest(BaseModel):
    """An agent describing itself to a running orchestrator.

    The body is a manifest in any shape `dispatch.discovery` accepts, so an
    agent that already publishes one can post the same document.
    """

    manifest: dict[str, Any] | list[Any]


@dataclass
class _State:
    """Everything resolved once at startup and reused per request."""

    runner: WorkflowRunner
    session_client: SessionClient | NullSessionClient
    directory: AgentDirectory


_state: _State | None = None


def _build_state() -> _State:
    """Load configuration, the prompt, and the provider, then assemble a runner.

    Returns:
        The service state.
    """
    config = load_config()
    init_telemetry(config.telemetry, llm=config.model)

    # The prompt keeps its catalog placeholder: the runner fills it per
    # request from whatever the directory can reach at that moment.
    prompt_template = load_prompt(config.prompt_path)

    directory = AgentDirectory(
        config.dispatch.registry,
        discovery=config.dispatch.discovery,
        health=config.dispatch.health,
    )
    directory.start()

    provider = load_provider(config.provider, config.allowed_providers)
    provider_client = provider.create_client(config)

    try:
        with trace_provider_preload(provider=config.provider, model=config.model):
            provider.preload(config, provider_client)
    except Exception:
        # Preloading only warms the model. A service that refuses to start
        # because the provider is momentarily unreachable takes the whole API
        # down with it, including the status endpoint someone would use to
        # find out why. Requests surface the real error individually.
        logger.warning(
            "Could not preload %s model %s; the service will start anyway and "
            "requests will fail until the provider is reachable",
            config.provider,
            config.model,
            exc_info=True,
        )

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
            prompt_template=prompt_template,
            provider=provider,
            provider_client=provider_client,
            recorder=session_client,
            directory=directory,
        ),
        session_client=session_client,
        directory=directory,
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
            state.directory.stop()
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


def _require_registry_token(request: Request) -> None:
    """Reject a registry call that is not authorised.

    Args:
        request: The incoming request, read for its `Authorization` header.

    Raises:
        HTTPException: 503 when no token is configured, so the endpoints stay
            closed on a deployment that never opted in; 401 when the header
            does not match.
    """
    expected = os.environ.get(REGISTRY_TOKEN_ENV, "").strip()

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The agent registry API is closed. Set "
                f"{REGISTRY_TOKEN_ENV} to a shared secret to open it, and send "
                "it as 'Authorization: Bearer <token>'."
            ),
        )

    header = request.headers.get("Authorization", "")
    offered = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""

    if not compare_digest(offered, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid registry token",
        )


@app.get("/registry/agents")
def list_agents(
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Return every known target and what is currently known about it.

    Args:
        request: The incoming request, checked for the registry token.
        state: The live directory.

    Returns:
        Every record, including the ones currently withdrawn, so a caller can
        see *why* an agent is not being planned for.
    """
    _require_registry_token(request)

    return {"agents": [record.to_summary() for record in state.directory.records()]}


@app.post("/registry/agents", status_code=status.HTTP_201_CREATED)
def register_agent(
    body: RegisterAgentRequest,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Add or update targets from a manifest an agent posted about itself.

    The manifest is probed before the agent is trusted, and the outcome is in
    the response: an agent that did not answer is recorded as `unreachable`
    rather than rejected, so a slow starter joins on the next health pass
    instead of having to post again.

    Args:
        body: The manifest, in any shape discovery accepts.
        request: The incoming request, checked for the registry token.
        state: The live directory.

    Returns:
        A summary of every record the manifest produced.

    Raises:
        HTTPException: 400 when the manifest cannot be read or names nothing
            usable.
    """
    _require_registry_token(request)

    source = ManifestSource(url=str(request.url))

    try:
        entries = entries_from_manifest(body.manifest, source)
    except DiscoveryError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    registered = []

    for agent_id, entry in entries.items():
        try:
            record = state.directory.register(agent_id, entry, ORIGIN_REGISTRATION)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{agent_id}: {error}",
            ) from error

        registered.append(record.to_summary())

    logger.info(
        "Agents registered: %s", ", ".join(item["agent_id"] for item in registered)
    )

    return {"agents": registered}


@app.delete("/registry/agents/{agent_id}")
def unregister_agent(
    agent_id: str,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, str]:
    """Remove a discovered or registered target.

    Args:
        agent_id: The target to remove.
        request: The incoming request, checked for the registry token.
        state: The live directory.

    Returns:
        Confirmation that it is gone.

    Raises:
        HTTPException: 404 when it is unknown, 409 when it came from
            `config.json` and so cannot be removed while running.
    """
    _require_registry_token(request)

    record = state.directory.record_for(agent_id)

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown agent {agent_id}"
        )

    if not state.directory.forget(agent_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{agent_id} is defined in config.json, which is its source of "
                "truth. Remove it there and restart."
            ),
        )

    return {"agent_id": agent_id, "status": "removed"}
