"""Rule store and screen results (P1-C3 / P1-C4, migration 016).

* ``rule_template`` - immutable template versions, keyed ``(id, version)``.
  ``definition`` is the canonical content without ``version``; ``hash`` its
  SHA-256 (:func:`investment_agent.rules.schema.canonical_hash`). Template
  sync inserts a new version when a file's hash differs from the latest one
  and never rewrites a stored version.
* ``rule_instance`` - per-stock rules (scope ``candidate`` / ``holding``).
  Every edit is a new row with ``version + 1`` per ``(listing, scope)``;
  ``definition`` is never updated, only the lifecycle columns
  (``status`` draft -> active -> retired, approvals). At most one ``active``
  row per ``(listing_id, scope)`` (partial unique index).
* ``screen_result`` - one row per (screen run, template version, member):
  ``outcome`` hit / truncated / miss / skipped / excluded, structured
  ``reasons``, the hit ``rank`` / ``strength`` and the member ``context``
  (provider, as_of, industry match) that the reasons' ``snapshot_ref`` omit.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin

SCREEN_OUTCOMES = frozenset({"hit", "truncated", "miss", "skipped", "excluded"})


class RuleTemplateRecord(Base):
    __tablename__ = "rule_template"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    definition: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calibration_status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuleInstanceRecord(Base, TimestampMixin):
    __tablename__ = "rule_instance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["rule_template.id", "rule_template.version"],
            name="fk_rule_instance_template",
        ),
        UniqueConstraint("listing_id", "scope", "version", name="uq_rule_instance_version"),
        Index(
            "uq_rule_instance_active",
            "listing_id",
            "scope",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    definition: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    drafted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuleDraftGenerationRecord(Base, TimestampMixin):
    """The shared drafting generation of one ``(listing, scope)`` (plan 7.2 WP9).

    Candidate lifecycle and watchlist entries both enqueue ``rule_draft`` runs
    for the same rule slot. Each enqueue claims the next generation, freezing
    the request ``token`` and the shared rule head it saw; the store and the
    completion callback re-check both inside their own transaction, so a
    superseded request can only leave audit rows.
    """

    __tablename__ = "rule_draft_generation"

    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    current_token: Mapped[str | None] = mapped_column(String(160), nullable=True)
    """Request identity: the candidate ``rule_draft`` idempotency key, or the watch request UUID."""
    current_origin_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    current_origin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rule_head: Mapped[str | None] = mapped_column(String(160), nullable=True)
    """The :meth:`RuleStore.head_token` the claimer saw; a later shared-rule
    change (approve, edit, retire) makes the frozen request stale too."""


class ScreenResultRecord(Base):
    __tablename__ = "screen_result"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["rule_template.id", "rule_template.version"],
            name="fk_screen_result_template",
        ),
        UniqueConstraint(
            "screen_run_id",
            "template_id",
            "template_version",
            "listing_id",
            name="uq_screen_result_member",
        ),
        Index("ix_screen_result_as_of_template", "as_of", "template_id"),
        Index("ix_screen_result_listing", "listing_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    screen_run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasons: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    context: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    """Member-level provenance shared by all reasons (provider, as_of, industry match)."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
