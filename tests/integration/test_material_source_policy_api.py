"""Source-policy response behavior for GET context and refresh registration."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from freezegun import freeze_time
from sqlalchemy import select

from investment_agent.config import Settings
from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, find_identity
from investment_agent.materials import source_policy
from investment_agent.materials.refresh import RefreshRequest, request_refresh
from investment_agent.settings import loader as settings_loader
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company, Listing
from tests.integration.test_api_research_materials import H, seed_listings, url_context, url_refresh
from tests.support.material_source_policy import synthetic_approved_source_policy


async def test_context_reports_bound_source_gates_and_legacy_aliases(api_client) -> None:
    seeded = await seed_listings()
    response = api_client.get(url_context(seeded["HKEX/00005"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    sources = body["freshness"]["sources"]
    control = sources["eastmoney.hk.native.v1"]
    assert control["source"] == "eastmoney.hk.native.v1"
    assert control["bound"] is True
    assert control["licensing"] == "unknown"
    assert control["technical_acceptance"] == "not_accepted"
    assert control["operational_enabled"] is False
    assert control["enabled"] is False
    assert control["accepted_for_fetch"] is False
    assert "licensing_unknown" in control["reason_codes"]
    assert sources["hk_annual_reports"] == control
    assert sources["sec.companyfacts.v1"]["bound"] is True
    assert body["capabilities"]["online_refresh"] is False


async def test_refresh_registers_audit_task_with_source_refusal_reasons(api_client) -> None:
    seeded = await seed_listings()
    response = api_client.post(url_refresh(seeded["HKEX/00005"]), headers=H, json={})
    assert response.status_code == 202
    body = response.json()
    assert isinstance(body["run_id"], int)
    assert body["online_refresh_enabled"] is False
    assert body["source_decisions"]["eastmoney.hk.native.v1"]["enabled"] is False
    assert body["source_decisions"]["eastmoney.hk.native.v1"]["reason_codes"]
    async with session_scope() as session:
        row = await session.get(PipelineRun, body["run_id"])
        assert row is not None and row.status == "queued"


async def test_explicit_hk_period_is_rejected_before_enqueue(api_client) -> None:
    seeded = await seed_listings()
    response = api_client.post(
        url_refresh(seeded["HKEX/00005"]),
        headers=H,
        json={"sources": ["eastmoney.hk.native.v1"], "periods": ["2025-12-31"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_period"
    async with session_scope() as session:
        runs = (await session.execute(select(PipelineRun))).scalars().all()
        assert runs == []


async def test_sec_explicit_period_remains_supported(api_client) -> None:
    seeded = await seed_listings()
    response = api_client.post(
        url_refresh(seeded["NASDAQ/AAPL"]),
        headers=H,
        json={"sources": ["sec.companyfacts.v1"], "periods": ["2025-09-27"]},
    )
    assert response.status_code == 202
    assert response.json()["online_refresh_enabled"] is False


async def test_source_gate_change_creates_a_new_same_day_audit_run(api_client, monkeypatch) -> None:
    seeded = await seed_listings()
    initial_policy = deepcopy(dict(source_policy.load_source_policy()))
    identity = find_identity(
        market_code="HKEX", ticker="00005", path=DEFAULT_PILOT_LIST_PATH
    )
    assert identity is not None
    updated_policy = deepcopy(initial_policy)
    updated_policy["sources"]["eastmoney.hk.native.v1"] = synthetic_approved_source_policy(
        "eastmoney.hk.native.v1", identity, "1"
    )["eastmoney.hk.native.v1"]

    async def operational_settings(_session=None):
        return Settings(
            api_key="test-key",
            materials_hk_refresh_enabled=True,
            materials_sec_refresh_enabled=True,
        )

    with freeze_time("2026-10-17"):
        first = api_client.post(url_refresh(seeded["HKEX/00005"]), headers=H, json={})
        duplicate = api_client.post(url_refresh(seeded["HKEX/00005"]), headers=H, json={})
        monkeypatch.setattr(
            source_policy,
            "load_source_policy",
            lambda: updated_policy,
        )
        monkeypatch.setattr(settings_loader, "load_settings", operational_settings)
        changed = api_client.post(url_refresh(seeded["HKEX/00005"]), headers=H, json={})
    assert first.status_code == duplicate.status_code == changed.status_code == 202
    assert first.json()["created"] is True
    assert duplicate.json()["created"] is False
    assert changed.json()["created"] is True
    assert changed.json()["run_id"] != first.json()["run_id"]


async def test_concurrent_same_scope_requests_deduplicate(api_client) -> None:
    seeded = await seed_listings()
    request = RefreshRequest(
        listing_id=seeded["NASDAQ/AAPL"], sources=("sec.companyfacts.v1",)
    )

    async def register():
        async with session_scope() as session:
            return await request_refresh(session, request)

    with freeze_time("2026-10-17"):
        first, second = await asyncio.gather(register(), register())
    assert first.run_id == second.run_id
    assert {first.created, second.created} == {False, True}


async def test_listing_with_mismatched_company_name_is_not_covered_or_queued(api_client) -> None:
    seeded = await seed_listings()
    listing_id = seeded["HKEX/00005"]
    async with session_scope() as session:
        listing = await session.get(Listing, listing_id)
        assert listing is not None
        company = await session.get(Company, listing.company_id)
        assert company is not None
        company.canonical_name = "Different Company"

    context = api_client.get(url_context(listing_id), headers=H)
    assert context.status_code == 200 and context.json()["covered"] is False
    response = api_client.post(url_refresh(listing_id), headers=H, json={})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "listing_not_covered"


async def test_synthetic_sec_approval_does_not_enable_hk_source(api_client) -> None:
    await seed_listings()
    identity = find_identity(
        market_code="HKEX", ticker="00005", path=DEFAULT_PILOT_LIST_PATH
    )
    assert identity is not None
    settings = Settings(
        api_key="test-key",
        materials_sec_refresh_enabled=True,
        materials_hk_refresh_enabled=True,
    )
    approved = synthetic_approved_source_policy("sec.companyfacts.v1", identity, "1")
    sec = source_policy.evaluate_source_policy(
        "sec.companyfacts.v1",
        identity,
        settings=settings,
        adapter_version="1",
        global_enabled=True,
        approvals=approved,
    )
    hk = source_policy.evaluate_source_policy(
        "eastmoney.hk.native.v1",
        identity,
        settings=settings,
        adapter_version="1",
        global_enabled=True,
    )
    assert sec.enabled is True
    assert hk.enabled is False
    assert "licensing_unknown" in hk.reason_codes

    settings_values = settings.model_dump()
    settings_values["materials_hk_licensing_approved"] = True
    ordinary_settings = Settings.model_validate(settings_values)
    assert not hasattr(ordinary_settings, "materials_hk_licensing_approved")
    still_denied = source_policy.evaluate_source_policy(
        "eastmoney.hk.native.v1",
        identity,
        settings=ordinary_settings,
        adapter_version="1",
        global_enabled=True,
    )
    assert still_denied.enabled is False
