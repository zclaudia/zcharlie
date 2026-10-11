"""AG19 local diagnostics never contact providers or lazily import settings."""
import pytest
from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.app_settings import AppSettingMeta

KEY = {"X-API-Key": "secret"}


@pytest.mark.asyncio
async def test_readiness_auth_default_off_no_secret_or_lazy_import(api_client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "private-never-exported")
    monkeypatch.setenv("AGENT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("AGENT_MODEL", "fixture")
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "false")
    assert api_client.get("/investor/readiness").status_code == 401
    response = api_client.get("/investor/readiness", headers=KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["network_verified"] is False
    assert len(body["model_config_fingerprint"]) == 64
    assert "private-never-exported" not in response.text
    assert "https://fixture.invalid" not in response.text
    assert any("价格未知" in issue for issue in body["local_configuration_issues"])
    assert any("后台开关" in issue for issue in body["review_issues"])
    async with session_scope() as session:
        assert await session.scalar(select(AppSettingMeta)) is None


@pytest.mark.asyncio
async def test_readiness_separate_purpose_caps_and_minimum_limits(api_client, monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    from investment_agent.investor.common import Mutation, Quantity
    from investment_agent.investor.contracts import Mandate, ResearchAuthorization
    from investment_agent.investor.foundations import InvestorService

    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    monkeypatch.setenv("INVESTOR_AGENT_BACKGROUND_ENABLED", "true")
    monkeypatch.setenv("INVESTOR_AGENT_MAX_LEADS_PER_BATCH", "0")
    auth = ResearchAuthorization(background_enabled=True,
        daily_budget=Quantity(value="10", unit="USD"),
        per_task_budget=Quantity(value="2", unit="USD"),
        review_daily_budget=Quantity(value="1", unit="USD"),
        max_leads_per_batch=20, max_new_tasks_per_batch=3, source_refs=())
    async with session_scope() as session:
        svc = InvestorService(session)
        mandate = await svc.create_mandate("local_owner", Mandate(
            id=uuid4(), revision=1, actor="untrusted", created_at=datetime.now(UTC), origin_ref=None,
            goals=(), scope_refs=(), unknowns=("用途未知",), hard_constraints=(), authorization=auth), uuid4())
    readiness = api_client.get("/investor/readiness", headers=KEY).json()
    assert not any("探索" in issue for issue in readiness["review_issues"])
    assert any("探索未授权" in issue for issue in readiness["exploration_issues"])
    assert any("上限为0" in issue for issue in readiness["exploration_issues"])
    assert readiness["mandate_background_enabled"] is True
    assert readiness["runtime_background_enabled"] is True
    async with session_scope() as session:
        await InvestorService(session).revise_mandate("local_owner", mandate.model_copy(update={
            "revision": 2, "authorization": auth.model_copy(update={
                "exploration_daily_budget": Quantity(value="1", unit="CNY"),
                "review_daily_budget": None})}), Mutation(expected_revision=1, idempotency_key=uuid4()))
    changed = api_client.get("/investor/readiness", headers=KEY).json()
    assert any("review_budget_unavailable" in issue for issue in changed["review_issues"])
    assert any("USD" in issue for issue in changed["exploration_issues"])


@pytest.mark.asyncio
async def test_initial_minimal_mandate_wire_then_cas_conflict(api_client):
    from datetime import UTC, datetime
    from uuid import uuid4

    candidate = {"id": str(uuid4()), "revision": 1, "created_at": datetime.now(UTC).isoformat(),
        "actor": "untrusted", "origin_ref": None, "goals": [], "scope_refs": [],
        "unknowns": ["用途与画像尚未知晓"], "hard_constraints": [],
        "authorization": {"background_enabled": False, "daily_budget": {"value": "2", "unit": "USD"},
            "per_task_budget": {"value": "1", "unit": "USD"}, "max_leads_per_batch": 20,
            "max_new_tasks_per_batch": 3, "source_refs": []}}
    key = str(uuid4())
    first = api_client.post("/investor/mandate", headers=KEY, json={"candidate": candidate, "idempotency_key": key})
    assert first.status_code == 201, first.text
    saved = first.json()
    assert saved["actor"] == "user"
    assert saved["authorization"]["background_enabled"] is False
    assert saved["authorization"]["exploration_daily_budget"] is None
    assert api_client.post("/investor/mandate", headers=KEY, json={"candidate": candidate, "idempotency_key": key}).json() == saved
    revision = {**saved, "revision": 2, "goals": ["局部问题"]}
    parameters = {"operation": "mandate_revision", "candidate": revision, "expected_revision": 1}
    changed = api_client.post("/investor/actions", headers=KEY, json={"parameters": parameters, "idempotency_key": str(uuid4())})
    assert changed.status_code == 200, changed.text
    conflict = api_client.post("/investor/actions", headers=KEY, json={"parameters": parameters, "idempotency_key": str(uuid4())})
    assert conflict.status_code == 409
    assert api_client.get("/investor/mandate", headers=KEY).json()["revision"] == 2
