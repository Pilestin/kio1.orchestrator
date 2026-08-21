import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from core.config_loader import Config
from core.formatter import parse_response
from core.telemetry import trace_gen_ai_request, trace_operation
from dispatch.agent_transport import AgentTransport
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import (
    STATUS_OK,
    WorkflowResult,
    execute_workflow,
)
from dispatch.workflow_planner import WorkflowPlan, parse_plan

logger = logging.getLogger(__name__)


class SessionRecorder(Protocol):
    """The subset of the Session Manager client a run needs."""

    def update_session(self, session_id: UUID, **fields: Any) -> None:
        """Record progress on a session."""
        ...

    def record_step(self, session_id: UUID, step: dict[str, Any]) -> None:
        """Record one step outcome."""
        ...


@dataclass(frozen=True)
class WorkflowRunner:
    """Turns one request into a plan, dispatches it, and records the outcome.

    Everything the run needs is resolved once at service startup: the provider
    client and system prompt are reused across requests rather than reloaded
    per call.
    """

    config: Config
    system_prompt: str
    provider: Any
    provider_client: Any
    recorder: SessionRecorder
    transport: AgentTransport

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

        with trace_operation("kio1.workflow.run", attributes):
            self.recorder.update_session(session_id, status="running")

            try:
                plan = self._build_plan(session_id, request_text)
            except Exception as error:
                logger.exception("Planning failed: session_id=%s", session_id)
                self.recorder.update_session(
                    session_id,
                    status="failed",
                    error=f"{type(error).__name__}: {error}",
                )
                return

            self.recorder.update_session(
                session_id,
                workflow_id=plan.workflow_id,
                execution_mode=plan.execution_mode,
            )

            try:
                result = execute_workflow(
                    plan,
                    self.config.dispatch.registry,
                    self.transport,
                    timeout=self.config.dispatch.request_timeout,
                    max_parallel_steps=self.config.dispatch.max_parallel_steps,
                )
            except Exception as error:
                logger.exception("Dispatch failed: session_id=%s", session_id)
                self.recorder.update_session(
                    session_id,
                    status="failed",
                    error=f"{type(error).__name__}: {error}",
                )
                return

            self._record_results(session_id, plan, result)

            self.recorder.update_session(
                session_id,
                status="failed" if result.failed else "completed",
            )
            logger.info(
                "Workflow run finished: session_id=%s ok=%d error=%d",
                session_id,
                result.succeeded,
                result.failed,
            )

    def _build_plan(self, session_id: UUID, request_text: str) -> WorkflowPlan:
        """Ask the model for a workflow plan and validate it.

        Args:
            session_id: The session this run belongs to, for trace correlation.
            request_text: The user request.

        Returns:
            The validated plan.

        Raises:
            Exception: Any provider or validation failure, handled by `run`.
        """
        messages = [{"role": "user", "content": request_text}]

        with trace_gen_ai_request(
            provider=self.config.provider,
            model=self.config.model,
            session_id=str(session_id),
            turn_number=1,
            message_count=len(messages) + 1,
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
        ):
            response = self.provider.send_request(
                config=self.config,
                client=self.provider_client,
                system_prompt=self.system_prompt,
                messages=messages,
            )

        content = self.provider.extract_content(response)

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
    system_prompt: str,
    provider: Any,
    provider_client: Any,
    recorder: SessionRecorder,
    transport: AgentTransport | None = None,
) -> WorkflowRunner:
    """Assemble a runner with the transport the configuration selects.

    Args:
        config: Application configuration.
        system_prompt: The loaded system prompt.
        provider: The loaded provider module.
        provider_client: The provider's client object.
        recorder: Session Manager client used to record progress.
        transport: Transport override, mainly for tests.

    Returns:
        A ready runner.
    """
    return WorkflowRunner(
        config=config,
        system_prompt=system_prompt,
        provider=provider,
        provider_client=provider_client,
        recorder=recorder,
        transport=(
            transport if transport is not None else build_transport(config.dispatch)
        ),
    )
