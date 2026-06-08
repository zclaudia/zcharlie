"""Pydantic DTOs returned by DataSource implementations."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _RecordBase(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ListingRef(_RecordBase):
    market_code: str
    ticker: str
    company_name: str
    sector_code: str | None = None


class DailyPriceRecord(_RecordBase):
    ticker: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_close: Decimal


class FinancialReportRecord(_RecordBase):
    ticker: str
    fiscal_period: str   # '2025-Q3' | '2024-FY'
    period_end: date
    revenue: Decimal | None
    net_income: Decimal | None
    fcf: Decimal | None
    gross_profit: Decimal | None = None
    total_assets: Decimal | None
    total_equity: Decimal | None
    raw_data: dict[str, object]


class FundamentalSnapshotRecord(_RecordBase):
    ticker: str
    date: date
    pe: Decimal | None
    pb: Decimal | None
    peg: Decimal | None
    ps: Decimal | None
    roe: Decimal | None
    roa: Decimal | None
    fcf_yield: Decimal | None
    debt_to_equity: Decimal | None
    market_cap: Decimal | None
    beta: Decimal | None = None


class NewsRecord(_RecordBase):
    ticker: str
    published_at: datetime
    source: str
    title: str
    url: str
    summary: str | None = None


class AnalystRatingRecord(_RecordBase):
    ticker: str
    date: date
    firm: str
    action: str
    from_rating: str | None = None
    to_rating: str | None = None
    price_target: Decimal | None = None
