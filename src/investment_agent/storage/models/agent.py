"""Durable business tasks; runs remain the existing worker queue."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class AgentTask(Base):
    __tablename__ = "agent_task"
    __table_args__ = (UniqueConstraint("owner_id", "input_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    input_key: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    event_floor: Mapped[int] = mapped_column(default=0, server_default="0")
    revision: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_run.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON)
    resolved: Mapped[list[str]] = mapped_column(JSON)
    consumed_events: Mapped[list[str]] = mapped_column(JSON)
    wait_token: Mapped[str | None] = mapped_column(String(36), nullable=True)


class TaskAttempt(Base):
    __tablename__ = "agent_task_attempt"
    __table_args__ = (UniqueConstraint("run_id", "lease_attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_run.id"))
    lease_attempt: Mapped[int]
    worker_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24))


class ToolAction(Base):
    __tablename__ = "agent_tool_action"
    __table_args__ = (UniqueConstraint("task_id", "intent_key"),)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"))
    intent_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision: Mapped[int]
    effect: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class PublicTaskEvent(Base):
    __tablename__ = "agent_task_event"
    __table_args__ = {"sqlite_autoincrement": True}
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class AgentModelRun(Base):
    """Independent model audit; no raw messages, credentials or provider packages."""

    __tablename__ = "agent_model_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_run.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task.id"))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task_attempt.id"))
    config_version: Mapped[str] = mapped_column(String(96), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    framework_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    success: Mapped[bool] = mapped_column(nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    framework_requests: Mapped[int] = mapped_column(nullable=False)
    http_attempts: Mapped[int | None] = mapped_column()
    http_responses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage_unknown_reason: Mapped[str | None] = mapped_column(String(128))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    cost_unknown_reason: Mapped[str | None] = mapped_column(String(128))


class ExecutionGrantRow(Base):
    """Trusted admission receipt; never accepted from model/tool arguments."""

    __tablename__ = "agent_execution_grant"
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BudgetEntry(Base):
    """One actual request reservation, settled once, including unknown charges."""

    __tablename__ = "agent_budget_entry"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("agent_task_attempt.id"))
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    state: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(16), default="interactive", server_default="interactive")
    calls: Mapped[int]
    estimated_tokens: Mapped[int]
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    price_version: Mapped[str | None] = mapped_column(String(128))
    input_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    output_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    unknown_reason: Mapped[str | None] = mapped_column(String(128))


def _immutable_grant(*args: object) -> None:
    raise ValueError("execution admission receipts are immutable")


event.listen(ExecutionGrantRow, "before_update", _immutable_grant)
event.listen(ExecutionGrantRow, "before_delete", _immutable_grant)


class ConversationRow(Base):
    __tablename__ = "agent_conversation"
    __table_args__ = (Index("ix_agent_conversation_activity", "owner_id", "last_activity_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    next_sequence: Mapped[int] = mapped_column(default=1)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    title_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    subject_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class MessageRow(Base):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_message_id"),
        UniqueConstraint("conversation_id", "sequence"),
        UniqueConstraint("completion_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("agent_conversation.id"), index=True)
    client_message_id: Mapped[str | None] = mapped_column(String(36))
    completion_key: Mapped[str | None] = mapped_column(String(36))
    input_hash: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
