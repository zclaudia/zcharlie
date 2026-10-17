"""P4 change journal and bounded CAS projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class SupplementalChangeReceiptRow(Base):
    __tablename__ = "supplemental_change_receipt"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_supplemental_change_sequence_positive"),
        CheckConstraint("schema_version = 1", name="ck_supplemental_change_schema_version"),
        CheckConstraint("length(payload_hash) = 64", name="ck_supplemental_change_payload_hash"),
        CheckConstraint(
            "request_hash IS NULL OR length(request_hash) = 64",
            name="ck_supplemental_change_request_hash",
        ),
        CheckConstraint(
            "source_manifest_hash IS NULL OR length(source_manifest_hash) = 64",
            name="ck_supplemental_change_manifest_hash",
        ),
        CheckConstraint("json_valid(payload)", name="ck_supplemental_change_payload_json"),
        CheckConstraint(
            "json_type(payload) = 'object'", name="ck_supplemental_change_payload_object"
        ),
        CheckConstraint(
            "(kind = 'scope_member' AND parent_sequence IS NOT NULL AND listing_id IS NOT NULL AND context IS NOT NULL) "
            "OR (kind <> 'scope_member')",
            name="ck_supplemental_change_member_shape",
        ),
        UniqueConstraint("owner_id", "change_key", name="uq_supplemental_change_owner_key"),
        Index(
            "uq_supplemental_change_owner_operation",
            "owner_id",
            "operation_id",
            unique=True,
            sqlite_where=text("operation_id IS NOT NULL"),
        ),
        Index(
            "uq_supplemental_change_parent_member",
            "parent_sequence",
            "listing_id",
            "context",
            unique=True,
            sqlite_where=text("kind = 'scope_member'"),
        ),
        Index("ix_supplemental_change_owner_sequence", "owner_id", "sequence"),
        Index("ix_supplemental_change_owner_kind_sequence", "owner_id", "kind", "sequence"),
        Index(
            "ix_supplemental_change_owner_member_outcome_latest",
            "owner_id",
            text("json_extract(payload, '$.member_sequence')"),
            text("sequence DESC"),
            sqlite_where=text("kind = 'scope_outcome'"),
        ),
        Index(
            "ix_supplemental_change_owner_scope_sequence",
            "owner_id",
            "listing_id",
            "context",
            "sequence",
        ),
        Index("ix_supplemental_change_origin_run", "owner_id", "origin_run_id"),
        Index(
            "ix_supplemental_change_evidence_proof",
            "owner_id", "listing_id", "context", "committed_at", "sequence",
            sqlite_where=text("kind = 'evidence_revision'"),
        ),
        Index(
            "ix_supplemental_change_poll_final_cutoff",
            "owner_id", "listing_id", "context", "committed_at", "sequence",
            sqlite_where=text(
                "kind = 'source_page' AND json_extract(payload, '$.record_type') = 'poll_final_v1'"
            ),
        ),
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    change_key: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_sequence: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("supplemental_change_receipt.sequence"), nullable=True
    )
    listing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SupplementalSourceStateRow(Base):
    __tablename__ = "supplemental_source_state"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_supplemental_source_revision_nonnegative"),
        CheckConstraint("length(config_hash) = 64", name="ck_supplemental_source_config_hash"),
        CheckConstraint("length(scope_hash) = 64", name="ck_supplemental_source_scope_hash"),
        CheckConstraint(
            "coverage_state IN ('complete','partial','unknown','failed')",
            name="ck_supplemental_source_coverage_state",
        ),
        CheckConstraint("success_sequence >= 0", name="ck_supplemental_source_success_sequence"),
        CheckConstraint("health_limit_seconds >= 0", name="ck_supplemental_source_health_limit"),
        CheckConstraint(
            "success_receipt_hash IS NULL OR length(success_receipt_hash) = 64",
            name="ck_supplemental_source_success_receipt_hash",
        ),
        CheckConstraint("json_valid(source_refs)", name="ck_supplemental_source_refs_json"),
        CheckConstraint("json_valid(payload)", name="ck_supplemental_source_payload_json"),
        CheckConstraint(
            "json_type(payload) = 'object'", name="ck_supplemental_source_payload_object"
        ),
        UniqueConstraint("owner_id", "feed_id", "scope_key", name="uq_supplemental_source_scope"),
        Index(
            "ix_supplemental_source_health_scope", "owner_id", "feed_id", "listing_id", "context"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    feed_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    listing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    complete_watermark: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    coverage_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    success_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_receipt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_limit_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SupplementalRefreshStateRow(Base):
    __tablename__ = "supplemental_refresh_state"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_supplemental_refresh_revision_nonnegative"),
        CheckConstraint(
            "confirmed_sequence >= 0", name="ck_supplemental_refresh_confirmed_nonnegative"
        ),
        CheckConstraint(
            "requested_sequence >= confirmed_sequence",
            name="ck_supplemental_refresh_requested_ge_confirmed",
        ),
        CheckConstraint("json_valid(payload)", name="ck_supplemental_refresh_payload_json"),
        CheckConstraint(
            "json_type(payload) = 'object'", name="ck_supplemental_refresh_payload_object"
        ),
        UniqueConstraint("owner_id", "listing_id", "context", name="uq_supplemental_refresh_scope"),
        Index("ix_supplemental_refresh_owner_due", "owner_id", "next_due_at"),
        Index("ix_supplemental_refresh_active_run", "owner_id", "active_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    listing_id: Mapped[int] = mapped_column(Integer, nullable=False)
    context: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


def _immutable(*args: object) -> None:
    raise ValueError("supplemental change receipts are immutable")


event.listen(SupplementalChangeReceiptRow, "before_update", _immutable)
event.listen(SupplementalChangeReceiptRow, "before_delete", _immutable)
for _operation in ("UPDATE", "DELETE"):
    event.listen(
        SupplementalChangeReceiptRow.__table__,
        "after_create",
        DDL(  # type: ignore[no-untyped-call]
            f"CREATE TRIGGER IF NOT EXISTS trg_supplemental_change_immutable_{_operation.lower()} "
            "BEFORE " + _operation + " ON supplemental_change_receipt "
            "BEGIN SELECT RAISE(ABORT, 'supplemental change receipt is immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
event.listen(
    SupplementalChangeReceiptRow.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_supplemental_change_immutable_insert "
        "BEFORE INSERT ON supplemental_change_receipt WHEN EXISTS "
        "(SELECT 1 FROM supplemental_change_receipt WHERE owner_id=NEW.owner_id AND change_key=NEW.change_key) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental change receipt identity already exists'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    SupplementalChangeReceiptRow.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_supplemental_change_member_parent "
        "BEFORE INSERT ON supplemental_change_receipt WHEN NEW.kind='scope_member' AND NOT EXISTS "
        "(SELECT 1 FROM supplemental_change_receipt p WHERE p.sequence=NEW.parent_sequence "
        "AND p.owner_id=NEW.owner_id AND p.kind<>'scope_member') "
        "BEGIN SELECT RAISE(ABORT, 'scope member requires an owner-matched non-member parent'); END"
    ).execute_if(dialect="sqlite"),
)
