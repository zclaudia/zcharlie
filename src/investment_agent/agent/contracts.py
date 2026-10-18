"""Public conversation/task/event contracts, independent of provider messages."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.evidence.disclosures import DisclosureAccess
from investment_agent.investor.common import (
    BusinessError,
    Contract,
    DecimalValue,
    ObjectRef,
    Quantity,
    Record,
    Revision,
    Text,
)
from investment_agent.investor.contracts import ContentNode, PublicationCheck
from investment_agent.investor.enums import StopReason, TaskStatus


class ContextBinding(Contract):
    context_refs: tuple[ObjectRef, ...]
    base_refs: tuple[ObjectRef, ...]
    as_of: datetime


class Conversation(Record):
    title: str | None
    context_refs: tuple[ObjectRef, ...]


class MessageInput(Contract):
    client_message_id: UUID
    text: Text
    context_refs: tuple[ObjectRef, ...]
    reply_to: ObjectRef | None = None
    research_kind: Literal["auto", "explanation", "local_check", "company_research", "comparison", "review", "portfolio_action"] = Field(
        default="auto",
        description="Explicit user override of the research depth. 'auto' lets the server-side keyword classifier choose a conservative default; any explicit value always wins, and the admitted kind is visible on task progress.")
    research_at: datetime | None = None
    disclosure_access: tuple[DisclosureAccess, ...] = Field(default=(), max_length=3)


class Message(Record):
    conversation_ref: ObjectRef
    sequence: Annotated[int, Field(strict=True, ge=1)]
    role: Literal["user", "assistant"]
    text: str
    content: tuple[ContentNode, ...] = ()
    context_refs: tuple[ObjectRef, ...]
    reply_to: ObjectRef | None
    client_message_id: UUID | None
    task_ref: ObjectRef | None
    artifact_refs: tuple[ObjectRef, ...] = ()
    disclosure_access: tuple[DisclosureAccess, ...] = ()


ConversationAttention = Literal["needs_you", "running", "none"]


class ConversationSubject(Contract):
    ref: ObjectRef
    status: Literal["available", "unavailable"]
    name: str | None = None
    ticker: str | None = None

    @model_validator(mode="after")
    def identity_matches_status(self) -> "ConversationSubject":
        known = self.status == "available"
        if known != (self.name is not None) or known != (self.ticker is not None):
            raise ValueError("available subjects carry identity metadata; unavailable ones stay null")
        return self


class ConversationTaskView(Contract):
    id: UUID
    status: TaskStatus
    cancel_requested: bool
    stop_reason: StopReason | None


class ConversationSummary(Contract):
    id: UUID
    title: str | None
    created_at: datetime
    last_activity_at: datetime
    title_revision: Annotated[int, Field(strict=True, ge=1)]
    subjects: tuple[ConversationSubject, ...] = ()
    message_count: Annotated[int, Field(strict=True, ge=0)] = 0
    attention: ConversationAttention
    latest_task: ConversationTaskView | None = None
    archived_at: datetime | None = None
    purge_at: datetime | None = None


class ConversationPage(Contract):
    items: tuple[ConversationSummary, ...] = ()
    next_cursor: str | None = None


class ConversationTitleUpdate(Contract):
    title: str | None
    expected_title_revision: Annotated[int, Field(strict=True, ge=1)]


class ConversationTitleState(Contract):
    id: UUID
    title: str | None
    title_revision: Annotated[int, Field(strict=True, ge=1)]


class ConversationArchiveState(Contract):
    id: UUID
    archived_at: datetime | None


class Usage(Contract):
    tool_calls: Annotated[int, Field(ge=0)] = 0
    requests: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] | None = 0
    output_tokens: Annotated[int, Field(ge=0)] | None = 0
    cost: Quantity


class WaitRequirement(Contract):
    required_input: Text
    dependency_ids: tuple[UUID, ...]
    deadline: datetime


class Task(Record):
    parent_task_id: UUID | None
    message_ref: ObjectRef | None
    binding: ContextBinding
    status: TaskStatus
    cancel_requested: bool = False
    stop_reason: StopReason | None
    waiting: WaitRequirement | None
    artifact_refs: tuple[ObjectRef, ...]
    usage: Usage

    @model_validator(mode="after")
    def waiting_and_terminal_shape(self) -> "Task":
        if (self.status in (TaskStatus.WAITING_DATA, TaskStatus.WAITING_USER)) != (
            self.waiting is not None
        ):
            raise ValueError("waiting state requires waiting metadata")
        terminal = self.status in (
            TaskStatus.SUCCEEDED,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )
        if terminal != (self.stop_reason is not None):
            raise ValueError("terminal tasks require a stop reason; active tasks cannot have one")
        return self


class TaskStatePayload(Contract):
    status: TaskStatus
    cancel_requested: bool


class StepPayload(Contract):
    step_id: UUID
    summary: Text


class ToolPayload(Contract):
    tool_call_id: Text
    tool_name: Text
    state: Literal["started", "waiting", "completed", "failed"]
    refs: tuple[ObjectRef, ...] = ()


class ProgressPayload(Contract):
    summary: Text


class ArtifactPayload(Contract):
    ref: ObjectRef


class InputPayload(Contract):
    question: Text
    fields: tuple[str, ...]
    deadline: datetime | None


class EventBase(Contract):
    schema_version: Literal[1] = 1
    event_id: Annotated[int, Field(strict=True, ge=1)]
    task_id: UUID
    attempt_id: UUID | None
    occurred_at: datetime


class TaskStateEvent(EventBase):
    type: Literal["task_state"]
    payload: TaskStatePayload


class StepEvent(EventBase):
    type: Literal["step_started"]
    payload: StepPayload


class ToolEvent(EventBase):
    type: Literal["tool_state"]
    payload: ToolPayload


class ProgressEvent(EventBase):
    type: Literal["progress"]
    payload: ProgressPayload


class ArtifactEvent(EventBase):
    type: Literal["artifact_saved"]
    payload: ArtifactPayload


class PublicationEvent(EventBase):
    type: Literal["publication_result"]
    payload: PublicationCheck


class InputEvent(EventBase):
    type: Literal["input_required"]
    payload: InputPayload


class UsageEvent(EventBase):
    type: Literal["usage"]
    payload: Usage


class ErrorEvent(EventBase):
    type: Literal["error"]
    payload: BusinessError


TaskEvent = Annotated[
    TaskStateEvent
    | StepEvent
    | ToolEvent
    | ProgressEvent
    | ArtifactEvent
    | PublicationEvent
    | InputEvent
    | UsageEvent
    | ErrorEvent,
    Field(discriminator="type"),
]


class MessageAccepted(Contract):
    message: Message
    task: Task
    events_url: Text


class CursorExpired(Contract):
    code: Literal["cursor_expired"]
    reload_snapshot: Literal[True]
    task_ref: ObjectRef


ResearchKind = Literal[
    "explanation", "local_check", "company_research", "comparison", "review", "portfolio_action"
]


class PlanStep(Contract):
    text: Text
    state: Literal["done", "next", "pending"]


class PlanView(Contract):
    ref: ObjectRef
    question: Text
    saved_at: datetime | None = None
    saved_at_unknown_reason: Text | None = None
    steps: tuple[PlanStep, ...] = ()
    extra_completed: tuple[Text, ...] = ()
    completion_criteria: tuple[Text, ...] = ()
    stop_conditions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def saved_at_known_shape(self) -> "PlanView":
        if (self.saved_at is None) != (self.saved_at_unknown_reason is not None):
            raise ValueError("unknown save time requires a reason; known times cannot have one")
        return self


class TaskLimits(Contract):
    """Current runtime policy reference, never a historical execution snapshot."""

    basis: Literal["current_policy"] = "current_policy"
    model_requests: Annotated[int, Field(strict=True, ge=0)]
    tool_calls: Annotated[int, Field(strict=True, ge=0)]
    tokens: Annotated[int, Field(strict=True, ge=0)]
    seconds: Annotated[float, Field(strict=True, gt=0)]
    cost_usd: DecimalValue | None = None
    cost_limit_reason: Text | None = None
    finish_only: bool = False

    @model_validator(mode="after")
    def cost_known_shape(self) -> "TaskLimits":
        if self.cost_usd is not None and self.cost_limit_reason is not None:
            raise ValueError("known cost caps cannot carry a reason")
        if self.cost_usd is None and self.cost_limit_reason is None:
            raise ValueError("missing cost caps require a stable reason")
        return self


class TaskProgress(Contract):
    task_id: UUID
    task_revision: Revision
    research_kind: ResearchKind
    as_of: datetime
    plan: PlanView | None
    limits: TaskLimits
    usage: Usage
    elapsed_seconds: Annotated[int, Field(strict=True, ge=0)] | None = None
    elapsed_unknown_reason: Text | None = None
    deadline: datetime

    @model_validator(mode="after")
    def elapsed_known_shape(self) -> "TaskProgress":
        if (self.elapsed_seconds is None) != (self.elapsed_unknown_reason is not None):
            raise ValueError("unknown elapsed time requires a reason; known times cannot have one")
        return self
