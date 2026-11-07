"""Immutable domain facts and mutable delivery bookkeeping (EV-02).

SQLite foreign keys are not enforced in this application. Services must still
resolve owner, subject and source, and coordinate all writes in one transaction.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class ResearchDomainEvent(Base):
    __tablename__ = "research_domain_event"
    __table_args__ = (
        UniqueConstraint("owner_id", "event_type", "source_identity", "source_revision",
                         name="uq_research_domain_event_source"),
        CheckConstraint("sequence >= 1", name="ck_domain_event_sequence"),
        CheckConstraint("source_revision >= 1", name="ck_domain_event_source_revision"),
        CheckConstraint("schema_version >= 1", name="ck_domain_event_schema_version"),
        CheckConstraint("length(payload_hash) = 64", name="ck_domain_event_payload_hash"),
        CheckConstraint("json_valid(source_ref) AND json_type(source_ref) = 'object'",
                        name="ck_domain_event_source_object"),
        CheckConstraint("json_valid(payload) AND json_type(payload) = 'object'",
                        name="ck_domain_event_payload_object"),
        Index("ix_research_domain_event_owner_sequence", "owner_id", "sequence"),
        Index("ix_research_domain_event_type_sequence", "event_type", "sequence"),
        {"sqlite_autoincrement": True},
    )

    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    source_identity: Mapped[str] = mapped_column(String(250))
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_revision: Mapped[int]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int]
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ConsumerReceipt(Base):
    __tablename__ = "consumer_receipt"
    __table_args__ = (
        CheckConstraint("length(consumer) > 0", name="ck_consumer_receipt_name"),
        CheckConstraint("state IN ('applied','ignored','retry','blocked_predecessor','quarantined')",
                        name="ck_consumer_receipt_state"),
        CheckConstraint("attempts >= 0", name="ck_consumer_receipt_attempts"),
        CheckConstraint("effect_cursor IS NULL OR effect_cursor >= 0",
                        name="ck_consumer_receipt_effect_cursor"),
        CheckConstraint(
            "(effect_manifest IS NULL AND effect_manifest_hash IS NULL AND effect_cursor IS NULL) "
            "OR (effect_manifest IS NOT NULL AND effect_manifest_hash IS NOT NULL AND "
            "effect_cursor IS NOT NULL AND length(effect_manifest_hash) = 64)",
            name="ck_consumer_receipt_manifest_fields"),
        CheckConstraint(
            "effect_manifest IS NULL OR (json_valid(effect_manifest) AND json_type(effect_manifest) = 'object')",
            name="ck_consumer_receipt_manifest_object"),
        Index("ix_consumer_receipt_retry", "consumer", "state", "next_retry_at"),
        Index("ix_consumer_receipt_ordering", "consumer", "ordering_key", "state"),
    )

    consumer: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_domain_event.event_id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(24))
    ordering_key: Mapped[str] = mapped_column(String(250))
    effect_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    effect_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    effect_cursor: Mapped[int | None]
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(160))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projector_version: Mapped[str] = mapped_column(String(80))
    effect_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class ProjectionCursor(Base):
    __tablename__ = "projection_cursor"
    __table_args__ = (
        CheckConstraint("length(consumer) > 0", name="ck_projection_cursor_name"),
        CheckConstraint("scanned_sequence >= 0", name="ck_projection_cursor_sequence"),
        CheckConstraint("revision >= 1", name="ck_projection_cursor_revision"),
        CheckConstraint("retry_count >= 0 AND quarantined_count >= 0",
                        name="ck_projection_cursor_counts"),
        CheckConstraint(
            "consumer = 'research_inbox.v1' OR (active_generation IS NULL AND rebuild_state IS NULL)",
            name="ck_projection_cursor_inbox_generation"),
        CheckConstraint(
            "rebuild_state IS NULL OR (active_generation IS NOT NULL AND "
            "json_valid(rebuild_state) AND json_type(rebuild_state) = 'object')",
            name="ck_projection_cursor_rebuild_object"),
    )

    consumer: Mapped[str] = mapped_column(String(80), primary_key=True)
    scanned_sequence: Mapped[int] = mapped_column(default=0, server_default="0")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(default=0, server_default="0")
    quarantined_count: Mapped[int] = mapped_column(default=0, server_default="0")
    active_generation: Mapped[str | None] = mapped_column(String(36))
    rebuild_state: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))


def _immutable(*args: object) -> None:
    raise ValueError("research domain events are immutable")


event.listen(ResearchDomainEvent, "before_update", _immutable)
event.listen(ResearchDomainEvent, "before_delete", _immutable)

# Export SQL so the migration can reproduce create_all's protection exactly.
# INSERT guard prevents SQLite OR REPLACE silently deleting a prior event even
# when recursive_triggers is off. Replays must read/compare the existing fact.
EVENT_IMMUTABILITY_DDL = (
    "CREATE TRIGGER IF NOT EXISTS trg_research_domain_event_immutable_update "
    "BEFORE UPDATE ON research_domain_event BEGIN "
    "SELECT RAISE(ABORT, 'research domain events are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_domain_event_immutable_delete "
    "BEFORE DELETE ON research_domain_event BEGIN "
    "SELECT RAISE(ABORT, 'research domain events are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_domain_event_immutable_insert "
    "BEFORE INSERT ON research_domain_event WHEN EXISTS "
    "(SELECT 1 FROM research_domain_event WHERE sequence = NEW.sequence OR event_id = NEW.event_id OR "
    "(owner_id = NEW.owner_id AND event_type = NEW.event_type AND "
    "source_identity = NEW.source_identity AND source_revision = NEW.source_revision)) "
    "BEGIN SELECT RAISE(ABORT, 'research domain event identity already exists'); END",
)
for _statement in EVENT_IMMUTABILITY_DDL:
    event.listen(ResearchDomainEvent.__table__, "after_create",
                 DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
