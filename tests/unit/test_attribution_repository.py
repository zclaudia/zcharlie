from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.storage.models.analysis import AttributionReport
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.attribution_report import (
    AttributionReportRepository,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository


async def _seed(session):
    session.add(Market(code="NASDAQ", name="NASDAQ",
                       timezone="America/New_York", currency="USD"))
    await session.flush()
    listing = await ListingRepository(session).upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    run = await PipelineRunRepository(session).create(kind="attribution")
    return run, listing


@pytest.mark.asyncio
async def test_upsert_inserts(db_session):
    run, listing = await _seed(db_session)
    report = await AttributionReportRepository(db_session).upsert(
        pipeline_run_id=run.id, listing_id=listing.id,
        analysis_date=date(2025, 6, 3),
        price_change_pct=0.025,
        beta_attribution={"beta_market": 1.1},
        triggering_events=[{"event_type": "news", "headline": "x"}],
        narrative="moved on news",
    )
    assert report.pipeline_run_id == run.id
    assert report.price_change_pct == Decimal("0.025")


@pytest.mark.asyncio
async def test_upsert_updates_existing(db_session):
    run, listing = await _seed(db_session)
    repo = AttributionReportRepository(db_session)
    await repo.upsert(
        pipeline_run_id=run.id, listing_id=listing.id,
        analysis_date=date(2025, 6, 3), price_change_pct=0.01,
        beta_attribution={}, triggering_events=[], narrative="v1",
    )
    await repo.upsert(
        pipeline_run_id=run.id, listing_id=listing.id,
        analysis_date=date(2025, 6, 3), price_change_pct=0.02,
        beta_attribution={"beta_market": 1.2}, triggering_events=[],
        narrative="v2",
    )
    rows = (await db_session.execute(select(AttributionReport))).scalars().all()
    assert len(rows) == 1
    assert rows[0].narrative == "v2"
    assert rows[0].beta_attribution == {"beta_market": 1.2}


@pytest.mark.asyncio
async def test_latest_for_listing_limit(db_session):
    _, listing = await _seed(db_session)
    repo = AttributionReportRepository(db_session)
    for d, narr in [
        (date(2025, 6, 1), "old"),
        (date(2025, 6, 2), "mid"),
        (date(2025, 6, 3), "new"),
    ]:
        another = await PipelineRunRepository(db_session).create(kind="attribution")
        await repo.upsert(
            pipeline_run_id=another.id, listing_id=listing.id,
            analysis_date=d, price_change_pct=0.01,
            beta_attribution={}, triggering_events=[], narrative=narr,
        )
    rows = await repo.latest_for_listing(listing.id, limit=2)
    assert [r.narrative for r in rows] == ["new", "mid"]
