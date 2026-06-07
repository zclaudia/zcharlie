"""Daily price, financial report, fundamental snapshot, news, analyst ratings."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class DailyPrice(Base, TimestampMixin):
    __tablename__ = "daily_price"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[int] = mapped_column(nullable=False)
    adjusted_close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)


class FinancialReport(Base, TimestampMixin):
    __tablename__ = "financial_report"

    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), primary_key=True
    )
    fiscal_period: Mapped[str] = mapped_column(String(16), primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    fcf: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    raw_data: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)


class FundamentalSnapshot(Base, TimestampMixin):
    __tablename__ = "fundamental_snapshot"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    pe: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    pb: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    peg: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    roa: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    fcf_yield: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    debt_to_equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)


class NewsItem(Base, TimestampMixin):
    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)


class AnalystRatingChange(Base, TimestampMixin):
    __tablename__ = "analyst_rating_change"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    firm: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_rating: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_rating: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_target: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
