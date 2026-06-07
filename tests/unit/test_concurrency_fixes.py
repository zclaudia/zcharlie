"""Regression tests for Plan B concurrency bugs (fixed in Plan C Tasks 1-2)."""

import asyncio
from datetime import date

import pytest
import pytest_asyncio

from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base


@pytest_asyncio.fixture
async def disk_engine(tmp_path):
    db_path = tmp_path / "concurrency.db"
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_check_or_run_deep_analysis_handles_concurrent_inserts(disk_engine):
    """Concurrent cache misses across distinct strategies must each insert a row.

    After Plan D Task 1, uq_deep_cache_key includes strategy_code, so concurrent
    inserts with distinct strategy_code values do not collide on the unique index.
    """
    from investment_agent.domain.strategy_types import DeepAnalysis, ListingSnapshot
    from investment_agent.pipelines.steps import _check_or_run_deep_analysis
    from investment_agent.storage.models.market_data import FinancialReport
    from investment_agent.storage.models.reference import Market
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        session.add(
            Market(
                code="NASDAQ",
                name="NASDAQ",
                timezone="America/New_York",
                currency="USD",
            )
        )
        repo = ListingRepository(session)
        listing = await repo.upsert_listing(
            market_code="NASDAQ", ticker="AAPL", company_name="Apple",
        )
        session.add(
            FinancialReport(
                company_id=listing.company_id,
                fiscal_period="2025-Q1",
                period_end=date(2025, 3, 31),
            )
        )
        listing_id = listing.id
        company_id = listing.company_id

    snap = ListingSnapshot(
        listing_id=listing_id,
        company_id=company_id,
        ticker="AAPL",
        market_code="NASDAQ",
        company_name="Apple",
        as_of=date(2025, 6, 3),
    )

    class _StubAgent:
        def __init__(self, label: str) -> None:
            self._label = label

        async def deep_analyze(self, listing, ctx):
            return DeepAnalysis(
                business_model=self._label,
                moat_assessment="x",
                risk_factors=[],
                upstream_dependencies=[],
                downstream_customers=[],
                competitor_overview=[],
                valuation_summary="x",
            )

    async def _run(strategy_code: str, label: str):
        async with session_scope() as session:
            return await _check_or_run_deep_analysis(
                session,
                listing_id=listing_id,
                strategy_code=strategy_code,
                agent=_StubAgent(label),  # type: ignore[arg-type]
                snapshot=snap,
                cache_days=90,
            )

    results = await asyncio.gather(
        _run("value", "value-bm"),
        _run("growth", "growth-bm"),
        _run("risk_aggressive", "risk-bm"),
        _run("momentum", "momentum-bm"),
    )
    assert all(isinstance(r, dict) for r in results)
    assert all("business_model" in r for r in results)

    # Each distinct strategy_code should have its own cache row.
    from sqlalchemy import select

    from investment_agent.storage.models.ops import DeepAnalysisCache

    async with session_scope() as session:
        rows = (await session.execute(select(DeepAnalysisCache))).scalars().all()
    assert len(rows) == 4
    assert {r.strategy_code for r in rows} == {
        "value",
        "growth",
        "risk_aggressive",
        "momentum",
    }
