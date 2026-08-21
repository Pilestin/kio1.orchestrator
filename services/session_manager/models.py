from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

SessionStatus = Literal["pending", "running", "completed", "failed"]
StepStatus = Literal["ok", "error", "skipped", "not_run"]
ExecutionMode = Literal["sequential", "parallel", "mixed"]
LineageBasis = Literal["execution_order", "declared_dependency"]


class CreateSessionRequest(BaseModel):
    """Opens a session for one workflow run."""

    request_text: str = Field(min_length=1, max_length=100_000)


class UpdateSessionRequest(BaseModel):
    """Records progress on a session.

    Every field is optional so a caller can update the status alone, or
    attach the plan identifiers once the model has produced them.
    """

    status: SessionStatus | None = None
    workflow_id: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode | None = None
    error: str | None = Field(default=None, max_length=4_000)


class RecordStepRequest(BaseModel):
    """Records the outcome of one workflow step.

    `output` is the agent's reply payload. It is stored verbatim as an
    artifact so the result can be inspected and its lineage tracked.
    """

    step_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(min_length=1, max_length=32)
    capability: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=100_000)
    status: StepStatus
    duration_ms: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=4_000)
    output: dict[str, Any] | None = None


class Session(BaseModel):
    """A workflow run."""

    session_id: UUID
    request_text: str
    status: SessionStatus
    workflow_id: str | None = None
    execution_mode: ExecutionMode | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


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
