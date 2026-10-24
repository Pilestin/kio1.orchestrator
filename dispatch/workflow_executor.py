import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

from core.config_loader import DispatchConfig
from core.run_events import emit
from core.telemetry import (
    record_step_result,
    trace_step_dispatch,
    trace_workflow_execution,
)
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.agent_transport import AgentTransport, AgentTransportError
from dispatch.failover import FailoverConfig
from dispatch.policy import Policy, applies_to, apply_gates, providers_of
from dispatch.scoreboard import Scoreboard
from dispatch.transport_factory import build_transport
from dispatch.workflow_planner import (
    ExecutionMessage,
    PlanStep,
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

# Optional fields an agent may put in its `output` to say what it concluded,
# separately from whether the call succeeded. An agent that reports code is
# clean has *succeeded*; without reading these, "nothing wrong here" and
# "found the defect" are indistinguishable in a result table.
VERDICT_DEFECT = "defect"
VERDICT_CLEAN = "clean"
VERDICT_INCONCLUSIVE = "inconclusive"

_KNOWN_VERDICTS = frozenset({VERDICT_DEFECT, VERDICT_CLEAN, VERDICT_INCONCLUSIVE})


@dataclass(frozen=True)
class StepAttempt:
    """One target's answer to a step, kept even when another one followed.

    Attributes:
        agent_id: The target that was asked.
        status: `ok` or `error`.
        duration_ms: Wall-clock time this attempt took.
        error: Why it failed, when it did.
    """

    agent_id: str
    status: str
    duration_ms: int = 0
    error: str | None = None


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
        verdict: What the agent concluded, when it said so: `defect`, `clean`,
            or `inconclusive`. Independent of `status`, which only says whether
            the call itself worked.
        review_required: Whether the agent asked for a human to look, usually
            because it was not confident.
        attempts: Every target this step was tried against, in order. One
            entry is the ordinary case; more than one means the step was
            carried out somewhere else after the first could not.
        planned_agent_id: The agent the plan named, when the step ran
            somewhere else. None when it ran where it was sent, so a reader
            never has to compare two fields to find out whether it moved.
    """

    step_id: str
    agent_id: str
    capability: str
    status: str
    duration_ms: int = 0
    output: dict[str, Any] | None = None
    error: str | None = None
    verdict: str | None = None
    review_required: bool = False
    attempts: tuple[StepAttempt, ...] = ()
    planned_agent_id: str | None = None


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
    def blocked(self) -> int:
        """Return the number of steps that were stopped before they could run.

        A step is blocked when a policy gate refused it, or when something it
        depends on did not complete. Either way the workflow did not do what
        the plan asked, which is a different outcome from every step
        succeeding — and from a step being skipped because the agent it named
        is simply not deployed.
        """
        return sum(1 for result in self.results if result.status == STATUS_NOT_RUN)

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
    workspace_data: dict[str, Any] | None = None,
    policy: Policy | None = None,
    failover: FailoverConfig | None = None,
    scoreboard: Scoreboard | None = None,
) -> WorkflowResult:
    """Route a workflow plan to agents and collect their results.

    Args:
        plan: The validated workflow plan.
        registry: Registry of agents KIO1 can reach.
        transport: Transport used to deliver execution messages.
        timeout: Seconds to wait for each agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches in
            `parallel` mode.
        workspace_data: The `data` fragment naming the code under analysis.
        policy: Gates deciding what must have passed before a step may run.
            Omitted runs the plan exactly as it was produced.
        failover: Whether a step may be carried out by a target other than the
            one the plan named. Omitted keeps every step where it was sent.
        scoreboard: Recent outcomes, used to order the candidates and
            updated with what this run learns. Omitted leaves the order
            alone and records nothing.

    Returns:
        One `StepResult` per plan step, in the plan's own step order.
    """
    policy = policy if policy is not None else Policy()
    failover = failover if failover is not None else FailoverConfig()
    # Gates add the ordering they need before routing, so a guarded step is
    # never dispatched alongside the step that has to clear it.
    plan = apply_gates(plan, policy)
    routing = route_plan(
        plan,
        registry,
        workspace_data=workspace_data,
        failover=failover,
        scoreboard=scoreboard,
    )

    with trace_workflow_execution(
        workflow_id=plan.workflow_id,
        execution_mode=plan.execution_mode,
        step_count=len(plan.steps),
        dispatchable_step_count=len(routing.messages),
    ):
        if has_dependencies(plan) or applies_to(plan, policy):
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
                policy=policy,
                scoreboard=scoreboard,
            )
        elif plan.execution_mode == "parallel":
            dispatched = _dispatch_concurrently(
                routing.messages,
                registry,
                transport,
                timeout=timeout,
                max_parallel_steps=max_parallel_steps,
                alternatives=routing.alternatives,
                scoreboard=scoreboard,
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
                alternatives=routing.alternatives,
                scoreboard=scoreboard,
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
    alternatives: dict[str, tuple[AgentEndpoint, ...]] | None = None,
    scoreboard: Scoreboard | None = None,
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
        alternatives: Further candidates per step id, from routing.

    Returns:
        Step results keyed by step id, covering only the steps that ran.
    """
    alternatives = alternatives if alternatives is not None else {}
    results: dict[str, StepResult] = {}

    for message in messages:
        result = _dispatch_step(
            message,
            _candidates_for_message(message, registry, alternatives),
            transport,
            timeout=timeout,
            scoreboard=scoreboard,
        )
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
    alternatives: dict[str, tuple[AgentEndpoint, ...]] | None = None,
    scoreboard: Scoreboard | None = None,
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
                _candidates_for_message(
                    message, registry, alternatives if alternatives else {}
                ),
                transport,
                timeout=timeout,
                scoreboard=scoreboard,
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
    policy: Policy | None = None,
    scoreboard: Scoreboard | None = None,
) -> dict[str, StepResult]:
    """Dispatch a plan level by level, honouring its declared dependencies.

    A step whose dependency failed or was skipped is never dispatched: its
    input was never produced, so asking an agent to act on it would waste a
    call and invite a misleading answer. Such steps surface as `not_run`.

    A step barred by a policy gate is stopped the same way, with the gate's
    reason recorded in its place.

    Args:
        plan: The validated plan, read for its dependency graph.
        routing: The routing outcome, whose skipped steps also block dependents.
        registry: Registry used to resolve each message's endpoint.
        transport: Transport used to deliver execution messages.
        timeout: Seconds to wait for each agent's reply.
        max_parallel_steps: Upper bound on concurrent dispatches within a level.
        concurrent: Whether steps in the same level may run at the same time.
        policy: Gates deciding what must have passed before a step may run.

    Returns:
        Step results keyed by step id, covering only the steps that ran and
        the steps a gate stopped.
    """
    policy = policy if policy is not None else Policy()
    by_step = {message.step_id: message for message in routing.messages}
    steps_by_id = {step.step_id: step for step in plan.steps}
    results: dict[str, StepResult] = {}
    blocked: set[str] = set()

    for skipped in routing.skipped:
        blocked.update(descendants_of(plan, skipped.step_id))

    def block_if_gated(message: ExecutionMessage) -> bool:
        """Record a policy block for a step, and report whether it was blocked."""
        reason = _gate_block_reason(steps_by_id[message.step_id], plan, policy, results)

        if reason is None:
            return False

        logger.warning(
            "Step blocked by policy: step_id=%s capability=%s reason=%s",
            message.step_id,
            message.capability,
            reason,
        )
        results[message.step_id] = StepResult(
            step_id=message.step_id,
            agent_id=message.agent_id,
            capability=message.capability,
            status=STATUS_NOT_RUN,
            error=reason,
        )
        blocked.add(message.step_id)
        blocked.update(descendants_of(plan, message.step_id))

        return True

    def note_failure(step_id: str, result: StepResult) -> None:
        """Stop everything downstream of a step that failed."""
        if result.status != STATUS_ERROR:
            return

        blocked.update(descendants_of(plan, step_id))
        logger.warning(
            "Blocking steps that depend on the failed step: step_id=%s", step_id
        )

    for level in execution_levels(plan):
        candidates = [
            by_step[step_id]
            for step_id in level
            if step_id in by_step and step_id not in blocked
        ]

        if not candidates:
            continue

        if concurrent and len(candidates) > 1:
            # Steps sharing a level really do run at the same time, so every
            # gate is evaluated against what finished in earlier levels.
            runnable = [
                message for message in candidates if not block_if_gated(message)
            ]

            if not runnable:
                continue

            level_results = _dispatch_concurrently(
                tuple(
                    _with_upstream_data(message, plan, results) for message in runnable
                ),
                registry,
                transport,
                timeout=timeout,
                max_parallel_steps=max_parallel_steps,
                alternatives=routing.alternatives,
                scoreboard=scoreboard,
            )
            results.update(level_results)

            for step_id, result in level_results.items():
                note_failure(step_id, result)

            continue

        # One at a time, so a gate sees what the step just before it reported
        # even when both sit in the same level. A plan with no declared
        # dependencies is one single level, and this is the only place a
        # review request can stop the steps that follow it.
        for message in candidates:
            if message.step_id in blocked or block_if_gated(message):
                continue

            prepared = _with_upstream_data(message, plan, results)
            result = _dispatch_step(
                prepared,
                _candidates_for_message(prepared, registry, routing.alternatives),
                transport,
                timeout=timeout,
                scoreboard=scoreboard,
            )
            results[message.step_id] = result
            note_failure(message.step_id, result)

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


