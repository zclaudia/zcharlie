import asyncio
from datetime import date

from investment_agent.config import Settings
from investment_agent.research.worker import _notify_meaningful_change
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


def test_get_unknown_research_run_returns_404(api_client):
    response = api_client.get(
        "/research/runs/999",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 404


def test_get_research_run_returns_result_and_safe_artifacts(api_client, tmp_path):
    async def seed_run() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(kind="ifind_research")
            return run.id

    run_id = asyncio.run(seed_run())
    artifact_dir = tmp_path / "research-runs" / str(run_id)
    artifact_dir.mkdir(parents=True)
    scores_path = artifact_dir / "scores.json"
    scores_path.write_text('{"business_score":55.22}', encoding="utf-8")

    async def seed_snapshot() -> None:
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
                    "business_score": 55.22,
                    "suggestion": "needs_review",
                    "needs_agent_review": True,
                },
                explanation_json={"summary": "fixture"},
                artifact_manifest={
                    "scores": str(scores_path),
                    "escape": "/etc/hosts",
                },
            )
            await PipelineRunRepository(session).mark_success(run_id)

    asyncio.run(seed_snapshot())
    response = api_client.get(
        f"/research/runs/{run_id}",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["ticker"] == "600406.SH"
    assert body["business_score"] == 55.22
    assert body["workflow_label"] == "needs_review"
    assert body["has_meaningful_changes"] is False
    assert body["meaningful_change_count"] == 0
    assert body["result"]["score"]["business_score"] == 55.22
    assert body["result"]["scoring_version"] == "v1.2"
    assert body["result"]["artifacts"] == [
        {
            "name": "escape",
            "download_url": f"/research/runs/{run_id}/artifacts/escape",
        },
        {
            "name": "scores",
            "download_url": f"/research/runs/{run_id}/artifacts/scores",
        },
    ]
    assert body["change"]["baseline"] is True
    assert body["change"]["current_run_id"] == run_id
    assert body["change"]["changes"] == []

    changes = api_client.get(
        f"/research/runs/{run_id}/changes",
        headers={"X-API-Key": "secret"},
    )
    assert changes.status_code == 200
    assert changes.json() == body["change"]

    artifacts = api_client.get(
        f"/research/runs/{run_id}/artifacts",
        headers={"X-API-Key": "secret"},
    )
    assert artifacts.status_code == 200
    assert len(artifacts.json()) == 2
    download = api_client.get(
        f"/research/runs/{run_id}/artifacts/scores",
        headers={"X-API-Key": "secret"},
    )
    assert download.status_code == 200
    assert download.json()["business_score"] == 55.22
    blocked = api_client.get(
        f"/research/runs/{run_id}/artifacts/escape",
        headers={"X-API-Key": "secret"},
    )
    assert blocked.status_code == 404


def test_get_partial_research_run_returns_deterministic_result(api_client):
    async def seed() -> int:
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            run = await repository.create(kind="ifind_research")
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
                score_json={
                    "business_score": 55.22,
                    "suggestion": "needs_review",
                    "needs_agent_review": True,
                },
                explanation_json=None,
                artifact_manifest={},
            )
            await repository.mark_partial(
                run.id,
                "explanation unavailable",
                {"partial_reason": "explanation_failed"},
            )
            return run.id

    run_id = asyncio.run(seed())
    response = api_client.get(
        f"/research/runs/{run_id}",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["business_score"] == 55.22
    assert body["workflow_label"] == "needs_review"
    assert body["needs_agent_review"] is True
    assert body["result"]["explanation"] is None
    assert body["result"]["artifacts"] == []


def test_research_run_exposes_diff_from_previous_snapshot(api_client, tmp_path):
    async def seed() -> tuple[int, int]:
        async with session_scope() as session:
            runs = PipelineRunRepository(session)
            snapshots = ResearchSnapshotRepository(session)
            first = await runs.create(kind="ifind_research")
            await snapshots.create(
                pipeline_run_id=first.id,
                symbol="600406",
                market="SH",
                company_name="国电南瑞",
                industry="电力设备",
                as_of=date(2026, 10, 4),
                report_year=2025,
                canonical_schema_version="ifind-canonical.v1",
                scoring_version="v1.2",
                score_schema_version="v2.0",
                score_json={
                    "business_score": 50.0,
                    "suggestion": "needs_review",
                    "primary_strategy": "quality_growth",
                    "key_metrics": {"roe": 0.10},
                    "data_quality_score": 0.90,
                    "missing_fields": ["dividend_yield"],
                },
                explanation_json=None,
                artifact_manifest={},
            )
            await runs.mark_success(first.id)
            second = await runs.create(kind="ifind_research")
            await snapshots.create(
                pipeline_run_id=second.id,
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
                    "business_score": 54.0,
                    "suggestion": "watch",
                    "primary_strategy": "steady_quality_growth",
                    "key_metrics": {"roe": 0.11},
                    "data_quality_score": 0.95,
                    "missing_fields": [],
                },
                explanation_json=None,
                artifact_manifest={},
            )
            await runs.mark_success(second.id)
            return first.id, second.id

    first_run_id, second_run_id = asyncio.run(seed())
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="secret",
        llm_tier3_api_key="x",
        data_dir=tmp_path,
    )
    assert (
        asyncio.run(
            _notify_meaningful_change(settings=settings, run_id=first_run_id)
        )
        is None
    )
    notification_path = asyncio.run(
        _notify_meaningful_change(settings=settings, run_id=second_run_id)
    )
    assert notification_path is not None
    assert asyncio.run(
        _notify_meaningful_change(settings=settings, run_id=second_run_id)
    ) == notification_path
    response = api_client.get(
        f"/research/runs/{second_run_id}",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 200
    change = response.json()["change"]
    assert change["baseline"] is False
    assert change["previous_run_id"] == first_run_id
    assert change["current_run_id"] == second_run_id
    assert change["has_meaningful_changes"] is True
    assert change["notification_url"] == (
        f"/research/runs/{second_run_id}/change-notification"
    )
    assert change["category_counts"] == {
        "score": 1,
        "strategy": 2,
        "financial_metric": 1,
        "quality": 2,
    }
    fields = {item["field"] for item in change["changes"]}
    assert fields == {
        "business_score",
        "primary_strategy",
        "suggestion",
        "key_metrics.roe",
        "data_quality_score",
        "missing_fields",
    }

    listed = api_client.get(
        "/research/runs",
        headers={"X-API-Key": "secret"},
    )
    assert listed.status_code == 200
    summaries = {item["run_id"]: item for item in listed.json()}
    assert summaries[first_run_id]["has_meaningful_changes"] is False
    assert summaries[first_run_id]["meaningful_change_count"] == 0
    assert summaries[second_run_id]["has_meaningful_changes"] is True
    assert summaries[second_run_id]["meaningful_change_count"] == 6

    notification = api_client.get(
        change["notification_url"],
        headers={"X-API-Key": "secret"},
    )
    assert notification.status_code == 200
    assert "研究变化提醒：600406.SH 国电南瑞" in notification.text
    assert "重要变化：6 项" in notification.text
    assert "不构成投资建议或交易信号" in notification.text


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
    assert body["status_url"] == f"/research/runs/{body['run_id']}"
    assert body["result_url"] == f"/research/runs/{body['run_id']}"

    detail = api_client.get(
        body["result_url"],
        headers={"X-API-Key": "secret"},
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "queued"
    assert detail.json()["result"] is None
    assert detail.json()["request"]["ticker"] == "600406.SH"

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


def test_list_research_runs_supports_status_filter(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    first = api_client.post(
        "/research/single-stock",
        headers={"X-API-Key": "secret"},
        json={
            "ticker": "600406.SH",
            "industry": "电力设备",
            "report_year": 2025,
            "as_of": "2026-10-05",
        },
    )
    second = api_client.post(
        "/research/single-stock",
        headers={"X-API-Key": "secret"},
        json={
            "ticker": "000001.SZ",
            "industry": "银行",
            "report_year": 2025,
            "as_of": "2026-10-05",
        },
    )

    response = api_client.get(
        "/research/runs?status=queued&limit=1",
        headers={"X-API-Key": "secret"},
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["run_id"] == second.json()["run_id"]
    assert rows[0]["ticker"] == "000001.SZ"
    assert rows[0]["status"] == "queued"
    assert rows[0]["business_score"] is None
    assert rows[0]["run_id"] > first.json()["run_id"]


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
