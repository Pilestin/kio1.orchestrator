import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

from core.config_loader import DispatchConfig
from core.telemetry import (
    record_step_result,
    trace_step_dispatch,
    trace_workflow_execution,
)
from dispatch.agent_registry import AgentRegistry
from dispatch.agent_transport import AgentTransport, AgentTransportError
from dispatch.transport_factory import build_transport
from dispatch.workflow_planner import (
    ExecutionMessage,
    RoutingResult,
    WorkflowPlan,
    descendants_of,
    execution_levels,
    has_dependencies,
    parse_plan,
    route_plan,
)

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
STATUS_NOT_RUN = "not_run"


@dataclass(frozen=True)
class StepResult:
    """The outcome of a single workflow step.

    Attributes:
        step_id: Identifier of the step within the workflow.
        agent_id: Agent the step was addressed to.
        capability: Capability requested from the agent.
        status: One of `ok`, `error`, `skipped`, or `not_run`.
        duration_ms: Wall-clock time spent dispatching, zero when not dispatched.
        output: The agent's `output` object, when it answered successfully.
        error: A human-readable failure reason, when the step did not succeed.
    """

    step_id: str
    agent_id: str
    capability: str
    status: str
    duration_ms: int = 0
    output: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class WorkflowResult:
    """The outcome of executing a whole workflow plan."""

    workflow_id: str
    execution_mode: str
    results: tuple[StepResult, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> int:
        """Return the number of steps the agents answered successfully."""
        return sum(1 for result in self.results if result.status == STATUS_OK)

    @property
    def failed(self) -> int:
        """Return the number of steps that failed."""
        return sum(1 for result in self.results if result.status == STATUS_ERROR)

    @property
    def dispatched(self) -> int:
        """Return the number of steps that were actually sent to an agent."""
        return sum(
            1 for result in self.results if result.status in {STATUS_OK, STATUS_ERROR}
        )


def execute_workflow(
    plan: WorkflowPlan,
    registry: AgentRegistry,
    transport: AgentTransport,
    *,
    timeout: float,
    max_parallel_steps: int,
) -> WorkflowResult:
    """Route a workflow plan to agents and collect their results.

    Args:
        plan: The validated workflow plan.
        registry: Registry of agents KIO1 can reach.
        transport: Transport used to deliver execution messages.
        timeout: Seconds to wait for each agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches in
            `parallel` mode.

    Returns:
        One `StepResult` per plan step, in the plan's own step order.
    """
    routing = route_plan(plan, registry)

    with trace_workflow_execution(
        workflow_id=plan.workflow_id,
        execution_mode=plan.execution_mode,
        step_count=len(plan.steps),
        dispatchable_step_count=len(routing.messages),
    ):
        if has_dependencies(plan):
            # Declared dependencies define the order, so execution_mode only
            # decides whether independent steps share a level concurrently.
            dispatched = _execute_levels(
                plan,
                routing,
                registry,
                transport,
                timeout=timeout,
                max_parallel_steps=max_parallel_steps,
                concurrent=plan.execution_mode != "sequential",
            )
        elif plan.execution_mode == "parallel":
            dispatched = _dispatch_concurrently(
                routing.messages,
                registry,
                transport,
                timeout=timeout,
                max_parallel_steps=max_parallel_steps,
            )
        else:
            if plan.execution_mode == "mixed":
                # Without declared dependencies there is no partial order to
                # derive, so sequential is the only safe reading of "mixed".
                logger.warning(
                    "execution_mode=mixed carries no depends_on information; "
                    "running sequentially: workflow_id=%s",
                    plan.workflow_id,
                )

            dispatched = _execute_sequential(
                routing.messages,
                registry,
                transport,
                timeout=timeout,
            )

    results = _merge_results(plan, routing, dispatched)

    logger.info(
        "Workflow executed: workflow_id=%s mode=%s ok=%d error=%d",
        plan.workflow_id,
        plan.execution_mode,
        sum(1 for result in results if result.status == STATUS_OK),
        sum(1 for result in results if result.status == STATUS_ERROR),
    )

    return WorkflowResult(
        workflow_id=plan.workflow_id,
        execution_mode=plan.execution_mode,
        results=tuple(results),
    )


def _execute_sequential(
    messages: tuple[ExecutionMessage, ...],
    registry: AgentRegistry,
    transport: AgentTransport,
    *,
    timeout: float,
) -> dict[str, StepResult]:
    """Dispatch messages one at a time, stopping at the first failure.

    Later steps in a sequential workflow are assumed to consume earlier
    results, so continuing past a failure would send agents work built on
    output that was never produced.

    Args:
        messages: Execution messages in plan order.
        registry: Registry used to resolve each message's endpoint.
        transport: Transport used to deliver execution messages.
        timeout: Seconds to wait for each agent's reply.

    Returns:
        Step results keyed by step id, covering only the steps that ran.
    """
    results: dict[str, StepResult] = {}

    for message in messages:
        result = _dispatch_step(message, registry, transport, timeout=timeout)
        results[message.step_id] = result

        if result.status == STATUS_ERROR:
            logger.warning(
                "Sequential workflow halted after a failed step: step_id=%s",
                message.step_id,
            )
            break

    return results


def _dispatch_concurrently(
    messages: tuple[ExecutionMessage, ...],
    registry: AgentRegistry,
    transport: AgentTransport,
    *,
    timeout: float,
    max_parallel_steps: int,
) -> dict[str, StepResult]:
    """Dispatch every message concurrently and wait for all of them.

    Args:
        messages: Execution messages in plan order.
        registry: Registry used to resolve each message's endpoint.
        transport: Transport used to deliver execution messages.
        timeout: Seconds to wait for each agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches.

    Returns:
        Step results keyed by step id.
    """
    if not messages:
        return {}

    worker_count = max(1, min(max_parallel_steps, len(messages)))
    results: dict[str, StepResult] = {}

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="kio1-dispatch",
    ) as pool:
        futures = {}

        for message in messages:
            # A fresh copy per task: OpenTelemetry stores the active span in a
            # contextvar, and a single Context object cannot be entered by two
            # threads at once. Without this, dispatch spans would be orphaned
            # from the workflow span.
            context = contextvars.copy_context()
            future = pool.submit(
                context.run,
                _dispatch_step,
                message,
                registry,
                transport,
                timeout=timeout,
            )
            futures[future] = message

        for future, message in futures.items():
            results[message.step_id] = future.result()

    return results


