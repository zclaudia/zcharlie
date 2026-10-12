"""Immutable industry-peer snapshots: named-cohort rosters and annual facts (WP1).

* ``peer_cohort_snapshot`` - one immutable version of a named cohort roster
  (``cohort_key=baijiu_current`` first). A roster change is a new row with
  ``revision + 1``; the row's ``revision`` is fixed at insert and its payload
  (classification system/version and code, index code, roster source with
  locator and hash, per-company entries with classification/listing-status
  evidence references and comparability flags, included/excluded companies
  with per-company reasons) is never updated. ``observed_on`` is the
  observation date - never presented as an official constituent effective
  date; ``acquired_at`` is when the system obtained the roster; ``verification``
  records the roster's check status.

* ``peer_annual_snapshot`` - one immutable version of one company's annual
  facts for one report period and source. Field values and verification
  metadata live in JSON (original responses stay in the run-artifact
  directory); the unique key is
  ``(company_id, report_period, provider, adapter_version, semantic_hash)``
  where ``semantic_hash`` covers amounts, semantics, the source revision and
  verification/conflict state but not retry times or local paths. The first
  save's ``captured_at`` never moves; a later re-read's check time belongs to
  batch artifacts, not to this row.

The ``fields`` JSON maps metric name to one canonical field entry; the entry
shape is fixed by :class:`investment_agent.industry_peers.contracts.PeerAnnualField`
(decimal-string values, never JSON floats; null value only with
``missing_reason``; period with explicit basis; source field name; check
status). The ``verification`` JSON maps metric name to its per-field proof
(original ObjectRef, PDF and locator hashes, report identity, page, header,
raw value and unit, period, accounting scope, checked time, rule version or
checker); its detailed shape is WP3's contract.

Rows are append-only: ORM listeners refuse updates/deletes, and SQLite
triggers also block bulk SQL and direct database mutations (migration 048).
Annual snapshots deliberately stay out of ``FinancialSnapshot`` default
queries and the scoring field registry.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class ImmutablePeerSnapshotError(ValueError, RuntimeError):
    """A flush tried to update or delete a peer snapshot row.

    Subclasses both conventions used in this codebase: the evidence tables raise
    ``ValueError``, the decision journal raises ``RuntimeError``.
    """


class PeerCohortSnapshot(Base, TimestampMixin):
    """One immutable roster version of a named peer cohort."""

    __tablename__ = "peer_cohort_snapshot"
    __table_args__ = (
        UniqueConstraint("cohort_key", "revision", name="uq_peer_cohort_snapshot_revision"),
        Index("ix_peer_cohort_snapshot_lookup", "cohort_key", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    """UUID; ``ObjectRef.kind="peer_cohort_snapshot"``."""
    cohort_key: Mapped[str] = mapped_column(String(64), nullable=False)
    """Stable roster name; first cohort is ``baijiu_current``."""
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    """Fixed roster version within ``cohort_key``; a change appends a new row."""
    observed_on: Mapped[date] = mapped_column(Date, nullable=False)
    """Observation date; precision stays separate from any effective date."""
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When the system obtained the roster (timezone-aware)."""
    verification: Mapped[str] = mapped_column(String(16), nullable=False)
    """Roster check status (e.g. ``candidate`` / ``verified``)."""
    inclusion_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    """Version of the inclusion rules that produced this roster."""
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """sha256 over the canonical payload; import idempotency key."""
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """Immutable roster JSON (source, classification, entries, inclusions/exclusions)."""


class PeerAnnualSnapshot(Base, TimestampMixin):
    """One immutable version of one company's annual facts for one period."""

    __tablename__ = "peer_annual_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "report_period",
            "provider",
            "adapter_version",
            "semantic_hash",
            name="uq_peer_annual_snapshot_version",
        ),
        Index("ix_peer_annual_snapshot_lookup", "company_id", "report_period"),
        Index("ix_peer_annual_snapshot_captured", "company_id", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    """UUID; ``ObjectRef.kind="peer_annual_snapshot"``."""
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    """The representative listing the facts belong to (declarative FK only)."""
    report_period: Mapped[date] = mapped_column(Date, nullable=False)
    """Fiscal year end (e.g. 2025-12-31 for the 2025 annual report)."""
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """First save time; never moved by a re-read (revisions are new rows)."""
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """sha256 over the raw payload as saved (retry-time independent)."""
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """Version identity in the unique key: amounts, semantics, source revision,
    verification and conflict state - not retry times or local paths."""
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """Metric name -> field entry; shape in
    ``investment_agent.industry_peers.contracts.PeerAnnualField``."""
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """Metric name -> per-field proof (originals, hashes, pages, check times)."""


_IMMUTABLE_INSERT_KEYS = {
    "peer_cohort_snapshot": ("cohort_key", "revision"),
    "peer_annual_snapshot": (
        "company_id", "report_period", "provider", "adapter_version", "semantic_hash",
    ),
}


def _immutable(*args: object) -> None:
    raise ImmutablePeerSnapshotError("saved peer snapshots are immutable")


for _model in (PeerCohortSnapshot, PeerAnnualSnapshot):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

    # create_all-based environments get the same database protection as 048.
    for _operation in ("UPDATE", "DELETE"):
        event.listen(
            _model.__table__,
            "after_create",
            DDL(  # type: ignore[no-untyped-call]
                f"CREATE TRIGGER IF NOT EXISTS trg_{_model.__tablename__}_immutable_{_operation.lower()} "
                f"BEFORE {_operation} ON {_model.__tablename__} "
                "BEGIN SELECT RAISE(ABORT, 'saved peer snapshots are immutable'); END"
            ).execute_if(dialect="sqlite"),
        )

    # INSERT OR REPLACE bypasses DELETE triggers unless recursive_triggers is
    # enabled. Refuse a conflicting insertion before SQLite can remove a row.
    _key_match = " AND ".join(
        f"{column} IS NEW.{column}" for column in _IMMUTABLE_INSERT_KEYS[_model.__tablename__]
    )
    event.listen(
        _model.__table__,
        "after_create",
        DDL(  # type: ignore[no-untyped-call]
            f"CREATE TRIGGER IF NOT EXISTS trg_{_model.__tablename__}_immutable_insert "
            f"BEFORE INSERT ON {_model.__tablename__} "
            f"WHEN EXISTS (SELECT 1 FROM {_model.__tablename__} "
            f"WHERE id = NEW.id OR ({_key_match})) "
            "BEGIN SELECT RAISE(ABORT, 'saved peer snapshots are immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
