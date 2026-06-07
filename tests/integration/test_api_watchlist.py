"""API tests for /watchlist."""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from investment_agent.api.app import create_app
from investment_agent.storage.database import init_engine, session_scope
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


def test_watchlist_requires_auth(api_client):
    r = api_client.get("/watchlist")
    assert r.status_code == 401


def test_list_empty(api_client):
    r = api_client.get("/watchlist", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert r.json() == []


def test_add_unknown_ticker_404(api_client):
    r = api_client.post(
        "/watchlist",
        json={"ticker": "UNKNOWN"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_then_list_then_delete(api_client):
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
        listing_id = listing.id

    # Add
    r = api_client.post(
        "/watchlist",
        json={"ticker": "AAPL", "note": "favorite"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["note"] == "favorite"

    # Same ticker again -> still 201, note updated
    r = api_client.post(
        "/watchlist",
        json={"ticker": "AAPL", "note": "v2"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 201

    # List -> 1 item
    r = api_client.get("/watchlist", headers={"X-API-Key": "secret"})
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["note"] == "v2"

    # Delete
    r = api_client.delete(
        f"/watchlist/{listing_id}",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 204

    # Delete again -> 404
    r = api_client.delete(
        f"/watchlist/{listing_id}",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404
