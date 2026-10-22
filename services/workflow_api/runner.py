import logging
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID

from core.approvals import ApprovalDesk
from core.config_loader import APPROVAL_AUTO, APPROVAL_NEVER, Config
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
from dispatch.failover import FailoverConfig, candidates_for
from dispatch.review import (
    DECISION_CONTINUE,
    DECISION_DONE,
    Review,
    Round,
    fallback_answer,
    follow_up_request,
    parse_review,
    prefix_plan,
    review_request,
)
from dispatch.scoreboard import Scoreboard
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import (
    STATUS_OK,
    WorkflowResult,
    execute_workflow,
)
from dispatch.workflow_planner import WorkflowPlan, parse_plan
from providers.provider_client import load_provider, supports_streaming

logger = logging.getLogger(__name__)


class PlanRejected(Exception):
    """A plan was cancelled, or not approved in time."""


class PlannerQuestion(Exception):
    """The planner answered with a question instead of a plan."""

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


@dataclass
class Clarifications:
    """The questions a run asked the person, and the answers it got.

    Kept for the whole run, so the reviewer and every later round see what
    the person said, not only the round that asked.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)

    def apply(self, request_text: str) -> str:
        """Return the request with the answers given so far appended."""
        if not self.pairs:
            return request_text

        lines = "\n".join(
            f"- Q: {question}\n  A: {answer}" for question, answer in self.pairs
        )

        return f"{request_text}\n\nClarified with the user:\n{lines}"


def guarded_targets(
    plan: WorkflowPlan, registry: AgentRegistry, failover: FailoverConfig
) -> list[str]:
    """Return the targets marked for approval that a plan could reach.

    Not only the targets the plan names: with failover on, a step can move
    to any target declaring its capability, and approving a plan has to
    cover where its steps may actually run.

    Args:
        plan: The plan about to run.
        registry: The targets.
        failover: Whether steps may move to another target.

    Returns:
        The marked targets, sorted.
    """
    found: set[str] = set()

    for step in plan.steps:
        for candidate in candidates_for(
            step.agent_id, step.capability, registry, failover
        ):
            if candidate.requires_approval:
                found.add(candidate.agent_id)

    return sorted(found)


@dataclass(frozen=True)
class ModelRole:
    """A model to ask, and what asking it takes.

    Attributes:
        config: The configuration to send with, carrying the provider and
            model names this role uses.
        provider: The provider module.
        client: The provider's client.
    """

    config: Config
    provider: Any
    client: Any


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
    # The reviewer's system prompt, catalog placeholder still in it. Empty
    # turns the review off whatever the configuration says: there is no
    # prompt to review with.
    review_template: str = ""
    # The reviewer's own provider, its client, and which provider they are
    # for — started on first use when `orchestration` names a provider other
    # than the planner's. None until then.
    review_provider: Any = None
    review_client: Any = None
    review_provider_name: str = ""
    # Where plans that need a person wait for one. None runs every plan
    # without asking — the terminal application and most tests.
    approvals: ApprovalDesk | None = None

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
        """Plan, dispatch, review, and answer one request. See `run`.

        Rounds: a plan is carried out, the reviewer reads the results, and
        either answers or names what is missing; then only that is planned
        and carried out, until the reviewer answers or the rounds run out.
        With review off this is the single plan-and-dispatch it always was.
        """
        self.recorder.update_session(session_id, status="running")
        emit("status", status="running")

        settings = self.config.orchestration
        reviewing = settings.review and bool(self.review_template)
        max_rounds = settings.max_rounds if reviewing else 1
        registry = self.directory.snapshot()
        rounds: list[Round] = []
        clarified = Clarifications()
        planning_request = request_text
        review: Review | None = None

        for number in range(1, max_rounds + 1):
            if number > 1:
                emit("round_started", round=number, request=planning_request)

            try:
                done = self._carry_out(
                    session_id, number, planning_request, registry, clarified
                )
            except PlanRejected:
                if not rounds:
                    reason = "The plan was not approved."
                    self.recorder.update_session(
                        session_id, status="failed", error=reason
                    )
                    emit("status", status="failed", error=reason)
                    return

                break  # a follow-up was turned down; the last answer stands

            if done is None:
                if not rounds:
                    return  # the first plan failed; already recorded

                break  # a follow-up failed; answer with what the earlier rounds got

            rounds.append(done)

            if not reviewing:
                break

            review = self._review(
                session_id,
                clarified.apply(request_text),
                rounds,
                registry,
                final=number == max_rounds,
            )

            if review.decision != DECISION_CONTINUE:
                break

            planning_request = follow_up_request(
                clarified.apply(request_text), rounds, review.next_request
            )

        last = rounds[-1].result
        # A workflow whose release was refused by a gate did not complete,
        # whatever the individual calls did. Reporting it as completed puts
        # the wrong word in the first place anyone looks. Only the last
        # round counts: an earlier failure a later round put right is not
        # the run's outcome.
        outcome = "failed" if last.failed or last.blocked else "completed"
        answer = None

        if reviewing:
            # A review cut short by a failed follow-up still has an answer;
            # a run that never reached one gets it from the results.
            answer = review.answer if review is not None else fallback_answer(rounds)
            emit("answer", text=answer)

        self.recorder.update_session(session_id, status=outcome, answer=answer)
        emit(
            "status",
            status=outcome,
            rounds=len(rounds),
            steps=[
                {
                    "step_id": step.step_id,
                    "agent_id": step.agent_id,
                    "status": step.status,
                    "error": step.error,
                }
                for finished in rounds
                for step in finished.result.results
            ],
        )
        logger.info(
            "Workflow run finished: session_id=%s rounds=%d ok=%d error=%d",
            session_id,
            len(rounds),
            last.succeeded,
            last.failed,
        )

    def _carry_out(
        self,
        session_id: UUID,
        number: int,
        planning_request: str,
        registry: AgentRegistry,
        clarified: Clarifications,
    ) -> Round | None:
        """Plan and dispatch one round, recording its steps.

        Args:
            session_id: The run.
            number: The round, from 1.
            planning_request: What to plan: the request itself in the first
                round, the missing work after that.
            registry: The targets this run plans against.
            clarified: The run's questions and answers so far; a question the
                planner asks in this round is added to it.

        Returns:
            The round, or None when planning or dispatch failed. A first
            round's failure is the run's and is recorded as such; a later
            one only ends the rounds early.
        """
        try:
            plan = self._plan_asking(
                session_id, number, planning_request, registry, clarified
            )
        except Exception as error:
            logger.exception(
                "Planning failed: session_id=%s round=%d", session_id, number
            )
            self._round_failed(session_id, number, error)
            return None

        if number > 1:
            plan = prefix_plan(plan, f"r{number}-")

        emit(
            "plan",
            round=number,
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

        if number == 1:
            self.recorder.update_session(
                session_id,
                workflow_id=plan.workflow_id,
                execution_mode=plan.execution_mode,
            )

        if not self._approved(session_id, number, plan, registry):
            raise PlanRejected(number)

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
            logger.exception(
                "Dispatch failed: session_id=%s round=%d", session_id, number
            )
            self._round_failed(session_id, number, error)
            return None

        self._record_results(session_id, plan, result)

        return Round(number=number, plan=plan, result=result)

    def _plan_asking(
        self,
        session_id: UUID,
        number: int,
        planning_request: str,
        registry: AgentRegistry,
        clarified: Clarifications,
    ) -> WorkflowPlan:
        """Plan, asking the person whatever the planner needs to know first.

        Each answer is added to the request and the round is planned again,
        until the planner produces a plan or the run's questions run out.

        Raises:
            ValueError: If the planner asks and cannot be answered — asking
                is off, nobody answers in time, or it keeps asking — with the
                question in the message, so the run says what was missing.
            Exception: Any planning failure, as `_build_plan`.
        """
        request = planning_request

        while True:
            try:
                return self._build_plan(session_id, request, registry)
            except PlannerQuestion as asked:
                reply = self._ask_person(session_id, number, asked.question, clarified)
                request = f"{request}\n\nQ: {asked.question}\nA: {reply}"

    def _ask_person(
        self, session_id: UUID, number: int, question: str, clarified: Clarifications
    ) -> str:
        """Put the planner's question to the person and wait for the answer.

        Returns:
            The answer.

        Raises:
            ValueError: If the question cannot be put, or is not answered.
        """
        settings = self.config.orchestration

        if not settings.clarify or self.approvals is None:
            raise ValueError(f"The planner needs more information: {question}")

        if len(clarified.pairs) >= settings.max_questions:
            raise ValueError(
                f"The planner still needs more after {len(clarified.pairs)} "
                f"answer(s): {question}"
            )

        emit(
            "question",
            round=number,
            question=question,
            timeout=settings.question_timeout,
        )
        logger.info("Waiting for an answer: session_id=%s round=%d", session_id, number)

        reply = self.approvals.ask_question(
            str(session_id), number, question, settings.question_timeout
        )

        if reply is None or not reply.strip():
            raise ValueError(f"The question was not answered: {question}")

        reply = reply.strip()
        clarified.pairs.append((question, reply))
        # Kept with the session, so a later request in the same conversation
        # knows what the person already said.
        self.recorder.update_session(
            session_id,
            clarifications=[
                {"question": asked, "answer": given} for asked, given in clarified.pairs
            ],
        )
        emit("question_answered", round=number, question=question, answer=reply)

        return reply

    def _approved(
        self, session_id: UUID, number: int, plan: WorkflowPlan, registry: AgentRegistry
    ) -> bool:
        """Ask a person to approve a plan when it needs it, and wait.

        Args:
            session_id: The run.
            number: The round the plan belongs to.
            plan: The plan about to run.
            registry: The targets, to find the ones that need approval.

        Returns:
            True when the plan may run: approval is off, nothing in it needs
            approval, or a person approved it. False when it was cancelled or
            nobody answered in time.
        """
        settings = self.config.orchestration

        if settings.approval == APPROVAL_NEVER or self.approvals is None:
            return True

        guarded = guarded_targets(plan, registry, self.config.dispatch.failover)

        if settings.approval == APPROVAL_AUTO and not guarded:
            return True

        summary = {
            "workflow_id": plan.workflow_id,
            "guarded": guarded,
            "steps": [
                {
                    "step_id": step.step_id,
                    "agent_id": step.agent_id,
                    "capability": step.capability,
                    "task": step.task,
                    "arguments": step.arguments,
                }
                for step in plan.steps
            ],
        }
        emit(
            "approval_required",
            round=number,
            timeout=settings.approval_timeout,
            **summary,
        )
        logger.info(
            "Waiting for approval: session_id=%s round=%d guarded=%s",
            session_id,
            number,
            ",".join(guarded) or "-",
        )

        approved = self.approvals.ask(
            str(session_id), number, summary, settings.approval_timeout
        )
        emit("approval", round=number, approved=approved)

        return approved

    def _round_failed(self, session_id: UUID, number: int, error: Exception) -> None:
        """Record why a round could not be carried out."""
        reason = f"{type(error).__name__}: {error}"

        if number == 1:
            self.recorder.update_session(session_id, status="failed", error=reason)
            emit("status", status="failed", error=reason)
        else:
            emit("round_failed", round=number, error=reason)

    def _review(
        self,
        session_id: UUID,
        request_text: str,
        rounds: list[Round],
        registry: AgentRegistry,
        *,
        final: bool,
    ) -> Review:
        """Have the model read the results, decide, and answer.

        Never raises: a review that fails — the provider times out, the
        model writes something that is not a review — falls back to an
        answer written from the results, so the run still ends with one.

        Args:
            session_id: The run, for trace correlation.
            request_text: What the person asked for.
            rounds: Everything carried out so far.
            registry: The targets, so the reviewer knows what a follow-up
                could use.
            final: Whether this review must end the run.

        Returns:
            The review.
        """
        named = self._review_config()
        emit(
            "review_started",
            round=rounds[-1].number,
            final=final,
            provider=named.provider,
            model=named.model,
        )

        try:
            content = self._ask_model(
                session_id,
                fill_catalog(self.review_template, registry),
                review_request(request_text, rounds, final=final),
                turn=len(rounds) * 2,
                role=self._reviewer(),
            )
            review = parse_review(parse_response(content), final=final)
        except Exception as error:  # noqa: BLE001 - any failure falls back to an answer
            logger.warning(
                "Review failed, answering from the results: session_id=%s error=%s",
                session_id,
                error,
            )
            review = Review(
                decision=DECISION_DONE,
                answer=fallback_answer(rounds),
                reason=f"The review could not be used ({type(error).__name__}).",
                fallback=True,
            )

        emit(
            "review",
            round=rounds[-1].number,
            decision=review.decision,
            reason=review.reason,
            next_request=review.next_request,
            fallback=review.fallback,
        )

        return review

    def _build_plan(
        self, session_id: UUID, request_text: str, registry: AgentRegistry
    ) -> WorkflowPlan:
        """Ask the model for a workflow plan and validate it.

        Args:
            session_id: The session this run belongs to, for trace correlation.
            request_text: What to plan.
            registry: The targets to describe in the prompt's catalog.

        Returns:
            The validated plan.

        Raises:
            Exception: Any provider or validation failure, handled by `run`.
        """
        system_prompt = fill_catalog(self.prompt_template, registry)

        emit(
            "planning_started",
            provider=self.config.provider,
            model=self.config.model,
            targets=sorted(registry.agent_ids()),
            prompt_chars=len(system_prompt),
            streaming=observed() and supports_streaming(self.provider),
        )

        content = self._ask_model(
            session_id, system_prompt, request_text, turn=1, role=self._planner()
        )
        decoded = parse_response(content)

        if (
            isinstance(decoded, dict)
            and "steps" not in decoded
            and isinstance(decoded.get("question"), str)
            and decoded["question"].strip()
        ):
            raise PlannerQuestion(decoded["question"].strip())

        return parse_plan(decoded)

    def _planner(self) -> ModelRole:
        """Return the model that plans."""
        return ModelRole(self.config, self.provider, self.provider_client)

    def _review_config(self) -> Config:
        """Return the configuration reviews are sent with."""
        settings = self.config.orchestration

        if not settings.separate_reviewer:
            return self.config

        return replace(
            self.config,
            provider=settings.review_provider or self.config.provider,
            model=settings.review_model or self.config.model,
        )

    def _reviewer(self) -> ModelRole:
        """Return the model that reviews and answers.

        The planner's unless `orchestration` names another. Read per review,
        not at startup, so a change made on the Settings tab — to either
        model — reaches the next run. Another model on the planner's provider
        shares the planner's client; another provider's client is started on
        first use and kept until the reviewer's provider changes.

        Raises:
            Exception: If the reviewer's provider cannot be started; the
                review then falls back to an answer from the results.
        """
        config = self._review_config()

        if config.provider == self.config.provider:
            return ModelRole(config, self.provider, self.provider_client)

        if self.review_provider is None or self.review_provider_name != config.provider:
            module = load_provider(config.provider, self.config.allowed_providers)
            self.review_client = module.create_client(config)
            self.review_provider = module
            self.review_provider_name = config.provider

        return ModelRole(config, self.review_provider, self.review_client)

    def _ask_model(
        self,
        session_id: UUID,
        system_prompt: str,
        message: str,
        *,
        turn: int,
        role: ModelRole,
    ) -> str:
        """Send one request to a model and return its text.

        Streamed only when someone can watch: the answer is identical either
        way, and a provider that cannot stream still answers.

        Args:
            session_id: The run, for trace correlation.
            system_prompt: The system prompt.
            message: The user message.
            turn: Which model call of the run this is, for tracing.
            role: The model to ask, with its provider and client.

        Returns:
            The model's answer.
        """
        messages = [{"role": "user", "content": message}]
        streaming = observed() and supports_streaming(role.provider)
        relay = TokenRelay()

        with trace_gen_ai_request(
            provider=role.config.provider,
            model=role.config.model,
            session_id=str(session_id),
            turn_number=turn,
            message_count=len(messages) + 1,
            max_output_tokens=role.config.max_output_tokens,
            temperature=role.config.temperature,
        ):
            extra = {"on_token": relay} if streaming else {}

            try:
                response = role.provider.send_request(
                    config=role.config,
                    client=role.client,
                    system_prompt=system_prompt,
                    messages=messages,
                    **extra,
                )
            finally:
                relay.flush()

        content = role.provider.extract_content(response)
        emit("model_answer", text=content)

        return content

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
    review_template: str = "",
    approvals: ApprovalDesk | None = None,
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
        review_template: The reviewer's prompt as read from disk. Omitted
            runs each plan once, without a review or an answer.

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
            else build_transport(config.dispatch, live_directory.mcp_pool, config)
        ),
        directory=live_directory,
        scoreboard=(
            scoreboard
            if scoreboard is not None
            else Scoreboard(config.dispatch.selection)
        ),
        events=events,
        review_template=review_template,
        approvals=approvals,
    )
