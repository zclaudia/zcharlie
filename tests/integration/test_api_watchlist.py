"""API tests for /watchlist."""

from datetime import date

import pytest

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


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
    assert body["market_code"] == "NASDAQ"
    assert body["name"] == "Apple"
    assert body["provider"] == "auto"
    assert body["last_refresh_status"] == "never"
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


def test_add_a_share_creates_listing_and_preserves_omitted_config(api_client):
    r = api_client.post(
        "/watchlist",
        json={
            "ticker": "600406.sh",
            "name": "国电南瑞",
            "industry": "电力设备",
            "provider": "ifind",
            "report_year": 2025,
            "note": "核心观察",
        },
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 201
    first = r.json()
    assert first["ticker"] == "600406.SH"
    assert first["market_code"] == "SSE"
    assert first["name"] == "国电南瑞"
    assert first["industry"] == "电力设备"
    assert first["provider"] == "ifind"
    assert first["report_year"] == 2025
    assert first["enabled"] is True
    assert first["last_refresh_status"] == "never"
    assert first["last_research_run_id"] is None

    r = api_client.post(
        "/watchlist",
        json={"ticker": "600406.SH", "note": "继续跟踪"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 201
    second = r.json()
    assert second["listing_id"] == first["listing_id"]
    assert second["added_at"] == first["added_at"]
    assert second["industry"] == "电力设备"
    assert second["provider"] == "ifind"
    assert second["report_year"] == 2025
    assert second["note"] == "继续跟踪"


@pytest.mark.asyncio
async def test_add_a_share_derives_metadata_from_latest_research(api_client):
    async with session_scope() as session:
        run = await PipelineRunRepository(session).create(kind="ifind_research")
        await ResearchSnapshotRepository(session).create(
            pipeline_run_id=run.id,
            symbol="920001",
            market="BJ",
            company_name="北交测试",
            industry="机械设备",
            as_of=date(2026, 10, 5),
            report_year=2025,
            canonical_schema_version="1.0.0",
            scoring_version="1.0.0",
            score_schema_version="1.0.0",
            score_json={},
            explanation_json=None,
            artifact_manifest={},
        )

    r = api_client.post(
        "/watchlist",
        json={"ticker": "920001.BJ"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ticker"] == "920001.BJ"
    assert body["market_code"] == "BSE"
    assert body["name"] == "北交测试"
    assert body["industry"] == "机械设备"
    assert body["provider"] == "ifind"
    assert body["report_year"] == 2025


def test_a_share_requires_metadata_and_rejects_market_conflict(api_client):
    missing = api_client.post(
        "/watchlist",
        json={"ticker": "000001.SZ"},
        headers={"X-API-Key": "secret"},
    )
    assert missing.status_code == 422
    assert "industry is required" in missing.json()["detail"]

    conflict = api_client.post(
        "/watchlist",
        json={
            "ticker": "600406.SH",
            "market_code": "SZSE",
            "name": "国电南瑞",
            "industry": "电力设备",
        },
        headers={"X-API-Key": "secret"},
    )
    assert conflict.status_code == 422
    assert "conflicts with ticker suffix SH" in conflict.json()["detail"]
