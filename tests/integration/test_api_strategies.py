"""API tests for /strategies + /universes."""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from investment_agent.api.app import create_app
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import Strategy
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import Universe


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


def test_strategies_requires_auth(api_client):
    r = api_client.get("/strategies")
    assert r.status_code == 401


def test_list_strategies_empty(api_client):
    r = api_client.get("/strategies", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert r.json() == []


def test_get_strategy_unknown_404(api_client):
    r = api_client.get(
        "/strategies/ghost",
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_strategies_returns_seeded(api_client):
    async with session_scope() as session:
        session.add(
            Strategy(
                code="value",
                name="Value",
                version="v1.0",
                description="d",
                config_json={"x": 1},
                enabled=True,
            ),
        )
        await session.flush()
    r = api_client.get("/strategies", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["code"] == "value"
    assert body[0]["config"] == {"x": 1}


def test_universes_requires_auth(api_client):
    r = api_client.get("/universes")
    assert r.status_code == 401


def test_list_universes_empty(api_client):
    r = api_client.get("/universes", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_universes_returns_seeded(api_client):
    async with session_scope() as session:
        session.add(
            Universe(code="NASDAQ_100", name="N100", description="d"),
        )
        await session.flush()
    r = api_client.get("/universes", headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["code"] == "NASDAQ_100"
