"""Earnings-event tables (mrd-intake C1, migration 024): Eastmoney via AkShare.

All three are point-in-time captures (``provider="akshare"``, ``captured_at``,
``raw_ref`` to the run artifact); rows are never updated.

* ``earnings_forecast`` - one 业绩预告 per (listing, report period, notice
  date): the normalized ``forecast_type`` (预增 / 预减 / 扭亏 / 首亏 / 略增 /
  略减 / 减亏 / 增亏 / 续盈 / 续亏 / 不确定), the forecast net-profit range
  in CNY (``profit_low`` / ``profit_high``; equal when a point estimate),
  the year-on-year range as fractions (``yoy_low`` / ``yoy_high``), the
  ``indicator`` the range refers to (归属于上市公司股东的净利润, else
  扣除非经常性损益后的净利润) and the summary text ``content``. A re-capture
  of an existing key is skipped (the notice is the natural version).
* ``disclosure_schedule`` - 预约披露时间 of (listing, report period) as seen
  at ``captured_at``: ``scheduled_date`` (the latest change, else the first
  appointment; may be in the future), ``first_date`` and ``actual_date``
  (实际披露时间, null until published). A new row is appended whenever the
  scheduled or actual date changes (unique per capture instant).
* ``business_segment`` - 主营构成 rows of (listing, report period,
  ``kind`` product / industry / region, ``name``): ``revenue`` / ``profit``
  in CNY, ``share`` (of main-business revenue) and ``margin`` (gross)
  fractions. A capture whose rows equal the latest stored capture of the
  period is skipped; otherwise all rows are appended under one
  ``captured_at``.
"""

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


class EarningsForecastRecord(Base, TimestampMixin):
    __tablename__ = "earnings_forecast"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "report_period", "notice_date", name="uq_earnings_forecast_notice"
        ),
        Index("ix_earnings_forecast_period", "report_period", "notice_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    report_period: Mapped[date] = mapped_column(Date, nullable=False)
    notice_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_type: Mapped[str] = mapped_column(String(16), nullable=False)
    indicator: Mapped[str] = mapped_column(String(64), nullable=False)
    profit_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    profit_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    yoy_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    yoy_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class DisclosureScheduleRecord(Base, TimestampMixin):
    __tablename__ = "disclosure_schedule"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "report_period", "captured_at", name="uq_disclosure_schedule_capture"
        ),
        Index("ix_disclosure_schedule_period", "report_period", "scheduled_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    report_period: Mapped[date] = mapped_column(Date, nullable=False)
    first_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    actual_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class BusinessSegmentRecord(Base, TimestampMixin):
    __tablename__ = "business_segment"
    __table_args__ = (
        UniqueConstraint(
            "listing_id",
            "report_period",
            "kind",
            "name",
            "captured_at",
            name="uq_business_segment_capture",
        ),
        Index("ix_business_segment_listing_period", "listing_id", "report_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    report_period: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    margin: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    share: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
