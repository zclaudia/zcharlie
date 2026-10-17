"""Root acceptance: real audited bank originals through online refresh jobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.industry_peers.batch import submit_offline_batch
from investment_agent.industry_peers.jobs import (
    CollectArtifactStore,
    PeerCollectHandler,
    enqueue_peer_collect,
)
from investment_agent.industry_peers.originals import validate_field_original
from investment_agent.industry_peers.resolver import PeerContextResolver
from investment_agent.industry_peers.service import (
    get_industry_peer_context,
    peer_context_binding_from,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.providers.eastmoney_annual import (
    EastmoneyAnnualTransientError,
    RawAnnualResponse,
)
from investment_agent.providers.eastmoney_bank_annual import BANK_ADAPTER_VERSION
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from tests.integration.test_bank_verified_import import (
    AUDIT_PATH,
    BANK_EVIDENCE,
    ORIGINALS_ROOT,
    PERIOD,
)
from tests.integration.test_bank_verified_import import bank_world as bank_world
from tests.integration.test_bank_verified_import import (
    requires_bank_originals as requires_bank_originals,
)
from tests.support.peer_world import DirectCtx, claim_job, make_settings, read_mapping

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/bank_run02"


class BankResponses:
    """Controlled transport using FY2025 values; added metadata is test data."""

    def __init__(self, *, revised: bool = False, refuse: bool = False) -> None:
        self.revised = revised
        self.refuse = refuse
        self.calls: list[str] = []

    async def fetch(self, url, params):
        assert url == "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        assert "2025-12-31" in params["filter"]
        assert params["pageNumber"] == "1"
        assert "callback" not in params
        code = params["filter"].split('"')[1]
        self.calls.append(code)
        if self.refuse:
            raise AssertionError("a checkpointed company was fetched again")
        payload = json.loads((FIXTURES / "raw/financial" / f"{code}_main.json").read_text())
        payload["success"] = True
        payload["version"] = "controlled-revision-1"
        payload["result"]["pages"] = 1
        row = payload["result"]["data"][0]
        row["ORG_TYPE"] = "银行"
        if self.revised and code == "002142.SZ":
            row["BLDKBBL"] = 373.17
            row["UPDATE_DATE"] = "2026-10-16 00:00:00"
            payload["version"] = "controlled-revision-2"
        return RawAnnualResponse(status=200, body=json.dumps(payload).encode())


async def _enqueue(world, cohort, *, mode="full"):
    async with session_scope() as session:
        result = await enqueue_peer_collect(
            session,
            report_period=PERIOD,
            mode=mode,
            triggered_by="root_acceptance",
            data_dir=world,
            cohort_row=cohort,
        )
    return result


async def _online_count():
    async with session_scope() as session:
        return await session.scalar(
            select(func.count())
            .select_from(PeerAnnualSnapshot)
            .where(
                PeerAnnualSnapshot.adapter_version == BANK_ADAPTER_VERSION,
            )
        )


async def test_real_originals_online_refresh_preserves_history_and_demotes_changed_ratio(
    bank_world,
    requires_bank_originals,
):
    from investment_agent.api.app import create_app

    imported = datetime.now(UTC) - timedelta(seconds=2)
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        old_roster = await import_sample_roster(
            session,
            sample_path=BANK_EVIDENCE / "sample.json",
            industry="bank",
            acquired_at=imported,
        )
        await import_bank_annual_snapshots(
            session,
            evidence_root=BANK_EVIDENCE,
            report_period=PERIOD,
            acquired_at=imported,
            audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
    async with session_scope() as session:
        offline = await submit_offline_batch(
            session,
            data_dir=bank_world,
            cohort_key="bank_run02_sample",
            report_period=PERIOD,
            submitted_at=datetime.now(UTC),
        )
    async with session_scope() as session:
        online_roster = await import_sample_roster(
            session,
            sample_path=BANK_EVIDENCE / "sample.json",
            industry="bank",
            bank_online=True,
            acquired_at=datetime.now(UTC),
        )
        assert online_roster.snapshot.revision == old_roster.snapshot.revision + 1
        assert (
            online_roster.snapshot.payload["read_scope"]["adapter_version"] == BANK_ADAPTER_VERSION
        )
        missing = await get_industry_peer_context(session, ticker="601398.SH", data_dir=bank_world)
        assert (
            missing.batch_run_id is None
        )  # Explicit source switch cannot fall back to the old scope.

    settings = make_settings(bank_world, retries=0)
    first = await _enqueue(bank_world, online_roster.snapshot)
    first_source = BankResponses()
    first_job = await claim_job(first.run.id)
    assert (
        await PeerCollectHandler(settings=settings, source=first_source).execute(
            first_job, DirectCtx()
        )
        == "completed"
    )
    assert set(first_source.calls) == set(BANK_CODES)
    assert len(first_source.calls) == len(BANK_CODES)
    first_mapping = await read_mapping(bank_world, first.run.id)
    assert first_mapping.metric_pack == "bank_annual_v1"
    assert first_mapping.adapter_version == BANK_ADAPTER_VERSION
    assert await _online_count() == 10

    async with session_scope() as session:
        rows = list(
            (
                await session.scalars(
                    select(PeerAnnualSnapshot).where(
                        PeerAnnualSnapshot.adapter_version == BANK_ADAPTER_VERSION,
                    )
                )
            ).all()
        )
        checked = excluded = 0
        for row in rows:
            for metric, field in row.fields.items():
                if field["verification"] == "verified":
                    assert await validate_field_original(session, row, metric)
                    checked += 1
                else:
                    assert row.verification[metric]["scope_exclusion"] is True
                    assert field["value"] is None
                    excluded += 1
        assert (checked, excluded) == (48, 2)
        current = await get_industry_peer_context(session, ticker="601398.SH", data_dir=bank_world)
        assert current.adapter_version == BANK_ADAPTER_VERSION
        first_binding = peer_context_binding_from(current)
        assert first_binding is not None
        first_values = {
            metric.metric_key: metric.model_dump(mode="json") for metric in current.metrics
        }
        resolver = PeerContextResolver(session, binding=first_binding, data_dir=bank_world)
        for metric in current.metrics:
            assert (
                await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
            ).inputs_verified

    client = TestClient(create_app())
    response = client.get(
        "/industries/bank_run02_sample", headers={"X-API-Key": "bank-proof-integration"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["batch_run_id"] == str(first.run.id)
    assert len(response.json()["metrics"]) == 4
    historical_response = client.get(
        f"/industries/bank_run02_sample?batch_run_id={offline.mapping.batch_run_id}",
        headers={"X-API-Key": "bank-proof-integration"},
    )
    assert historical_response.status_code == 200, historical_response.text
    assert historical_response.json()["cohort_ref"]["revision"] == old_roster.snapshot.revision

    # Re-observing identical response bytes and versions cannot append snapshots.
    same = await _enqueue(bank_world, online_roster.snapshot, mode="refresh")
    assert (
        await PeerCollectHandler(settings=settings, source=BankResponses()).execute(
            await claim_job(same.run.id),
            DirectCtx(),
        )
        == "completed"
    )
    assert await _online_count() == 10

    class StopBeforeCommit(PeerCollectHandler):
        async def _finalize(self, *args, **kwargs):
            raise EastmoneyAnnualTransientError(
                "controlled failure before batch commit", provider="eastmoney"
            )

    changed = await _enqueue(bank_world, online_roster.snapshot, mode="refresh")
    changed_job = await claim_job(changed.run.id)
    changed_source = BankResponses(revised=True)
    with pytest.raises(EastmoneyAnnualTransientError, match="before batch commit"):
        await StopBeforeCommit(settings=settings, source=changed_source).execute(
            changed_job, DirectCtx()
        )
    assert await _online_count() == 11
    async with session_scope() as session:
        stale = await get_industry_peer_context(session, ticker="601398.SH", data_dir=bank_world)
        assert stale.status.value == "stale"
        assert stale.batch_run_id == first_mapping.batch_run_id
        historical = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=first_mapping.batch_run_id,
            cutoff=first_binding.cutoff,
            data_dir=bank_world,
        )
        assert {
            metric.metric_key: metric.model_dump(mode="json") for metric in historical.metrics
        } == first_values
    resumed_source = BankResponses(refuse=True)
    assert (
        await PeerCollectHandler(settings=settings, source=resumed_source).execute(
            changed_job, DirectCtx()
        )
        == "completed"
    )
    assert resumed_source.calls == []
    index = CollectArtifactStore(bank_world, changed.run.id).load_index()
    assert (
        "provision_coverage" in index["companies"]["002142.SZ"]["review_summary"]["review_needed"]
    )
    async with session_scope() as session:
        revised = await get_industry_peer_context(session, ticker="601398.SH", data_dir=bank_world)
        assert revised.batch_run_id == str(changed.run.id)
        binding = peer_context_binding_from(revised)
        assert binding is not None
        resolver = PeerContextResolver(session, binding=binding, data_dir=bank_world)
        for metric in revised.metrics:
            if metric.metric_key == "provision_coverage":
                with pytest.raises(InvestorError, match="pending_recheck|not fully verified"):
                    await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
            else:
                assert (
                    await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
                ).inputs_verified
        old = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=offline.mapping.batch_run_id,
            data_dir=bank_world,
        )
        assert old.report_period == PERIOD
        assert old.adapter_version == "offline-run02-v1"
        after = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=first_mapping.batch_run_id,
            cutoff=first_binding.cutoff,
            data_dir=bank_world,
        )
        assert {
            metric.metric_key: metric.model_dump(mode="json") for metric in after.metrics
        } == first_values