def _candidates_for_message(
    message: ExecutionMessage,
    registry: AgentRegistry,
    alternatives: dict[str, tuple[AgentEndpoint, ...]],
) -> tuple[AgentEndpoint, ...]:
    """Return the targets a step may be tried against, best first.

    Args:
        message: The execution message, addressed to the first candidate.
        registry: Registry used to resolve that first candidate.
        alternatives: Further candidates per step id, from routing.

    Returns:
        The full ordered list.
    """
    first = registry.resolve(message.agent_id)
    rest = alternatives.get(message.step_id, ())

    return ((first,) if first is not None else ()) + rest


def _dispatch_step(
    message: ExecutionMessage,
    candidates: tuple[AgentEndpoint, ...],
    transport: AgentTransport,
    *,
    timeout: float,
    scoreboard: Scoreboard | None = None,
) -> StepResult:
    """Deliver a step, moving to the next target when one cannot carry it out.

    The plan named an agent, but what it asked for was a capability. When the
    named agent fails and another target declares the same capability, the
    step is tried there rather than abandoned — and every attempt is recorded,
    so a step that moved never looks like one that ran where it was sent.

    Args:
        message: The execution message, addressed to the first candidate.
        candidates: Targets to try, in order.
        transport: Transport used to deliver the message.
        timeout: Seconds to wait for each reply.
        scoreboard: Recorded with the outcome of every attempt, including
            the ones that lost the step to a substitute. That a target
            failed is exactly what the next routing decision needs.

    Returns:
        The step's result, carrying the whole attempt history. Transport and
        protocol failures are captured as an `error` result rather than
        raised, so one unreachable agent cannot abort the whole workflow.
    """
    if not candidates:
        # Unreachable in practice: route_plan only emits messages for steps
        # something can carry out. Kept so this is safe to call alone.
        return StepResult(
            step_id=message.step_id,
            agent_id=message.agent_id,
            capability=message.capability,
            status=STATUS_ERROR,
            error=f"No target available for {message.agent_id}",
        )

    attempts: list[StepAttempt] = []
    result = None

    for index, agent in enumerate(candidates):
        addressed = (
            message
            if agent.agent_id == message.agent_id
            else replace(message, agent_id=agent.agent_id, endpoint=agent.endpoint)
        )
        emit(
            "step_started",
            step_id=message.step_id,
            agent_id=agent.agent_id,
            target_kind=agent.kind,
            capability=message.capability,
            task=message.task,
            attempt=index + 1,
        )
        result = _attempt_step(addressed, agent, transport, timeout=timeout)
        emit(
            "step_finished",
            step_id=message.step_id,
            agent_id=agent.agent_id,
            status=result.status,
            duration_ms=result.duration_ms,
            error=result.error,
            output=_preview(result.output),
            attempt=index + 1,
        )
        attempts.append(
            StepAttempt(
                agent_id=agent.agent_id,
                status=result.status,
                duration_ms=result.duration_ms,
                error=result.error,
            )
        )

        if scoreboard is not None:
            scoreboard.record(
                agent.agent_id,
                message.capability,
                ok=result.status == STATUS_OK,
                duration_ms=result.duration_ms,
            )

        if result.status == STATUS_OK:
            break

        if index + 1 < len(candidates):
            logger.warning(
                "Step failed on %s; trying %s next: step_id=%s capability=%s",
                agent.agent_id,
                candidates[index + 1].agent_id,
                message.step_id,
                message.capability,
            )

    assert result is not None  # candidates is non-empty

    return replace(result, attempts=tuple(attempts))


