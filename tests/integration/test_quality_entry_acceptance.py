"""QT-041: independent local admission and packaging slices; never run a worker."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from investment_agent.api.app import create_app
from investment_agent.api.auth import get_bootstrap_settings
from investment_agent.api.routes import agent, research_entry
from investment_agent.config import Settings
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import (
    AgentModelRun,
    AgentTask,
    BudgetEntry,
    ExecutionGrantRow,
    MessageRow,
    TaskAttempt,
    ToolAction,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investor import MandateHead, MethodProposal, TaskMethodBinding
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.next_iterations_package import (
    assert_wheel_assets,
    asset_paths,
    build_wheel,
    prepare_source,
)

LISTING = {"kind": "listing", "id": 18, "revision": 1}
PREFLIGHT = {"listing_ref": LISTING, "template_key": "company_research", "question": "净利润有哪些反证与缺口？"}


@dataclass
class Harness:
    client: httpx.AsyncClient
    settings: Settings
    app: FastAPI
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.api_key}


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    base = Settings(api_key="g0-acceptance-inert", data_dir=tmp_path,
                    investor_agent_enabled=True, investor_agent_background_enabled=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'g0-acceptance.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all([
            Market(code="SSE", name="synthetic", timezone="Asia/Shanghai", currency="CNY"),
            Company(id=18, canonical_name="合成公司"),
            Listing(id=18, company_id=18, market_code="SSE", ticker="600036"),
        ])

    @asynccontextmanager
    async def local_scope() -> AsyncIterator[AsyncSession]:
        async with sessions() as session, session.begin():
            yield session

    monkeypatch.setattr(agent, "session_scope", local_scope)
    monkeypatch.setattr(research_entry, "session_scope", local_scope)
    app = create_app(settings=base)
    app.dependency_overrides[get_bootstrap_settings] = lambda: base
    # ASGITransport has no lifespan: no scheduler, gateway, executor or provider.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://offline.test") as client:
        yield Harness(client, base, app, sessions, engine)
    await engine.dispose()


async def conversation(harness: Harness) -> str:
    response = await harness.client.post("/agent/conversations", headers=harness.headers,
                                        json={"title": "独立验收", "context_refs": [LISTING]})
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    assert isinstance(identity, str)
    return identity


def message() -> dict[str, object]:
    return {"client_message_id": str(uuid4()), "text": PREFLIGHT["question"],
            "context_refs": [LISTING], "research_kind": "company_research"}


async def test_only_existing_message_admission_creates_task_and_original_grant(harness: Harness) -> None:
    preflight = await harness.client.post("/investor/research-entry/preflight", headers=harness.headers, json=PREFLIGHT)
    assert preflight.status_code == 200
    assert preflight.json()["coverage"] == "missing"
    model: type[Base]
    async with harness.sessions() as session:
        for model in (AgentTask, BudgetEntry, ExecutionGrantRow, TaskMethodBinding, MethodProposal, MandateHead):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    identity = await conversation(harness)
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentTask)) == 0
    response = await harness.client.post(f"/agent/conversations/{identity}/messages", headers=harness.headers, json=message())
    assert response.status_code == 202, response.text
    task = response.json()["task"]
    assert task["status"] == "queued"
    async with harness.sessions() as session:
        grant = await session.get(ExecutionGrantRow, task["id"])
        binding = await session.get(TaskMethodBinding, task["id"])
        assert grant is not None and binding is not None
        assert grant.payload["owner"] == "local_owner"
        assert grant.payload["source"] == "authenticated_message"
        assert grant.payload["mode"] == "interactive"
        assert grant.payload["research_kind"] == "company_research"
        assert grant.payload["mandate_id"] is None
        assert grant.payload["watchlist"] is False
        assert {(ref["kind"], str(ref["id"])) for ref in grant.payload["scope_refs"] if ref["kind"] == "listing"} == {("listing", "18")}
        assert all(ref["kind"] in {"listing", "case"} for ref in grant.payload["scope_refs"])
        assert "request_data_refresh" not in grant.payload["tool_names"]
        assert "fetch_disclosure_original" not in grant.payload["tool_names"]
        assert binding.payload["schema_version"] == 1
        assert "shadow" not in binding.payload and "receipt" not in binding.payload
        assert await session.scalar(select(func.count()).select_from(MethodProposal)) == 0
        for model in (AgentTask, ExecutionGrantRow, TaskMethodBinding, PipelineRun):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        for model in (BudgetEntry, AgentModelRun, TaskAttempt, ToolAction):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    # Original read contracts work even though no executor has produced research.
    read = await harness.client.get(f'/agent/tasks/{task["id"]}', headers=harness.headers)
    assert read.status_code == 200 and read.json()["id"] == task["id"]
    artifacts = await harness.client.get(f'/agent/tasks/{task["id"]}/research', headers=harness.headers)
    assert artifacts.status_code == 200 and artifacts.json() == []


async def test_lost_ack_replay_is_same_task_and_changed_payload_conflicts(harness: Harness) -> None:
    identity, body = await conversation(harness), message()
    path = f"/agent/conversations/{identity}/messages"
    first = await harness.client.post(path, headers=harness.headers, json=body)
    assert first.status_code == 202, first.text
    repeated = await harness.client.post(path, headers=harness.headers, json=body)
    assert repeated.status_code == 202
    assert repeated.json() == first.json()
    changed = await harness.client.post(path, headers=harness.headers, json={**body, "text": "另一个问题"})
    assert changed.status_code == 409
    async with harness.sessions() as session:
        for model in (AgentTask, ExecutionGrantRow, TaskMethodBinding, MessageRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert await session.scalar(select(func.count()).select_from(BudgetEntry)) == 0


async def test_send_rechecks_live_agent_switch_and_rejection_does_not_admit(harness: Harness) -> None:
    identity = await conversation(harness)
    preflight = await harness.client.post("/investor/research-entry/preflight", headers=harness.headers, json=PREFLIGHT)
    assert preflight.status_code == 200
    harness.app.dependency_overrides[get_bootstrap_settings] = lambda: harness.settings.model_copy(update={"investor_agent_enabled": False})
    rejected = await harness.client.post(f"/agent/conversations/{identity}/messages", headers=harness.headers, json=message())
    # Original message API keeps its existing execution_denied=409 contract;
    # the separate read-only preflight maps feature_disabled to 503.
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "execution_denied"
    async with harness.sessions() as session:
        for model in (AgentTask, ExecutionGrantRow, TaskMethodBinding, MessageRow, PipelineRun, BudgetEntry):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


def test_actual_g0_assets_exist_in_a_disposable_wheel(tmp_path: Path) -> None:
    source = prepare_source(tmp_path / "source")
    wheel = build_wheel(source, tmp_path / "wheel")
    expected = asset_paths("g0")
    assert expected == (
        "investment_agent/web/templates/_research_entry.html",
        "investment_agent/web/static/research_entry.js",
        "investment_agent/web/static/pages/research_entry.css",
    )
    assert_wheel_assets(wheel, expected)
