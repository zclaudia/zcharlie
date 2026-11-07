"""Bank topic heads and append-only history.

Foreign keys are declarative: owner/listing, exact references, parent existence,
proof authority and semantic periods must be checked by the service. This module
never downloads, imports a PDF or enables a condition. Mutable heads are changed
through later CAS repositories; revisions and receipts cannot be replaced.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class TopicCase(Base):
    __tablename__ = "topic_case"
    __table_args__ = (
        UniqueConstraint("owner_id", "listing_id", "template_key", "base_period", name="uq_topic_case_identity"),
        CheckConstraint("head_revision >= 1", name="ck_topic_case_head_revision"),
        CheckConstraint("control_revision >= 1", name="ck_topic_case_control_revision"),
        CheckConstraint("state IN ('draft', 'ready', 'archived')", name="ck_topic_case_state"),
        Index("ix_topic_case_owner_state", "owner_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    template_key: Mapped[str] = mapped_column(String(80), nullable=False)
    base_period: Mapped[date] = mapped_column(Date, nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    control_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopicRevision(Base):
    __tablename__ = "topic_revision"
    __table_args__ = (
        UniqueConstraint("case_id", "revision", name="uq_topic_revision_case_version"),
        CheckConstraint("revision >= 1", name="ck_topic_revision_revision"),
        CheckConstraint("schema_version = 1", name="ck_topic_revision_schema"),
        Index("ix_topic_revision_case_created", "case_id", "created_at"),
        {"sqlite_with_rowid": False},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("topic_case.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    baseline_period: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    accounting_standard: Mapped[str] = mapped_column(String(16), nullable=False)
    reporting_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fact_bindings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    decomposition: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    explanations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    condition_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)


class TopicFactRevision(Base):
    __tablename__ = "topic_fact_revision"
    __table_args__ = (
        UniqueConstraint("case_id", "stable_fact_id", "revision", name="uq_topic_fact_stable_version"),
        CheckConstraint("revision >= 1", name="ck_topic_fact_revision"),
        CheckConstraint("schema_version = 1", name="ck_topic_fact_schema"),
        CheckConstraint("(value IS NULL AND reason IS NOT NULL) OR (value IS NOT NULL AND reason IS NULL)", name="ck_topic_fact_unknown_reason"),
        Index("ix_topic_fact_case_metric_revision", "case_id", "metric", "revision"),
        {"sqlite_with_rowid": False},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("topic_case.id"), nullable=False)
    stable_fact_id: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    comparison_period: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    accounting_standard: Mapped[str] = mapped_column(String(16), nullable=False)
    reporting_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    proof: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    published_precision: Mapped[str] = mapped_column(String(16), nullable=False)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_actor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopicMutationReceipt(Base):
    __tablename__ = "topic_mutation_receipt"
    __table_args__ = (
        CheckConstraint("status >= 100 AND status <= 599", name="ck_topic_receipt_status"),
        Index("ix_topic_receipt_owner_created", "owner_id", "created_at"),
        {"sqlite_with_rowid": False},
    )

    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mutation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route: Mapped[str] = mapped_column(String(160), nullable=False)
    object_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)



class TopicConditionRevision(Base):
    __tablename__ = "topic_condition_revision"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_topic_condition_revision"),
        CheckConstraint("schema_version = 1", name="ck_topic_condition_schema"),
        CheckConstraint("review_due_at < expires_at", name="ck_topic_condition_window"),
        Index("ix_topic_condition_case_revision", "case_id", "revision"),
        {"sqlite_with_rowid": False},
    )

    condition_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("topic_case.id"), nullable=False)
    topic_revision_ref: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dsl_version: Mapped[str] = mapped_column(String(64), nullable=False)
    predicate: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dependency_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    target_selector: Mapped[str] = mapped_column(String(64), nullable=False)
    review_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    activation_basis: Mapped[str | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)


class TopicConditionHead(Base):
    __tablename__ = "topic_condition_head"
    __table_args__ = (
        CheckConstraint("head_revision >= 1", name="ck_topic_condition_head_revision"),
        CheckConstraint("control_revision >= 1", name="ck_topic_condition_control_revision"),
        CheckConstraint("state IN ('draft', 'armed', 'paused', 'cancelled', 'completed', 'expired')", name="ck_topic_condition_state"),
        Index("ix_topic_condition_due", "owner_id", "state", "next_due_at"),
    )

    condition_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("topic_case.id"), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    control_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_run.id"), nullable=True)
    last_check_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class TopicCheckRevision(Base):
    __tablename__ = "topic_check_revision"
    __table_args__ = (
        UniqueConstraint("owner_id", "condition_id", "condition_revision", "semantic_input_hash", "evaluator_version", name="uq_topic_check_semantic_input"),
        ForeignKeyConstraint(["condition_id", "condition_revision"], ["topic_condition_revision.condition_id", "topic_condition_revision.revision"]),
        CheckConstraint("condition_revision >= 1", name="ck_topic_check_condition_revision"),
        CheckConstraint("schema_version = 1", name="ck_topic_check_schema"),
        Index("ix_topic_check_condition_created", "owner_id", "condition_id", "created_at"),
        {"sqlite_with_rowid": False},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(36), nullable=False)
    condition_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    condition_ref: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    execution_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    unknown_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    prior_check_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    supersedes_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

def _immutable(*args: object) -> None:
    raise ValueError("bank topic history and receipts are immutable")


def _history_ddl(table_name: str, identity_keys: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    """Shared with the migration: protect every unique identity before REPLACE deletes."""
    ddl = tuple(
        f"CREATE TRIGGER IF NOT EXISTS trg_{table_name}_immutable_{operation.lower()} "
        f"BEFORE {operation} ON {table_name} "
        "BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END"
        for operation in ("UPDATE", "DELETE")
    )
    matches = " OR ".join(
        "(" + " AND ".join(f"{column} IS NEW.{column}" for column in key) + ")"
        for key in identity_keys
    )
    return (*ddl,
        f"CREATE TRIGGER IF NOT EXISTS trg_{table_name}_immutable_insert "
        f"BEFORE INSERT ON {table_name} "
        f"WHEN EXISTS (SELECT 1 FROM {table_name} WHERE {matches}) "
        "BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    )


_IMMUTABLE_HISTORY = (
    (TopicRevision, (("id",), ("case_id", "revision"))),
    (TopicFactRevision, (("id",), ("case_id", "stable_fact_id", "revision"))),
    (TopicMutationReceipt, (("owner_id", "mutation_id"),)),
    (TopicConditionRevision, (("condition_id", "revision"),)),
    (TopicCheckRevision, (("id",), ("owner_id", "condition_id", "condition_revision", "semantic_input_hash", "evaluator_version"))),
)
BANK_TOPIC_IMMUTABLE_DDL = tuple(
    statement for model, keys in _IMMUTABLE_HISTORY
    for statement in _history_ddl(model.__tablename__, keys)
)
for _model, _keys in _IMMUTABLE_HISTORY:
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
    for _statement in _history_ddl(_model.__tablename__, _keys):
        event.listen(_model.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
