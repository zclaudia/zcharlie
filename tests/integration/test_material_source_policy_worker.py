from __future__ import annotations

from pathlib import Path

import pytest

from investment_agent.materials import handlers
from investment_agent.materials.handlers import MaterialCollectionHandler
from investment_agent.materials.jobs import MaterialCollectionPayload
from investment_agent.providers.errors import ProviderTransientError
from investment_agent.storage.models.reference import Company
from tests.integration.test_material_collection_job import (
    BATCH,
    HK_SOURCE,
    HSBC,
    SEC_SOURCE,
    FakeAdapter,
    FakeFetcher,
    _claim,
    _Ctx,
    _identity,
    _listing_id,
    _replay_response,
    _run_row,
    _seed,
    _settings,
)
from tests.support.material_source_policy import synthetic_approved_source_policy


async def test_run_records_independent_source_decisions_and_rejects_unapproved_source(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    identity = _identity(HSBC)
    hk_adapter = FakeAdapter(source=HK_SOURCE)
    sec_adapter = FakeAdapter(source=SEC_SOURCE)
    settings = _settings(tmp_path).model_copy(
        update={"materials_hk_refresh_enabled": True, "materials_sec_refresh_enabled": True}
    )
    approvals = synthetic_approved_source_policy(HK_SOURCE, identity, "fake-1")
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    handler = MaterialCollectionHandler(
        settings=settings,
        fetcher=fetcher,
        adapters={HK_SOURCE: hk_adapter, SEC_SOURCE: sec_adapter},
        approvals=approvals,
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id,
            refresh_batch=BATCH,
            adapter_versions={HK_SOURCE: "fake-1", SEC_SOURCE: "fake-1"},
        )
    )

    assert await handler.execute(job, _Ctx()) == "partial"

    run = await _run_row(run_id)
    metadata = run.metadata_json
    assert metadata["subject"]["company_id"] == companies[HSBC]
    assert metadata["source_status"][HK_SOURCE] == "saved"
    assert metadata["source_status"][SEC_SOURCE] == "source_policy_disabled"
    assert metadata["source_decisions"][HK_SOURCE]["enabled"] is True
    assert metadata["source_decisions"][SEC_SOURCE]["enabled"] is False
    assert metadata["source_decisions"][SEC_SOURCE]["licensing"] != "approved"
    assert [request.name for request in fetcher.calls] == ["income.json"]


async def test_missing_payload_adapter_version_is_recorded_and_never_fetched(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    adapter = FakeAdapter(source=HK_SOURCE)
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path).model_copy(update={"materials_hk_refresh_enabled": True}),
        fetcher=FakeFetcher(responses={"income.json": _replay_response("income.json")}),
        adapters={HK_SOURCE: adapter},
        approvals=synthetic_approved_source_policy(HK_SOURCE, _identity(HSBC), "fake-1"),
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "partial"
    run = await _run_row(run_id)
    assert run.metadata_json["source_status"][HK_SOURCE] == "adapter_version_mismatch"
    assert run.metadata_json["source_decisions"][HK_SOURCE]["reason"] == (
        "payload_adapter_version_missing_or_mismatch"
    )


@pytest.mark.parametrize("company_name", ["Wrong issuer", "汇丰控股", "hsbc holdings plc"])
async def test_company_mismatch_stops_before_policy_or_upstream_request(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, company_name: str
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    companies = await _seed(db_session)
    company = await db_session.get(Company, companies[HSBC])
    assert company is not None
    company.canonical_name = company_name
    await db_session.commit()
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    adapter = FakeAdapter(source=HK_SOURCE)
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path).model_copy(update={"materials_hk_refresh_enabled": True}),
        fetcher=fetcher,
        adapters={HK_SOURCE: adapter},
        approvals=synthetic_approved_source_policy(HK_SOURCE, _identity(HSBC), "fake-1"),
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id,
            sources=(HK_SOURCE,),
            refresh_batch=BATCH,
            adapter_versions={HK_SOURCE: "fake-1"},
        )
    )

    assert await handler.execute(job, _Ctx()) == "partial"
    run = await _run_row(run_id)
    assert run.metadata_json["listing_status"] == "identity_company_mismatch"
    assert "identity_company_mismatch" in run.metadata_json["problems"][0]
    assert fetcher.calls == []


async def test_transient_source_is_checkpointed_after_other_allowed_source_runs(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    identity = _identity(HSBC)
    approvals = {
        **synthetic_approved_source_policy(HK_SOURCE, identity, "fake-1"),
        **synthetic_approved_source_policy(SEC_SOURCE, identity, "fake-1"),
    }
    settings = _settings(tmp_path).model_copy(
        update={"materials_hk_refresh_enabled": True, "materials_sec_refresh_enabled": True}
    )
    fetcher = FakeFetcher(
        responses={"income.json": _replay_response("income.json")},
        error=ProviderTransientError("temporary", provider=HK_SOURCE, error_code="timeout"),
        error_on_call=1,
    )
    handler = MaterialCollectionHandler(
        settings=settings,
        fetcher=fetcher,
        adapters={HK_SOURCE: FakeAdapter(source=HK_SOURCE), SEC_SOURCE: FakeAdapter(source=SEC_SOURCE)},
        approvals=approvals,
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id,
            refresh_batch=BATCH,
            adapter_versions={HK_SOURCE: "fake-1", SEC_SOURCE: "fake-1"},
        )
    )

    with pytest.raises(ProviderTransientError):
        await handler.execute(job, _Ctx())

    run = await _run_row(run_id)
    assert [request.name for request in fetcher.calls] == ["income.json", "income.json"]
    assert run.metadata_json["source_status"][HK_SOURCE] == "transient_error"
    assert run.metadata_json["source_status"][SEC_SOURCE] == "saved"
    assert run.metadata_json["source_decisions"][SEC_SOURCE]["enabled"] is True
