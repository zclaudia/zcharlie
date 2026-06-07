"""API tests for /admin."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.ops import LLMCall


def test_admin_requires_auth(api_client):
    r = api_client.get("/admin/llm-calls")
    assert r.status_code == 401


def test_list_empty(api_client):
    r = api_client.get(
        "/admin/llm-calls",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    assert r.json() == []


def test_cost_summary_empty(api_client):
    r = api_client.get(
        "/admin/llm-calls/cost",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["by_purpose"] == []


def test_cost_summary_bad_month_400(api_client):
    r = api_client.get(
        "/admin/llm-calls/cost",
        params={"month": "not-a-month"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_and_cost_aggregates(api_client):
    now = datetime.now(UTC)
    async with session_scope() as session:
        session.add(
            LLMCall(
                purpose="score",
                model="m",
                provider="p",
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=Decimal("0.10"),
                latency_ms=100,
                started_at=now - timedelta(seconds=20),
                success=True,
            ),
        )
        session.add(
            LLMCall(
                purpose="deep",
                model="m",
                provider="p",
                prompt_tokens=500,
                completion_tokens=250,
                cost_usd=Decimal("0.50"),
                latency_ms=200,
                started_at=now,
                success=True,
            ),
        )
        await session.flush()

    r = api_client.get(
        "/admin/llm-calls?limit=10",
        headers={"X-API-Key": "secret"},
    )
    body = r.json()
    assert len(body) == 2
    assert body[0]["purpose"] == "deep"  # newest first

    r = api_client.get(
        "/admin/llm-calls/cost",
        headers={"X-API-Key": "secret"},
    )
    body = r.json()
    assert body["total_calls"] == 2
    assert body["total_cost_usd"] == 0.60
    by_p = body["by_purpose"]
    assert by_p[0]["purpose"] == "deep"
    assert by_p[0]["cost_usd"] == 0.50
