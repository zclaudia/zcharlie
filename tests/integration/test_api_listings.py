"""API tests for /listings."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import (
    PipelineRun,
    StockScore,
    Strategy,
)
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository


async def _seed_aapl():
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
        return listing.id, listing.company_id


def test_listings_requires_auth(api_client):
    r = api_client.get("/listings/AAPL")
    assert r.status_code == 401


def test_get_listing_unknown_404(api_client):
    r = api_client.get(
        "/listings/UNKNOWN",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_listing_returns_seeded(api_client):
    await _seed_aapl()
    r = api_client.get(
        "/listings/AAPL",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["market_code"] == "NASDAQ"
    assert body["company_name"] == "Apple"


@pytest.mark.asyncio
async def test_get_listing_scores_returns_seeded(api_client):
    listing_id, _ = await _seed_aapl()
    async with session_scope() as session:
        session.add(
            Strategy(
                code="value",
                name="Value",
                version="v1.0",
                description="d",
                config_json={},
                enabled=True,
            ),
        )
        run = PipelineRun(
            kind="discovery",
            listing_id=listing_id,
            triggered_by="test",
            started_at=datetime.now(UTC),
            status="success",
            metadata_json={},
        )
        session.add(run)
        await session.flush()
        session.add(
            StockScore(
                pipeline_run_id=run.id,
                listing_id=listing_id,
                strategy_code="value",
                tier=1,
                score=85,
                confidence=Decimal("0.8"),
                bull_thesis="bull",
                bear_thesis="bear",
            ),
        )
        await session.flush()
    r = api_client.get(
        "/listings/AAPL/scores",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["strategy_code"] == "value"
    assert body[0]["score"] == 85
    assert body[0]["bull_thesis"] == "bull"


@pytest.mark.asyncio
async def test_list_relationships_initially_empty(api_client):
    await _seed_aapl()
    r = api_client.get(
        "/listings/AAPL/relationships",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_and_list_relationship(api_client):
    await _seed_aapl()
    r = api_client.post(
        "/listings/AAPL/relationships",
        json={
            "counterparty_name": "TSMC",
            "relationship_type": "supplier_of",
            "strength": 0.9,
        },
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["counterparty_name"] == "TSMC"
    assert body["relationship_type"] == "supplier_of"
    assert body["strength"] == 0.9
    assert body["source"] == "manual"

    r2 = api_client.get(
        "/listings/AAPL/relationships",
        headers={"X-API-Key": "secret"},
    )
    assert r2.status_code == 200
    rows = r2.json()
    assert len(rows) == 1
    assert rows[0]["counterparty_name"] == "TSMC"


@pytest.mark.asyncio
async def test_create_self_relationship_400(api_client):
    await _seed_aapl()
    r = api_client.post(
        "/listings/AAPL/relationships",
        json={
            "counterparty_name": "Apple",
            "relationship_type": "competes_with",
            "strength": 0.5,
        },
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 400
