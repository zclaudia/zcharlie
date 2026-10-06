"""Reference entities: Market, Sector, Company, Listing, Universe, UniverseMembership.

Legacy tables (from the retired NASDAQ Discovery/Attribution product, preserved
at tag ``legacy-nasdaq-final``) keep their ORM classes so ``Base.metadata``
keeps matching the alembic-migrated schema; nothing at runtime uses them.
Legacy here: Universe, UniverseMembership (PipelineRun.universe_code still
has a foreign key to ``universe``).
"""

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from investment_agent.storage.models.base import Base, TimestampMixin


class Market(Base, TimestampMixin):
    __tablename__ = "market"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)


class Sector(Base, TimestampMixin):
    __tablename__ = "sector"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Company(Base, TimestampMixin):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    sector_code: Mapped[str | None] = mapped_column(
        ForeignKey("sector.code"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(256), nullable=True)

    listings: Mapped[list["Listing"]] = relationship(back_populates="company")


class Listing(Base, TimestampMixin):
    __tablename__ = "listing"
    __table_args__ = (
        UniqueConstraint("market_code", "ticker", name="uq_listing_market_ticker"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    market_code: Mapped[str] = mapped_column(ForeignKey("market.code"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="listings")


class Universe(Base, TimestampMixin):
    __tablename__ = "universe"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class UniverseMembership(Base, TimestampMixin):
    __tablename__ = "universe_membership"

    universe_code: Mapped[str] = mapped_column(
        ForeignKey("universe.code"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), primary_key=True
    )
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
