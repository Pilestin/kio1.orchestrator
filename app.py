"""The KIO1 web service: the Workflow API and the operator console.

Started with `uvicorn app:app`. It sits beside `main.py` on purpose —
one is how you drive KIO1 from a terminal, the other is how you drive
it from a browser or another program, and neither is buried.
"""

import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from hmac import compare_digest
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.agent_store import (
    ACTION_RESTORED,
    STORE_FILE,
    AgentStore,
    AgentStoreError,
    FileAgentStore,
    open_agent_store,
    registry_from,
)
from core.approvals import ApprovalDesk
from core.config_loader import Config, Workspace, load_config, rehost_entry
from core.config_writer import (
    ConfigWriteError,
    config_path,
    save_settings,
    writing_enabled,
)
from core.dotenv import load_dotenv, save_dotenv_value
from core.model_pull import PullManager
from core.prompt_loader import load_prompt
from core.run_events import RunEvent, RunEventBus, install_log_relay
from core.service_telemetry import TraceContextMiddleware
from core.session_client import NullSessionClient, SessionClient, SessionClientError
from core.system_info import hardware, ollama_status, suggest
from core.telemetry import init_telemetry, shutdown_telemetry, trace_provider_preload
from dispatch.agent_registry import AgentEndpoint, McpToolSpec, load_agent_entry
from dispatch.directory import (
    ORIGIN_CONFIG,
    ORIGIN_REGISTRATION,
    STATUS_READY,
    STATUS_UNCHECKED,
    STATUS_UNREACHABLE,
    AgentDirectory,
    probe_address,
    probe_target,
)
from dispatch.discovery import DiscoveryError, ManifestSource, entries_from_manifest
from dispatch.scoreboard import Scoreboard
from providers.provider_client import describe_provider, load_provider
from services.session_manager.storage import (
    DATABASE_URL_ENV,
    DEFAULT_DATABASE_URL,
    EmbeddedSessionClient,
    open_repository,
)
from services.workflow_api.runner import WorkflowRunner, build_runner

logger = logging.getLogger(__name__)

SESSION_MANAGER_URL_ENV = "KIO1_SESSION_MANAGER_URL"
# Registering an agent means task text and upstream results start flowing
# to an address someone supplied over the network. That is not something to
# leave open by default, so the endpoints stay closed until a token is set.
REGISTRY_TOKEN_ENV = "KIO1_REGISTRY_TOKEN"


class RunWorkflowRequest(BaseModel):
    """A natural-language software-development request.

    `conversation_id` continues a conversation; without it the request
    starts a new one.
    """

    request: str = Field(min_length=1, max_length=100_000)
    conversation_id: UUID | None = None


class RunWorkflowResponse(BaseModel):
    """Acknowledges that a workflow was accepted for execution."""

    session_id: UUID
    conversation_id: UUID
    status: str
    status_url: str


class RegisterAgentRequest(BaseModel):
    """An agent describing itself to a running orchestrator.

    The body is a manifest in any shape `dispatch.discovery` accepts, so an
    agent that already publishes one can post the same document.

    `persist` writes what was registered into `config.json`, which is what
    makes the console a place you administer from rather than one you look
    at: without it the entry is gone at the next restart.
    """

    manifest: dict[str, Any] | list[Any]
    persist: bool = True


class ProbeAgentRequest(BaseModel):
    """A definition to try before committing to it."""

    entry: dict[str, Any]
    agent_id: str = ""


class UpdateAgentRequest(BaseModel):
    """A replacement definition for one target.

    The whole entry is replaced rather than merged, because a merge cannot
    express removing a field — dropping a tool's `health_url` would be
    impossible.
    """

    entry: dict[str, Any]
    persist: bool = True


@dataclass
class _State:
    """Everything resolved once at startup and reused per request."""

    runner: WorkflowRunner
    session_client: SessionClient | NullSessionClient | EmbeddedSessionClient
    directory: AgentDirectory
    scoreboard: Scoreboard
    events: RunEventBus = field(default_factory=RunEventBus)
    approvals: ApprovalDesk = field(default_factory=ApprovalDesk)
    store: AgentStore = field(default_factory=FileAgentStore)
    pulls: PullManager = field(default_factory=PullManager)


_state: _State | None = None


def _load_review_prompt(config: Config) -> str:
    """Read the reviewer's prompt, or turn the review off without it.

    Args:
        config: Application configuration.

    Returns:
        The prompt, catalog placeholder still in it; empty when reviewing is
        off or the file cannot be read. A missing reviewer prompt costs the
        answer, not the service.
    """
    if not config.orchestration.review:
        return ""

    try:
        return load_prompt(config.orchestration.review_prompt_path)
    except OSError:
        logger.warning(
            "Review prompt %s could not be read; runs will not be reviewed",
            config.orchestration.review_prompt_path,
        )
        return ""


