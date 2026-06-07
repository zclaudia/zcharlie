"""API tests for /attribute."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from investment_agent.api.app import create_app
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import AttributionReport, PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository


@pytest_asyncio.fixture
async def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TestClient(create_app())
    await engine.dispose()


def test_attribute_requires_auth(api_client):
    r = api_client.post("/attribute/AAPL", json={})
    assert r.status_code == 401


def test_post_unknown_ticker_404(api_client):
    r = api_client.post(
        "/attribute/UNKNOWN",
        json={},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


def test_get_latest_no_report_404(api_client):
    import asyncio

    async def _seed():
        async with session_scope() as session:
            session.add(
                Market(
                    code="NASDAQ",
                    name="NASDAQ",
                    timezone="America/New_York",
                    currency="USD",
                ),
            )
            await ListingRepository(session).upsert_listing(
                market_code="NASDAQ",
                ticker="AAPL",
                company_name="Apple",
            )

    asyncio.run(_seed())
    r = api_client.get(
        "/attribute/AAPL/latest",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


def test_get_unknown_run_404(api_client):
    r = api_client.get(
        "/attribute/runs/999",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_run_returns_seeded(api_client):
    async with session_scope() as session:
        session.add(
            Market(
                code="NASDAQ",
                name="NASDAQ",
                timezone="America/New_York",
                currency="USD",
            ),
        )
        listing = await ListingRepository(session).upsert_listing(
            market_code="NASDAQ",
            ticker="AAPL",
            company_name="Apple",
        )
        run = PipelineRun(
            kind="attribution",
            listing_id=listing.id,
            triggered_by="test",
            started_at=datetime.now(UTC),
            status="success",
            metadata_json={},
        )
        session.add(run)
        await session.flush()
        session.add(
            AttributionReport(
                pipeline_run_id=run.id,
                listing_id=listing.id,
                analysis_date=date(2025, 6, 3),
                price_change_pct=Decimal("0.025"),
                beta_attribution={"beta_market": 1.1},
                triggering_events=[{"event_type": "news", "headline": "x"}],
                narrative="moved on news",
            ),
        )
        await session.flush()
        run_id = run.id
    r = api_client.get(
        f"/attribute/runs/{run_id}",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["narrative"] == "moved on news"
    assert body["ticker"] == "AAPL"
    assert body["price_change_pct"] == 0.025


@pytest.mark.asyncio
async def test_get_latest_returns_newest(api_client):
    async with session_scope() as session:
        session.add(
            Market(
                code="NASDAQ",
                name="NASDAQ",
                timezone="America/New_York",
                currency="USD",
            ),
        )
        listing = await ListingRepository(session).upsert_listing(
            market_code="NASDAQ",
            ticker="AAPL",
            company_name="Apple",
        )
        for d, narr in [
            (date(2025, 6, 1), "old"),
            (date(2025, 6, 3), "new"),
        ]:
            run = PipelineRun(
                kind="attribution",
                listing_id=listing.id,
                triggered_by="test",
                started_at=datetime.now(UTC),
                status="success",
                metadata_json={},
            )
            session.add(run)
            await session.flush()
            session.add(
                AttributionReport(
                    pipeline_run_id=run.id,
                    listing_id=listing.id,
                    analysis_date=d,
                    price_change_pct=Decimal("0.01"),
                    beta_attribution={},
                    triggering_events=[],
                    narrative=narr,
                ),
            )
            await session.flush()
    r = api_client.get(
        "/attribute/AAPL/latest",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    assert r.json()["narrative"] == "new"
