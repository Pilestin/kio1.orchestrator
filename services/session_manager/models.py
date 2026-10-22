from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

SessionStatus = Literal["pending", "running", "completed", "failed"]
StepStatus = Literal["ok", "error", "skipped", "not_run"]
ExecutionMode = Literal["sequential", "parallel", "mixed"]
LineageBasis = Literal["execution_order", "declared_dependency"]


# How much of a conversation's first request becomes its title.
TITLE_MAX_CHARS = 60


def conversation_title(request_text: str) -> str:
    """Return a short title for a conversation, from the request that opened it.

    Whitespace is collapsed so a pasted traceback does not become a title of
    line breaks; a long request is cut at a word boundary where there is one.

    Args:
        request_text: The conversation's first request.

    Returns:
        At most `TITLE_MAX_CHARS` characters, an ellipsis included.
    """
    collapsed = " ".join(request_text.split())

    if len(collapsed) <= TITLE_MAX_CHARS:
        return collapsed

    cut = collapsed[: TITLE_MAX_CHARS - 1]
    word_end = cut.rfind(" ")

    if word_end > TITLE_MAX_CHARS // 2:
        cut = cut[:word_end]

    return f"{cut.rstrip()}…"


class Clarification(BaseModel):
    """A question the planner put to the person, and the answer it got."""

    question: str = Field(max_length=4_000)
    answer: str = Field(max_length=4_000)


class CreateSessionRequest(BaseModel):
    """Opens a session for one workflow run.

    Without a conversation the session opens one of its own: every run
    belongs to a conversation, so a history never has runs that no
    conversation lists.
    """

    request_text: str = Field(min_length=1, max_length=100_000)
    conversation_id: UUID | None = None


class UpdateSessionRequest(BaseModel):
    """Records progress on a session.

    Every field is optional so a caller can update the status alone, or
    attach the plan identifiers once the model has produced them, and the
    answer once the run has been reviewed.
    """

    status: SessionStatus | None = None
    workflow_id: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode | None = None
    error: str | None = Field(default=None, max_length=4_000)
    answer: str | None = Field(default=None, max_length=20_000)
    # Replaces the whole list: a run sends everything asked so far, so a
    # lost update is made good by the next one.
    clarifications: list[Clarification] | None = Field(default=None, max_length=32)


class RecordStepRequest(BaseModel):
    """Records the outcome of one workflow step.

    `output` is the agent's reply payload. It is stored verbatim as an
    artifact so the result can be inspected and its lineage tracked.

    `depends_on` names the steps this one consumed. When it is supplied the
    lineage edges are recorded as `declared_dependency`; when it is empty the
    Session Manager falls back to inferring edges from execution order.
    """

    step_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(min_length=1, max_length=32)
    capability: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=100_000)
    status: StepStatus
    duration_ms: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=4_000)
    output: dict[str, Any] | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=64)


class Session(BaseModel):
    """A workflow run."""

    session_id: UUID
    request_text: str
    status: SessionStatus
    workflow_id: str | None = None
    execution_mode: ExecutionMode | None = None
    error: str | None = None
    answer: str | None = None
    conversation_id: UUID | None = None
    clarifications: list[Clarification] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class Conversation(BaseModel):
    """A run of requests that follow on from each other.

    `updated_at` moves when a request is added, so the list a console shows
    puts the conversation last written to first.
    """

    conversation_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class CreateConversationRequest(BaseModel):
    """Opens an empty conversation; its first request gives it a title."""

    title: str = Field(default="", max_length=200)


class Step(BaseModel):
    """One recorded workflow step."""

    step_id: str
    agent_id: str
    capability: str
    task: str
    status: StepStatus
    duration_ms: int
    error: str | None = None
    output: dict[str, Any] | None = None
    created_at: datetime


class LineageEdge(BaseModel):
    """A recorded flow of one step's output into another step."""

    produced_by_step_id: str
    consumed_by_step_id: str
    artifact_id: int
    basis: LineageBasis


class SessionDetail(BaseModel):
    """A session together with everything recorded against it."""

    session: Session
    steps: list[Step] = Field(default_factory=list)
    lineage: list[LineageEdge] = Field(default_factory=list)


class ConversationDetail(BaseModel):
    """A conversation with each of its runs, oldest first.

    Everything a chat needs to show it again in one read: every request,
    its steps, and its answer.
    """

    conversation: Conversation
    sessions: list[SessionDetail] = Field(default_factory=list)
