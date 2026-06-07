"""WatchlistItem, HoldingItem, AuxiliaryListing."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


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
