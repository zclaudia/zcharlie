"""Core protocols for pluggable extension points."""

from datetime import date
from typing import Protocol, runtime_checkable

from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    ListingRef,
    NewsRecord,
)


@runtime_checkable
class DataSource(Protocol):
    name: str
    supported_markets: list[str]

    async def fetch_universe(
        self, universe_code: str, as_of: date
    ) -> list[ListingRef]: ...

    async def fetch_daily_prices(
        self, tickers: list[str], start: date, end: date
    ) -> list[DailyPriceRecord]: ...

    async def fetch_financial_report(
        self, ticker: str, periods: list[str]
    ) -> list[FinancialReportRecord]: ...

    async def fetch_fundamental_snapshot(
        self, ticker: str, as_of: date
    ) -> FundamentalSnapshotRecord: ...

    async def fetch_news(
        self, ticker: str, days: int
    ) -> list[NewsRecord]: ...

    async def fetch_analyst_ratings(
        self, ticker: str, days: int
    ) -> list[AnalystRatingRecord]: ...
