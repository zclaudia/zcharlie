"""Immutable research material: source bindings and versioned datasets (migration 046).

* ``research_source_binding`` - which symbol (or account id) one controlled
  company/listing has at one source, over a validity interval
  (``valid_from`` inclusive, ``valid_to`` exclusive when set). ``verified_at``
  and ``verified_by`` record when and by which controlled list the binding was
  confirmed (e.g. ``pilot_list@2026-10-12``).
* ``research_dataset`` - one immutable version of a source's material for one
  subject and period. ``content_hash`` identifies the version; changed
  content is a new row with ``revision + 1`` and ``previous_id`` pointing at
  the version it supersedes. ``subject_id`` is ``company.id`` or
  ``listing.id`` depending on ``subject_kind`` (company facts belong to the
  company, prices and corporate actions to the listing); there is no
  polymorphic foreign key, the repository and the contracts layer enforce the
  pairing. ``period_end`` is null for company-level ``filing_index`` rows,
  and ``payload`` always carries the canonical ``{"facts": [...]}`` shape from
  ``investment_agent.materials.contracts``.

Rows are never updated or deleted: the ORM refuses flushes that would
(``before_update`` / ``before_delete`` listeners), like the decision journal.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class ImmutableResearchMaterialError(ValueError, RuntimeError):
    """A flush tried to update or delete a research material row.

    Subclasses both conventions used in this codebase: the evidence tables raise
    ``ValueError``, the decision journal raises ``RuntimeError``.
    """


class ResearchSourceBinding(Base, TimestampMixin):
    """One source's symbol for a subject over a validity interval."""

    __tablename__ = "research_source_binding"
    __table_args__ = (
        UniqueConstraint(
            "subject_kind",
            "subject_id",
            "source",
            "source_symbol",
            "valid_from",
            name="uq_research_source_binding_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_by: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ResearchDataset(Base, TimestampMixin):
    """One immutable version of a source's material for one subject and period.

    The unique constraint covers ``(kind, subject_kind, subject_id, source,
    period_end, content_hash)``; SQLite treats null ``period_end`` values as
    distinct, so filing-index rows are deduplicated by the store's content-hash
    lookup, not by the constraint.
    """

    __tablename__ = "research_dataset"
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "subject_kind",
            "subject_id",
            "source",
            "period_end",
            "content_hash",
            name="uq_research_dataset_version",
        ),
        Index("ix_research_dataset_lookup", "kind", "subject_kind", "subject_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Report period end; null for company-level ``filing_index`` datasets."""
    period_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    """Version counter within (kind, subject_kind, subject_id, source, period_end)."""
    previous_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Plain integer, no foreign key: the version this row supersedes."""
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Original research fetch time (imports only); never back-filled."""
    raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Path relative to ``data_dir`` of the saved raw artifact."""
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


def _immutable(*args: object) -> None:
    raise ImmutableResearchMaterialError("saved research material is immutable")


for _model in (ResearchSourceBinding, ResearchDataset):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