def _execute_levels(
    plan: WorkflowPlan,
    routing: RoutingResult,
    registry: AgentRegistry,
    transport: AgentTransport,
    *,
    timeout: float,
    max_parallel_steps: int,
    concurrent: bool,
) -> dict[str, StepResult]:
    """Dispatch a plan level by level, honouring its declared dependencies.

    A step whose dependency failed or was skipped is never dispatched: its
    input was never produced, so asking an agent to act on it would waste a
    call and invite a misleading answer. Such steps surface as `not_run`.

    Args:
        plan: The validated plan, read for its dependency graph.
        routing: The routing outcome, whose skipped steps also block dependents.
        registry: Registry used to resolve each message's endpoint.
        transport: Transport used to deliver execution messages.
        timeout: Seconds to wait for each agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches within a level.
        concurrent: Whether steps in the same level may run at the same time.

    Returns:
        Step results keyed by step id, covering only the steps that ran.
    """
    by_step = {message.step_id: message for message in routing.messages}
    results: dict[str, StepResult] = {}
    blocked: set[str] = set()

    for skipped in routing.skipped:
        blocked.update(descendants_of(plan, skipped.step_id))

    for level in execution_levels(plan):
        runnable = [
            by_step[step_id]
            for step_id in level
            if step_id in by_step and step_id not in blocked
        ]

        if not runnable:
            continue

        prepared = [_with_upstream_data(message, plan, results) for message in runnable]

        if concurrent and len(prepared) > 1:
            level_results = _dispatch_concurrently(
                tuple(prepared),
                registry,
                transport,
                timeout=timeout,
                max_parallel_steps=max_parallel_steps,
            )
        else:
            level_results = {
                message.step_id: _dispatch_step(
                    message, registry, transport, timeout=timeout
                )
                for message in prepared
            }

        results.update(level_results)

        for step_id, result in level_results.items():
            if result.status == STATUS_ERROR:
                blocked.update(descendants_of(plan, step_id))
                logger.warning(
                    "Blocking steps that depend on the failed step: step_id=%s",
                    step_id,
                )

    return results


