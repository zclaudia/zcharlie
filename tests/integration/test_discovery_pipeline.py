"""Discovery pipeline end-to-end with fake DataSource + fake LLM."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import Agent
from sqlalchemy import select

from investment_agent.pipelines.steps import (
    step1_data_ingestion,
    step2_tier1_screening,
)
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
from investment_agent.strategies.base import BaseStrategyAgent, StrategyConfig
from investment_agent.strategies.schemas import ValueDeepAnalysis, ValueStockScore
from investment_agent.strategies.screening_rule import Condition, ScreeningRule


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


@pytest.mark.asyncio
async def test_step2_screening_filters_by_strategy(db_session, seeded_market):
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple"
    )
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
    )
    as_of = date(2025, 6, 3)
    db_session.add(
        FundamentalSnapshot(listing_id=aapl.id, date=as_of, pe=Decimal("20"))
    )
    db_session.add(
        FundamentalSnapshot(listing_id=msft.id, date=as_of, pe=Decimal("40"))
    )
    await db_session.flush()

    cfg = StrategyConfig(
        code="test_low_pe",
        version="v1",
        name="t",
        description="",
        screening_rule=ScreeningRule(
            required=[Condition(metric="pe", op="<", value=30)]
        ),
        scoring_prompt_template="x",
        deep_prompt_template="y",
        scoring_schema=ValueStockScore,
        deep_schema=ValueDeepAnalysis,
    )

    class _StubAgent:
        pass

    class _Strategy(BaseStrategyAgent):
        pass

    _Strategy.config = cfg
    strategy = _Strategy(
        prompts_dir=Path("."),
        tier2_agent=cast(Agent, _StubAgent()),
        tier3_agent=cast(Agent, _StubAgent()),
    )

    result = await step2_tier1_screening(
        db_session,
        listings=[aapl, msft],
        strategies={"test_low_pe": strategy},
        as_of=as_of,
    )
    assert len(result["test_low_pe"]) == 1
    assert result["test_low_pe"][0].ticker == "AAPL"
