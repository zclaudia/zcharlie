"""Immutable P2 rule/event revisions and mutable owner-level heads."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class SupplementalRuleRevision(Base):
    __tablename__ = "supplemental_rule_revision"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_supplemental_rule_revision_positive"),
        CheckConstraint("schema_version = 1", name="ck_supplemental_rule_schema_version"),
        CheckConstraint("length(content_hash) = 64", name="ck_supplemental_rule_content_hash"),
        CheckConstraint("length(semantic_hash) = 64", name="ck_supplemental_rule_semantic_hash"),
        CheckConstraint("json_valid(payload)", name="ck_supplemental_rule_payload_json"),
        CheckConstraint("json_type(payload) = 'object'", name="ck_supplemental_rule_payload_object"),
        UniqueConstraint("owner_id", "id", "revision", name="uq_supplemental_rule_owner_revision"),
        Index("ix_supplemental_rule_owner_id_revision", "owner_id", "id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SupplementalRuleHead(Base):
    __tablename__ = "supplemental_rule_head"
    __table_args__ = (
        CheckConstraint("head_revision >= 0", name="ck_supplemental_rule_head_revision_nonnegative"),
        CheckConstraint(
            "(active_revision IS NULL AND active_state IS NULL) OR "
            "(active_revision IS NOT NULL AND active_revision >= 1 AND active_state IS NOT NULL "
            "AND active_state IN ('active','suspended','retired'))",
            name="ck_supplemental_rule_active_shape",
        ),
        CheckConstraint(
            "(draft_revision IS NULL AND draft_state IS NULL) OR "
            "(draft_revision IS NOT NULL AND draft_revision >= 1 AND draft_state IS NOT NULL "
            "AND draft_state IN ('draft','validated'))",
            name="ck_supplemental_rule_draft_shape",
        ),
        CheckConstraint(
            "(last_validation_revision IS NULL AND last_validation_hash IS NULL) OR "
            "(last_validation_revision IS NOT NULL AND last_validation_revision >= 1 "
            "AND last_validation_hash IS NOT NULL AND length(last_validation_hash) = 64)",
            name="ck_supplemental_rule_validation_shape",
        ),
    )

    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_validation_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_validation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_validation_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class SupplementalRuleEvent(Base):
    __tablename__ = "supplemental_rule_event"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_supplemental_rule_event_sequence_positive"),
        CheckConstraint("json_valid(payload)", name="ck_supplemental_rule_event_payload_json"),
        CheckConstraint("json_type(payload) = 'object'", name="ck_supplemental_rule_event_payload_object"),
        Index("ix_supplemental_rule_event_owner_rule_sequence", "owner_id", "rule_id", "sequence"),
        Index("ix_supplemental_rule_event_operation", "owner_id", "operation_id"),
    )

    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SupplementalRegistryHead(Base):
    __tablename__ = "supplemental_registry_head"
    __table_args__ = (
        CheckConstraint("registry_revision >= 0", name="ck_supplemental_registry_revision_nonnegative"),
        CheckConstraint("rules_generation >= 0", name="ck_supplemental_registry_rules_generation_nonnegative"),
        CheckConstraint("events_generation >= 0", name="ck_supplemental_registry_events_generation_nonnegative"),
    )

    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    registry_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rules_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    semantic_probe_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchEventRevision(Base):
    __tablename__ = "research_event_revision"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_research_event_revision_positive"),
        CheckConstraint("previous_ref IS NULL OR json_valid(previous_ref)", name="ck_research_event_previous_ref_json"),
        CheckConstraint("json_valid(event_aliases)", name="ck_research_event_aliases_json"),
        CheckConstraint("json_valid(payload)", name="ck_research_event_payload_json"),
        CheckConstraint("json_type(payload) = 'object'", name="ck_research_event_payload_object"),
        CheckConstraint("length(content_hash) = 64", name="ck_research_event_content_hash"),
        UniqueConstraint("owner_id", "id", "revision", name="uq_research_event_owner_revision"),
        Index("ix_research_event_owner_key_revision", "owner_id", "event_key", "revision"),
        Index("ix_research_event_owner_available", "owner_id", "available_from"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    event_aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


def _immutable(*args: object) -> None:
    raise ValueError("supplemental rule/event revisions are immutable")


for _model in (SupplementalRuleRevision, ResearchEventRevision):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
    for _operation in ("UPDATE", "DELETE"):
        event.listen(
            _model.__table__,
            "after_create",
            DDL(  # type: ignore[no-untyped-call]
                f"CREATE TRIGGER IF NOT EXISTS trg_{_model.__tablename__}_immutable_{_operation.lower()} "
                f"BEFORE {_operation} ON {_model.__tablename__} "
                "BEGIN SELECT RAISE(ABORT, 'supplemental revision is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )
    event.listen(
        _model.__table__,
        "after_create",
        DDL(  # type: ignore[no-untyped-call]
            f"CREATE TRIGGER IF NOT EXISTS trg_{_model.__tablename__}_immutable_insert "
            f"BEFORE INSERT ON {_model.__tablename__} "
            "WHEN EXISTS (SELECT 1 FROM "
            f"{_model.__tablename__} WHERE id = NEW.id AND revision = NEW.revision) "
            "BEGIN SELECT RAISE(ABORT, 'supplemental revision is immutable'); END"
        ).execute_if(dialect="sqlite"),
    )


event.listen(
    SupplementalRuleHead.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_supplemental_rule_head_refs_insert "
        "BEFORE INSERT ON supplemental_rule_head WHEN "
        "NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id) OR "
        "(NEW.active_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id AND r.revision = NEW.active_revision)) OR "
        "(NEW.draft_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id AND r.revision = NEW.draft_revision)) "
        "OR (NEW.last_validation_revision IS NOT NULL AND (NEW.last_validation_revision IS NOT NEW.draft_revision "
        "OR NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r WHERE r.owner_id = NEW.owner_id "
        "AND r.id = NEW.rule_id AND r.revision = NEW.last_validation_revision))) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental rule head reference owner mismatch'); END"
    ).execute_if(dialect="sqlite"),
)

event.listen(
    SupplementalRuleEvent.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_supplemental_rule_event_parent_insert "
        "BEFORE INSERT ON supplemental_rule_event WHEN NOT EXISTS (SELECT 1 FROM supplemental_rule_head h "
        "WHERE h.owner_id = NEW.owner_id AND h.rule_id = NEW.rule_id) OR "
        "(NEW.rule_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id AND r.revision = NEW.rule_revision)) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental rule event parent mismatch'); END"
    ).execute_if(dialect="sqlite"),
)

event.listen(
    ResearchEventRevision.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_research_event_revision_parent_insert "
        "BEFORE INSERT ON research_event_revision WHEN "
        "(NEW.revision = 1 AND NEW.previous_ref IS NOT NULL) OR "
        "(NEW.revision > 1 AND (NEW.previous_ref IS NULL OR "
        "json_type(NEW.previous_ref) IS NOT 'object' OR "
        "json_type(NEW.previous_ref, '$.kind') IS NOT 'text' OR "
        "json_extract(NEW.previous_ref, '$.kind') IS NOT 'research_event' OR "
        "json_type(NEW.previous_ref, '$.id') IS NOT 'text' OR "
        "json_extract(NEW.previous_ref, '$.id') IS NOT NEW.id OR "
        "json_type(NEW.previous_ref, '$.revision') IS NOT 'integer' OR "
        "json_extract(NEW.previous_ref, '$.revision') IS NOT NEW.revision - 1 OR "
        "NOT EXISTS (SELECT 1 FROM research_event_revision p WHERE p.owner_id = NEW.owner_id "
        "AND p.id = NEW.id AND p.revision = NEW.revision - 1))) "
        "BEGIN SELECT RAISE(ABORT, 'research event previous revision mismatch'); END"
    ).execute_if(dialect="sqlite"),
)

event.listen(SupplementalRuleEvent, "before_update", _immutable)
event.listen(SupplementalRuleEvent, "before_delete", _immutable)
for _operation in ("UPDATE", "DELETE"):
    event.listen(
        SupplementalRuleEvent.__table__,
        "after_create",
        DDL(  # type: ignore[no-untyped-call]
            f"CREATE TRIGGER IF NOT EXISTS trg_supplemental_rule_event_immutable_{_operation.lower()} "
            f"BEFORE {_operation} ON supplemental_rule_event "
            "BEGIN SELECT RAISE(ABORT, 'supplemental rule event is immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
event.listen(
    SupplementalRuleEvent.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_supplemental_rule_event_immutable_insert "
        "BEFORE INSERT ON supplemental_rule_event WHEN EXISTS (SELECT 1 FROM supplemental_rule_event "
        "WHERE owner_id = NEW.owner_id AND rule_id = NEW.rule_id AND sequence = NEW.sequence) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental rule event is immutable'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    SupplementalRuleHead.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_supplemental_rule_head_refs_update "
        "BEFORE UPDATE ON supplemental_rule_head WHEN "
        "NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id) OR "
        "(NEW.active_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id AND r.revision = NEW.active_revision)) OR "
        "(NEW.draft_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id = NEW.owner_id AND r.id = NEW.rule_id AND r.revision = NEW.draft_revision)) "
        "OR (NEW.last_validation_revision IS NOT NULL AND (NEW.last_validation_revision IS NOT NEW.draft_revision "
        "OR NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r WHERE r.owner_id = NEW.owner_id "
        "AND r.id = NEW.rule_id AND r.revision = NEW.last_validation_revision))) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental rule head reference owner mismatch'); END"
    ).execute_if(dialect="sqlite"),
)
