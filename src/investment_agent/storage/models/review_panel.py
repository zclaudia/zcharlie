"""Multi-perspective review storage (docs/product/multi-model-review-design.md §9).

* ``review_round`` - one run of a panel over one listing. The row freezes the
  panel composition (``panel_snapshot``), the data boundary (``as_of`` /
  ``evidence_cutoff``), the public base pack and the tokens/requests reserved
  for the whole round. After creation only ``status`` and ``settled_usage``
  change.
* ``review_seat_run`` - one row per seat of a round; the opinion, server
  annotations, actually-read evidence refs and usage are written once the seat
  reaches a terminal state.
* ``review_comparison`` - deterministic comparison output, keyed by the
  algorithm version so an upgrade can add a new row while old rows stay.

The stock reference is ``listing_id`` (like ``watchlist_item`` and the rule
tables), so the watchlist API can join entries to rounds directly. The auto
trigger dedupe identity is ``(listing_id, trigger, trigger_ref)`` while
``trigger_ref`` is set: a change-tracking/snapshot run can create at most one
round.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class ReviewRoundRecord(Base):
    __tablename__ = "review_round"
    __table_args__ = (
        Index("ix_review_round_listing_created", "listing_id", "created_at"),
        Index(
            "uq_review_round_trigger",
            "listing_id",
            "trigger",
            "trigger_ref",
            unique=True,
            sqlite_where=text("trigger_ref IS NOT NULL"),
            postgresql_where=text("trigger_ref IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    """``manual`` / ``change`` / ``scheduled`` (:class:`RoundTrigger`)."""
    trigger_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    """The change-tracking or snapshot run that caused an automatic round."""
    panel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    """Frozen seats: seat_id, lens_id, lens_ref, model_ref, model snapshot."""
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    """The latest committed snapshot date all seats analyze."""
    evidence_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    base_pack: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    """The public base pack every seat receives (design §4.3 item 4). Null
    for rounds created before the column existed; the hash is always set."""
    base_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    """``queued`` / ``running`` / ``completed`` / ``failed`` (:class:`RoundStatus`)."""
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    settled_usage: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReviewSeatRunRecord(Base):
    __tablename__ = "review_seat_run"
    __table_args__ = (
        UniqueConstraint("round_id", "seat_id", name="uq_review_seat_run_round_seat"),
        Index("ix_review_seat_run_round", "round_id"),
        Index("ix_review_seat_run_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("review_round.id"), nullable=False
    )
    seat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lens_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lens_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    """The lens version used, e.g. ``value.v1``."""
    model_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    model_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    """``queued`` / ``running`` / ``succeeded`` / ``partial`` / ``failed``."""
    opinion_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    annotations_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    """Evidence the seat actually read, recorded by the server, not the model."""
    usage: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReviewComparisonRecord(Base):
    __tablename__ = "review_comparison"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "algorithm_version", name="uq_review_comparison_version"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("review_round.id"), nullable=False
    )
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
