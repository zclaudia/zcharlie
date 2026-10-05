import asyncio
from datetime import date

from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


def test_research_requires_auth(api_client):
    response = api_client.post(
        "/research/single-stock",
        json={
            "ticker": "600406.SH",
            "industry": "电力设备",
            "report_year": 2025,
        },
    )

    assert response.status_code == 401


def test_research_requires_ifind_configuration(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "")

    response = api_client.post(
        "/research/single-stock",
        headers={"X-API-Key": "secret"},
        json={
            "ticker": "600406.SH",
            "industry": "电力设备",
            "report_year": 2025,
        },
    )

    assert response.status_code == 503


def test_get_unknown_research_snapshot_returns_404(api_client):
    response = api_client.get(
        "/research/runs/999",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 404


def test_get_research_snapshot_returns_versioned_result(api_client):
    async def seed() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(kind="ifind_research")
            await ResearchSnapshotRepository(session).create(
                pipeline_run_id=run.id,
                symbol="600406",
                market="SH",
                company_name="国电南瑞",
                industry="电力设备",
                as_of=date(2026, 10, 5),
                report_year=2025,
                canonical_schema_version="ifind-canonical.v1",
                scoring_version="v1.2",
                score_schema_version="v2.0",
                score_json={"business_score": 55.22},
                explanation_json={"summary": "fixture"},
                artifact_manifest={"scores": "/tmp/scores.json"},
            )
            return run.id

    run_id = asyncio.run(seed())
    response = api_client.get(
        f"/research/runs/{run_id}",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "600406.SH"
    assert body["score"]["business_score"] == 55.22
    assert body["scoring_version"] == "v1.2"
    assert body["artifacts"]["scores"] == "/tmp/scores.json"


def test_research_enqueues_auditable_run(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")

    response = api_client.post(
        "/research/single-stock",
        headers={"X-API-Key": "secret"},
        json={
            "ticker": "600406.sh",
            "industry": "电力设备",
            "report_year": 2025,
            "as_of": "2026-10-05",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["stage"] == "queued"
    assert body["created"] is True
    assert body["status_url"] == f"/pipelines/{body['run_id']}"
    assert body["result_url"] == f"/research/runs/{body['run_id']}"

    async def load_run():
        async with session_scope() as session:
            return await PipelineRunRepository(session).get(body["run_id"])

    run = asyncio.run(load_run())
    assert run is not None
    assert run.triggered_by == "api"
    assert run.request_json == {
        "ticker": "600406.SH",
        "industry": "电力设备",
        "report_year": 2025,
        "as_of": "2026-10-05",
    }
    assert run.available_at is not None


def test_research_idempotency_returns_existing_queued_run(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    headers = {
        "X-API-Key": "secret",
        "Idempotency-Key": "single-stock:600406:2026-10-05",
    }
    payload = {
        "ticker": "600406.SH",
        "industry": "电力设备",
        "report_year": 2025,
        "as_of": "2026-10-05",
    }

    first = api_client.post(
        "/research/single-stock",
        headers=headers,
        json=payload,
    )
    second = api_client.post(
        "/research/single-stock",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["status"] == "queued"


def test_research_idempotency_rejects_different_payload(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    headers = {
        "X-API-Key": "secret",
        "Idempotency-Key": "single-stock:payload-mismatch",
    }
    first = api_client.post(
        "/research/single-stock",
        headers=headers,
        json={
            "ticker": "600406.SH",
            "industry": "电力设备",
            "report_year": 2025,
        },
    )
    second = api_client.post(
        "/research/single-stock",
        headers=headers,
        json={
            "ticker": "600406.SH",
            "industry": "电力设备",
            "report_year": 2024,
        },
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert "different input" in second.json()["detail"]
