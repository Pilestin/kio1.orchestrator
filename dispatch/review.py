"""Reading a run's results and deciding what to tell the person who asked.

Until this existed a run ended when its last step did: the plan was carried
out and the steps' raw outputs were the answer. Nobody looked at them. A step
could fail, a plan could miss half the request, and the run still said
"completed" with a table of JSON.

A review closes the loop. The model is shown the request and what every step
returned, and answers with one of two decisions:

- `done`: the results answer the request — or nothing more can be done —
  and here is the answer, in the person's words, citing where each part
  came from.
- `continue`: something is missing that another plan could get. KIO1 plans
  again for only that, carries it out, and reviews again. The number of
  rounds is bounded, and the last review must answer.

Everything here is pure: the model call itself is the runner's. A review
that cannot be read is not a failed run — `fallback_answer` writes one from
the results directly.
"""

import json
from dataclasses import dataclass, replace
from typing import Any

from dispatch.step_references import rename_references
from dispatch.workflow_executor import STATUS_OK, StepResult, WorkflowResult
from dispatch.workflow_planner import PlanStep, WorkflowPlan

DECISION_DONE = "done"
DECISION_CONTINUE = "continue"

# What one step's output may take up in the review prompt, and all of them
# together. A local model reads a few thousand tokens a minute on a CPU; an
# unbounded digest of a test run's output would cost more than the run.
_STEP_OUTPUT_CHARS = 1500
_DIGEST_CHARS = 9000
_ANSWER_CHARS = 20_000


@dataclass(frozen=True)
class Round:
    """One plan and what carrying it out produced.

    Attributes:
        number: 1 for the first plan, 2 for the first follow-up, and so on.
        plan: The plan as executed, with this round's step ids.
        result: What each step did.
    """

    number: int
    plan: WorkflowPlan
    result: WorkflowResult


@dataclass(frozen=True)
class Review:
    """What the reviewer decided.

    Attributes:
        decision: `done` or `continue`.
        answer: The reply to the person, always present: even a `continue`
            carries the answer as it stands, so a run cut short still says
            something useful.
        reason: Why, in a sentence, for the activity log.
        next_request: For `continue`, the work still missing, phrased as a
            request the planner can act on.
        fallback: True when the answer was written from the results because
            the model's review could not be used.
    """

    decision: str
    answer: str
    reason: str = ""
    next_request: str = ""
    fallback: bool = False


def prefix_plan(plan: WorkflowPlan, prefix: str) -> WorkflowPlan:
    """Give a follow-up plan's steps ids that cannot collide with earlier ones.

    The planner numbers every plan from `s1`. A run's steps are recorded by
    id, so a second round's `s1` would overwrite the first round's.

    Args:
        plan: The plan as the model wrote it.
        prefix: Prepended to every step id and dependency, such as `r2-`.

    Returns:
        The same plan with renamed steps.
    """
    renamed = {step.step_id: prefix + step.step_id for step in plan.steps}
    steps: list[PlanStep] = [
        replace(
            step,
            step_id=prefix + step.step_id,
            depends_on=tuple(prefix + dependency for dependency in step.depends_on),
            # `${s1.latitude}` follows s1 to its new name.
            arguments=rename_references(step.arguments, renamed),
        )
        for step in plan.steps
    ]

    return replace(plan, steps=tuple(steps))


def _clip(text: str, limit: int) -> str:
    """Bound a piece of text, marking what was cut."""
    if len(text) <= limit:
        return text

    return f"{text[:limit]}… [{len(text) - limit} more chars]"


# Output fields that hold text a person would read, in the order worth
# showing. A command's stdout is the common case.
_TEXT_FIELDS = ("summary", "answer", "result", "stdout", "message")


def _main_text(output: dict[str, Any]) -> tuple[str, str] | None:
    """Return the first readable text field of an output, with its name."""
    for key in _TEXT_FIELDS:
        value = output.get(key)

        if isinstance(value, str) and value.strip():
            return key, value.strip()

    return None


def _render_output(output: dict[str, Any] | None) -> str:
    """Return a step's output for the reviewer.

    Text such as a command's stdout is given as its own lines rather than as
    a JSON string full of `\\n` escapes: a small model reads the lines of a
    git log correctly and misreads the escaped form. The remaining fields
    follow as compact JSON.
    """
    if not output:
        return ""

    main = _main_text(output)

    if main is None:
        return _clip(
            json.dumps(output, ensure_ascii=False, default=str), _STEP_OUTPUT_CHARS
        )

    key, text = main
    rest = {
        name: value
        for name, value in output.items()
        if name != key and value not in ("", None, 0, [], {})
    }
    lines = "\n".join(
        f"    {line}" for line in _clip(text, _STEP_OUTPUT_CHARS).splitlines()
    )
    extra = (
        f"\n  other fields: {_clip(json.dumps(rest, ensure_ascii=False, default=str), 300)}"
        if rest
        else ""
    )

    return f"({key})\n{lines}{extra}"


def _render_step(step: StepResult, planned: dict[str, PlanStep]) -> str:
    """Return one step as the lines the reviewer reads."""
    declared = planned.get(step.step_id)
    task = declared.task if declared is not None else ""
    lines = [
        (
            f"- {step.step_id} {step.agent_id}.{step.capability} "
            f"[{step.status}, {step.duration_ms} ms]: {task}"
        )
    ]

    if step.error:
        lines.append(f"  error: {_clip(step.error, 400)}")

    rendered = _render_output(step.output)

    if rendered:
        lines.append(f"  output: {rendered}")

    return "\n".join(lines)