def _with_upstream_data(
    message: ExecutionMessage,
    plan: WorkflowPlan,
    results: dict[str, StepResult],
) -> ExecutionMessage:
    """Attach the outputs of a step's dependencies to its `data` field.

    The KIO1-KIO10 protocol describes `data` as the extension point for
    inputs a KIO needs beyond the free-text task, so upstream results travel
    there rather than in any new field. Steps that declare no dependencies
    keep an empty `data`, exactly as before.

    Args:
        message: The execution message about to be dispatched.
        plan: The plan, read for the step's declared dependencies.
        results: Results of the steps that have already run.

    Returns:
        The message, with `data.upstream` populated when there is anything
        to pass on.
    """
    step = next(
        (candidate for candidate in plan.steps if candidate.step_id == message.step_id),
        None,
    )

    if step is None or not step.depends_on:
        return message

    upstream = {
        dependency: results[dependency].output
        for dependency in step.depends_on
        if dependency in results and results[dependency].output is not None
    }

    if not upstream:
        return message

    return replace(message, data={**message.data, "upstream": upstream})


def _dispatch_step(
    message: ExecutionMessage,
    registry: AgentRegistry,
    transport: AgentTransport,
    *,
    timeout: float,
) -> StepResult:
    """Deliver one execution message and interpret the agent's reply.

    Args:
        message: The execution message to deliver.
        registry: Registry used to resolve the message's endpoint.
        transport: Transport used to deliver the message.
        timeout: Seconds to wait for the agent's reply.

    Returns:
        The step's result. Transport and protocol failures are captured as an
        `error` result rather than raised, so one unreachable agent cannot
        abort the whole workflow.
    """
    agent = registry.resolve(message.agent_id)

    if agent is None:
        # Unreachable in practice: route_plan only emits messages for agents
        # the registry resolved. Kept so this function is safe to call alone.
        return StepResult(
            step_id=message.step_id,
            agent_id=message.agent_id,
            capability=message.capability,
            status=STATUS_ERROR,
            error=f"No endpoint configured for {message.agent_id}",
        )

    started_at = perf_counter()

    with trace_step_dispatch(
        workflow_id=message.workflow_id,
        step_id=message.step_id,
        agent_id=message.agent_id,
        capability=message.capability,
    ):
        try:
            reply = transport.send(message, agent, timeout)
        except Exception as error:
            elapsed_ms = int((perf_counter() - started_at) * 1000)

            if isinstance(error, AgentTransportError):
                logger.warning(
                    "Step dispatch failed: step_id=%s agent_id=%s duration_ms=%d",
                    message.step_id,
                    message.agent_id,
                    elapsed_ms,
                )
                detail = str(error)
            else:
                # A transport is contracted to raise AgentTransportError.
                # Anything else is a bug, but letting it escape would discard
                # the results of every step that already succeeded, so it is
                # reported as a failed step and logged with its traceback.
                logger.exception(
                    "Transport raised an unexpected error: step_id=%s agent_id=%s",
                    message.step_id,
                    message.agent_id,
                )
                detail = f"{type(error).__name__}: {error}"

            record_step_result(
                agent_id=message.agent_id,
                capability=message.capability,
                status=STATUS_ERROR,
                duration_seconds=elapsed_ms / 1000,
            )
            return StepResult(
                step_id=message.step_id,
                agent_id=message.agent_id,
                capability=message.capability,
                status=STATUS_ERROR,
                duration_ms=elapsed_ms,
                error=detail,
            )

    elapsed_ms = int((perf_counter() - started_at) * 1000)
    _warn_on_correlation_mismatch(message, reply)

    status = reply.get("status", STATUS_OK)
    if status == STATUS_ERROR:
        error_text = reply.get("error")
        record_step_result(
            agent_id=message.agent_id,
            capability=message.capability,
            status=STATUS_ERROR,
            duration_seconds=elapsed_ms / 1000,
        )
        return StepResult(
            step_id=message.step_id,
            agent_id=message.agent_id,
            capability=message.capability,
            status=STATUS_ERROR,
            duration_ms=elapsed_ms,
            error=(
                error_text
                if isinstance(error_text, str) and error_text
                else f"{message.agent_id} reported an unspecified error"
            ),
        )

    output = reply.get("output")

    logger.info(
        "Step completed: step_id=%s agent_id=%s duration_ms=%d",
        message.step_id,
        message.agent_id,
        elapsed_ms,
    )
    record_step_result(
        agent_id=message.agent_id,
        capability=message.capability,
        status=STATUS_OK,
        duration_seconds=elapsed_ms / 1000,
    )

    return StepResult(
        step_id=message.step_id,
        agent_id=message.agent_id,
        capability=message.capability,
        status=STATUS_OK,
        duration_ms=elapsed_ms,
        output=output if isinstance(output, dict) else None,
    )


