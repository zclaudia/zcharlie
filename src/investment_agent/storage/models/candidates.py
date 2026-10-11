"""Candidates and the decision journal (P1-D1 / P1-D2, migration 018).

* ``candidate`` - one stock proposed by the weekly screen or nominated by
  the user. ``status``: ``open`` -> ``promoted`` | ``dismissed`` |
  ``expired`` (terminal). At most one ``open`` row per listing (partial
  unique index); a later screen hit for the same listing appends to
  ``sources`` instead of opening a second row. ``draft_state`` tracks the
  LLM rule draft (``none`` / ``pending`` / ``ready`` / ``failed``) and
  ``draft_rule_instance_id`` the draft it produced.
  ``last_screen_result_id`` is a plain integer (no foreign key): a retried
  screen run rewrites its ``screen_result`` rows, and the candidate keeps
  the id it saw; readers fall back to the sources' run and template.
* ``decision`` - immutable, append-only journal of every state change
  (user or system). Rows are never updated or deleted: the ORM refuses
  flushes that would (``before_update`` / ``before_delete`` listeners).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin

CANDIDATE_STATUSES = ("open", "promoted", "dismissed", "expired")
DRAFT_STATES = ("none", "pending", "ready", "failed")
DECISION_KINDS = (
    "nominate",
    "promote",
    "dismiss",
    "approve_rule",
    "reject_rule",
    "edit_rule",
    "expire",
    "remove_watch",
    "drop",
)
DECISION_ACTORS = ("user", "system")


class CandidateRecord(Base, TimestampMixin):
    __tablename__ = "candidate"
    __table_args__ = (
        Index(
            "uq_candidate_open_listing",
            "listing_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_candidate_status_opened", "status", "opened_at"),
        Index("ix_candidate_listing", "listing_id", "opened_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    last_screen_result_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_state: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    draft_rule_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("rule_instance.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionRecord(Base):
    __tablename__ = "decision"
    __table_args__ = (
        Index("ix_decision_listing_at", "listing_id", "at"),
        Index("ix_decision_candidate", "candidate_id"),
        Index("ix_decision_kind_at", "kind", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("rule_instance.id"), nullable=True
    )
    rule_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"), nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ImmutableDecisionError(RuntimeError):
    """A flush tried to update or delete a ``decision`` row."""


@event.listens_for(DecisionRecord, "before_update")
def _refuse_update(_mapper: object, _connection: object, target: DecisionRecord) -> None:
    raise ImmutableDecisionError(f"decision {target.id} is immutable (append-only journal)")


@event.listens_for(DecisionRecord, "before_delete")
def _refuse_delete(_mapper: object, _connection: object, target: DecisionRecord) -> None:
    raise ImmutableDecisionError(f"decision {target.id} cannot be deleted (append-only journal)")
