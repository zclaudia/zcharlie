"""WatchlistItem, WatchDraftRequest, HoldingItem, AuxiliaryListing.

Legacy tables (from the retired NASDAQ Discovery/Attribution product, preserved
at tag ``legacy-nasdaq-final``) keep their ORM classes so ``Base.metadata``
keeps matching the alembic-migrated schema; nothing at runtime uses them.
Legacy here: AuxiliaryListing.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin

WATCH_DRAFT_STATES = (
    "none",
    "needs_template",
    "queued",
    "drafting",
    "ready",
    "failed",
    "rejected",
)
"""``watchlist_item.draft_state`` values (plan 1.5).

``none`` = nothing to draft (an active rule exists); ``needs_template`` = the
user has not picked a drafting template yet; the rest mirror the current
request's status.
"""

WATCH_DRAFT_REQUEST_STATUSES = (
    "queued",
    "drafting",
    "ready",
    "failed",
    "rejected",
    "cancelled",
)


class WatchlistItem(Base, TimestampMixin):
    __tablename__ = "watchlist_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False, unique=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    report_year: Mapped[int | None] = mapped_column(nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_refresh_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="never"
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refresh_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_research_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=True
    )
    draft_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    draft_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    draft_rule_instance_id: Mapped[int | None] = mapped_column(nullable=True)
    """Plain integer, no foreign key (like ``candidate.last_screen_result_id``):
    an audit pointer, so inserts/removals of rules never fail on the entry."""


class WatchDraftRequest(Base, TimestampMixin):
    """One rule-draft request of an observation entry (watch-holding plan 1.5).

    The request carries the drafting identity: a retried add reuses ``id``
    (network retry), while an explicit re-draft creates a new UUID and sets
    ``superseded_by`` on the previous open request. The item's current request
    is ``watchlist_item.draft_request_id``; the worker's store-and-link guard
    only accepts a draft while its request is still that current one, so a
    late worker can leave audit rows but can neither replace a newer draft nor
    revive a removed entry. ``watchlist_id`` is a plain integer (removal
    hard-deletes the item row; the request rows stay as audit).
    """

    __tablename__ = "watch_draft_request"
    __table_args__ = (
        Index("ix_watch_draft_request_watchlist", "watchlist_id"),
        Index("ix_watch_draft_request_listing_status", "listing_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(nullable=False)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False
    )
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("rule_instance.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class HoldingItem(Base, TimestampMixin):
    """Reserved for Phase B -- V1 not populated."""

    __tablename__ = "holding_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    opened_at: Mapped[date] = mapped_column(Date, nullable=False)


class AuxiliaryListing(Base, TimestampMixin):
    __tablename__ = "auxiliary_listing"
    __table_args__ = (UniqueConstraint("listing_id", name="uq_aux_listing"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False
    )
    added_via_relationship_id: Mapped[int | None] = mapped_column(
        ForeignKey("company_relationship.id"), nullable=True
    )
    added_by: Mapped[str] = mapped_column(String(32), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