def digest(rounds: list[Round]) -> str:
    """Describe every round's steps and results, bounded in size.

    Args:
        rounds: The rounds so far, oldest first.

    Returns:
        Plain text the reviewer and a follow-up planner can read. When it
        would be too long, the oldest rounds are cut first: the latest round
        is the one a decision turns on.
    """
    blocks: list[str] = []

    for done in rounds:
        planned = {step.step_id: step for step in done.plan.steps}
        header = (
            f"Round {done.number} — plan {done.plan.workflow_id} "
            f"({done.plan.execution_mode})"
        )
        steps = [_render_step(step, planned) for step in done.result.results]
        blocks.append("\n".join([header, *steps] if steps else [header, "- no steps"]))

    text = "\n\n".join(blocks)

    if len(text) > _DIGEST_CHARS:
        text = "… [earlier results cut]\n" + text[-_DIGEST_CHARS:]

    return text


def review_request(request_text: str, rounds: list[Round], *, final: bool) -> str:
    """Build the message the reviewer is sent.

    Args:
        request_text: What the person asked for.
        rounds: Everything carried out so far.
        final: Whether this review must end the run. The reviewer is told,
            so it answers instead of asking for more.

    Returns:
        The user message for the review call.
    """
    closing = (
        'This is the last round: decide "done" and answer with what you have.'
        if final
        else 'Decide "done" or "continue".'
    )

    return (
        f"## Request\n{request_text}\n\n"
        f"## What was carried out\n{digest(rounds)}\n\n"
        f"{closing}"
    )


def parse_review(decoded: Any, *, final: bool) -> Review:
    """Validate the reviewer's decoded answer.

    Lenient where leniency costs nothing — an unknown decision is read as
    `done`, and `continue` on the last round becomes `done` — and strict only
    about the one thing a review is for: there must be an answer.

    Args:
        decoded: The model's answer, already decoded from JSON.
        final: Whether this review must end the run.

    Returns:
        The review.

    Raises:
        ValueError: If there is no usable answer.
    """
    if not isinstance(decoded, dict):
        raise ValueError("A review must be a JSON object.")

    answer = decoded.get("answer")

    if isinstance(answer, list) and all(isinstance(part, str) for part in answer):
        # Small models often write the answer as a list of lines. That is
        # an answer; refusing it would throw away a review over its shape.
        answer = "\n".join(part.strip() for part in answer if part.strip())

    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("A review must contain a non-empty answer.")

    decision = decoded.get("decision")
    next_request = decoded.get("next_request")
    next_request = next_request.strip() if isinstance(next_request, str) else ""
    reason = decoded.get("reason")

    if decision != DECISION_CONTINUE or final or not next_request:
        # Continuing needs something to continue with, and the last round
        # cannot continue at all.
        decision = DECISION_DONE

    return Review(
        decision=decision,
        answer=_clip(answer.strip(), _ANSWER_CHARS),
        reason=reason.strip() if isinstance(reason, str) else "",
        next_request=next_request if decision == DECISION_CONTINUE else "",
    )


# How much of a multi-line output the fallback answer shows.
_FALLBACK_LINES = 6


def _summarize_output(output: dict[str, Any] | None) -> str:
    """Return the readable part of an output: one line, or its first few.

    A multi-line result such as a commit list keeps its lines, indented under
    the step, and says how many more there were; squeezing it into one line
    made it unreadable.
    """
    if not output:
        return ""

    main = _main_text(output)

    if main is None:
        return _clip(json.dumps(output, ensure_ascii=False, default=str), 300)

    lines = [_clip(line.rstrip(), 160) for line in main[1].splitlines() if line.strip()]

    if len(lines) == 1:
        return lines[0]

    shown = "\n".join(f"    {line}" for line in lines[:_FALLBACK_LINES])
    more = len(lines) - _FALLBACK_LINES

    return "\n" + shown + (f"\n    (+{more} more lines)" if more > 0 else "")


def fallback_answer(rounds: list[Round]) -> str:
    """Write an answer from the results alone, when the review could not.

    Args:
        rounds: Everything carried out.

    Returns:
        One entry per step: which target did what, and what came of it.
    """
    lines: list[str] = []

    for done in rounds:
        for step in done.result.results:
            if step.status == STATUS_OK:
                detail = _summarize_output(step.output) or "done"
            else:
                detail = step.error or step.status

            lines.append(f"- [{step.agent_id}] {step.capability}: {detail}")

    if not lines:
        return "Nothing could be carried out for this request."

    return "Results:\n" + "\n".join(lines)


def follow_up_request(request_text: str, rounds: list[Round], next_request: str) -> str:
    """Build the planner's request for the next round.

    The planner sees the original request for context, what has already
    been done so it does not repeat it, and the reviewer's description of
    what is still missing, which is what it should plan.

    Args:
        request_text: What the person asked for.
        rounds: Everything carried out so far.
        next_request: The reviewer's account of the missing work.

    Returns:
        The request text for the follow-up plan.
    """
    return (
        f"Original request: {request_text}\n\n"
        f"Already carried out (do not repeat it):\n{digest(rounds)}\n\n"
        f"Still needed: {next_request}\n\n"
        "Plan only the work that is still needed."
    )
