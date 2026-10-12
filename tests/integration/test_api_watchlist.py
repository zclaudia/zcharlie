"""API tests for /watchlist."""

from datetime import date

import pytest
from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository


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
    # Non-A-share entries never draft rules (plan 1.5 scope).
    assert body["rule"]["state"] == "none"

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

    # Delete without a reason -> 422 (the journal needs one)
    r = api_client.request(
        "DELETE",
        f"/watchlist/{listing_id}",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 422

    # Delete
    r = api_client.request(
        "DELETE",
        f"/watchlist/{listing_id}",
        json={"reason": "不再跟踪"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 204, r.text

    # Delete again -> 404
    r = api_client.request(
        "DELETE",
        f"/watchlist/{listing_id}",
        json={"reason": "不再跟踪"},
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
    # New A-share entries refresh from the free-source snapshot store (P1-B5).
    assert body["provider"] == "auto"
    assert body["report_year"] == 2025


def test_a_share_requires_metadata_and_rejects_market_conflict(api_client):
    missing = api_client.post(
        "/watchlist",
        json={"ticker": "000001.SZ", "provider": "ifind"},
        headers={"X-API-Key": "secret"},
    )
    assert missing.status_code == 422
    assert "industry is required" in missing.json()["detail"]

    # Snapshot research (the default) falls back to the data industry group,
    # so only the company name is still needed for an unknown listing.
    unnamed = api_client.post(
        "/watchlist",
        json={"ticker": "000001.SZ"},
        headers={"X-API-Key": "secret"},
    )
    assert unnamed.status_code == 422
    assert "name is required" in unnamed.json()["detail"]

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


@pytest.mark.asyncio
async def test_batch_refresh_queues_valid_items_and_isolates_invalid_ones(
    api_client,
    monkeypatch,
):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    added = api_client.post(
        "/watchlist",
        json={
            "ticker": "600406.SH",
            "name": "国电南瑞",
            "industry": "电力设备",
            "report_year": 2025,
        },
        headers={"X-API-Key": "secret"},
    )
    assert added.status_code == 201
    a_share_listing_id = added.json()["listing_id"]

    async with session_scope() as session:
        session.add(
            Market(
                code="NASDAQ",
                name="NASDAQ",
                timezone="America/New_York",
                currency="USD",
            ),
        )
        legacy_listing = await ListingRepository(session).upsert_listing(
            market_code="NASDAQ",
            ticker="AAPL",
            company_name="Apple",
        )
        await WatchlistRepository(session).add(legacy_listing.id)

    response = api_client.post(
        "/watchlist/refresh",
        json={"as_of": "2026-10-05"},
        headers={
            "X-API-Key": "secret",
            "Idempotency-Key": "daily-refresh-2026-10-05",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["as_of"] == "2026-10-05"
    assert body["total"] == 2
    assert body["created_runs"] == 1
    assert body["reused_runs"] == 0
    assert body["failed"] == 1
    outcomes = {item["ticker"]: item for item in body["items"]}
    assert outcomes["600406.SH"]["status"] == "queued"
    assert outcomes["600406.SH"]["created"] is True
    assert outcomes["600406.SH"]["run_id"] is not None
    assert outcomes["AAPL"]["status"] == "failed"
    assert outcomes["AAPL"]["run_id"] is None
    assert "only canonical A-share" in outcomes["AAPL"]["error"]

    repeated = api_client.post(
        "/watchlist/refresh",
        json={"as_of": "2026-10-05"},
        headers={
            "X-API-Key": "secret",
            "Idempotency-Key": "daily-refresh-2026-10-05",
        },
    )
    assert repeated.status_code == 202
    repeated_body = repeated.json()
    assert repeated_body["created_runs"] == 0
    assert repeated_body["reused_runs"] == 1

    async with session_scope() as session:
        runs = list(
            (
                await session.execute(
                    select(PipelineRun).where(PipelineRun.kind == "ifind_research")
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 1
        assert runs[0].listing_id == a_share_listing_id
        assert runs[0].request_json["ticker"] == "600406.SH"


def test_batch_refresh_skips_disabled_items(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    added = api_client.post(
        "/watchlist",
        json={
            "ticker": "600406.SH",
            "name": "国电南瑞",
            "industry": "电力设备",
            "report_year": 2025,
            "enabled": False,
        },
        headers={"X-API-Key": "secret"},
    )
    assert added.status_code == 201
    assert added.json()["enabled"] is False

    response = api_client.post(
        "/watchlist/refresh",
        json={"as_of": "2026-10-05"},
        headers={"X-API-Key": "secret"},
    )
    assert response.status_code == 202
    assert response.json() == {
        "as_of": "2026-10-05",
        "total": 0,
        "created_runs": 0,
        "reused_runs": 0,
        "failed": 0,
        "items": [],
    }


async def test_remove_has_a_proxy_safe_post_form_equivalent_to_delete(api_client):
    """P3e: POST /watchlist/{id}/remove is the same journaled removal as the
    legacy DELETE-with-body (kept until the web client migrates)."""

    key = {"X-API-Key": "secret"}
    async with session_scope() as session:
        session.add(Market(code="NASDAQ", name="NASDAQ", timezone="America/New_York", currency="USD"))
        listing_id = (
            await ListingRepository(session).upsert_listing(
                market_code="NASDAQ", ticker="AAPL", company_name="Apple"
            )
        ).id
    assert api_client.post("/watchlist", json={"ticker": "AAPL"}, headers=key).status_code == 201

    # Identical validation: the journal needs a reason on both forms.
    assert api_client.post(f"/watchlist/{listing_id}/remove", headers=key).status_code == 422
    assert (
        api_client.request("DELETE", f"/watchlist/{listing_id}", headers=key).status_code == 422
    )
    # POST removes and journals; the legacy DELETE then sees 404 (already gone).
    assert api_client.post(
        f"/watchlist/{listing_id}/remove", json={"reason": "不再跟踪"}, headers=key
    ).status_code == 204
    assert (
        api_client.request(
            "DELETE", f"/watchlist/{listing_id}", json={"reason": "不再跟踪"}, headers=key
        ).status_code
        == 404
    )
    # And vice versa: the legacy DELETE removes, the POST form then sees 404.
    assert api_client.post("/watchlist", json={"ticker": "AAPL"}, headers=key).status_code == 201
    assert (
        api_client.request(
            "DELETE", f"/watchlist/{listing_id}", json={"reason": "不再跟踪"}, headers=key
        ).status_code
        == 204
    )
    assert (
        api_client.post(
            f"/watchlist/{listing_id}/remove", json={"reason": "不再跟踪"}, headers=key
        ).status_code
        == 404
    )
