"""Point-in-time (PIT) snapshot tables for the selection funnel (P1-A2).

* ``industry_classification`` - interval rows (``valid_from`` inclusive,
  ``valid_to`` exclusive) per listing and classification system.
* ``market_snapshot`` / ``financial_snapshot`` - immutable captures. A
  re-capture of the same natural key inserts a new row with ``version + 1``;
  payload columns are never updated. Readers pick the latest version whose
  ``captured_at`` is not after their knowledge time.

Index constituents reuse ``universe`` / ``universe_membership`` (decision D4).

Two providers write the same snapshot tables (decision D6): baostock for the
target pool, iFinD for the watchlist. ``provider`` tells them apart; readers
that care filter by it. ``version`` counts captures of a natural key across
providers, so it stays unique without a provider column in the key.

* ``report_overdue`` (P1-A6) - a target-pool member still without a period's
  report after the statutory deadline; ``resolved_on`` records when the late
  report became available (rows are never deleted).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin

# Raw ("none", for valuation) and backward-adjusted ("backward", 后复权, for
# returns) series only: backward-adjusted history never changes, whereas
# forward-adjusted prices rewrite every past bar on each dividend.
MARKET_ADJUST_KINDS = frozenset({"none", "backward"})
FINANCIAL_PERIOD_KINDS = frozenset({"annual", "semi", "q1", "q3"})
FINANCIAL_AVAILABILITY = frozenset({"actual", "approx", "unknown"})


class IndustryClassification(Base, TimestampMixin):
    __tablename__ = "industry_classification"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "system", "valid_from", name="uq_industry_classification_interval"
        ),
        Index("ix_industry_classification_listing_system", "listing_id", "system"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    system: Mapped[str] = mapped_column(String(16), nullable=False)
    # Pre-2012 CSRC labels carry no code; the name then holds the full label.
    code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketSnapshot(Base, TimestampMixin):
    __tablename__ = "market_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "trade_date", "adjust", "version", name="uq_market_snapshot_version"
        ),
        Index("ix_market_snapshot_listing_date", "listing_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    # iFinD watchlist rows only; baostock rows leave it NULL - factor-time
    # market cap uses the latest baostock financial ``total_shares``
    # available on the trade date.
    total_shares: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    # baostock per-day valuation / status (migration 014); NULL for iFinD rows.
    pe_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    pb_mrq: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ps_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    """Fraction of float shares traded that day (baostock ``turn`` ÷ 100)."""
    trade_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """1 trading, 0 suspended (suspended rows repeat the previous close)."""
    is_st: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    adjust: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class FinancialSnapshot(Base, TimestampMixin):
    __tablename__ = "financial_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "report_period", "version", name="uq_financial_snapshot_version"
        ),
        Index("ix_financial_snapshot_listing_available", "listing_id", "available_from"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    report_period: Mapped[date] = mapped_column(Date, nullable=False)
    period_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    available_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    availability: Mapped[str] = mapped_column(String(16), nullable=False)
    fields: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_backfill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ReportOverdue(Base, TimestampMixin):
    __tablename__ = "report_overdue"
    __table_args__ = (
        UniqueConstraint("listing_id", "report_period", name="uq_report_overdue_period"),
        Index("ix_report_overdue_period", "report_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    report_period: Mapped[date] = mapped_column(Date, nullable=False)
    deadline: Mapped[date] = mapped_column(Date, nullable=False)
    detected_on: Mapped[date] = mapped_column(Date, nullable=False)
    resolved_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    """``available_from`` (actual pubDate) of the late report."""
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    detected_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=True
    )
    resolved_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=True
    )
