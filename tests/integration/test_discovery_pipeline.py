"""Discovery pipeline end-to-end with fake DataSource + fake LLM."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.pipelines.steps import step1_data_ingestion
from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    ListingRef,
    NewsRecord,
)
from investment_agent.storage.models.market_data import DailyPrice, FundamentalSnapshot
from investment_agent.storage.models.reference import Universe, UniverseMembership
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository


class FakeDataSource:
    name = "fake"
    supported_markets = ["NASDAQ"]

    async def fetch_universe(
        self, universe_code: str, as_of: date
    ) -> list[ListingRef]:
        return []

    async def fetch_daily_prices(
        self, tickers: list[str], start: date, end: date
    ) -> list[DailyPriceRecord]:
        return [
            DailyPriceRecord(
                ticker=t,
                date=date(2025, 6, 3),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=1_000_000,
                adjusted_close=Decimal("100.5"),
            )
            for t in tickers
        ]

    async def fetch_fundamental_snapshot(
        self, ticker: str, as_of: date
    ) -> FundamentalSnapshotRecord:
        return FundamentalSnapshotRecord(
            ticker=ticker,
            date=as_of,
            pe=Decimal("20"),
            pb=Decimal("3"),
            peg=None,
            ps=None,
            roe=Decimal("0.15"),
            roa=Decimal("0.08"),
            fcf_yield=Decimal("0.06"),
            debt_to_equity=Decimal("1.0"),
            market_cap=Decimal("100000000000"),
        )

    async def fetch_financial_report(
        self, ticker: str, periods: list[str]
    ) -> list[FinancialReportRecord]:
        return []

    async def fetch_news(self, ticker: str, days: int) -> list[NewsRecord]:
        return []

    async def fetch_analyst_ratings(
        self, ticker: str, days: int
    ) -> list[AnalystRatingRecord]:
        return []


@pytest.mark.asyncio
async def test_step1_ingestion_populates_price_and_fundamental(
    db_session, seeded_market
):
    db_session.add(Universe(code="TEST_U", name="Test"))
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    db_session.add(
        UniverseMembership(
            universe_code="TEST_U",
            listing_id=listing.id,
            valid_from=date(2024, 1, 1),
        )
    )
    await db_session.flush()

    run = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="TEST_U",
    )

    universe_listings, aux_listings = await step1_data_ingestion(
        db_session,
        universe_code="TEST_U",
        data_source=FakeDataSource(),
        as_of=date(2025, 6, 3),
        pipeline_run_id=run.id,
    )
    assert len(universe_listings) == 1
    assert len(aux_listings) == 0

    prices = (await db_session.execute(select(DailyPrice))).scalars().all()
    assert len(prices) == 1
    snaps = (await db_session.execute(select(FundamentalSnapshot))).scalars().all()
    assert len(snaps) == 1