_PREVIEW_CHARS = 2000


def _preview(output: dict[str, Any] | None) -> str | None:
    """Return a step's output as bounded text for the live view.

    The full output is recorded with the step; the live view only needs
    enough to see what came back while the run continues.
    """
    if output is None:
        return None

    text = json.dumps(output, ensure_ascii=False, indent=2, default=str)

    if len(text) <= _PREVIEW_CHARS:
        return text

    return f"{text[:_PREVIEW_CHARS]}\n… [{len(text)} chars]"


def _attempt_step(
    message: ExecutionMessage,
    agent: AgentEndpoint,
    transport: AgentTransport,
    *,
    timeout: float,
) -> StepResult:
    """Deliver one execution message to one target and read its reply.

    Args:
        message: The execution message, already addressed to this target.
        agent: The target to deliver it to.
        transport: Transport used to deliver the message.
        timeout: Seconds to wait for the reply.

    Returns:
        The outcome of this single attempt.
    """
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
        # A transport that translates another protocol says what the answer
        # means beside the output, leaving the agent's own fields as they
        # are: KIO9's `output.verdict` is a conformance verdict, and what it
        # means for the step is read from its gate.
        verdict=_verdict_of(reply) or _verdict_of(output),
        review_required=_review_required_of(reply) or _review_required_of(output),
    )


