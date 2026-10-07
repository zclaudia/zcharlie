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
    false,
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
    is_backfill: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    """Written by the historical backfill (P1-A4, migration 015), captured long
    after ``trade_date``; see :meth:`MarketSnapshotRepository.series_as_of`."""
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


class DividendEventSnapshot(Base, TimestampMixin):
    """One distribution (分红送转) of a listing, keyed by ex-date (migration 017).

    Immutable and versioned like the other snapshots: a re-capture whose
    payload differs from the latest version inserts ``version + 1``; an
    identical re-capture inserts nothing. ``available_from`` is when the
    event became public (plan announcement, else implementation announcement,
    else record date); yield factors only count cash dividends whose
    ``ex_date <= as_of``.
    """

    __tablename__ = "dividend_event"
    __table_args__ = (
        UniqueConstraint("listing_id", "ex_date", "version", name="uq_dividend_event_version"),
        Index("ix_dividend_event_listing_ex_date", "listing_id", "ex_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    pay_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    announce_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Plan announcement (baostock ``dividPlanAnnounceDate``)."""
    implement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Implementation announcement (baostock ``dividPlanDate``)."""
    available_from: Mapped[date] = mapped_column(Date, nullable=False)
    cash_per_share_pre_tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    stock_per_share: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    """Bonus shares (送股) per share."""
    reserve_per_share: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    """Capitalisation shares (转增) per share."""
    operate_year: Mapped[int] = mapped_column(Integer, nullable=False)
    """The ex-date year the event was collected under (baostock ``yearType=operate``)."""
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_backfill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class DividendCoverage(Base, TimestampMixin):
    """Which ex-date years were collected per listing and provider (migration 017).

    Distinguishes "no dividend" from "not collected": a listing without
    events in a covered year paid nothing with an ex-date in that year.
    ``first_checked_at`` is the knowledge time of the coverage (backfilled
    rows: reconstructed, like the other backfill tables); ``last_checked_at``
    and ``event_count`` follow the latest check. Point-in-time readers take
    the latest check known on a date from :class:`DividendCoverageCheck`.
    """

    __tablename__ = "dividend_coverage"
    __table_args__ = (
        UniqueConstraint("listing_id", "provider", "year", name="uq_dividend_coverage_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    first_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_backfill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DividendCoverageCheck(Base, TimestampMixin):
    """Append-only history of dividend coverage checks (migration 020).

    ``dividend_coverage.last_checked_at`` moves with every re-collection, so
    reading it for a past ``as_of`` leaked later checks into point-in-time
    results (the dividend factor's freshness and ``inputs_ref``). One row per
    check lets readers take the latest check known by the end of ``as_of``.
    """

    __tablename__ = "dividend_coverage_check"
    __table_args__ = (
        Index(
            "ix_dividend_coverage_check_lookup", "listing_id", "provider", "year", "checked_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_backfill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
