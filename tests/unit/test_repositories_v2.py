"""Tests for PipelineRun / StockScore / Company / Fundamental / Price repositories."""

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from investment_agent.storage.models.analysis import Strategy
from investment_agent.storage.models.reference import Company, Listing, Market, Sector
from investment_agent.storage.repositories.company import CompanyRepository
from investment_agent.storage.repositories.fundamental import FundamentalRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.price import PriceRepository
from investment_agent.storage.repositories.stock_score import StockScoreRepository


@pytest_asyncio.fixture
async def seeded_world(db_session):
    """Seed Market + Sector + 2 Companies + 1 Listing + 1 Strategy."""
    market = Market(
        code="NASDAQ",
        name="NASDAQ",
        timezone="America/New_York",
        currency="USD",
    )
    sector = Sector(code="TECH", name="Technology")
    db_session.add_all([market, sector])
    await db_session.flush()

    company1 = Company(canonical_name="Apple Inc.", sector_code="TECH")
    company2 = Company(canonical_name="Microsoft Corp.", sector_code="TECH")
    company3 = Company(canonical_name="Ford Motor Co.", sector_code=None)
    db_session.add_all([company1, company2, company3])
    await db_session.flush()

    listing = Listing(company_id=company1.id, market_code="NASDAQ", ticker="AAPL")
    db_session.add(listing)
    await db_session.flush()

    strategy = Strategy(
        code="value_v1",
        name="Value v1",
        version="1.0",
        description="Value strategy",
        config_json={},
        enabled=True,
    )
    db_session.add(strategy)
    await db_session.flush()

    return {
        "market": market,
        "sector": sector,
        "company1": company1,
        "company2": company2,
        "company3": company3,
        "listing": listing,
        "strategy": strategy,
    }


@pytest.mark.asyncio
async def test_pipeline_run_create_returns_running(db_session, seeded_world):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="discovery", triggered_by="cli")
    assert run.id is not None
    assert run.status == "running"
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.metadata_json == {}


@pytest.mark.asyncio
async def test_pipeline_run_mark_success_merges_metadata(db_session, seeded_world):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="discovery")
    run.metadata_json = {"existing": "value"}
    await db_session.flush()

    await repo.mark_success(run.id, metadata={"new": "data"})
    refreshed = await repo.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "success"
    assert refreshed.finished_at is not None
    assert refreshed.metadata_json == {"existing": "value", "new": "data"}


@pytest.mark.asyncio
async def test_pipeline_run_mark_failed_sets_error(db_session, seeded_world):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="attribution")

    await repo.mark_failed(run.id, "yfinance timeout")
    refreshed = await repo.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error_summary == "yfinance timeout"
    assert refreshed.finished_at is not None


@pytest.mark.asyncio
async def test_pipeline_run_mark_partial(db_session, seeded_world):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="discovery")

    await repo.mark_partial(run.id, "2/3 strategies failed")
    refreshed = await repo.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "partial"
    assert refreshed.error_summary == "2/3 strategies failed"


