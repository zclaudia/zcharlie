"""Core protocols for pluggable extension points."""

from datetime import date
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

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


class CompletionResult(BaseModel):
    text: str
    output: Any | None = None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str
    provider: str


@runtime_checkable
class LLMClient(Protocol):
    tier_name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_model: type[BaseModel] | None = None,
        tools: list[Any] | None = None,
        purpose: str,
        pipeline_run_id: int | None = None,
    ) -> CompletionResult: ...
