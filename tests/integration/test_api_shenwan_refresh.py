"""``/health/shenwan-directory`` - status card and the refresh enqueue (WP5)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import desc, select

from investment_agent.clock import shanghai_today
from investment_agent.providers.shenwan_directory import (
    DEFAULT_SHENWAN_DIRECTORY_PATH,
    dump_shenwan_directory,
    load_shenwan_directory,
    runtime_shenwan_directory_path,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun

KEY = {"X-API-Key": "secret"}


def test_shenwan_directory_routes_require_auth(api_client) -> None:
    assert api_client.get("/health/shenwan-directory").status_code == 401
    assert api_client.post("/health/shenwan-directory/refresh").status_code == 401


def test_status_reports_the_baseline_and_counts(api_client, tmp_path: Path) -> None:
    body = api_client.get("/health/shenwan-directory", headers=KEY).json()

    baseline = load_shenwan_directory(DEFAULT_SHENWAN_DIRECTORY_PATH)
    assert body["source"] == "baseline"
    assert body["path"] == str(DEFAULT_SHENWAN_DIRECTORY_PATH)
    assert body["baseline"] == str(DEFAULT_SHENWAN_DIRECTORY_PATH)
    assert body["target"] == str(tmp_path / "shenwan" / "shenwan_2021.yaml")
    assert body["version"] == baseline.version
    assert body["captured_on"] == (
        baseline.captured_on.isoformat() if baseline.captured_on else None
    )
    assert body["level1"] == len(baseline.codes(1))
    assert body["level3"] == len(baseline.codes(3))
    assert body["writable"] is True
    assert body["last_refresh"] is None


def test_status_prefers_the_data_dir_copy(api_client, tmp_path: Path) -> None:
    target = runtime_shenwan_directory_path(data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(
        dump_shenwan_directory(load_shenwan_directory(DEFAULT_SHENWAN_DIRECTORY_PATH)),
        encoding="utf-8",
    )

    body = api_client.get("/health/shenwan-directory", headers=KEY).json()

    assert body["source"] == "data_dir"
    assert body["path"] == str(target)


async def test_refresh_enqueues_one_deduped_run_per_day(api_client) -> None:
    response = api_client.post("/health/shenwan-directory/refresh", headers=KEY)

    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["created"] is True

    async with session_scope() as session:
        run = await session.scalar(
            select(PipelineRun)
            .where(PipelineRun.kind == "shenwan_directory_refresh")
            .order_by(desc(PipelineRun.id))
            .limit(1)
        )
        assert run is not None and run.id == queued["run_id"]
        assert run.status == "queued" and run.triggered_by == "api"
        assert run.request_json == {"as_of": shanghai_today().isoformat()}
        assert run.idempotency_key == f"shenwan_directory_refresh:{run.request_json['as_of']}"

    # A second click the same day reuses the pending run (calendar-check dedupe).
    again = api_client.post("/health/shenwan-directory/refresh", headers=KEY).json()
    assert again["created"] is False and again["run_id"] == queued["run_id"]

    status = api_client.get("/health/shenwan-directory", headers=KEY).json()
    assert status["last_refresh"]["run_id"] == queued["run_id"]
    assert status["last_refresh"]["status"] == "queued"
    assert status["last_refresh"]["outcome"] is None
