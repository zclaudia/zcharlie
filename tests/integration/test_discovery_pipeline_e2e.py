"""Full Discovery Pipeline end-to-end smoke test.

Exercises the entire DiscoveryPipeline.run flow against a disk-backed sqlite
with a fake DataSource + fake StrategyAgents. Asserts DB state + markdown
report file are produced.
"""

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.config import (
    AppYamlConfig,
    AuxiliaryTrackingConfig,
    PipelineConfig,
    SchedulerConfig,
    Settings,
    UniverseEntry,
)
from investment_agent.domain.strategy_types import (
    DeepAnalysis,
    ListingSnapshot,
    ScoringContext,
)
from investment_agent.domain.strategy_types import StockScore as StockScoreDTO
from investment_agent.pipelines.discovery import DiscoveryPipeline
from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    ListingRef,
    NewsRecord,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import (
    DiscoveryReport,
    PipelineRun,
    StockScore,
)
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import (
    Market,
    Universe,
    UniverseMembership,
)
from investment_agent.storage.repositories.listing import ListingRepository


@pytest_asyncio.fixture
async def e2e_engine(tmp_path):
    """Disk-backed sqlite so multiple session_scope() opens share state."""
    db_path = tmp_path / "e2e.db"
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


class _FakeDS:
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
                date=end,
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
            pe=Decimal("15"),
            pb=Decimal("2"),
            peg=None,
            ps=None,
            roe=Decimal("0.20"),
            roa=Decimal("0.10"),
            fcf_yield=Decimal("0.06"),
            debt_to_equity=Decimal("0.5"),
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


class _FakeStrategy:
    """Pass-everything screener returning a deterministic score."""

    def __init__(self, code: str, score: int, version: str = "v1") -> None:
        self.code = code
        self.version = version
        self._score = score

    async def screen(
        self, candidates: list[ListingSnapshot]
    ) -> list[ListingSnapshot]:
        return list(candidates)

    async def score(
        self, listing: ListingSnapshot, ctx: ScoringContext
    ) -> StockScoreDTO:
        return StockScoreDTO(
            score=self._score,
            confidence=0.9,
            bull_thesis=f"{self.code} bull",
            bear_thesis=f"{self.code} bear",
            key_metrics_explained={"pe": "fair"},
        )

    async def deep_analyze(self, listing, ctx) -> DeepAnalysis:
        return DeepAnalysis(
            business_model="fake",
            moat_assessment="fake",
            risk_factors=[],
            upstream_dependencies=[],
            downstream_customers=[],
            competitor_overview=[],
            valuation_summary="fake",
        )


@pytest.mark.asyncio
async def test_full_discovery_pipeline_e2e(e2e_engine, tmp_path, monkeypatch):
    """E2E: fake DataSource + fake StrategyAgents → DB rows + markdown written."""
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")

    # 1. Seed market + universe + 3 listings.
    as_of = date(2025, 6, 3)
    async with session_scope() as session:
        session.add(
            Market(
                code="NASDAQ",
                name="NASDAQ",
                timezone="America/New_York",
                currency="USD",
            )
        )
        session.add(Universe(code="TEST_U", name="Test"))
        await session.flush()
        repo = ListingRepository(session)
        aapl = await repo.upsert_listing(
            market_code="NASDAQ", ticker="AAPL", company_name="Apple"
        )
        msft = await repo.upsert_listing(
            market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
        )
        nvda = await repo.upsert_listing(
            market_code="NASDAQ", ticker="NVDA", company_name="Nvidia"
        )
        for listing in (aapl, msft, nvda):
            session.add(
                UniverseMembership(
                    universe_code="TEST_U",
                    listing_id=listing.id,
                    valid_from=date(2024, 1, 1),
                )
            )
        await session.flush()

    # 2. Build strategies (three to encourage consensus classification).
    strategies = {
        "value": _FakeStrategy("value", 75),
        "growth": _FakeStrategy("growth", 78),
        "risk_aggressive": _FakeStrategy("risk_aggressive", 72),
    }

    settings = Settings()  # type: ignore[call-arg]
    # Redirect data_dir at tmp_path so FileNotifier writes under the test sandbox.
    settings.data_dir = tmp_path
    cfg = AppYamlConfig(
        universes=[UniverseEntry(code="TEST_U")],
        pipeline=PipelineConfig(),
        scheduler=SchedulerConfig(
            discovery_cron="0 0 * * *", discovery_timezone="UTC"
        ),
        auxiliary_tracking=AuxiliaryTrackingConfig(),
    )
    pipeline = DiscoveryPipeline(
        data_source=_FakeDS(),  # type: ignore[arg-type]
        strategies=strategies,  # type: ignore[arg-type]
        settings=settings,
        app_config=cfg,
    )

    # 3. Run end-to-end.
    run_id = await pipeline.run("TEST_U", as_of)
    assert run_id > 0

    # 4. Assert DB state.
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        assert run.status == "success"
        # Step 7 attaches a hit-rate review to metadata_json.
        assert "hit_rate_review" in run.metadata_json

        scores = list(
            (
                await session.execute(
                    select(StockScore).where(StockScore.pipeline_run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        # 3 listings × 3 strategies (Tier 2) = 9 minimum; Tier 3 may add rows.
        assert len(scores) >= 9

        report = await session.get(DiscoveryReport, run_id)
        assert report is not None
        # Scores 72-78 with spread 6 → all three classify as consensus.
        assert len(report.consensus_picks) >= 1
        assert report.markdown_path is not None

    # 5. Assert the markdown file was written under data_dir/reports/<date>/.
    md_files = list((tmp_path / "reports").rglob("discovery_*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text()
    assert "Discovery Report" in content
    assert "Consensus" in content
