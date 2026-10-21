"""The KIO1 web service: the Workflow API and the operator console.

Started with `uvicorn app:app`. It sits beside `main.py` on purpose —
one is how you drive KIO1 from a terminal, the other is how you drive
it from a browser or another program, and neither is buried.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from hmac import compare_digest
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config_loader import Config, Workspace, load_config, rehost_entry
from core.config_writer import (
    ConfigWriteError,
    config_path,
    read_agents,
    save_agents,
    save_settings,
    writing_enabled,
)
from core.prompt_loader import load_prompt
from core.service_telemetry import TraceContextMiddleware
from core.session_client import NullSessionClient, SessionClient, SessionClientError
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
    session_client: SessionClient | NullSessionClient
    directory: AgentDirectory
    scoreboard: Scoreboard


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
        sources=_configured_entries(),
    )
    directory.start()
    # One scoreboard for the process, so history accumulates across
    # requests instead of resetting with every workflow.
    scoreboard = Scoreboard(config.dispatch.selection)

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
            scoreboard=scoreboard,
        ),
        session_client=session_client,
        directory=directory,
        scoreboard=scoreboard,
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


STATIC_DIR = Path(__file__).with_name("static")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:  # pragma: no cover - only a trimmed deployment lacks it
    logger.warning("Console assets are missing at %s", STATIC_DIR)


@lru_cache(maxsize=1)
def _console_page() -> str:
    """Return the console's HTML, read once.

    Returns:
        The page source, or a plain message when the file is missing so a
        trimmed deployment answers something readable rather than a traceback.
    """
    try:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
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


def _configured_entries() -> dict[str, Any]:
    """Return `dispatch.agents` as the file holds it, or nothing on failure.

    Returns:
        The raw entries. An unreadable file is logged rather than raised on:
        the service has already parsed its configuration successfully by the
        time this runs, and losing the console's edit forms is a smaller
        failure than refusing to start.
    """
    try:
        return read_agents()
    except ConfigWriteError as error:
        logger.warning("Could not read the configured agents: %s", error)
        return {}


def _persist(changes: dict[str, Any | None]) -> None:
    """Apply changes to `dispatch.agents` in the configuration file.

    Args:
        changes: Agent id to entry, or to None to remove it.

    Raises:
        HTTPException: 503 when the file could not be written, naming why.
    """
    try:
        agents = read_agents()

        for agent_id, entry in changes.items():
            if entry is None:
                agents.pop(agent_id, None)
            else:
                agents[agent_id] = entry

        save_agents(agents)
    except ConfigWriteError as error:
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
        The configuration path and whether it is writable, so the console can
        say up front that Save will not stick rather than after someone tries.
    """
    _require_registry_token(request)

    return {
        "config_path": str(config_path()),
        "writable": writing_enabled(),
    }


class SettingsRequest(BaseModel):
    """The settings the console may change while the service runs.

    Every field is optional: the console sends what changed, and anything
    absent is left as it is.
    """

    model: str | None = None
    temperature: float | None = None
    request_timeout: float | None = None
    workspace_path: str | None = None
    workspace_entry_point: str | None = None


def _settings_view(state: _State) -> dict[str, Any]:
    """Return the current planning settings, and what may be chosen instead.

    Args:
        state: The live service, for the configuration the runner is using.

    Returns:
        What is in force now, the models the provider can offer, and where a
        change would be saved.
    """
    config = state.runner.config
    workspace = config.dispatch.workspace
    models: list[str] = []
    models_error = ""

    lister = getattr(state.runner.provider, "list_models", None)

    if lister is None:
        # Not every provider can say. An OpenAI or Anthropic deployment names
        # its model in the file, and a picker that silently offers nothing
        # would read as "there are none" rather than "I cannot ask".
        models_error = (
            f"The {config.provider} provider cannot list its models from here; "
            "type the name you want."
        )
    else:
        try:
            models = lister(config)
        except Exception as error:  # noqa: BLE001 - shown, not swallowed
            # Listing is a convenience. A provider that cannot answer must not
            # take the page down with it, and the reason is reported rather
            # than hidden: "no models" and "the provider is not running" are
            # different problems, and an empty list looks like the first.
            models_error = f"Models could not be listed: {error}"

    if config.model and config.model not in models:
        # The model in force is always offerable, even when the provider has
        # since forgotten it: a picker that cannot show the current value
        # looks like it changed something by being opened.
        models = sorted([*models, config.model])

    return {
        "provider": config.provider,
        "model": config.model,
        "models": models,
        "models_error": models_error,
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

    if not updates:
        return _settings_view(state)

    try:
        save_settings(updates)
    except ConfigWriteError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error

    _apply_settings(state, updates)

    if "model" in updates:
        # A local model has to be read into memory before it can answer, and
        # that read is charged to whichever request triggers it. Without this
        # the first run after a change pays it and frequently times out, which
        # reads as "the new model is broken" rather than "it was cold".
        background.add_task(_preload_model, state)

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
        _persist(dict.fromkeys(entries, None) | entries)

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
        HTTPException: 400 when the entry is malformed, 503 when it could not
            be saved.
    """
    _require_registry_token(request)

    try:
        record = state.directory.replace_entry(
            agent_id, rehost_entry(agent_id, body.entry)
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    if body.persist:
        _persist({agent_id: body.entry})

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
        _persist({agent_id: None})

    state.directory.forget(agent_id, force=True)
    logger.info("Agent removed: agent_id=%s origin=%s", agent_id, record.origin)

    return {"agent_id": agent_id, "status": "removed"}
