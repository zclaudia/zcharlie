"""API tests for /pipelines."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from investment_agent.api.app import create_app
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base


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


def test_pipelines_requires_auth(api_client):
    r = api_client.get("/pipelines")
    assert r.status_code == 401


def test_list_runs_empty(api_client):
    r = api_client.get("/pipelines", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert r.json() == []


def test_get_unknown_run_404(api_client):
    r = api_client.get(
        "/pipelines/999", headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


def test_get_unknown_scores_404(api_client):
    r = api_client.get(
        "/pipelines/999/scores", headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_run_returns_seeded(api_client):
    async with session_scope() as session:
        run = PipelineRun(
            kind="discovery",
            universe_code="TEST",
            triggered_by="test",
            started_at=datetime.now(UTC),
            status="success",
            metadata_json={},
        )
        session.add(run)
        await session.flush()
        run_id = run.id
    r = api_client.get(
        f"/pipelines/{run_id}", headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["universe_code"] == "TEST"


@pytest.mark.asyncio
async def test_list_runs_filters_by_kind(api_client):
    async with session_scope() as session:
        for kind in ("discovery", "attribution", "discovery"):
            session.add(PipelineRun(
                kind=kind,
                universe_code="X",
                triggered_by="t",
                started_at=datetime.now(UTC),
                status="success",
                metadata_json={},
            ))
        await session.flush()
    r = api_client.get(
        "/pipelines",
        params={"kind": "discovery"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(x["kind"] == "discovery" for x in rows)