def _gate_block_reason(
    step: PlanStep,
    plan: WorkflowPlan,
    policy: Policy,
    results: dict[str, StepResult],
) -> str | None:
    """Return why a policy bars a step from running, or None if it does not.

    The reason is written to be actionable on its own: it names the gate that
    applied, the step that failed to clear it, and what that step reported.

    Args:
        step: The plan step about to be dispatched.
        plan: The plan, read for which steps provide a capability.
        policy: The configured policy.
        results: Results of the steps that have already run.

    Returns:
        A human-readable reason, or None when the step may proceed.
    """
    if policy.block_on_review_required:
        waiting = next(
            (
                completed.step_id
                for completed in results.values()
                if completed.review_required
            ),
            None,
        )

        if waiting is not None:
            return (
                f"Blocked: {waiting} asked for human review, and "
                "block_on_review_required is set."
            )

    for gate in policy.gates_for(step.capability):
        for capability in sorted(gate.requires):
            providers = tuple(
                step_id
                for step_id in providers_of(plan, capability)
                if step_id != step.step_id
            )

            if not providers:
                return (
                    f"Blocked: {step.capability} requires {capability} to have "
                    "passed first, and no step in this plan provides it."
                )

            for provider in providers:
                outcome = results.get(provider)

                if outcome is None:
                    return (
                        f"Blocked: {step.capability} requires {capability}, and "
                        f"{provider} has not run."
                    )

                if outcome.status != STATUS_OK:
                    return (
                        f"Blocked: {step.capability} requires {capability} to "
                        f"have passed, and {provider} is {outcome.status}."
                    )

                if not gate.verdict:
                    continue

                expected = ", ".join(sorted(gate.verdict))

                if outcome.verdict is None:
                    return (
                        f"Blocked: {step.capability} requires {capability} to "
                        f"report one of [{expected}], and {provider} reported "
                        "no verdict."
                    )

                if outcome.verdict not in gate.verdict:
                    return (
                        f"Blocked: {step.capability} requires {capability} to "
                        f"report one of [{expected}], and {provider} reported "
                        f"{outcome.verdict!r}."
                    )

    return None


