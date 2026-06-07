"""CompanyRelationship: supplier_of / competes_with / subsidiary_of / partner_of."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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


class CompanyRelationship(Base, TimestampMixin):
    __tablename__ = "company_relationship"
    __table_args__ = (
        UniqueConstraint(
            "from_company_id",
            "to_company_id",
            "relationship_type",
            "valid_from",
            name="uq_company_rel",
        ),
        Index("ix_rel_from", "from_company_id", "relationship_type"),
        Index("ix_rel_to", "to_company_id", "relationship_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False
    )
    to_company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)

    strength: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, default=0.5)
    revenue_share_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 3), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