def _warn_on_correlation_mismatch(
    message: ExecutionMessage,
    reply: dict[str, Any],
) -> None:
    """Log when a reply does not echo the ids it was correlated by.

    The protocol correlates replies to steps through `workflow_id` and
    `step_id`. Mismatches are logged rather than rejected, so an agent that
    is still being brought up does not fail the whole workflow.

    Args:
        message: The execution message that was sent.
        reply: The agent's decoded reply.

    Returns:
        None.
    """
    for field_name, expected in (
        ("workflow_id", message.workflow_id),
        ("step_id", message.step_id),
    ):
        actual = reply.get(field_name)

        if actual is None:
            logger.warning(
                "Reply omits %s: step_id=%s agent_id=%s",
                field_name,
                message.step_id,
                message.agent_id,
            )
        elif actual != expected:
            logger.warning(
                "Reply %s mismatch: expected=%r actual=%r agent_id=%s",
                field_name,
                expected,
                actual,
                message.agent_id,
            )


def _merge_results(
    plan: WorkflowPlan,
    routing: RoutingResult,
    dispatched: dict[str, StepResult],
) -> list[StepResult]:
    """Combine dispatched, skipped, and never-run steps in plan order.

    Args:
        plan: The workflow plan, used for step order.
        routing: The routing outcome, carrying the skipped steps.
        dispatched: Results of the steps that were actually dispatched.

    Returns:
        One result per plan step, in the plan's own order.
    """
    skipped_reasons = {step.step_id: step.reason for step in routing.skipped}
    results: list[StepResult] = []

    for step in plan.steps:
        if step.step_id in dispatched:
            results.append(dispatched[step.step_id])
            continue

        if step.step_id in skipped_reasons:
            results.append(
                StepResult(
                    step_id=step.step_id,
                    agent_id=step.agent_id,
                    capability=step.capability,
                    status=STATUS_SKIPPED,
                    error=skipped_reasons[step.step_id],
                )
            )
            continue

        results.append(
            StepResult(
                step_id=step.step_id,
                agent_id=step.agent_id,
                capability=step.capability,
                status=STATUS_NOT_RUN,
                error="An earlier step in the sequential workflow failed",
            )
        )

    return results


def render_summary(result: WorkflowResult) -> str:
    """Render a workflow result as a compact terminal table.

    Args:
        result: The executed workflow result.

    Returns:
        A multi-line, printable summary.
    """
    lines = [
        (
            f"Workflow {result.workflow_id} ({result.execution_mode}): "
            f"{result.succeeded} ok, {result.failed} error, "
            f"{len(result.results) - result.dispatched} not dispatched"
        ),
    ]

    for step in result.results:
        detail = ""

        if step.status == STATUS_OK:
            detail = f"{step.duration_ms} ms"
        elif step.error:
            detail = step.error

        lines.append(
            f"  {step.step_id:<4} {step.agent_id:<6} {step.capability:<28} "
            f"{step.status:<9} {detail}"
        )

    return "\n".join(lines)


def dispatch_plan(
    parsed_response: Any,
    dispatch_config: DispatchConfig,
    transport: AgentTransport | None = None,
) -> WorkflowResult | None:
    """Validate a decoded model response and dispatch it to KIO agents.

    Args:
        parsed_response: The already-decoded workflow plan from the model.
        dispatch_config: Dispatch settings, including the agent registry.
        transport: The transport to dispatch with. Callers that dispatch
            more than once should build one transport and reuse it: a NATS
            transport owns a connection and a thread. Omitting it builds a
            throwaway transport, which is only cheap for HTTP.

    Returns:
        The workflow result, or None when dispatch is disabled or the
        response is not a usable workflow plan. A malformed plan is reported
        as None rather than raised, so a bad model response cannot end the
        interactive session.
    """
    if not dispatch_config.enabled:
        return None

    try:
        plan = parse_plan(parsed_response)
    except ValueError as error:
        logger.warning("Response is not a dispatchable workflow plan: %s", error)
        return None

    return execute_workflow(
        plan,
        dispatch_config.registry,
        transport if transport is not None else build_transport(dispatch_config),
        timeout=dispatch_config.request_timeout,
        max_parallel_steps=dispatch_config.max_parallel_steps,
    )
