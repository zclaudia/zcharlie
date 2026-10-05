"""Aggregated read models for the browser UI (work package B4)."""

import asyncio
from datetime import date

from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


def _headers():
    return {"X-API-Key": "secret"}


def _seed_run(status: str = "success", *, error_summary: str | None = None) -> int:
    async def seed() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(
                kind="ifind_research",
                request_json={"ticker": "600406.SH", "industry": "电力设备"},
            )
            if status == "failed":
                await PipelineRunRepository(session).mark_failed(
                    run.id, error_summary=error_summary or "provider unavailable"
                )
            elif status == "partial":
                await PipelineRunRepository(session).mark_partial(
                    run.id, error_summary="explanation failed"
                )
            elif status == "success":
                await PipelineRunRepository(session).mark_success(run.id)
            return run.id

    return asyncio.run(seed())


def _seed_snapshot(run_id: int, *, business_score: float) -> None:
    async def seed() -> None:
        async with session_scope() as session:
            await ResearchSnapshotRepository(session).create(
                pipeline_run_id=run_id,
                symbol="600406",
                market="SH",
                company_name="国电南瑞",
                industry="电力设备",
                as_of=date(2026, 10, 5),
                report_year=2025,
                canonical_schema_version="ifind-canonical.v1",
                scoring_version="v1.2",
                score_schema_version="v2.0",
                score_json={
                    "items": [
                        {
                            "strategy_label": "稳健质量成长",
                            "investment_score": business_score + 5,
                            "business_score": business_score,
                            "research_confidence_score": 92.0,
                            "needs_agent_review": True,
                            "freshness": {
                                "market_data": {"is_stale": False},
                                "financial_data": {"is_stale": False},
                            },
                        }
                    ],
                    "business_score": business_score,
                    "needs_agent_review": True,
                },
                explanation_json=None,
                artifact_manifest={},
            )

    asyncio.run(seed())


def _add_watchlist_item(api_client) -> None:
    response = api_client.post(
        "/watchlist",
        headers=_headers(),
        json={"ticker": "600406.SH", "industry": "电力设备", "name": "国电南瑞"},
    )
    assert response.status_code == 201, response.text


def test_dashboard_requires_auth(api_client):
    assert api_client.get("/dashboard/today").status_code == 401


def test_dashboard_accepts_api_key(api_client):
    response = api_client.get("/dashboard/today", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    for bucket in ("running", "failed", "partial", "needs_review", "meaningful_changes"):
        assert isinstance(body[bucket]["count"], int)
        assert isinstance(body[bucket]["entries"], list)


def test_dashboard_accepts_web_session_cookie(api_client):
    login = api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert login.status_code == 200
    response = api_client.get("/dashboard/today")
    assert response.status_code == 200


def test_dashboard_buckets_recent_runs_and_changes(api_client):
    running_id = _seed_run("running")
    failed_id = _seed_run("failed", error_summary="gateway 502")
    partial_id = _seed_run("partial")
    baseline_run = _seed_run("success")
    _seed_snapshot(baseline_run, business_score=40.0)
    changed_run = _seed_run("success")
    _seed_snapshot(changed_run, business_score=70.0)

    body = api_client.get("/dashboard/today", headers=_headers()).json()

    assert any(entry["run_id"] == running_id for entry in body["running"]["entries"])
    assert body["running"]["count"] >= 1

    failed_entry = next(
        (entry for entry in body["failed"]["entries"] if entry["run_id"] == failed_id),
        None,
    )
    assert failed_entry is not None
    assert failed_entry["summary"] == "gateway 502"
    assert failed_entry["ticker"] == "600406.SH"

    assert any(entry["run_id"] == partial_id for entry in body["partial"]["entries"])

    review_tickers = {entry["ticker"] for entry in body["needs_review"]["entries"]}
    assert "600406.SH" in review_tickers

    changed_entries = [
        entry for entry in body["meaningful_changes"]["entries"] if entry["run_id"] == changed_run
    ]
    assert changed_entries, "meaningful score change must appear in the changes bucket"
    assert changed_entries[0]["summary"] and "重要变化" in changed_entries[0]["summary"]


def test_watchlist_list_enriched_with_latest_research(api_client):
    _add_watchlist_item(api_client)

    empty = api_client.get("/watchlist", headers=_headers()).json()
    assert empty[0]["latest_research"] is None

    run_id = _seed_run("success")
    _seed_snapshot(run_id, business_score=55.0)

    body = api_client.get("/watchlist", headers=_headers()).json()
    latest = body[0]["latest_research"]
    assert latest is not None
    assert latest["run_id"] == run_id
    assert latest["investment_score"] == 60.0
    assert latest["research_confidence_score"] == 92.0
    assert latest["strategy_label"] == "稳健质量成长"
    assert latest["needs_agent_review"] is True
    assert latest["freshness_ok"] is True
    assert latest["scoring_version"] == "v1.2"


def test_stock_snapshot_history_paginates(api_client):
    first_run = _seed_run("success")
    _seed_snapshot(first_run, business_score=40.0)
    second_run = _seed_run("success")
    _seed_snapshot(second_run, business_score=70.0)

    full = api_client.get(
        "/research/stocks/600406.SH/history",
        headers=_headers(),
    ).json()
    assert [item["run_id"] for item in full] == [second_run, first_run]
    assert full[0]["investment_score"] == 75.0
    assert full[0]["has_meaningful_changes"] is True
    assert full[1]["has_meaningful_changes"] is False  # baseline

    page = api_client.get(
        "/research/stocks/600406.SH/history",
        params={"limit": 1, "offset": 1},
        headers=_headers(),
    ).json()
    assert [item["run_id"] for item in page] == [first_run]


def test_stock_snapshot_history_rejects_invalid_ticker(api_client):
    response = api_client.get(
        "/research/stocks/NOPE/history",
        headers=_headers(),
    )
    assert response.status_code == 400


def test_cookie_authenticated_state_change_requires_same_origin(api_client):
    api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    cross_origin = api_client.post(
        "/watchlist",
        json={"ticker": "600519.SH", "industry": "白酒", "name": "贵州茅台"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403

    same_origin = api_client.post(
        "/watchlist",
        json={"ticker": "600519.SH", "industry": "白酒", "name": "贵州茅台"},
        headers={"Origin": "http://testserver"},
    )
    assert same_origin.status_code == 201
