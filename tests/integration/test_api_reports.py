"""API tests for /reports."""

import pytest_asyncio
from fastapi.testclient import TestClient

from investment_agent.api.app import create_app
from investment_agent.storage.database import init_engine
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
    yield TestClient(create_app()), tmp_path
    await engine.dispose()


def test_reports_requires_auth(api_client):
    client, _ = api_client
    r = client.get("/reports/discovery")
    assert r.status_code == 401


def test_list_discovery_empty(api_client):
    client, _ = api_client
    r = client.get("/reports/discovery", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert r.json() == []


def test_get_discovery_no_dir_404(api_client):
    client, _ = api_client
    r = client.get(
        "/reports/discovery/2025-06-03", headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


def test_list_and_get_discovery_after_writing_files(api_client):
    client, tmp_path = api_client
    day = tmp_path / "reports" / "2025-06-03"
    day.mkdir(parents=True)
    (day / "discovery_42.md").write_text("# Discovery 42\n\nbody")
    (day / "discovery_43.md").write_text("# Discovery 43\n\nlater")

    # List
    r = client.get("/reports/discovery", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(x["kind"] == "discovery" for x in rows)
    # Newest first (sorted reverse): discovery_43 then discovery_42
    assert rows[0]["run_id"] == 43

    # Get by day — returns newest
    r = client.get(
        "/reports/discovery/2025-06-03", headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["run_id"] == 43
    assert "Discovery 43" in body["body"]


def test_list_attribution_after_writing_files(api_client):
    client, tmp_path = api_client
    day = tmp_path / "reports" / "2025-06-03"
    day.mkdir(parents=True)
    (day / "attribution_AAPL_15.md").write_text("# Attribution AAPL")
    (day / "attribution_MSFT_16.md").write_text("# Attribution MSFT")

    r = client.get("/reports/attribution", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(x["kind"] == "attribution" for x in rows)
    assert {x["run_id"] for x in rows} == {15, 16}
