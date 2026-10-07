"""Read-only ``GET /verification/requests`` (P1-A13 status shown by the P1-E1 page)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.verification import VerificationRequest
from investment_agent.verification.queue import enqueue_verification
from investment_agent.web.session import SESSION_COOKIE_NAME
from tests.unit._candidate_support import seed_listing

KEY = {"X-API-Key": "secret"}


async def _seed() -> dict[str, int]:
    now = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    async with session_scope() as session:
        moutai = await seed_listing(session)
        other = await seed_listing(session, "000001.SZ", "平安银行")
        await enqueue_verification(
            listing_id=moutai.id, ticker="600519.SH", trigger="promote", session=session, now=now
        )
        await enqueue_verification(
            listing_id=moutai.id,
            ticker="600519.SH",
            trigger="conflict",
            fields=["roe"],
            report_period=date(2025, 12, 31),
            session=session,
            now=now + timedelta(minutes=1),
        )
        await enqueue_verification(
            listing_id=other.id, ticker="000001.SZ", trigger="calibration", session=session, now=now
        )
        await session.flush()
        rows = {
            (row.ticker, row.trigger): row
            for row in (await session.execute(select(VerificationRequest))).scalars()
        }
        done = rows[("600519.SH", "conflict")]
        done.status = "done"
        done.finished_at = now + timedelta(hours=1)
        done.run_id = 77
        done.result = {
            "run_id": 77,
            "report_period": "2025-12-31",
            "fields": {
                "roe": {"verdict": "conflict", "ifind": 0.3, "free": 0.2, "tolerance": 0.02},
                "gross_margin": {"verdict": "verified", "ifind": 0.91, "free": 0.91},
            },
            "verified": ["gross_margin"],
            "conflicts": ["roe"],
            "unavailable": [],
            "filled": [],
            "financial_snapshot_id": 5,
            "market_snapshot_id": None,
        }
        deferred = rows[("000001.SZ", "calibration")]
        deferred.status = "deferred"
        deferred.not_before = now + timedelta(hours=5)
        deferred.result = {"deferred": "budget", "not_before": "x", "detail": "window full"}
        return {"moutai": moutai.id, "other": other.id}


def test_requires_auth(api_client) -> None:
    assert api_client.get("/verification/requests").status_code == 401
    assert (
        api_client.get("/verification/requests", headers={"X-API-Key": "nope"}).status_code == 401
    )


async def test_lists_newest_first_with_result_summary(api_client) -> None:
    ids = await _seed()
    response = api_client.get(f"/verification/requests?listing_id={ids['moutai']}", headers=KEY)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 2 and page["limit"] == 50
    conflict, promote = page["items"]
    assert (conflict["trigger"], conflict["status"]) == ("conflict", "done")
    assert conflict["fields"] == ["roe"]
    assert conflict["report_period"] == "2025-12-31"
    summary = conflict["result_summary"]
    assert summary["verified"] == ["gross_margin"] and summary["conflicts"] == ["roe"]
    assert summary["conflict_fields"] == {
        "roe": {"verdict": "conflict", "ifind": 0.3, "free": 0.2, "tolerance": 0.02}
    }
    assert "financial_snapshot_id" not in summary
    assert (promote["trigger"], promote["status"]) == ("promote", "pending")
    assert promote["fields"] is None and promote["result_summary"] is None


async def test_filters_and_deferred_state(api_client) -> None:
    ids = await _seed()
    page = api_client.get("/verification/requests?status=deferred", headers=KEY).json()
    [item] = page["items"]
    assert item["listing_id"] == ids["other"] and item["ticker"] == "000001.SZ"
    assert item["not_before"].startswith("2026-10-06T14:00:00")
    assert item["result_summary"]["deferred"] == "budget"
    by_ticker = api_client.get("/verification/requests?ticker=600519.SH", headers=KEY).json()
    assert by_ticker["total"] == 2
    assert api_client.get("/verification/requests?limit=1", headers=KEY).json()["limit"] == 1
    for bad in ("status=bogus", "ticker=ABC", "limit=0", "limit=500", "listing_id=0"):
        assert api_client.get(f"/verification/requests?{bad}", headers=KEY).status_code == 422, bad


async def test_web_session_can_read(api_client) -> None:
    await _seed()
    api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert api_client.cookies.get(SESSION_COOKIE_NAME)
    response = api_client.get("/verification/requests")
    assert response.status_code == 200
    assert response.json()["total"] == 3
    # Read-only: no write verbs on the collection.
    assert (
        api_client.post(
            "/verification/requests", json={}, headers={"X-Requested-With": "fetch"}
        ).status_code
        == 405
    )