def _build_state() -> _State:
    """Load configuration, the prompt, and the provider, then assemble a runner.

    Returns:
        The service state.
    """
    # API keys from .env, before anything reads them. The environment wins.
    load_dotenv()
    config = load_config()
    init_telemetry(config.telemetry, llm=config.model)

    # The prompt keeps its catalog placeholder: the runner fills it per
    # request from whatever the directory can reach at that moment.
    prompt_template = load_prompt(config.prompt_path)
    review_template = _load_review_prompt(config)

    database_url = os.environ.get(DATABASE_URL_ENV, "").strip() or DEFAULT_DATABASE_URL
    store = open_agent_store(database_url)
    stored = store.read()

    if store.kind != STORE_FILE:
        # The database holds the agents now; config.json's were imported
        # into it on its first start and are not read again.
        config.dispatch = replace(config.dispatch, registry=registry_from(stored))
        logger.info("Agents: %d from %s", len(stored), store.location)

    directory = AgentDirectory(
        config.dispatch.registry,
        discovery=config.dispatch.discovery,
        health=config.dispatch.health,
        sources=stored,
    )
    directory.start()
    # One scoreboard for the process, so history accumulates across
    # requests instead of resetting with every workflow.
    scoreboard = Scoreboard(config.dispatch.selection)
    events = RunEventBus()
    approvals = ApprovalDesk()

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

    session_client: SessionClient | EmbeddedSessionClient
    if session_manager_url:
        session_client = SessionClient(session_manager_url)
        logger.info("Session Manager at %s", session_manager_url)
    else:
        # No separate service: history is kept by this process, in SQLite
        # unless KIO1_DATABASE_URL says otherwise. A first run on one machine
        # needs neither Docker nor a second program to see what it did.
        session_client = EmbeddedSessionClient(*open_repository(database_url))

    return _State(
        runner=build_runner(
            config=config,
            prompt_template=prompt_template,
            provider=provider,
            provider_client=provider_client,
            recorder=session_client,
            directory=directory,
            scoreboard=scoreboard,
            events=events,
            review_template=review_template,
            approvals=approvals,
        ),
        session_client=session_client,
        directory=directory,
        scoreboard=scoreboard,
        events=events,
        approvals=approvals,
        store=store,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare the provider and telemetry on startup, flush them on shutdown."""
    global _state

    remove_log_relay = install_log_relay()
    _state = _build_state()

    try:
        yield
    finally:
        state, _state = _state, None
        remove_log_relay()

        if state is not None:
            state.directory.stop()
            state.runner.transport.close()

            if isinstance(state.session_client, EmbeddedSessionClient):
                state.session_client.close()

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


STATIC_DIR = Path(__file__).with_name("static")

# The console's own assets, versioned in the page by their content.
_VERSIONED_ASSETS = ("app.css", "app.js")

# Revalidate on every load. Without a Cache-Control header a browser may reuse
# a stylesheet for hours on its own judgement, and an updated page paired with
# a stale stylesheet or script looks — and is — broken.
_NO_CACHE = "no-cache"

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:  # pragma: no cover - only a trimmed deployment lacks it
    logger.warning("Console assets are missing at %s", STATIC_DIR)


@app.middleware("http")
async def _revalidate_console_assets(request: Request, call_next: Any) -> Any:
    """Make browsers check the console's files rather than guess they are fresh."""
    response = await call_next(request)

    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = _NO_CACHE

    return response


@lru_cache(maxsize=4)
def _console_page_for(mtimes: tuple[float, ...]) -> str:
    """Return the console's HTML with each asset reference versioned.

    Cached per set of modification times, so editing any of the three files
    is picked up on the next load without restarting the service.

    Args:
        mtimes: Modification times of the page and its assets; the cache key.

    Returns:
        The page, its stylesheet and script referenced as `app.css?v=<hash>`
        so a changed file is a new address no cache can answer for.
    """
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    for name in _VERSIONED_ASSETS:
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        page = page.replace(f'"/static/{name}"', f'"/static/{name}?v={digest}"')

    return page


def _console_page() -> str:
    """Return the console's HTML, current with the files on disk.

    Returns:
        The page source, or a plain message when a file is missing so a
        trimmed deployment answers something readable rather than a traceback.
    """
    try:
        mtimes = tuple(
            (STATIC_DIR / name).stat().st_mtime
            for name in ("index.html", *_VERSIONED_ASSETS)
        )
        return _console_page_for(mtimes)
    except OSError:
        logger.warning("Console page is missing in %s", STATIC_DIR)
        return "<h1>KIO1</h1><p>The console page is not installed.</p>"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def console() -> HTMLResponse:
    """Serve the operator console.

    Returns:
        The page. Its stylesheet and script come from `/static`, and it loads
        nothing from anywhere else, so it works on a machine with no route to
        the internet.
    """
    return HTMLResponse(_console_page())


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
        HTTPException: 404 when the conversation does not exist, 503 when a
            session could not be opened.
    """
    conversation_id = body.conversation_id

    try:
        # Opened here rather than left to the Session Manager, so its
        # identifier can be handed back with the session's.
        if conversation_id is None:
            conversation_id = UUID(
                state.session_client.create_conversation()["conversation_id"]
            )

        session_id = state.session_client.create_session(body.request, conversation_id)
    except SessionClientError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No conversation {conversation_id}",
            ) from error

        logger.error("Could not open a session: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable, so the request was not accepted",
        ) from error

    state.events.publish(str(session_id), "status", status="pending")
    background_tasks.add_task(state.runner.run, session_id, body.request)

    logger.info("Workflow accepted: session_id=%s", session_id)

    return RunWorkflowResponse(
        session_id=session_id,
        conversation_id=conversation_id,
        status="pending",
        status_url=str(request.url_for("get_workflow", session_id=session_id)),
    )


@app.get("/workflows")
def list_workflows(
    limit: int = 25,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Return the most recent runs, newest first.

    Args:
        limit: How many to return.
        state: The Session Manager client.

    Returns:
        Recent sessions without their steps. An empty list when nothing
        is recorded, rather than an error: a console asking what has run
        wants an empty answer when the answer is nothing.
    """
    try:
        return {"workflows": state.session_client.list_sessions(limit)}
    except SessionClientError as error:
        logger.warning("Could not list workflows: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable",
        ) from error


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


@app.post("/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(state: _State = Depends(get_state)) -> dict[str, Any]:
    """Open an empty conversation: the console's "New chat".

    Raises:
        HTTPException: 503 when the Session Manager is unavailable.
    """
    try:
        return state.session_client.create_conversation()
    except SessionClientError as error:
        logger.warning("Could not open a conversation: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable",
        ) from error


@app.get("/conversations")
def list_conversations(
    limit: int = 25,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Return the conversations most recently added to, newest first.

    Args:
        limit: How many to return.
        state: The Session Manager client.

    Returns:
        Conversations without their runs; an empty list when nothing is
        recorded, as `/workflows`.
    """
    try:
        return {
            "conversations": state.session_client.list_conversations(
                max(1, min(limit, 200))
            )
        }
    except SessionClientError as error:
        logger.warning("Could not list conversations: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable",
        ) from error


@app.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: UUID,
    state: _State = Depends(get_state),
) -> JSONResponse:
    """Return a conversation with each of its runs, oldest first.

    Every request, its steps, and its answer: what a chat shows when it is
    opened again.

    Raises:
        HTTPException: 404 when the conversation is unknown, 503 when the
            Session Manager is unavailable.
    """
    try:
        detail = state.session_client.get_conversation(conversation_id)
    except SessionClientError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No conversation {conversation_id}",
            ) from error

        logger.warning("Could not read conversation %s: %s", conversation_id, error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable",
        ) from error

    return JSONResponse(content=detail)


@app.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: UUID,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, str]:
    """Delete a conversation and every run in it.

    Behind the registry token: deleting history cannot be undone, and
    running workflows, which needs no token, must not be enough to do it.

    Raises:
        HTTPException: 404 when the conversation is unknown, 503 when the
            Session Manager is unavailable.
    """
    _require_registry_token(request)

    try:
        deleted = state.session_client.delete_conversation(conversation_id)
    except SessionClientError as error:
        logger.warning("Could not delete conversation %s: %s", conversation_id, error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session Manager is unavailable",
        ) from error

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No conversation {conversation_id}",
        )

    logger.info("Conversation deleted: conversation_id=%s", conversation_id)

    return {"conversation_id": str(conversation_id), "status": "deleted"}


# How long one wait for new events lasts before a keep-alive is sent. Short
# enough that a proxy does not close an idle stream, long enough to be cheap.
_EVENT_WAIT_SECONDS = 15.0


def _sse(event: RunEvent) -> str:
    """Format one event as a Server-Sent Events frame."""
    payload = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
    return f"id: {event.id}\nevent: {event.kind}\ndata: {payload}\n\n"


@app.get("/workflow/{session_id}/events")
def workflow_events(
    session_id: UUID,
    request: Request,
    after: int = 0,
    state: _State = Depends(get_state),
) -> StreamingResponse:
    """Stream what a run is doing while it does it.

    Server-Sent Events: the plan as the model writes it, each step as it
    starts and finishes, and KIO1's own log lines for the run. The stream
    ends after the run's `finished` event. A browser's `EventSource`
    reconnects on its own and resumes from `Last-Event-ID`; any other client
    passes `after`.

    Only recent runs of this process are held. For an older run, or one
    started before a restart, the stream is empty and ends at once — its
    record is at `/workflow/{session_id}`.

    Args:
        session_id: The run to follow.
        request: Read for `Last-Event-ID`.
        after: Resume after this event id.
        state: The event bus.

    Returns:
        A `text/event-stream` response.
    """
    run_id = str(session_id)
    resume = request.headers.get("Last-Event-ID", "")
    cursor = int(resume) if resume.isdigit() else after

    def stream() -> Iterator[str]:
        last = cursor
        # A run nobody has heard of is not one to wait on.
        if not state.events.read(run_id) and not state.events.is_finished(run_id):
            yield ": no live events for this run\n\n"
            return

        while True:
            events = state.events.wait(run_id, last, timeout=_EVENT_WAIT_SECONDS)

            for event in events:
                last = event.id
                yield _sse(event)

            if state.events.is_finished(run_id) and not state.events.read(run_id, last):
                return

            if not events:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ApprovalRequest(BaseModel):
    """A person's answer to a plan waiting for approval."""

    approve: bool


class AnswerRequest(BaseModel):
    """A person's answer to the planner's question."""

    text: str = Field(min_length=1, max_length=4_000)


@app.get("/workflow/{session_id}/waiting")
@app.get("/workflow/{session_id}/approval")
def get_waiting(session_id: UUID, state: _State = Depends(get_state)) -> dict[str, Any]:
    """Return what a run is waiting for a person to do, if anything.

    Args:
        session_id: The run.
        state: The approval desk.

    Returns:
        `waiting: false` when the run is not waiting. Otherwise `kind` —
        `approval`, with the plan and the targets that made it need one, or
        `question`, with the planner's question — and when the wait ends. Not
        waiting is an answer rather than a 404: the console asks this of
        every running run, and most of them are not waiting.
    """
    pending = state.approvals.pending(str(session_id))

    if pending is None:
        return {"session_id": str(session_id), "waiting": False}

    return {**pending.to_dict(), "waiting": True}


@app.post("/workflow/{session_id}/answer")
def answer_question(
    session_id: UUID, body: AnswerRequest, state: _State = Depends(get_state)
) -> dict[str, Any]:
    """Answer the question a run's planner asked.

    Not behind the administration token: starting a run is not either, and
    an answer only lets the planner plan — a plan that reaches a marked
    target still waits for approval.

    Args:
        session_id: The run.
        body: The answer.
        state: The approval desk.

    Returns:
        Confirmation.

    Raises:
        HTTPException: 409 when the run is not waiting on a question.
    """
    if not state.approvals.answer(str(session_id), body.text.strip()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run is not waiting for an answer.",
        )

    return {"session_id": str(session_id), "answered": True}


@app.post("/workflow/{session_id}/approval")
def decide_approval(
    session_id: UUID,
    body: ApprovalRequest,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Approve or cancel a plan that is waiting.

    Behind the administration token: approving lets a plan run commands and
    reach targets that were marked as needing a person's say-so.

    Args:
        session_id: The run.
        body: Whether to approve.
        request: Checked for the registry token.
        state: The approval desk.

    Returns:
        What was decided.

    Raises:
        HTTPException: 409 when nothing is waiting — decided already, timed
            out, or never asked.
    """
    _require_registry_token(request)

    if not state.approvals.decide(str(session_id), body.approve):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run is not waiting for approval.",
        )

    logger.info(
        "Plan %s: session_id=%s",
        "approved" if body.approve else "cancelled",
        session_id,
    )

    return {"session_id": str(session_id), "approved": body.approve}


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
                "The administration API is closed, so this page cannot list "
                "or change targets, or change which model plans a run. "
                "Running workflows still works.\n\n"
                "To open it, restart the service with a secret of your "
                f"choosing in {REGISTRY_TOKEN_ENV}, then paste that same value "
                "into the Registry token box:\n\n"
                f'  PowerShell   $env:{REGISTRY_TOKEN_ENV} = "choose-a-secret"\n'
                f"  bash         export {REGISTRY_TOKEN_ENV}=choose-a-secret\n"
                f"  Docker       {REGISTRY_TOKEN_ENV}=choose-a-secret in "
                "deploy/compose.yaml\n\n"
                "It is closed by default because registering an agent starts "
                "sending task text to an address someone supplied, and "
                "changing the workspace points every agent at different code."
            ),
        )

    header = request.headers.get("Authorization", "")
    offered = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""

    if not compare_digest(offered, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid registry token",
        )


def _persist(state: _State, changes: dict[str, Any | None]) -> None:
    """Save changes to the targets wherever this deployment keeps them.

    Args:
        state: The service, for its agent store.
        changes: Agent id to entry, or to None to remove it.

    Raises:
        HTTPException: 503 when the change could not be saved, naming why.
    """
    try:
        state.store.write(changes)
    except AgentStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


@app.get("/registry/config")
def registry_config(
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Report where registry changes are saved, and whether they can be.

    Args:
        request: The incoming request, checked for the registry token.
        state: Unused, present so this fails the same way as its neighbours
            when the service is not ready.

    Returns:
        Where targets are kept — the database or the configuration file —
        and whether changes can be saved, so the console can say up front
        that Save will not stick rather than after someone tries.
    """
    _require_registry_token(request)

    return {
        "config_path": state.store.location,
        "store": state.store.kind,
        "writable": state.store.writable,
    }


class SettingsRequest(BaseModel):
    """The settings the console may change while the service runs.

    Every field is optional: the console sends what changed, and anything
    absent is left as it is.
    """

    provider: str | None = None
    api_key: str | None = Field(default=None, max_length=512)
    model: str | None = None
    temperature: float | None = None
    request_timeout: float | None = None
    workspace_path: str | None = None
    workspace_entry_point: str | None = None
    # The model that reviews and answers. An empty provider means "the same
    # as planning", and clears the model with it.
    review_provider: str | None = None
    review_model: str | None = None
    review_api_key: str | None = Field(default=None, max_length=512)


def _models_for(provider: Any, config: Config) -> tuple[list[str], str]:
    """Ask a provider which models it offers.

    Args:
        provider: The provider module.
        config: The configuration to ask with, for endpoints.

    Returns:
        The model names, and why there are none when that is not simply the
        answer.
    """
    lister = getattr(provider, "list_models", None)

    if lister is None:
        # Not every provider can say. An OpenAI or Anthropic deployment names
        # its model in the file, and a picker that silently offers nothing
        # would read as "there are none" rather than "I cannot ask".
        return [], (
            f"The {config.provider} provider cannot list its models from here; "
            "type the name you want."
        )

    try:
        return list(lister(config)), ""
    except Exception as error:  # noqa: BLE001 - shown, not swallowed
        # Listing is a convenience. A provider that cannot answer must not
        # take the page down with it, and the reason is reported rather than
        # hidden: "no models" and "the provider is not running" are different
        # problems, and an empty list looks like the first.
        return [], f"Models could not be listed: {error}"


def _settings_view(state: _State) -> dict[str, Any]:
    """Return the current planning settings, and what may be chosen instead.

    Args:
        state: The live service, for the configuration the runner is using.

    Returns:
        What is in force now, the providers and models that can be chosen,
        and where a change would be saved.
    """
    config = state.runner.config
    workspace = config.dispatch.workspace
    models, models_error = _models_for(state.runner.provider, config)

    if config.model and config.model not in models:
        # The model in force is always offerable, even when the provider has
        # since forgotten it: a picker that cannot show the current value
        # looks like it changed something by being opened.
        models = sorted([*models, config.model])

    return {
        "provider": config.provider,
        "providers": [
            describe_provider(name) for name in sorted(config.allowed_providers)
        ],
        "model": config.model,
        "models": models,
        "models_error": models_error,
        "review": {
            "enabled": config.orchestration.review,
            "provider": config.orchestration.review_provider,
            "model": config.orchestration.review_model,
        },
        "temperature": config.temperature,
        "request_timeout": config.request_timeout,
        "prompt_path": config.prompt_path,
        "workspace": {
            "path": workspace.path,
            "entry_point": workspace.entry_point,
        },
        "config_path": str(config_path()),
        "writable": writing_enabled(),
    }


@app.get("/settings")
def get_settings(
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Return what the planner is currently using.

    Args:
        request: The incoming request, checked for the registry token.
        state: The live service.

    Returns:
        The current model, the models available, and the workspace agents are
        pointed at.
    """
    _require_registry_token(request)

    return _settings_view(state)


@app.get("/settings/models")
def get_provider_models(
    provider: str,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """List the models of a provider, chosen or not.

    The console asks this when someone picks a different provider, so the
    model list matches the provider before anything is saved.

    Args:
        provider: The provider to ask.
        request: The incoming request, checked for the registry token.
        state: The live service, for the configuration to ask with.

    Returns:
        The models, and why there are none when listing failed.

    Raises:
        HTTPException: 400 for a provider this deployment does not allow.
    """
    _require_registry_token(request)

    config = state.runner.config

    try:
        module = load_provider(provider, config.allowed_providers)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    models, reason = _models_for(module, replace(config, provider=provider))

    return {"provider": provider, "models": models, "models_error": reason}


def _switch_provider(
    state: _State, body: SettingsRequest
) -> tuple[str, Any, Any] | None:
    """Prepare a change of provider or of its key, without applying it.

    Building the new client first means a missing or wrong key is refused
    before anything is saved, rather than discovered by the next run.

    Args:
        state: The live service.
        body: The requested change.

    Returns:
        The provider name, its module, and a ready client — or None when
        neither the provider nor its key is changing.

    Raises:
        HTTPException: 400 when the provider is not allowed, needs a model,
            or its client cannot be created.
    """
    config = state.runner.config
    name = (body.provider or config.provider).strip()
    key = (body.api_key or "").strip()

    if name == config.provider and not key:
        return None

    try:
        module = load_provider(name, config.allowed_providers)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    model = (body.model or "").strip()

    if name != config.provider and not model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Choose a {name} model to go with the provider.",
        )

    return name, module, _start_client(config, module, name, model or config.model, key)


def _switch_reviewer(state: _State, body: SettingsRequest) -> dict[str, str] | None:
    """Validate a change of the reviewing model, without applying it.

    Args:
        state: The live service.
        body: The requested change.

    Returns:
        The `orchestration` fields to save, or None when the reviewer is not
        changing.

    Raises:
        HTTPException: 400 when the provider is not allowed, has no model, or
            cannot be started with the key given.
    """
    if (
        body.review_provider is None
        and body.review_model is None
        and not body.review_api_key
    ):
        return None

    config = state.runner.config
    settings = config.orchestration
    provider = (
        body.review_provider
        if body.review_provider is not None
        else settings.review_provider
    ).strip()
    model = (
        body.review_model if body.review_model is not None else settings.review_model
    ).strip()

    if not provider:
        return {"review_provider": "", "review_model": ""}

    if not model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Choose a {provider} model to answer with.",
        )

    try:
        module = load_provider(provider, config.allowed_providers)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    # Started here only to prove it can be; the runner starts its own on the
    # next review. A provider shared with the planner is already running.
    if provider != config.provider or body.review_api_key:
        _start_client(
            config, module, provider, model, (body.review_api_key or "").strip()
        )

    return {"review_provider": provider, "review_model": model}


def _start_client(config: Config, module: Any, name: str, model: str, key: str) -> Any:
    """Create a provider's client, trying a new key before keeping it.

    Args:
        config: The configuration to start from.
        module: The provider module.
        name: The provider's name.
        model: The model the client is for.
        key: A new API key to try, or empty to use the one already set.

    Returns:
        The client.

    Raises:
        HTTPException: 400 when the client cannot be created, or the key
            works but could not be saved.
    """
    declared = getattr(module, "API_KEY_ENV", None)
    # The variable a new key goes into, or None when no key is being tried.
    key_env: str | None = declared if key and isinstance(declared, str) else None
    previous_key = os.environ.get(key_env) if key_env is not None else None

    if key_env is not None:
        # Tried in this process first; written to .env only once a client
        # accepts it, so a mistyped key is not left behind for the next start.
        os.environ[key_env] = key

    try:
        client = module.create_client(replace(config, provider=name, model=model))
    except Exception as error:
        if key_env is not None:
            if previous_key is None:
                os.environ.pop(key_env, None)
            else:
                os.environ[key_env] = previous_key

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} cannot be used yet: {error}",
        ) from error

    if key_env is not None:
        try:
            # Into .env, which git ignores, so it survives a restart.
            save_dotenv_value(key_env, key)
        except (ValueError, OSError) as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The key works but could not be saved: {error}",
            ) from error

    return client


@app.put("/settings")
def put_settings(
    body: SettingsRequest,
    background: BackgroundTasks,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Change the planning settings, in the file and in the running service.

    Both halves matter. Writing only the file would mean a restart before a
    change took effect, which is the opposite of what someone trying models
    against one request wants; changing only memory would lose it at the next
    restart.

    Args:
        body: The settings to change. Absent fields are left alone.
        background: Used to warm a newly chosen model after answering.
        request: The incoming request, checked for the registry token.
        state: The live service.

    Returns:
        The settings as they now stand.

    Raises:
        HTTPException: 400 when a value is unusable, 409 when the file could
            not be written.
    """
    _require_registry_token(request)

    config = state.runner.config
    updates: dict[str, Any] = {}
    switch = _switch_provider(state, body)

    if switch is not None and switch[0] != config.provider:
        updates["provider"] = switch[0]

    if body.model is not None:
        model = body.model.strip()

        if not model:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A model name cannot be empty.",
            )

        updates["model"] = model

    if body.temperature is not None:
        if not 0.0 <= body.temperature <= 2.0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Temperature must be between 0 and 2.",
            )

        updates["temperature"] = body.temperature

    if body.request_timeout is not None:
        if not 5.0 <= body.request_timeout <= 3600.0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The planning timeout must be between 5 and 3600 seconds.",
            )

        updates["request_timeout"] = body.request_timeout

    workspace = _workspace_update(body, config)

    if workspace is not None:
        updates["dispatch"] = {"workspace": workspace}

    reviewer = _switch_reviewer(state, body)

    if reviewer is not None:
        updates["orchestration"] = reviewer

    if updates:
        try:
            save_settings(updates)
        except ConfigWriteError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    if switch is not None:
        # The provider, its model, and its client change together: a run
        # that read the new provider with the old client would send one
        # vendor's model name to another.
        name, module, client = switch
        state.runner.config.provider = name
        state.runner.provider = module
        state.runner.provider_client = client
        logger.info("Planning provider is now %s", name)

    if not updates:
        return _settings_view(state)

    _apply_settings(state, updates)

    if "model" in updates or "provider" in updates:
        # A local model has to be read into memory before it can answer, and
        # that read is charged to whichever request triggers it. Without this
        # the first run after a change pays it and frequently times out, which
        # reads as "the new model is broken" rather than "it was cold".
        background.add_task(_preload_model, state)

    if updates.get("orchestration", {}).get("review_provider"):
        background.add_task(_preload_reviewer, state)

    return _settings_view(state)


def _preload_model(state: _State) -> None:
    """Warm the current model so the next run is not charged for loading it.

    Failures are logged rather than raised: this runs detached from the
    request that scheduled it, and a provider that cannot preload is not a
    reason to refuse a setting that is already saved.

    Args:
        state: The live service.
    """
    config = state.runner.config

    try:
        with trace_provider_preload(provider=config.provider, model=config.model):
            state.runner.provider.preload(config, state.runner.provider_client)
    except Exception:
        logger.warning(
            "Could not preload %s; the first run will load it instead",
            config.model,
            exc_info=True,
        )


def _preload_reviewer(state: _State) -> None:
    """Warm the reviewing model, for the same reason as the planning one."""
    try:
        reviewer = state.runner._reviewer()
        reviewer.provider.preload(reviewer.config, reviewer.client)
    except Exception:
        logger.warning(
            "Could not preload the reviewing model; the first review will load it",
            exc_info=True,
        )


def _workspace_update(body: SettingsRequest, config: Config) -> dict[str, str] | None:
    """Return the workspace block to save, or None when it is unchanged.

    Args:
        body: The requested change.
        config: The configuration in force.

    Returns:
        The complete workspace object, because it is saved whole.
    """
    if body.workspace_path is None and body.workspace_entry_point is None:
        return None

    current = config.dispatch.workspace
    path = body.workspace_path if body.workspace_path is not None else current.path
    entry_point = (
        body.workspace_entry_point
        if body.workspace_entry_point is not None
        else current.entry_point
    )

    return {"path": path.strip(), "entry_point": entry_point.strip()}


def _apply_settings(state: _State, updates: dict[str, Any]) -> None:
    """Make saved settings take effect without a restart.

    `Config` is read at the start of each run rather than captured, so
    changing it here reaches the next run. A run already in flight keeps the
    values it started with, which is the behaviour worth having: a model
    swapped mid-plan would produce a result nobody could attribute.

    Args:
        state: The live service.
        updates: What was just saved.
    """
    config = state.runner.config

    if "model" in updates:
        config.model = updates["model"]
        logger.info("Planning model changed to %s", config.model)

    if "temperature" in updates:
        config.temperature = updates["temperature"]

    if "request_timeout" in updates:
        config.request_timeout = updates["request_timeout"]

    reviewer = updates.get("orchestration")

    if reviewer is not None:
        config.orchestration = replace(config.orchestration, **reviewer)
        logger.info(
            "Reviewing model is now %s",
            (
                f"{reviewer['review_provider']}/{reviewer['review_model']}"
                if reviewer["review_provider"]
                else "the planning model"
            ),
        )

    workspace = updates.get("dispatch", {}).get("workspace")

    if workspace is not None:
        config.dispatch = replace(
            config.dispatch,
            workspace=Workspace(
                path=workspace["path"], entry_point=workspace["entry_point"]
            ),
        )
        logger.info("Workspace changed to %s", workspace["path"])


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
        Every record, including the ones currently withdrawn, so a caller
        can see *why* an agent is not being planned for, and what each has
        recently done per capability.
    """
    _require_registry_token(request)

    return {
        "agents": [
            {
                **record.to_summary(),
                "history": [
                    score.to_summary()
                    for score in state.scoreboard.scores_for_agent(record.agent_id)
                ],
            }
            for record in state.directory.records()
        ]
    }


def _described_tools(endpoint: AgentEndpoint) -> list[dict[str, Any]]:
    """Return what an MCP server said about itself, for the console.

    Args:
        endpoint: The probed target.

    Returns:
        One object per discovered tool, with its name, what it does, and the
        arguments it takes. Empty for every other kind, and for an MCP server
        that could not be asked.
    """
    spec = endpoint.tool

    if not isinstance(spec, McpToolSpec):
        return []

    return [
        {
            "name": tool.name,
            "description": tool.description,
            "arguments": tool.summarize_arguments(),
        }
        for tool in spec.discovered
    ]


@app.post("/registry/agents/probe")
def probe_agent(
    body: ProbeAgentRequest,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Try a definition without adding it, and say what was actually tried.

    A definition that does not work usually fails for a reason the person who
    wrote it cannot see: a loopback address means something different inside a
    container, and a program that is on their machine is not in it. Finding
    that out before saving, with the address this service really dialled, is
    the difference between one edit and several.

    Args:
        body: The definition, and the id to check it under.
        request: The incoming request, checked for the registry token.
        state: The live directory, for its probe settings.

    Returns:
        What the definition parsed as, what was dialled, and what answered.

    Raises:
        HTTPException: 400 when the definition is malformed, which is itself
            worth finding out before saving.
    """
    _require_registry_token(request)

    agent_id = body.agent_id or str(body.entry.get("agent_id") or "Probe")
    entry = {
        key: value for key, value in body.entry.items() if key not in {"agent_id", "id"}
    }
    running = rehost_entry(agent_id, entry)

    try:
        endpoint = load_agent_entry(agent_id, running)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    health = state.directory.health
    address = probe_address(endpoint, health)
    # No pool: this entry has not been saved, so the server it names must not
    # be left running in the pool the real targets dispatch through.
    result = probe_target(endpoint, health)
    # Probing an MCP server also established what it offers, and that is the
    # most useful thing to show — the capabilities the entry is about to get.
    endpoint = result.endpoint

    if not result.reachable:
        outcome = STATUS_UNREACHABLE
    elif result.detail:
        outcome = STATUS_UNCHECKED
    else:
        outcome = STATUS_READY

    # What the definition said versus what this deployment will dial. They
    # differ whenever KIO1_AGENT_HOST is in play, and that difference is the
    # thing worth showing.
    as_written = probe_address(load_agent_entry(agent_id, entry), health)

    return {
        "agent_id": agent_id,
        "kind": endpoint.kind,
        "capabilities": sorted(endpoint.capabilities),
        "status": outcome,
        "detail": result.detail,
        "probed": address,
        "rehosted_from": as_written if as_written != address else "",
        "tools": _described_tools(endpoint),
    }


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

    if body.persist:
        # Written first: a target the console said it saved and did not is
        # worse than one it refused to add. The file is the source of truth
        # for a configured entry, so an entry that reaches it is one.
        _persist(state, entries)

    registered = []
    origin = ORIGIN_CONFIG if body.persist else ORIGIN_REGISTRATION

    for agent_id, entry in entries.items():
        try:
            # Saved as written, run as this deployment can reach it. Inside a
            # container `127.0.0.1` means the container, and KIO1_AGENT_HOST
            # says where the real one is — but baking that into the file would
            # put a container's address in something the host shares.
            record = state.directory.register(
                agent_id, rehost_entry(agent_id, entry), origin, replace=True
            )
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


@app.put("/registry/agents/{agent_id}")
def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Replace one target's definition, in the directory and in the file.

    Args:
        agent_id: The target to replace.
        body: The new entry, and whether to save it.
        request: The incoming request, checked for the registry token.
        state: The live directory.

    Returns:
        A summary of the stored record.

    Raises:
        HTTPException: 400 when the entry is malformed or names another
            target, 503 when it could not be saved.
    """
    _require_registry_token(request)

    named = body.entry.get("agent_id", body.entry.get("id"))

    if isinstance(named, str) and named.strip() and named.strip() != agent_id:
        # Pasting another target's definition into the editor would save it
        # under this target's name — a "WeatherForecast" that calls a time
        # API and answers weather requests with an error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"This definition is for {named.strip()}, not {agent_id}. "
                f"Use Add to create {named.strip()}, or change agent_id."
            ),
        )

    try:
        record = state.directory.replace_entry(
            agent_id, rehost_entry(agent_id, body.entry)
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    if body.persist:
        _persist(state, {agent_id: body.entry})

    logger.info("Agent updated: agent_id=%s persisted=%s", agent_id, body.persist)

    return {"agent": record.to_summary()}


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
        HTTPException: 404 when it is unknown, 503 when the removal could not
            be saved.
    """
    _require_registry_token(request)

    record = state.directory.record_for(agent_id)

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown agent {agent_id}"
        )

    if record.origin == ORIGIN_CONFIG:
        _persist(state, {agent_id: None})

    state.directory.forget(agent_id, force=True)
    logger.info("Agent removed: agent_id=%s origin=%s", agent_id, record.origin)

    return {"agent_id": agent_id, "status": "removed"}


class RestoreRequest(BaseModel):
    """Which earlier version of a target to bring back."""

    version: int = Field(ge=1)


@app.get("/registry/agents/{agent_id}/history")
def agent_history(
    agent_id: str, request: Request, state: _State = Depends(get_state)
) -> dict[str, Any]:
    """Return every saved version of a target, newest first.

    Args:
        agent_id: The target.
        request: Checked for the registry token.
        state: The agent store.

    Returns:
        The versions; empty when the store keeps no history (the file).
    """
    _require_registry_token(request)

    return {"agent_id": agent_id, "versions": state.store.history(agent_id)}


@app.post("/registry/agents/{agent_id}/restore")
def restore_agent(
    agent_id: str,
    body: RestoreRequest,
    request: Request,
    state: _State = Depends(get_state),
) -> dict[str, Any]:
    """Bring a target back to an earlier version — or back at all, if removed.

    The restore is itself a new version, so it can be undone the same way.

    Args:
        agent_id: The target.
        body: The version to restore.
        request: Checked for the registry token.
        state: The agent store and the live directory.

    Returns:
        The restored target as the directory now holds it.

    Raises:
        HTTPException: 404 for an unknown version, 400 when it records a
            removal or is no longer valid.
    """
    _require_registry_token(request)

    version = next(
        (
            item
            for item in state.store.history(agent_id)
            if item["version"] == body.version
        ),
        None,
    )

    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{agent_id} has no version {body.version}.",
        )

    entry = version["entry"]

    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That version records the target's removal; pick one before it.",
        )

    try:
        record = state.directory.register(
            agent_id, rehost_entry(agent_id, entry), ORIGIN_CONFIG, replace=True
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    try:
        state.store.write({agent_id: entry}, action=ACTION_RESTORED)
    except AgentStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error

    logger.info("Agent restored: agent_id=%s version=%d", agent_id, body.version)

    return {"agent": record.to_summary(), "restored_version": body.version}


@app.get("/registry/export")
def export_agents(
    request: Request, state: _State = Depends(get_state)
) -> dict[str, Any]:
    """Return every target's definition in `dispatch.agents` form.

    For a backup, for moving agents to another KIO1, or for keeping them in
    git: the result can be pasted under `dispatch.agents` in a config.json,
    or posted to `/registry/agents` as a manifest.

    Args:
        request: Checked for the registry token.
        state: The agent store.

    Returns:
        `{"agents": {id: definition, ...}}`.
    """
    _require_registry_token(request)

    return {"agents": state.store.read()}


class PullRequest(BaseModel):
    """A local model to download."""

    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")


def _ollama_endpoint(config: Config) -> str:
    """Return Ollama's address, as configured for the planner."""
    endpoint = config.provider_options.get("endpoint")
    return (
        endpoint if isinstance(endpoint, str) and endpoint else "http://127.0.0.1:11434"
    )


@app.get("/system")
def system_status(
    request: Request, state: _State = Depends(get_state)
) -> dict[str, Any]:
    """Describe this machine and what it can run, for first-time setup.

    Args:
        request: Checked for the registry token.
        state: The live service.

    Returns:
        The hardware, Ollama's state, model suggestions marked for this
        machine, and the settings a setup step changes.
    """
    _require_registry_token(request)

    config = state.runner.config
    machine = hardware()
    ollama = ollama_status(_ollama_endpoint(config))

    return {
        "hardware": asdict(machine),
        "ollama": ollama,
        "suggestions": suggest(machine, ollama["models"]),
        "planner": {"provider": config.provider, "model": config.model},
        "answerer": {
            "provider": config.orchestration.review_provider or config.provider,
            "model": config.orchestration.review_model or config.model,
        },
        "workspace": config.dispatch.workspace.path,
        "pulls": state.pulls.status(),
    }


@app.post("/system/models/pull", status_code=status.HTTP_202_ACCEPTED)
def pull_model(
    body: PullRequest, request: Request, state: _State = Depends(get_state)
) -> dict[str, Any]:
    """Start downloading a local model; progress is at `GET /system/models/pull`.

    Args:
        body: The model.
        request: Checked for the registry token.
        state: The download manager.

    Returns:
        The download's progress so far.
    """
    _require_registry_token(request)

    endpoint = _ollama_endpoint(state.runner.config)

    if not ollama_status(endpoint)["running"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ollama is not running. Start it, or run start.py, which starts it.",
        )

    return state.pulls.start(endpoint, body.model).to_dict()


@app.get("/system/models/pull")
def pull_progress(
    request: Request, state: _State = Depends(get_state)
) -> dict[str, Any]:
    """Return every model download this service started, newest first."""
    _require_registry_token(request)

    return {"pulls": state.pulls.status()}
