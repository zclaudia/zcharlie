"""G0 registration/authentication checks, in-process and without app startup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from investment_agent.api.app import create_app
from investment_agent.api.auth import get_bootstrap_settings
from investment_agent.api.deps import investor_error_status
from investment_agent.api.routes import research_entry
from investment_agent.config import Settings
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import AgentTask, BudgetEntry, ExecutionGrantRow
from investment_agent.storage.models.investor import TaskMethodBinding
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.web.session import SESSION_COOKIE_NAME, create_session_token

INPUT = {"listing_ref": {"kind": "listing", "id": 18, "revision": 1},
         "template_key": "company_research", "question": "主要商业驱动及反证？"}
ROOT = Path(__file__).resolve().parents[2]
type EntryHarness = tuple[httpx.AsyncClient, Settings, FastAPI, async_sessionmaker[AsyncSession], AsyncEngine]


@pytest.fixture
async def entry_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[EntryHarness]:
    monkeypatch.setenv("API_KEY", "g0-offline-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings(api_key="g0-offline-secret", data_dir=tmp_path,
                        investor_agent_enabled=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wiring.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all([
            Market(code="SSE", name="fixture", timezone="Asia/Shanghai", currency="CNY"),
            Company(id=18, canonical_name="合成银行"),
            Listing(id=18, company_id=18, market_code="SSE", ticker="600036"),
        ])

    @asynccontextmanager
    async def local_scope() -> AsyncIterator[AsyncSession]:
        async with sessions() as session, session.begin():
            yield session

    monkeypatch.setattr(research_entry, "session_scope", local_scope)
    app = create_app(settings=settings)
    app.dependency_overrides[get_bootstrap_settings] = lambda: settings
    # ASGITransport does not run lifespan. No gateway, scheduler or providers start.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://offline.test") as client:
        yield client, settings, app, sessions, engine
    await engine.dispose()


async def test_registered_preflight_reads_without_original_task_side_effects(entry_client: EntryHarness) -> None:
    client, _settings, _app, sessions, engine = entry_client
    statements: list[str] = []

    def record(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.post("/investor/research-entry/preflight", json=INPUT,
                                     headers={"X-API-Key": "g0-offline-secret"})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert response.json()["coverage"] == "missing"
    assert response.json()["template_key"] == "company_research"
    assert response.json()["method"]["binding_schema_version"] == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    async with sessions() as session:
        for model in (AgentTask, BudgetEntry, ExecutionGrantRow, TaskMethodBinding):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_original_auth_and_cookie_origin_guard_cover_both_entry_and_conversation(entry_client: EntryHarness) -> None:
    client, settings, _app, _sessions, _engine = entry_client
    for path, body in (("/investor/research-entry/preflight", INPUT),
                       ("/agent/conversations", {"title": "fixture", "context_refs": [INPUT["listing_ref"]]})):
        assert (await client.post(path, json=body)).status_code == 401
    client.cookies.set(SESSION_COOKIE_NAME, create_session_token(api_key=settings.api_key, ttl_minutes=5))
    for path, body in (("/investor/research-entry/preflight", INPUT),
                       ("/agent/conversations", {"title": "fixture", "context_refs": [INPUT["listing_ref"]]})):
        for headers in ({}, {"Origin": "https://foreign.invalid"}, {"Origin": "null", "X-Requested-With": "fetch"}):
            assert (await client.post(path, json=body, headers=headers)).status_code == 403
    accepted = await client.post("/investor/research-entry/preflight", json=INPUT,
                                 headers={"Origin": "http://offline.test"})
    assert accepted.status_code == 200


async def test_disabled_feature_has_explicit_503_and_business_error_shape(entry_client: EntryHarness) -> None:
    client, settings, app, _sessions, _engine = entry_client
    app.dependency_overrides[get_bootstrap_settings] = lambda: settings.model_copy(update={"investor_agent_enabled": False})
    response = await client.post("/investor/research-entry/preflight", json=INPUT,
                                 headers={"X-API-Key": settings.api_key})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "feature_disabled"
    assert response.json()["detail"]["field_errors"] == []
    assert response.json()["detail"]["current_refs"] == []
    assert investor_error_status("writes_disabled") == 403


async def test_route_rejects_injected_authority_and_unknown_listing(entry_client: EntryHarness) -> None:
    client, settings, _app, _sessions, _engine = entry_client
    headers = {"X-API-Key": settings.api_key}
    assert (await client.post("/investor/research-entry/preflight", json={**INPUT, "owner": "other"}, headers=headers)).status_code == 422
    response = await client.post("/investor/research-entry/preflight", json={**INPUT, "listing_ref": {"kind": "listing", "id": 999, "revision": 1}}, headers=headers)
    assert response.status_code == 404


async def test_original_agent_page_mounts_guide_with_versioned_assets(entry_client: EntryHarness) -> None:
    client, settings, _app, _sessions, _engine = entry_client
    response = await client.get("/app/agent", headers={"X-API-Key": settings.api_key})
    assert response.status_code == 200
    assert response.text.count('id="research-entry"') == 1
    assert 'id="agent-compose"' in response.text
    assert 'data-entry-consent' in response.text
    assert '/pages/research_entry.css' in response.text
    assert 'data-entry-submit disabled' in response.text
    javascript = (ROOT / "src/investment_agent/web/static/agent.js").read_text()
    assert 'import { bootResearchEntry } from "./research_entry.js"' in javascript
    assert "bootResearchEntry();" in javascript
