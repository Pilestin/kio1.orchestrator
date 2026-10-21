import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from core.config_loader import Config
from core.formatter import parse_response
from core.prompt_loader import fill_catalog
from core.run_events import (
    FINISHED,
    RunEventBus,
    TokenRelay,
    emit,
    observed,
    observing,
)
from core.telemetry import trace_gen_ai_request, trace_operation
from dispatch.agent_registry import AgentRegistry
from dispatch.agent_transport import AgentTransport
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import (
    STATUS_OK,
    WorkflowResult,
    execute_workflow,
)
from dispatch.workflow_planner import WorkflowPlan, parse_plan
from providers.provider_client import supports_streaming

logger = logging.getLogger(__name__)


class SessionRecorder(Protocol):
    """The subset of the Session Manager client a run needs."""

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        """Record progress on a session."""
        ...

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Record one step outcome."""
        ...


@dataclass
class WorkflowRunner:
    """Turns one request into a plan, dispatches it, and records the outcome.

    The prompt file is read once at startup. The provider and its client are
    too, but the console may swap them for another provider while the
    service runs, so they are not frozen; a run reads them once, when it
    plans, and a swap reaches the next run. The
    set of targets is not: it changes while the service runs, so each run
    takes one snapshot and uses it for both the catalog the model sees and the
    routing that follows. Planning against one set of agents and dispatching
    against another would produce steps addressed to agents that had just gone
    away.
    """

    config: Config
    prompt_template: str
    provider: Any
    provider_client: Any
    recorder: SessionRecorder
    transport: AgentTransport
    directory: AgentDirectory
    scoreboard: Scoreboard
    events: RunEventBus | None = None

    def run(self, session_id: UUID, request_text: str) -> None:
        """Execute one workflow to completion, recording progress as it goes.

        Failures are recorded against the session and swallowed: this runs
        detached from the HTTP request that started it, so raising would only
        surface in the server log and leave the session stuck at `running`.

        Args:
            session_id: The session opened for this run.
            request_text: The user request to plan and execute.

        Returns:
            None.
        """
        attributes = {
            "session.id": str(session_id),
            "gen_ai.provider.name": self.config.provider,
            "gen_ai.request.model": self.config.model,
        }

        with (
            trace_operation("kio1.workflow.run", attributes),
            observing(self.events, str(session_id)),
        ):
            try:
                self._run(session_id, request_text)
            finally:
                # Always the last event, whatever happened, so a reader knows
                # the stream is over and stops waiting for more.
                emit(FINISHED)

    def _run(self, session_id: UUID, request_text: str) -> None:
        """Plan, dispatch, and record one run. See `run`."""
        self.recorder.update_session(session_id, status="running")
        emit("status", status="running")

        registry = self.directory.snapshot()

        try:
            plan = self._build_plan(session_id, request_text, registry)
        except Exception as error:
            logger.exception("Planning failed: session_id=%s", session_id)
            reason = f"{type(error).__name__}: {error}"
            self.recorder.update_session(session_id, status="failed", error=reason)
            emit("status", status="failed", error=reason)
            return

        emit(
            "plan",
            workflow_id=plan.workflow_id,
            execution_mode=plan.execution_mode,
            explanation=plan.explanation,
            steps=[
                {
                    "step_id": step.step_id,
                    "agent_id": step.agent_id,
                    "capability": step.capability,
                    "task": step.task,
                    "depends_on": list(step.depends_on),
                    "arguments": step.arguments,
                }
                for step in plan.steps
            ],
        )
        self.recorder.update_session(
            session_id,
            workflow_id=plan.workflow_id,
            execution_mode=plan.execution_mode,
        )

        try:
            result = execute_workflow(
                plan,
                registry,
                self.transport,
                timeout=self.config.dispatch.request_timeout,
                max_parallel_steps=self.config.dispatch.max_parallel_steps,
                workspace_data=self.config.dispatch.workspace.to_data(),
                policy=self.config.dispatch.policy,
                failover=self.config.dispatch.failover,
                scoreboard=self.scoreboard,
            )
        except Exception as error:
            logger.exception("Dispatch failed: session_id=%s", session_id)
            reason = f"{type(error).__name__}: {error}"
            self.recorder.update_session(session_id, status="failed", error=reason)
            emit("status", status="failed", error=reason)
            return

        self._record_results(session_id, plan, result)

        # A workflow whose release was refused by a gate did not complete,
        # whatever the individual calls did. Reporting it as completed puts
        # the wrong word in the first place anyone looks.
        outcome = "failed" if result.failed or result.blocked else "completed"
        self.recorder.update_session(session_id, status=outcome)
        emit(
            "status",
            status=outcome,
            steps=[
                {
                    "step_id": step.step_id,
                    "agent_id": step.agent_id,
                    "status": step.status,
                    "error": step.error,
                }
                for step in result.results
            ],
        )
        logger.info(
            "Workflow run finished: session_id=%s ok=%d error=%d",
            session_id,
            result.succeeded,
            result.failed,
        )

    def _build_plan(
        self, session_id: UUID, request_text: str, registry: AgentRegistry
    ) -> WorkflowPlan:
        """Ask the model for a workflow plan and validate it.

        Args:
            session_id: The session this run belongs to, for trace correlation.
            request_text: The user request.
            registry: The targets to describe in the prompt's catalog.

        Returns:
            The validated plan.

        Raises:
            Exception: Any provider or validation failure, handled by `run`.
        """
        messages = [{"role": "user", "content": request_text}]
        system_prompt = fill_catalog(self.prompt_template, registry)
        streaming = observed() and supports_streaming(self.provider)
        relay = TokenRelay()

        emit(
            "planning_started",
            provider=self.config.provider,
            model=self.config.model,
            targets=sorted(registry.agent_ids()),
            prompt_chars=len(system_prompt),
            streaming=streaming,
        )

        with trace_gen_ai_request(
            provider=self.config.provider,
            model=self.config.model,
            session_id=str(session_id),
            turn_number=1,
            message_count=len(messages) + 1,
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
        ):
            # Streamed only when someone can watch: the answer is identical
            # either way, and a provider that cannot stream still plans.
            extra = {"on_token": relay} if streaming else {}

            try:
                response = self.provider.send_request(
                    config=self.config,
                    client=self.provider_client,
                    system_prompt=system_prompt,
                    messages=messages,
                    **extra,
                )
            finally:
                relay.flush()

        content = self.provider.extract_content(response)
        emit("model_answer", text=content)

        return parse_plan(parse_response(content))

    def _record_results(
        self,
        session_id: UUID,
        plan: WorkflowPlan,
        result: WorkflowResult,
    ) -> None:
        """Record every step of a finished run.

        Args:
            session_id: The session the run belongs to.
            plan: The plan that was executed, used for each step's task text.
            result: The execution outcome.

        Returns:
            None.
        """
        planned = {step.step_id: step for step in plan.steps}

        for step in result.results:
            declared = planned.get(step.step_id)

            self.recorder.record_step(
                session_id,
                {
                    "step_id": step.step_id,
                    "agent_id": step.agent_id,
                    "capability": step.capability,
                    "task": declared.task if declared is not None else "",
                    "status": step.status,
                    "duration_ms": step.duration_ms,
                    "error": step.error,
                    "output": step.output if step.status == STATUS_OK else None,
                    "depends_on": (
                        list(declared.depends_on) if declared is not None else []
                    ),
                },
            )


def build_runner(
    config: Config,
    prompt_template: str,
    provider: Any,
    provider_client: Any,
    recorder: SessionRecorder,
    transport: AgentTransport | None = None,
    directory: AgentDirectory | None = None,
    scoreboard: Scoreboard | None = None,
    events: RunEventBus | None = None,
) -> WorkflowRunner:
    """Assemble a runner with the transport the configuration selects.

    Args:
        config: Application configuration.
        prompt_template: The prompt as read from disk, catalog placeholder
            still in it.
        provider: The loaded provider module.
        provider_client: The provider's client object.
        recorder: Session Manager client used to record progress.
        transport: Transport override, mainly for tests.
        directory: The live set of targets. Omitted builds one holding just
            the configured registry, which is how a run behaves with neither
            discovery nor health checks turned on.
        scoreboard: Recent outcomes shared across requests. Omitted builds
            an empty one, which records history but reorders nothing until
            selection is turned on.
        events: Where a run reports what it is doing while it does it.
            Omitted reports nowhere.

    Returns:
        A ready runner.
    """
    # The directory is settled first because the transport borrows its MCP
    # sessions: a step dispatched to an MCP server should reuse the one the
    # probe already started rather than starting a second copy.
    live_directory = (
        directory if directory is not None else AgentDirectory(config.dispatch.registry)
    )

    return WorkflowRunner(
        config=config,
        prompt_template=prompt_template,
        provider=provider,
        provider_client=provider_client,
        recorder=recorder,
        transport=(
            transport
            if transport is not None
            else build_transport(config.dispatch, live_directory.mcp_pool)
        ),
        directory=live_directory,
        scoreboard=(
            scoreboard
            if scoreboard is not None
            else Scoreboard(config.dispatch.selection)
        ),
        events=events,
    )
