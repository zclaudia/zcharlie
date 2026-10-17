"""Durable P4 coordinator and lease-fencing acceptance tests."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from investment_agent.config import Settings
from investment_agent.jobs.base import ClaimedJob
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.research_material import ResearchDataset, ResearchSourceBinding
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.admission import SupplementalRuleAdmission
from investment_agent.supplemental_rules.contracts import EvaluationRequest
from investment_agent.supplemental_rules.update_jobs import (
    SupplementalUpdateHandler,
    _origin_run_state,
    _require_lease,
    _scan_due_boundaries,
    enqueue_pending_updates,
    settle_update_handoffs,
)
from investment_agent.supplemental_rules.update_targets import freeze_target_snapshot, target_scopes
from tests.support.research_closure import activate_rule, metric_rule, seed_listing


class _InlineContext:
    worker_id = "p4-engine-test"

    async def run_with_heartbeat(self, lease, work):
        return await work()


@pytest_asyncio.fixture
async def p4_engine_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'p4-engine.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "p4-engine-test-key")
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="p4-engine-test-key",
        data_dir=tmp_path,
        supplemental_rules_enabled=True,
        supplemental_updates_enabled=True,
    )


async def _claim(run_id: int) -> ClaimedJob:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).claim_next(
            worker_id="p4-engine-test", kind="supplemental_update_process", run_id=run_id,
        )
        assert row is not None
        return ClaimedJob(
            run_id=row.id, kind=row.kind,
            lease=RunLease(row.id, "p4-engine-test", row.attempt_count),
            request_json=dict(row.request_json),
        )


@pytest.mark.asyncio
async def test_real_worker_dispatches_frozen_company_scope_over_100_listing_pages(
    p4_engine_db, tmp_path: Path,
):
    settings = _settings(tmp_path)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P4 paged company")
        session.add(company)
        await session.flush()
        listings = [
            Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
            for index in range(1, 103)
        ]
        session.add_all(listings)
        await session.flush()
        dataset = ResearchDataset(
            kind="filing_index", subject_kind="company", subject_id=company.id,
            source="local-fixture", schema_version=1, adapter_version="test", revision=1,
            content_hash="c" * 64, captured_at=datetime.now(UTC), payload={},
        )
        session.add(dataset)
        await session.flush()
        receipt = await SupplementalUpdatesRepository(session).append_change(
            "local_owner", change_key="p4-worker-company-source", kind="material_revision",
            payload={"source_ref": {"kind": "research_dataset", "id": dataset.id, "revision": 1},
                     "content_hash": dataset.content_hash,
                     "contexts": ["company_research"]},
        )
        snapshot = await freeze_target_snapshot(session, receipt)
        assert len(snapshot["listing_ids"]) == 102
        assert snapshot["complete"] is True
        queued, created = await PipelineRunRepository(session).enqueue(
            kind="supplemental_update_process", idempotency_key="p4:engine:first-page",
            triggered_by="p4-engine-test",
            request_json={"schema_version": 1, "owner_id": "local_owner",
                          "receipt_sequence": receipt.sequence,
                          "receipt_id": str(receipt.id),
                          "receipt_payload_hash": receipt.payload_hash,
                          "target_snapshot": snapshot},
        )
        assert created
        first_run_id = queued.id

    handler = SupplementalUpdateHandler(settings=settings)
    first_job = await _claim(first_run_id)
    assert await handler.execute(first_job, _InlineContext()) == "completed"

    async with session_scope() as session:
        members_before = int(await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.kind == "scope_member")) or 0)
        assert members_before == 100
        next_run = await session.scalar(select(PipelineRun).where(
            PipelineRun.kind == "supplemental_update_process",
            PipelineRun.idempotency_key == f"p4:u:{first_job.request_json['receipt_sequence']}:o:100",
        ))
        assert next_run is not None and next_run.status == "queued"
        second_run_id = next_run.id

    second_job = await _claim(second_run_id)
    assert await handler.execute(second_job, _InlineContext()) == "completed"
    async with session_scope() as session:
        members = (await session.scalars(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.kind == "scope_member",
        ).order_by(SupplementalChangeReceiptRow.listing_id))).all()
        assert len(members) == 102
        assert [row.listing_id for row in members] == list(range(1, 103))
        assert len({row.listing_id for row in members}) == 102
        repo = SupplementalUpdatesRepository(session)
        for member in members:
            refresh = await repo.get_refresh("local_owner", member.listing_id, member.context)
            assert refresh is not None and refresh.active_run_id is not None
            active = await session.get(PipelineRun, refresh.active_run_id)
            assert active is not None
            assert active.request_json["source_manifest"]["listing_refs"] == [
                {"kind": "listing", "id": member.listing_id, "revision": 1, "field_path": None}]
        assert int(await session.scalar(select(func.count()).select_from(PipelineRun).where(
            PipelineRun.kind == "supplemental_rule_evaluate",
        )) or 0) == 102


@pytest.mark.asyncio
async def test_old_attempt_cannot_write_a_p4_receipt_after_lease_fencing(
    p4_engine_db,
):
    async with session_scope() as session:
        run, created = await PipelineRunRepository(session).enqueue(
            kind="supplemental_update_process", idempotency_key="p4:engine:stale-lease",
            triggered_by="p4-engine-test",
            request_json={"schema_version": 1, "owner_id": "local_owner", "action": "maintenance"},
        )
        assert created
        run_id = run.id
    stale_job = await _claim(run_id)
    async with session_scope() as competing:
        await competing.execute(update(PipelineRun).where(PipelineRun.id == run_id).values(
            worker_id="replacement-worker", attempt_count=stale_job.lease.attempt + 1,
        ))
    with pytest.raises(LeaseLostError):
        async with session_scope() as session:
            await _require_lease(session, stale_job)
            await SupplementalUpdatesRepository(session).append_change(
                "local_owner", change_key="must-not-persist", kind="bootstrap",
                payload={"schema_version": 1},
            )
    async with session_scope() as session:
        row = await session.scalar(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.change_key == "must-not-persist",
        ))
        assert row is None


@pytest.mark.asyncio
async def test_native_p2_origin_is_adopted_for_only_its_frozen_scope(
    p4_engine_db, tmp_path: Path,
):
    settings = _settings(tmp_path)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600121")
        rule = await activate_rule(session, metric_rule(listing_ref))
        request = EvaluationRequest(
            idempotency_key=__import__("uuid").uuid4(),
            listing_refs=(listing_ref,), contexts=frozenset({"company_research"}),
            mode="live",
        )
        admission = await SupplementalRuleAdmission(
            session, settings=settings, owner_id="local_owner",
        ).accept_evaluation(request)
        run_id = int(admission.run_ref.id)
        repo = SupplementalUpdatesRepository(session)
        source = await repo.append_change(
            "local_owner", change_key="p4-native-origin-fixture",
            kind="rule_lifecycle", origin_run_id=run_id,
            source_manifest_hash=admission.manifest_hash,
            payload={"source_ref": {"kind": "supplemental_rule", "id": str(rule.id),
                                     "revision": rule.revision}, "state": "active"},
        )
        state, adopted_run, covered = await _origin_run_state(
            session, source, [(int(listing_ref.id), "company_research"),
                              (int(listing_ref.id), "holding_review")],
        )
        assert state == "waiting_origin"
        assert adopted_run == run_id
        assert covered == {(int(listing_ref.id), "company_research")}
        assert int(await session.scalar(select(func.count()).select_from(PipelineRun).where(
            PipelineRun.kind == "supplemental_rule_evaluate",
        )) or 0) == 1


@pytest.mark.asyncio
async def test_queue_capacity_keeps_unprocessed_journal_cursor(
    p4_engine_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    async def no_due_scan(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        "investment_agent.supplemental_rules.update_jobs._scan_due_boundaries",
        no_due_scan,
    )
    settings = _settings(tmp_path)
    async with session_scope() as session:
        receipt = await SupplementalUpdatesRepository(session).append_change(
            "local_owner", change_key="p4:capacity:receipt", kind="bootstrap",
            payload={"schema_version": 1, "bootstrap_initial": False},
        )
        coordinator, created = await PipelineRunRepository(session).enqueue(
            kind="supplemental_update_process", idempotency_key="p4:capacity:maintenance",
            triggered_by="p4-engine-test",
            request_json={"schema_version": 1, "owner_id": "local_owner", "action": "maintenance"},
        )
        assert created
        maintenance_id = coordinator.id
        for index in range(127):
            _, queued = await PipelineRunRepository(session).enqueue(
                kind="supplemental_update_process", idempotency_key=f"p4:capacity:queued:{index}",
                triggered_by="p4-engine-test",
                request_json={"schema_version": 1, "owner_id": "local_owner",
                              "receipt_sequence": receipt.sequence},
            )
            assert queued
    job = await _claim(maintenance_id)
    async with session_scope() as session:
        assert await enqueue_pending_updates(
            session, settings=settings, limit=1, job=job,
        ) == 0
        cursor = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "local_journal_dispatch", "all",
        )
        assert cursor is not None
        assert cursor.payload["after_sequence"] == 0
        assert cursor.payload["capacity_blocked"] is True
        assert cursor.payload["blocked_from_sequence"] == receipt.sequence


@pytest.mark.asyncio
async def test_due_binding_sweep_is_keyset_paged_and_idempotent(
    p4_engine_db, tmp_path: Path,
):
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P4 due bindings")
        session.add(company)
        await session.flush()
        listings = [
            Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
            for index in range(1, 106)
        ]
        session.add_all(listings)
        await session.flush()
        today = date.today()
        session.add_all([
            ResearchSourceBinding(
                subject_kind="listing", subject_id=int(listing.id), source="fixture",
                source_symbol=f"symbol-{index}", valid_from=date(2020, 1, 1),
                valid_to=today - timedelta(days=1), verified_at=datetime.now(UTC),
                verified_by="test", payload={},
            )
            for index, listing in enumerate(listings)
        ])
        run, created = await PipelineRunRepository(session).enqueue(
            kind="supplemental_update_process", idempotency_key="p4:due:maintenance",
            triggered_by="p4-engine-test",
            request_json={"schema_version": 1, "owner_id": "local_owner", "action": "maintenance"},
        )
        assert created
        run_id = run.id
    job = await _claim(run_id)
    for _ in range(3):
        async with session_scope() as session:
            await _scan_due_boundaries(
                session, SupplementalUpdatesRepository(session), owner_id="local_owner",
                limit=50, job=job,
            )
    async with session_scope() as session:
        due_count = int(await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.kind == "due_boundary")) or 0)
        assert due_count == 105
    # A complete second pass uses deterministic due keys and adds nothing.
    for _ in range(3):
        async with session_scope() as session:
            await _scan_due_boundaries(
                session, SupplementalUpdatesRepository(session), owner_id="local_owner",
                limit=50, job=job,
            )
    async with session_scope() as session:
        due_count = int(await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.kind == "due_boundary")) or 0)
        assert due_count == 105


@pytest.mark.asyncio
async def test_company_due_boundary_freezes_all_105_targets_in_two_pages(
    p4_engine_db,
):
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P4 due company fanout")
        session.add(company)
        await session.flush()
        listings = [
            Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
            for index in range(1, 106)
        ]
        session.add_all(listings)
        await session.flush()
        binding = ResearchSourceBinding(
            subject_kind="company", subject_id=company.id, source="fixture",
            source_symbol="issuer-source", valid_from=date(2020, 1, 1),
            valid_to=date.today() - timedelta(days=1), verified_at=datetime.now(UTC),
            verified_by="test", payload={},
        )
        session.add(binding)
        await session.flush()
        due_at = datetime.combine(binding.valid_to, datetime.min.time(), tzinfo=UTC)
        receipt = await SupplementalUpdatesRepository(session).append_change(
            "local_owner", change_key="p4:due:company-binding:one", kind="due_boundary",
            available_from=due_at,
            payload={"schema_version": 1,
                     "source_ref": {"kind": "research_source_binding", "id": binding.id,
                                    "revision": 1},
                     "boundary": "material_binding_valid_to", "due_at": due_at.isoformat()},
        )
        snapshot = await freeze_target_snapshot(session, receipt)
        first, complete_first = await target_scopes(
            session, receipt, offset=0, target_snapshot=snapshot,
        )
        second, complete_second = await target_scopes(
            session, receipt, offset=100, target_snapshot=snapshot,
        )
        assert len(first) == 400
        assert complete_first is False
        assert len(second) == 20
        assert complete_second is True
        assert {listing_id for listing_id, _ in first + second} == set(range(1, 106))


@pytest.mark.asyncio
async def test_handoff_settlement_cursor_reaches_more_than_100_manual_rows(
    p4_engine_db, tmp_path: Path,
):
    settings = _settings(tmp_path)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P4 handoff fairness")
        session.add(company)
        await session.flush()
        listings = [
            Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
            for index in range(1, 106)
        ]
        session.add_all(listings)
        await session.flush()
        repo = SupplementalUpdatesRepository(session)
        parent = await repo.append_change(
            "local_owner", change_key="p4:handoff:fairness:source", kind="bootstrap",
            payload={"schema_version": 1},
        )
        for listing in listings:
            member = await repo.append_change(
                "local_owner", change_key=f"p4:handoff:fairness:member:{listing.id}",
                kind="scope_member", parent_sequence=parent.sequence,
                listing_id=int(listing.id), context="company_research",
                payload={"source_sequence": parent.sequence, "source_kind": "bootstrap"},
            )
            await repo.append_change(
                "local_owner", change_key=f"p4:handoff:fairness:row:{listing.id}",
                kind="scope_handoff", parent_sequence=parent.sequence,
                listing_id=int(listing.id), context="company_research",
                payload={"state": "needs_manual", "p2_run_id": None,
                         "member_sequences": [member.sequence]},
            )
        coordinator, created = await PipelineRunRepository(session).enqueue(
            kind="supplemental_update_process", idempotency_key="p4:handoff:fairness:maintenance",
            triggered_by="p4-engine-test",
            request_json={"schema_version": 1, "owner_id": "local_owner", "action": "maintenance"},
        )
        assert created
        coordinator_id = coordinator.id
    job = await _claim(coordinator_id)
    async with session_scope() as session:
        assert await settle_update_handoffs(session, settings=settings, limit=100, job=job) == 100
        count = int(await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.kind == "scope_outcome")) or 0)
        assert count == 100
    async with session_scope() as session:
        assert await settle_update_handoffs(session, settings=settings, limit=100, job=job) == 5
        count = int(await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.kind == "scope_outcome")) or 0)
        assert count == 105

