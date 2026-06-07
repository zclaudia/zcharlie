"""Attribution pipeline end-to-end with fake DataSource + fake AttributionAgent."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from investment_agent.config import Settings
from investment_agent.domain.strategy_types import (
    AttributionResult,
    TriggeringEvent,
)
from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FundamentalSnapshotRecord,
    NewsRecord,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import AttributionReport
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.market_data import DailyPrice
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository


@pytest_asyncio.fixture
async def attribution_engine(tmp_path):
    db_path = tmp_path / "attr.db"
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


class FakeDS:
    name = "fake"
    supported_markets = ["NASDAQ"]

    async def fetch_universe(self, code, as_of):  # noqa: ARG002
        return []

    async def fetch_daily_prices(self, tickers, start, end):  # noqa: ARG002
        return [
            DailyPriceRecord(
                ticker=t, date=end,
                open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
                close=Decimal("100.5"), volume=1_000_000,
                adjusted_close=Decimal("100.5"),
            )
            for t in tickers
        ]

    async def fetch_financial_report(self, ticker, periods):  # noqa: ARG002
        return []

    async def fetch_fundamental_snapshot(
        self, ticker, as_of,
    ) -> FundamentalSnapshotRecord:
        return FundamentalSnapshotRecord(
            ticker=ticker, date=as_of, pe=None, pb=None, peg=None, ps=None,
            roe=None, roa=None, fcf_yield=None, debt_to_equity=None,
            market_cap=None,
        )

    async def fetch_news(self, ticker, days):  # noqa: ARG002
        return [
            NewsRecord(
                ticker=ticker, published_at=datetime.now(UTC),
                source="reuters", title="Apple beats earnings",
                url="https://x", summary="strong services growth",
            ),
        ]

    async def fetch_analyst_ratings(self, ticker, days):  # noqa: ARG002
        return [
            AnalystRatingRecord(
                ticker=ticker, date=date.today(),
                firm="GS", action="upgraded",
                from_rating="Hold", to_rating="Buy",
                price_target=Decimal("220"),
            ),
        ]


class FakeAgent:
    async def run_for_context(
        self, *, session, context, as_of,  # noqa: ARG002
    ) -> AttributionResult:
        return AttributionResult(
            summary="Earnings beat drove idiosyncratic +1%.",
            triggering_events=[
                TriggeringEvent(
                    event_type="earnings",
                    headline="Apple beats earnings",
                    source="reuters",
                    impact_estimate="+1%",
                    confidence=0.8,
                ),
            ],
            valuation_delta="PE 28→29",
            confidence=0.7,
        )


@pytest.mark.asyncio
async def test_attribution_pipeline_e2e(attribution_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")

    as_of = date(2025, 6, 3)

    # Seed: market + AAPL + SPY + 32 days of prices for both
    async with session_scope() as session:
        session.add(Market(
            code="NASDAQ", name="NASDAQ",
            timezone="America/New_York", currency="USD",
        ))
        repo = ListingRepository(session)
        aapl = await repo.upsert_listing(
            market_code="NASDAQ", ticker="AAPL", company_name="Apple",
        )
        spy = await repo.upsert_listing(
            market_code="NASDAQ", ticker="SPY", company_name="SPDR S&P 500",
        )
        for i in range(32):
            d = as_of - timedelta(days=31 - i)
            aapl_close = 100 + i * 0.5 + (1.5 if i == 31 else 0)
            spy_close = 100 + i * 0.3
            session.add(DailyPrice(
                listing_id=aapl.id, date=d,
                open=Decimal(str(aapl_close)), high=Decimal(str(aapl_close)),
                low=Decimal(str(aapl_close)), close=Decimal(str(aapl_close)),
                volume=1_000_000, adjusted_close=Decimal(str(aapl_close)),
            ))
            session.add(DailyPrice(
                listing_id=spy.id, date=d,
                open=Decimal(str(spy_close)), high=Decimal(str(spy_close)),
                low=Decimal(str(spy_close)), close=Decimal(str(spy_close)),
                volume=1_000_000, adjusted_close=Decimal(str(spy_close)),
            ))
        await session.flush()

    settings = Settings()  # type: ignore[call-arg]
    settings.data_dir = tmp_path

    from investment_agent.attribution.pipeline import AttributionPipeline

    pipeline = AttributionPipeline(
        data_source=FakeDS(),  # type: ignore[arg-type]
        agent=FakeAgent(),  # type: ignore[arg-type]
        settings=settings, benchmark_ticker="SPY",
    )

    run_id = await pipeline.run("AAPL", as_of)
    assert run_id > 0

    async with session_scope() as session:
        report = await session.get(AttributionReport, run_id)
        assert report is not None
        assert "Earnings beat" in report.narrative
        assert report.markdown_path is not None

    md_files = list((tmp_path / "reports").rglob("attribution_AAPL_*.md"))
    assert len(md_files) == 1
    body = md_files[0].read_text()
    assert "AAPL" in body
    assert "Attribution" in body
    assert "Earnings beat" in body