def _verdict_of(output: Any) -> str | None:
    """Return the agent's verdict, when it reported a recognized one.

    Args:
        output: The agent's `output` object, whatever shape it had.

    Returns:
        `defect`, `clean`, or `inconclusive`, or None when the agent said
        nothing or used a value KIO1 does not know how to present.
    """
    if not isinstance(output, dict):
        return None

    verdict = output.get("verdict")

    if not isinstance(verdict, str):
        return None

    normalized = verdict.strip().lower()

    if normalized not in _KNOWN_VERDICTS:
        logger.warning("Ignoring an unrecognized verdict: %r", verdict)
        return None

    return normalized


def _review_required_of(output: Any) -> bool:
    """Return whether the agent asked for human review.

    Args:
        output: The agent's `output` object, whatever shape it had.

    Returns:
        True only when the agent set the flag to a true boolean.
    """
    return isinstance(output, dict) and output.get("hitl_required") is True


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
            outcome = dispatched[step.step_id]
            results.append(
                outcome
                if outcome.agent_id == step.agent_id
                else replace(outcome, planned_agent_id=step.agent_id)
            )
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
                error="An earlier step it depends on did not complete",
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

    # Sized to the content rather than to `KIO<N>`: a registered target may be
    # a local command or an API with a name of any length, and a column that
    # overflows pushes every later column out of line.
    step_width = max((len(step.step_id) for step in result.results), default=4)
    agent_width = max((len(step.agent_id) for step in result.results), default=6)
    capability_width = max(
        (len(step.capability) for step in result.results), default=28
    )

    for step in result.results:
        notes: list[str] = []

        if step.status == STATUS_OK:
            notes.append(f"{step.duration_ms} ms")

            if step.verdict is not None:
                notes.append(step.verdict)

            if step.review_required:
                notes.append("review required")
        elif step.error:
            notes.append(step.error)

        if step.planned_agent_id:
            # The agent column shows who ran it. Without this, a step that
            # moved reads as a step the model planned there.
            notes.append(f"planned {step.planned_agent_id}")

        detail = "  ".join(notes)

        lines.append(
            f"  {step.step_id:<{step_width}} {step.agent_id:<{agent_width}} "
            f"{step.capability:<{capability_width}} {step.status:<9} {detail}".rstrip()
        )

    return "\n".join(lines)


def dispatch_plan(
    parsed_response: Any,
    dispatch_config: DispatchConfig,
    transport: AgentTransport | None = None,
    scoreboard: Scoreboard | None = None,
) -> WorkflowResult | None:
    """Validate a decoded model response and dispatch it to KIO agents.

    Args:
        parsed_response: The already-decoded workflow plan from the model.
        dispatch_config: Dispatch settings, including the agent registry.
        transport: The transport to dispatch with. Callers that dispatch
            more than once should build one transport and reuse it: a NATS
            transport owns a connection and a thread. Omitting it builds a
            throwaway transport, which is only cheap for HTTP.
        scoreboard: Recent outcomes, shared across dispatches so history
            accumulates. Omitting it records nothing and reorders nothing.

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
        workspace_data=dispatch_config.workspace.to_data(),
        policy=dispatch_config.policy,
        failover=dispatch_config.failover,
        scoreboard=scoreboard,
    )
