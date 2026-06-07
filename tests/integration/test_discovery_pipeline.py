"""Discovery pipeline end-to-end with fake DataSource + fake LLM."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import Agent
from sqlalchemy import select

from investment_agent.domain.protocols import DataSource
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
async def test_step1_skips_disabled_auxiliary_listing(db_session, seeded_market):
    """An AuxiliaryListing with enabled=False is not fetched."""
    from datetime import UTC, date, datetime

    from sqlalchemy import select

    from investment_agent.pipelines.steps import step1_data_ingestion
    from investment_agent.storage.models.market_data import DailyPrice
    from investment_agent.storage.models.reference import (
        Universe,
        UniverseMembership,
    )
    from investment_agent.storage.models.user_domain import AuxiliaryListing
    from investment_agent.storage.repositories.listing import ListingRepository
    from investment_agent.storage.repositories.pipeline_run import (
        PipelineRunRepository,
    )

    db_session.add(Universe(code="TEST_U", name="Test"))
    repo = ListingRepository(db_session)
    universe_l = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple"
    )
    aux_disabled = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
    )
    db_session.add(
        UniverseMembership(
            universe_code="TEST_U",
            listing_id=universe_l.id,
            valid_from=date(2024, 1, 1),
        )
    )
    db_session.add(
        AuxiliaryListing(
            listing_id=aux_disabled.id,
            added_by="test",
            added_at=datetime.now(UTC),
            enabled=False,
        )
    )
    await db_session.flush()

    run = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="TEST_U"
    )
    universe, aux = await step1_data_ingestion(
        db_session,
        universe_code="TEST_U",
        data_source=FakeDataSource(),
        as_of=date(2025, 6, 3),
        pipeline_run_id=run.id,
    )
    assert len(universe) == 1
    assert len(aux) == 0
    prices = (await db_session.execute(select(DailyPrice))).scalars().all()
    assert {p.listing_id for p in prices} == {universe_l.id}


@pytest.mark.asyncio
async def test_step1_fetches_enabled_auxiliary_listing(db_session, seeded_market):
    """An enabled AuxiliaryListing on a different market is fetched (prices + news only, no snapshot)."""
    from datetime import UTC, date, datetime

    from sqlalchemy import select

    from investment_agent.pipelines.steps import step1_data_ingestion
    from investment_agent.storage.models.market_data import (
        DailyPrice,
        FundamentalSnapshot,
    )
    from investment_agent.storage.models.reference import (
        Market,
        Universe,
        UniverseMembership,
    )
    from investment_agent.storage.models.user_domain import AuxiliaryListing
    from investment_agent.storage.repositories.listing import ListingRepository
    from investment_agent.storage.repositories.pipeline_run import (
        PipelineRunRepository,
    )

    db_session.add(
        Market(code="TPE", name="Taiwan", timezone="Asia/Taipei", currency="TWD")
    )
    db_session.add(Universe(code="TEST_U", name="Test"))
    repo = ListingRepository(db_session)
    universe_l = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple"
    )
    aux_listing = await repo.upsert_listing(
        market_code="TPE", ticker="2330", company_name="TSMC"
    )
    db_session.add(
        UniverseMembership(
            universe_code="TEST_U",
            listing_id=universe_l.id,
            valid_from=date(2024, 1, 1),
        )
    )
    db_session.add(
        AuxiliaryListing(
            listing_id=aux_listing.id,
            added_by="test",
            added_at=datetime.now(UTC),
            enabled=True,
        )
    )
    await db_session.flush()

    run = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="TEST_U"
    )
    universe, aux = await step1_data_ingestion(
        db_session,
        universe_code="TEST_U",
        data_source=FakeDataSource(),
        as_of=date(2025, 6, 3),
        pipeline_run_id=run.id,
    )
    assert len(universe) == 1
    assert len(aux) == 1
    prices = (await db_session.execute(select(DailyPrice))).scalars().all()
    assert {p.listing_id for p in prices} == {universe_l.id, aux_listing.id}
    snaps = (await db_session.execute(select(FundamentalSnapshot))).scalars().all()
    # Auxiliary listings have no fundamental snapshot fetched (step1 spec)
    assert {s.listing_id for s in snaps} == {universe_l.id}


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


@pytest.mark.asyncio
async def test_step4_cache_hit_skips_llm(db_session, seeded_market):
    """If cache row is fresh, _check_or_run_deep_analysis returns cached, no LLM call."""
    from datetime import UTC, date, datetime
    from investment_agent.domain.strategy_types import ListingSnapshot
    from investment_agent.pipelines.steps import _check_or_run_deep_analysis
    from investment_agent.storage.models.market_data import FinancialReport
    from investment_agent.storage.models.ops import DeepAnalysisCache
    from investment_agent.storage.repositories.listing import ListingRepository

    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple"
    )
    db_session.add(
        FinancialReport(
            company_id=aapl.company_id,
            fiscal_period="2025-Q1",
            period_end=date(2025, 3, 31),
        )
    )
    db_session.add(
        DeepAnalysisCache(
            listing_id=aapl.id,
            latest_financial_report_period="2025-Q1",
            strategy_code="value",
            analysis_json={"business_model": "cached"},
            generated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    snap = ListingSnapshot(
        listing_id=aapl.id,
        company_id=aapl.company_id,
        ticker="AAPL",
        market_code="NASDAQ",
        company_name="Apple",
        as_of=date(2025, 6, 3),
    )

    class _NeverCalledAgent:
        async def deep_analyze(self, listing, ctx):
            raise AssertionError("LLM should not be called on cache hit")

    result = await _check_or_run_deep_analysis(
        db_session,
        listing_id=aapl.id,
        strategy_code="value",
        agent=cast(BaseStrategyAgent, _NeverCalledAgent()),
        snapshot=snap,
        cache_days=90,
    )
    assert result == {"business_model": "cached"}


@pytest.mark.asyncio
async def test_step4_cache_miss_calls_llm_and_persists(db_session, seeded_market):
    """Cache miss → LLM called → result persisted to DeepAnalysisCache."""
    from datetime import date
    from sqlalchemy import select
    from investment_agent.domain.strategy_types import DeepAnalysis, ListingSnapshot
    from investment_agent.pipelines.steps import _check_or_run_deep_analysis
    from investment_agent.storage.models.market_data import FinancialReport
    from investment_agent.storage.models.ops import DeepAnalysisCache
    from investment_agent.storage.repositories.listing import ListingRepository

    repo = ListingRepository(db_session)
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
    )
    db_session.add(
        FinancialReport(
            company_id=msft.company_id,
            fiscal_period="2025-Q1",
            period_end=date(2025, 3, 31),
        )
    )
    await db_session.flush()

    snap = ListingSnapshot(
        listing_id=msft.id,
        company_id=msft.company_id,
        ticker="MSFT",
        market_code="NASDAQ",
        company_name="Microsoft",
        as_of=date(2025, 6, 3),
    )

    class _StubAgent:
        async def deep_analyze(self, listing, ctx):
            return DeepAnalysis(
                business_model="cloud + productivity",
                moat_assessment="dominant",
                risk_factors=["regulation"],
                upstream_dependencies=[],
                downstream_customers=["enterprises"],
                competitor_overview=["AWS"],
                valuation_summary="fair",
            )

    result = await _check_or_run_deep_analysis(
        db_session,
        listing_id=msft.id,
        strategy_code="growth",
        agent=cast(BaseStrategyAgent, _StubAgent()),
        snapshot=snap,
        cache_days=90,
    )
    assert result["business_model"] == "cloud + productivity"
    rows = (await db_session.execute(select(DeepAnalysisCache))).scalars().all()
    assert len(rows) == 1
    assert rows[0].strategy_code == "growth"


@pytest.mark.asyncio
async def test_step4_stale_cache_refreshes(db_session, seeded_market):
    """Cache older than cache_days triggers a fresh LLM call and updates the row."""
    from datetime import UTC, date, datetime, timedelta
    from sqlalchemy import select
    from investment_agent.domain.strategy_types import DeepAnalysis, ListingSnapshot
    from investment_agent.pipelines.steps import _check_or_run_deep_analysis
    from investment_agent.storage.models.market_data import FinancialReport
    from investment_agent.storage.models.ops import DeepAnalysisCache
    from investment_agent.storage.repositories.listing import ListingRepository

    repo = ListingRepository(db_session)
    nvda = await repo.upsert_listing(
        market_code="NASDAQ", ticker="NVDA", company_name="Nvidia"
    )
    db_session.add(
        FinancialReport(
            company_id=nvda.company_id,
            fiscal_period="2025-Q1",
            period_end=date(2025, 3, 31),
        )
    )
    db_session.add(
        DeepAnalysisCache(
            listing_id=nvda.id,
            latest_financial_report_period="2025-Q1",
            strategy_code="value",
            analysis_json={"business_model": "stale"},
            generated_at=datetime.now(UTC) - timedelta(days=200),
        )
    )
    await db_session.flush()

    snap = ListingSnapshot(
        listing_id=nvda.id,
        company_id=nvda.company_id,
        ticker="NVDA",
        market_code="NASDAQ",
        company_name="Nvidia",
        as_of=date(2025, 6, 3),
    )

    class _FreshAgent:
        async def deep_analyze(self, listing, ctx):
            return DeepAnalysis(
                business_model="GPUs",
                moat_assessment="strong",
                risk_factors=[],
                upstream_dependencies=[],
                downstream_customers=[],
                competitor_overview=[],
                valuation_summary="rich",
            )

    result = await _check_or_run_deep_analysis(
        db_session,
        listing_id=nvda.id,
        strategy_code="value",
        agent=cast(BaseStrategyAgent, _FreshAgent()),
        snapshot=snap,
        cache_days=90,
    )
    assert result["business_model"] == "GPUs"
    rows = (await db_session.execute(select(DeepAnalysisCache))).scalars().all()
    assert len(rows) == 1
    assert rows[0].analysis_json["business_model"] == "GPUs"


@pytest.mark.asyncio
async def test_step7_no_prior_run_yields_skip_note(db_session, seeded_market):
    from datetime import date

    from investment_agent.pipelines.steps import step7_hit_rate_review
    from investment_agent.storage.repositories.pipeline_run import (
        PipelineRunRepository,
    )

    run = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="NASDAQ_100",
    )
    review = await step7_hit_rate_review(
        db_session,
        pipeline_run_id=run.id,
        universe_code="NASDAQ_100",
        as_of=date(2025, 6, 3),
    )
    assert review["note"] is not None
    assert review["hit_rate"] is None


@pytest.mark.asyncio
async def test_step7_computes_hit_rate_vs_benchmark(db_session, seeded_market):
    from datetime import date
    from decimal import Decimal

    from investment_agent.pipelines.steps import step7_hit_rate_review
    from investment_agent.storage.models.analysis import DiscoveryReport
    from investment_agent.storage.models.market_data import DailyPrice
    from investment_agent.storage.repositories.listing import ListingRepository
    from investment_agent.storage.repositories.pipeline_run import (
        PipelineRunRepository,
    )

    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple"
    )
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
    )
    spy = await repo.upsert_listing(
        market_code="NASDAQ", ticker="SPY", company_name="SPDR S&P 500"
    )

    # Prior run with two consensus picks.
    prior = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="NASDAQ_100",
    )
    await PipelineRunRepository(db_session).mark_success(prior.id)
    db_session.add(
        DiscoveryReport(
            pipeline_run_id=prior.id,
            consensus_picks=[
                {"listing_id": aapl.id, "avg_score": 75, "supporting": ["value"]},
                {"listing_id": msft.id, "avg_score": 80, "supporting": ["growth"]},
            ],
            divergent_picks=[],
            overlooked_picks=[],
        )
    )

    # Prices: AAPL +5%, MSFT +1%, SPY +2% → AAPL outperforms, MSFT does not.
    def _seed_price(lid: int, d: date, close: float) -> None:
        db_session.add(
            DailyPrice(
                listing_id=lid,
                date=d,
                open=Decimal(str(close)),
                high=Decimal(str(close)),
                low=Decimal(str(close)),
                close=Decimal(str(close)),
                volume=1_000_000,
                adjusted_close=Decimal(str(close)),
            )
        )

    yesterday = date(2025, 6, 2)
    today = date(2025, 6, 3)
    _seed_price(aapl.id, yesterday, 100)
    _seed_price(aapl.id, today, 105)
    _seed_price(msft.id, yesterday, 100)
    _seed_price(msft.id, today, 101)
    _seed_price(spy.id, yesterday, 100)
    _seed_price(spy.id, today, 102)
    await db_session.flush()

    current = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="NASDAQ_100",
    )
    review = await step7_hit_rate_review(
        db_session,
        pipeline_run_id=current.id,
        universe_code="NASDAQ_100",
        as_of=today,
    )
    assert review["hit_rate"] == 0.5
    assert review["n_picks"] == 2
    assert review["n_outperformed"] == 1
    assert review["benchmark_return"] == 0.02


def test_discovery_pipeline_constructs(monkeypatch):
    """DiscoveryPipeline can be instantiated with fakes — full e2e in Task 22."""
    from investment_agent.config import (
        AppYamlConfig,
        AuxiliaryTrackingConfig,
        PipelineConfig,
        SchedulerConfig,
        Settings,
        UniverseEntry,
    )
    from investment_agent.pipelines.discovery import DiscoveryPipeline

    cfg = AppYamlConfig(
        universes=[UniverseEntry(code="NASDAQ_100")],
        pipeline=PipelineConfig(),
        scheduler=SchedulerConfig(
            discovery_cron="0 0 * * *", discovery_timezone="UTC"
        ),
        auxiliary_tracking=AuxiliaryTrackingConfig(),
    )
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "test")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "test")
    settings = Settings()  # type: ignore[call-arg]
    pipeline = DiscoveryPipeline(
        data_source=cast("DataSource", FakeDataSource()),
        strategies={},
        settings=settings,
        app_config=cfg,
    )
    assert pipeline._app_config.pipeline.tier3_top_n == 10