@pytest.mark.asyncio
async def test_stock_score_upsert_insert_then_update(db_session, seeded_world):
    pr_repo = PipelineRunRepository(db_session)
    repo = StockScoreRepository(db_session)
    run = await pr_repo.create(kind="discovery")

    first = await repo.upsert(
        pipeline_run_id=run.id,
        listing_id=seeded_world["listing"].id,
        strategy_code="value_v1",
        tier=2,
        score=70,
        confidence=0.6,
    )
    assert first.id is not None
    assert first.score == 70

    second = await repo.upsert(
        pipeline_run_id=run.id,
        listing_id=seeded_world["listing"].id,
        strategy_code="value_v1",
        tier=3,
        score=85,
        confidence=0.9,
        deep_analysis="strong moat",
    )
    assert second.id == first.id
    assert second.score == 85
    assert second.tier == 3
    assert second.confidence == Decimal("0.9")
    assert second.deep_analysis == "strong moat"

    rows = await repo.by_run(run.id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_stock_score_by_run_filters(db_session, seeded_world):
    pr_repo = PipelineRunRepository(db_session)
    repo = StockScoreRepository(db_session)
    run_a = await pr_repo.create(kind="discovery")
    run_b = await pr_repo.create(kind="discovery")

    await repo.upsert(
        pipeline_run_id=run_a.id,
        listing_id=seeded_world["listing"].id,
        strategy_code="value_v1",
        tier=1, score=50, confidence=0.5,
    )
    await repo.upsert(
        pipeline_run_id=run_b.id,
        listing_id=seeded_world["listing"].id,
        strategy_code="value_v1",
        tier=2, score=75, confidence=0.7,
    )

    rows_a = await repo.by_run(run_a.id)
    rows_b = await repo.by_run(run_b.id)
    assert len(rows_a) == 1
    assert len(rows_b) == 1
    assert rows_a[0].score == 50
    assert rows_b[0].score == 75


@pytest.mark.asyncio
async def test_company_get_by_canonical_name(db_session, seeded_world):
    repo = CompanyRepository(db_session)
    found = await repo.get_by_canonical_name("Apple Inc.")
    assert found is not None
    assert found.id == seeded_world["company1"].id

    missing = await repo.get_by_canonical_name("Nonexistent Co.")
    assert missing is None


@pytest.mark.asyncio
async def test_company_list_sector_peers_excludes(db_session, seeded_world):
    repo = CompanyRepository(db_session)
    peers = await repo.list_sector_peers(
        "TECH", exclude_company_id=seeded_world["company1"].id
    )
    ids = [p.id for p in peers]
    assert seeded_world["company1"].id not in ids
    assert seeded_world["company2"].id in ids
    assert seeded_world["company3"].id not in ids  # sector mismatch


@pytest.mark.asyncio
async def test_company_list_by_ids(db_session, seeded_world):
    repo = CompanyRepository(db_session)
    empty = await repo.list_by_ids([])
    assert empty == []

    rows = await repo.list_by_ids(
        [seeded_world["company1"].id, seeded_world["company3"].id]
    )
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_fundamental_upsert_snapshot_and_latest(db_session, seeded_world):
    repo = FundamentalRepository(db_session)
    listing_id = seeded_world["listing"].id

    snap_old = await repo.upsert_snapshot(
        listing_id=listing_id, date=date(2026, 1, 1), pe=Decimal("20")
    )
    assert snap_old.pe == Decimal("20")

    # update same PK
    snap_old_updated = await repo.upsert_snapshot(
        listing_id=listing_id, date=date(2026, 1, 1), pe=Decimal("25")
    )
    assert snap_old_updated.pe == Decimal("25")

    # new date
    await repo.upsert_snapshot(
        listing_id=listing_id, date=date(2026, 6, 1), pe=Decimal("30")
    )

    latest = await repo.latest_snapshot(listing_id)
    assert latest is not None
    assert latest.date == date(2026, 6, 1)
    assert latest.pe == Decimal("30")


@pytest.mark.asyncio
async def test_fundamental_financial_report_and_latest_reports(db_session, seeded_world):
    repo = FundamentalRepository(db_session)
    cid = seeded_world["company1"].id

    await repo.upsert_financial_report(
        company_id=cid, fiscal_period="2025Q4", period_end=date(2025, 12, 31),
        revenue=Decimal("100"),
    )
    await repo.upsert_financial_report(
        company_id=cid, fiscal_period="2026Q1", period_end=date(2026, 3, 31),
        revenue=Decimal("110"),
    )
    await repo.upsert_financial_report(
        company_id=cid, fiscal_period="2026Q2", period_end=date(2026, 6, 30),
        revenue=Decimal("120"),
    )
    # update existing
    updated = await repo.upsert_financial_report(
        company_id=cid, fiscal_period="2025Q4", period_end=date(2025, 12, 31),
        revenue=Decimal("105"),
    )
    assert updated.revenue == Decimal("105")

    reports = await repo.latest_reports(cid, limit=2)
    assert len(reports) == 2
    assert reports[0].period_end == date(2026, 6, 30)
    assert reports[1].period_end == date(2026, 3, 31)


@pytest.mark.asyncio
async def test_price_upsert_and_latest_close(db_session, seeded_world):
    repo = PriceRepository(db_session)
    lid = seeded_world["listing"].id

    await repo.upsert_daily_price(
        listing_id=lid, date=date(2026, 6, 1),
        open=Decimal("100"), high=Decimal("105"), low=Decimal("99"),
        close=Decimal("104"), volume=1000, adjusted_close=Decimal("104"),
    )
    # update same key
    await repo.upsert_daily_price(
        listing_id=lid, date=date(2026, 6, 1),
        open=Decimal("100"), high=Decimal("106"), low=Decimal("99"),
        close=Decimal("106"), volume=1500, adjusted_close=Decimal("106"),
    )
    await repo.upsert_daily_price(
        listing_id=lid, date=date(2026, 6, 2),
        open=Decimal("106"), high=Decimal("110"), low=Decimal("105"),
        close=Decimal("109"), volume=2000, adjusted_close=Decimal("109"),
    )

    latest = await repo.latest_close(lid)
    assert latest == Decimal("109")

    missing = await repo.latest_close(99999)
    assert missing is None


@pytest.mark.asyncio
async def test_price_range_filters_and_orders(db_session, seeded_world):
    repo = PriceRepository(db_session)
    lid = seeded_world["listing"].id

    for d in [date(2026, 5, 30), date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 10)]:
        await repo.upsert_daily_price(
            listing_id=lid, date=d,
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), volume=1, adjusted_close=Decimal("100"),
        )

    rows = await repo.range(lid, date(2026, 6, 1), date(2026, 6, 5))
    assert [r.date for r in rows] == [date(2026, 6, 1), date(2026, 6, 5)]
