"""P4 recovery over real HTTP, committed P2 checkpoints and SQLite workers."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.admission import SupplementalRuleAdmission
from investment_agent.supplemental_rules.contracts import UpdateRecoveryCommand
from investment_agent.supplemental_rules.update_jobs import SupplementalUpdateHandler
from investment_agent.supplemental_rules.update_targets import freeze_target_snapshot
from tests.integration.test_supplemental_acceptance import (
    API_KEY,
    _drain_run,
    _InlineContext,
    _run_worker_paused_after_first_checkpoint,
)
from tests.support.research_closure import activate_rule, metric_rule, seed_listing


async def _updates_enabled(enabled: bool) -> None:
    async with session_scope() as session:
        store = store_for(session, Settings(_env_file=None))
        await store.claim_import()
        await store.apply({"supplemental_rules_enabled": True,
                           "supplemental_updates_enabled": enabled}, actor="api")


async def _coordinator(run_id: int) -> str:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(
            worker_id="p4-recovery", kind="supplemental_update_process", run_id=run_id)
        assert run is not None
        job = ClaimedJob(run_id=run.id, kind=run.kind,
                         lease=RunLease(run.id, "p4-recovery", run.attempt_count),
                         request_json=dict(run.request_json))
    return await SupplementalUpdateHandler(settings=Settings(_env_file=None)).execute(job, _InlineContext())


@pytest.mark.parametrize("pair_count", [2, 3])
def test_running_close_and_explicit_resume_preserve_saved_pair_and_only_admit_pending_pair(
    api_client, monkeypatch, pair_count,
):
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    monkeypatch.setenv("SUPPLEMENTAL_UPDATES_ENABLED", "true")
    asyncio.run(_updates_enabled(True))

    async def seed():
        async with session_scope() as session:
            listing = await seed_listing(session, ticker="600871")
            for _ in range(pair_count):
                await activate_rule(session, metric_rule(listing))
            dataset = ResearchDataset(
                kind="filing_index", subject_kind="listing", subject_id=int(listing.id),
                source="local-recovery-fixture", schema_version=1, adapter_version="test",
                revision=1, content_hash="a" * 64, captured_at=datetime.now(UTC), payload={},
            )
            session.add(dataset)
            await session.flush()
            receipt = await SupplementalUpdatesRepository(session).append_change(
                "local_owner", change_key="p4:recovery:local-material", kind="material_revision",
                listing_id=int(listing.id), context="company_research",
                payload={"source_ref": {"kind": "research_dataset", "id": dataset.id, "revision": 1},
                         "content_hash": dataset.content_hash, "contexts": ["company_research"]})
            snapshot = await freeze_target_snapshot(session, receipt)
            run, _ = await PipelineRunRepository(session).enqueue(
                kind="supplemental_update_process", idempotency_key="p4:recovery:coordinator",
                triggered_by="test", request_json={"schema_version": 1, "owner_id": "local_owner",
                    "receipt_sequence": receipt.sequence, "receipt_id": str(receipt.id),
                    "receipt_payload_hash": receipt.payload_hash, "target_snapshot": snapshot})
            return run.id

    coordinator = asyncio.run(seed())
    assert asyncio.run(_coordinator(coordinator)) == "completed"
    batch = api_client.get(f"/supplemental-updates/batches/{coordinator}", headers=API_KEY)
    assert batch.status_code == 200, batch.text
    assert any(entry["kind"] == "scope_handoff" and entry["payload"]["coordinator_run_id"] == coordinator
               for entry in batch.json()["entries"])

    async def p2_run():
        async with session_scope() as session:
            admissions = await SupplementalUpdatesRepository(session).list_changes(
                "local_owner", kinds=("evaluation_admission",), limit=10)
            assert len(admissions) == 1
            return admissions[0].origin_run_id

    run_id = asyncio.run(p2_run())
    assert run_id is not None
    assert _run_worker_paused_after_first_checkpoint(
        run_id, monkeypatch, worker_id="p4-midrun-close",
        after_checkpoint=lambda: _updates_enabled(False)) == run_id
    stopped = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "failed"
    assert [member["state"] for member in stopped.json()["members"]] == ["saved", *(["pending"] * (pair_count - 1))]
    saved_ref = stopped.json()["members"][0]["application_ref"]
    body = {"operation_id": str(uuid4()), "expected_revision": 0, "reason": "resume pending pair after local gate restored"}
    path = f"/supplemental-updates/batches/{run_id}/resume"
    assert api_client.post(path, json=body, headers=API_KEY).status_code == 403
    asyncio.run(_updates_enabled(True))
    resumed = api_client.post(path, json=body, headers=API_KEY)
    assert resumed.status_code == 202, resumed.text
    resumed_id = resumed.json()["run_id"]
    replay = api_client.post(path, json=body, headers=API_KEY)
    assert replay.status_code == 202 and replay.json()["run_id"] == resumed_id

    async def inspect_pending():
        async with session_scope() as session:
            run = await session.get(PipelineRun, resumed_id)
            assert run is not None
            scope = run.request_json["resume_scopes"]
            assert len(scope) == 1 and len(scope[0]["exact_rule_refs"]) == pair_count - 1
            assert scope[0]["saved_application_refs"] == [saved_ref]

    asyncio.run(inspect_pending())
    assert asyncio.run(_coordinator(resumed_id)) == "completed"

    async def pending_run():
        async with session_scope() as session:
            admissions = await SupplementalUpdatesRepository(session).list_changes(
                "local_owner", kinds=("evaluation_admission",), limit=10)
            assert len(admissions) == 2
            row = await session.get(PipelineRun, admissions[-1].origin_run_id)
            assert row is not None
            assert len(row.request_json["source_manifest"]["pairs"]) == pair_count - 1
            body = dict(row.request_json["command_body"])
            original = await session.get(PipelineRun, run_id)
            assert original is not None
            assert row.request_json["source_manifest"]["as_of"] == original.request_json["source_manifest"]["as_of"]
            assert row.request_json["source_manifest"]["rules_generation"] == original.request_json["source_manifest"]["rules_generation"]
            recovered_id = row.id
        for changed in (
            {"pending_ordinals": [0]},  # already saved, never eligible for another evaluation
            {"saved_application_refs": []},
            {"original_manifest_hash": "0" * 64},
            {"member_sequence": 999999},
            {"source_sequence": 999999},
        ):
            with pytest.raises(InvestorError):
                async with session_scope() as session:
                    await SupplementalRuleAdmission(session, settings=Settings(_env_file=None)).accept_update_recovery(
                        UpdateRecoveryCommand.model_validate({**body, **changed, "idempotency_key": str(uuid4())}))
        return recovered_id

    pending_id = asyncio.run(pending_run())
    if pair_count == 3:
        # A recovery generation can itself stop after a committed pair. Its
        # successor must retain the same knowledge window and all earlier work.
        assert _run_worker_paused_after_first_checkpoint(
            pending_id, monkeypatch, worker_id="p4-second-midrun-close",
            after_checkpoint=lambda: _updates_enabled(False)) == pending_id
        child = api_client.get(f"/supplemental-rule-evaluations/runs/{pending_id}", headers=API_KEY)
        assert child.status_code == 200
        assert [member["state"] for member in child.json()["members"]] == ["saved", "pending"]
        asyncio.run(_updates_enabled(True))
        second_resume = api_client.post(f"/supplemental-updates/batches/{pending_id}/resume", headers=API_KEY,
            json={"operation_id": str(uuid4()), "expected_revision": 0, "reason": "second interrupted recovery"})
        assert second_resume.status_code == 202, second_resume.text
        assert asyncio.run(_coordinator(second_resume.json()["run_id"])) == "completed"

        async def final_pending():
            async with session_scope() as session:
                admissions = await SupplementalUpdatesRepository(session).list_changes(
                    "local_owner", kinds=("evaluation_admission",), limit=10)
                assert len(admissions) == 3
                run = await session.get(PipelineRun, admissions[-1].origin_run_id)
                original = await session.get(PipelineRun, run_id)
                assert run is not None and original is not None
                assert len(run.request_json["source_manifest"]["pairs"]) == 1
                assert run.request_json["source_manifest"]["as_of"] == original.request_json["source_manifest"]["as_of"]
                return run.id

        pending_id = asyncio.run(final_pending())
    assert _drain_run(pending_id) == "completed"

    async def verify_saved_once():
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(InvestmentAssessment).where(
                InvestmentAssessment.kind == "rule_application")) == pair_count
            saved = await session.get(InvestmentAssessment, (saved_ref["id"], saved_ref["revision"]))
            assert saved is not None
            prior = await session.get(PipelineRun, run_id)
            assert prior is not None and prior.status == "failed"
            assert prior.metadata_json["supplemental_checkpoint"]["members"][0]["application_ref"] == saved_ref

    asyncio.run(verify_saved_once())
