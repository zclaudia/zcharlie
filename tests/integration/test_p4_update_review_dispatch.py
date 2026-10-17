"""P4 local-result review dispatch gates and exact dirty-batch handoff."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.agent.execution import ExecutionGrant
from investment_agent.agent.model import AgentModelConfig
from investment_agent.agent.tasks import Task, TaskService
from investment_agent.config import Settings
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.common import Mutation, ObjectRef, Quantity
from investment_agent.investor.contracts import ReviewEvent
from investment_agent.investor.enums import StopReason
from investment_agent.investor.foundations import InvestorService
from investment_agent.investor.monitoring import MonitoringService
from investment_agent.jobs.base import ClaimedJob
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import load_settings, store_for
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.publication import PublicationArtifact
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
    RunLease,
    lease_scope,
)
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import Context
from investment_agent.supplemental_rules.jobs import SupplementalRuleHandler
from investment_agent.supplemental_rules.update_intake import (
    ImportPreviewRequest,
    ImportRequest,
    import_saved_sources,
    resolve_saved_sources,
)
from investment_agent.supplemental_rules.update_jobs import (
    SUPPLEMENTAL_UPDATE_KIND,
    SupplementalUpdateHandler,
    settle_update_handoffs,
    validate_review_batch,
)
from investment_agent.supplemental_rules.update_targets import freeze_target_snapshot
from tests.support.conversations import base as conversation_base
from tests.support.investor import saved
from tests.support.publication import metadata
from tests.support.research_closure import (
    activate_rule,
    metric_rule,
    requirement_rule,
    seed_company_view,
    seed_listing,
)


@pytest_asyncio.fixture
async def p4_update_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'p4-review-dispatch.db'}")
    async with engine.begin() as connection:
        from investment_agent.storage.models.base import Base

        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def _prepare_settings(*, priced: bool) -> Settings:
    """Isolate the DB runtime overlay and give tests a controlled model config."""
    settings = conversation_base(investor_agent_background_enabled=True)
    runtime = {
        "investor_agent_enabled": True,
        "investor_agent_background_enabled": True,
        "supplemental_rules_enabled": True,
        "supplemental_updates_enabled": True,
        "agent_model": settings.agent_model,
    }
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(runtime, actor="p4-test")
        if priced:
            effective = await load_settings(session, base=settings)
            model_config = AgentModelConfig.from_settings(effective, 0)
            await store.apply({
                "investor_agent_price_version": "p4-fixture-price-v1",
                "investor_agent_price_model": effective.agent_model,
                "investor_agent_price_config_version": model_config.version,
                "investor_agent_input_usd_per_million": Decimal("1"),
                "investor_agent_output_usd_per_million": Decimal("1"),
            }, actor="p4-test")
    return settings


async def _set_background_authorization(
    *, subject: ObjectRef, budget: Decimal, enabled: bool = True,
) -> None:
    async with session_scope() as session:
        service = InvestorService(session)
        current = await service.get_mandate("local_owner")
        auth = current.authorization.model_copy(update={
            "background_enabled": enabled,
            "exploration_scope_refs": (subject,),
            "daily_budget": Quantity(value=budget, unit="USD"),
            "per_task_budget": Quantity(value=budget, unit="USD"),
            "review_daily_budget": Quantity(value=budget, unit="USD"),
        })
        await service.revise_mandate(
            "local_owner", current.model_copy(update={
                "revision": current.revision + 1, "authorization": auth,
            }), Mutation(expected_revision=current.revision, idempotency_key=uuid4()),
        )


async def _member(*, listing_id: int, context: str, key: str) -> int:
    """Seed one durable P2-local result and its exact scope outcome."""
    repo_session = session_scope()
    async with repo_session as session:
        repo = SupplementalUpdatesRepository(session)
        source_hash = canonical_hash({"source": key, "context": context})
        parent = await repo.append_change(
            "local_owner", change_key=f"receipt:{key}", kind="evidence_revision",
            listing_id=listing_id, context=context,
            payload={"source_refs": [], "semantic_key": key},
        )
        member = await repo.append_change(
            "local_owner", change_key=f"member:{key}", kind="scope_member",
            parent_sequence=parent.sequence, listing_id=listing_id, context=context,
            source_manifest_hash=source_hash,
            payload={"source_kind": "evidence_revision", "semantic_key": key},
        )
        await repo.append_change(
            "local_owner", change_key=f"outcome:{key}", kind="scope_outcome",
            parent_sequence=parent.sequence, listing_id=listing_id, context=context,
            source_manifest_hash=source_hash,
            payload={"member_sequence": member.sequence, "state": "completed",
                     "reason": "saved_result_verified"},
        )
        return member.sequence


async def _dispatch(*, listing_id: int, member_sequence: int, settings: Settings):
    async with session_scope() as session:
        member = await SupplementalUpdatesRepository(session).get_change(
            "local_owner", member_sequence)
        assert member is not None and member.source_manifest_hash
        result = await MonitoringService(session, settings).dispatch_update_scope(
            listing_id=listing_id, context=member.context or "company_research",
            member_sequences=[member_sequence], source_manifest_hash=member.source_manifest_hash,
        )
        if result and result.get("state") == "blocked":
            cycle = await session.get(ReviewCycle, result["cycle_id"])
            result["reason"] = cycle.payload.get("pending_reason") if cycle else None
        return result


async def _counts():
    async with session_scope() as session:
        return {
            "tasks": len((await session.scalars(select(AgentTask))).all()),
            "grants": len((await session.scalars(select(ExecutionGrantRow))).all()),
            "cycles": list((await session.scalars(select(ReviewCycle))).all()),
            "batches": list((await session.scalars(select(SupplementalChangeReceiptRow).where(
                SupplementalChangeReceiptRow.kind == "scope_handoff"))).all()),
        }


async def _import_saved_evidence(
    *, listing_ref: ObjectRef, evidence_ref: ObjectRef, settings: Settings, key: str,
    context: Context = "company_research",
) -> int:
    """Use the real controlled-intake path to persist one P4 source receipt."""
    async with session_scope() as session:
        preview = ImportPreviewRequest(
            listing_ref=listing_ref, context=context, source_refs=(evidence_ref,),
        )
        sources = await resolve_saved_sources(
            session, listing_ref, preview.source_refs, settings=settings,
            now=datetime.now(UTC),
        )
        manifest_hash = canonical_hash({
            "listing_ref": listing_ref, "context": preview.context, "sources": sources,
        })
        scope_key = f"listing:{listing_ref.id}:{preview.context}"
        state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "controlled_local_v1", scope_key,
        )
        request = ImportRequest(
            **preview.model_dump(mode="python"), operation_id=uuid4(),
            manifest_hash=manifest_hash,
            expected_source_revision=state.revision if state else 0,
        )
        result = await import_saved_sources(session, request, settings)
        assert result["state"] == "persisted", (key, result)
        return int(result["receipt"]["sequence"])


async def _enqueue_receipt_coordinator(sequence: int) -> int:
    """Persist the same receipt-bound coordinator request used by journal wakeup."""
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        receipt = await repo.get_change("local_owner", sequence)
        assert receipt is not None
        snapshot = await freeze_target_snapshot(session, receipt)
        run, created = await PipelineRunRepository(session).enqueue(
            kind=SUPPLEMENTAL_UPDATE_KIND,
            idempotency_key=f"p4:u:{receipt.sequence}",
            triggered_by="p4-review-bridge-test",
            request_json={"schema_version": 1, "owner_id": "local_owner",
                          "receipt_sequence": receipt.sequence,
                          "receipt_id": str(receipt.id),
                          "receipt_payload_hash": receipt.payload_hash,
                          "target_snapshot": snapshot},
        )
        assert created
        return run.id


async def _run_local_update_job(settings: Settings, run_id: int) -> int | None:
    """Run only durable P4 and P2 rule jobs; no agent/provider handler is registered."""
    worker = ResearchWorker(
        settings=settings,
        worker_id="p4-review-bridge-test",
        handlers=[SupplementalUpdateHandler(settings=settings),
                  SupplementalRuleHandler(settings=settings)],
        kinds=[SUPPLEMENTAL_UPDATE_KIND, "supplemental_rule_evaluate"],
    )
    completed = await worker.run_once(run_id=run_id)
    if completed is not None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None and run.status == "success", (
                run_id, run.status if run is not None else None,
            )
    return completed


async def _settle_update_handoffs_under_lease(settings: Settings) -> int:
    """Run the real handoff settlement behind an owned, fenced maintenance lease."""
    async with session_scope() as session:
        run, created = await PipelineRunRepository(session).enqueue(
            kind=SUPPLEMENTAL_UPDATE_KIND,
            idempotency_key=f"p4-review-bridge-settle:{uuid4()}",
            triggered_by="p4-review-bridge-test",
            request_json={"schema_version": 1, "owner_id": "local_owner",
                          "action": "settle_only"},
        )
        assert created
        run_id = run.id
    async with session_scope() as session:
        claimed = await PipelineRunRepository(session).claim_next(
            worker_id="p4-review-bridge-settler", kind=SUPPLEMENTAL_UPDATE_KIND,
            run_id=run_id,
        )
        assert claimed is not None
        lease = RunLease(run_id=claimed.id, worker_id="p4-review-bridge-settler",
                         attempt=claimed.attempt_count)
        job = ClaimedJob(
            run_id=claimed.id, kind=claimed.kind, lease=lease,
            request_json=dict(claimed.request_json),
        )
    with lease_scope(lease):
        async with session_scope() as session:
            settled = await settle_update_handoffs(
                session, settings=settings, limit=50, job=job,
            )
            await PipelineRunRepository(session).mark_success(
                run_id, {"settled_count": settled}, lease=lease,
            )
    return run_id


async def _changed_evidence_ref(listing_ref: ObjectRef, prior_ref: ObjectRef) -> ObjectRef:
    """Add a distinct exact saved evidence version with changed immutable content."""
    async with session_scope() as session:
        prior_row = await session.get(SavedEvidence, (str(prior_ref.id), prior_ref.revision))
        assert prior_row is not None
        prior = EvidenceRecord.model_validate(prior_row.payload)
        new_id = uuid4()
        ref = ObjectRef(kind="evidence", id=new_id, revision=1)
        record = prior.model_copy(update={
            "id": new_id,
            "source_ref": ref,
            "locator": f"p4-review-bridge-{listing_ref.id}-revision-2",
            "content_hash": canonical_hash({"prior": prior.content_hash, "revision": 2}),
            "text": "A separately saved local evidence revision for the review bridge.",
        })
        session.add(SavedEvidence(
            id=str(new_id), revision=1, owner_id="local_owner",
            payload=record.model_dump(mode="json"),
        ))
        await session.flush()
        return ref


async def _make_local_source_stale(listing_ref: ObjectRef) -> None:
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        scope_key = f"listing:{listing_ref.id}:company_research"
        state = await repo.get_source("local_owner", "controlled_local_v1", scope_key)
        assert state is not None
        await repo.save_source(state.model_copy(update={
            "revision": state.revision + 1,
            "last_success_at": datetime.now(UTC) - timedelta(days=2),
        }), expected_revision=state.revision)


async def _p2_handoff_for_member(member_sequence: int) -> int:
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        receipts = await repo.list_changes(
            "local_owner", after_sequence=0, limit=100,
            kinds=("scope_handoff",),
        )
        matching = [receipt for receipt in receipts
                    if receipt.payload.get("member_sequences") == [member_sequence]]
        assert matching
        run_id = matching[-1].payload.get("p2_run_id")
        assert isinstance(run_id, int)
        return run_id


@pytest.mark.asyncio
async def test_local_result_with_case_needs_background_authorization_price_and_nonzero_budget(p4_update_db):
    _, _, subject, _ = await saved()
    listing_id = int(subject.id)
    member = await _member(listing_id=listing_id, context="company_research", key="no-auth")
    settings = await _prepare_settings(priced=True)
    await _set_background_authorization(subject=subject, budget=Decimal("5"), enabled=False)
    baseline = await _counts()
    result = await _dispatch(listing_id=listing_id, member_sequence=member, settings=settings)
    counts = await _counts()
    assert result and result["state"] == "blocked"
    assert "background_disabled" in result["reason"], result
    assert counts["tasks"] == baseline["tasks"]
    assert counts["grants"] == baseline["grants"]


@pytest.mark.asyncio
async def test_blocked_unattempted_batch_retries_after_authorization_but_terminal_is_not_revived(
    p4_update_db,
):
    _, _, subject, _ = await saved()
    listing_id = int(subject.id)
    member = await _member(
        listing_id=listing_id, context="company_research", key="authorization-retry",
    )
    settings = await _prepare_settings(priced=True)
    await _set_background_authorization(
        subject=subject, budget=Decimal("5"), enabled=False,
    )
    baseline = await _counts()
    blocked = await _dispatch(
        listing_id=listing_id, member_sequence=member, settings=settings,
    )
    assert blocked and blocked["state"] == "blocked"
    assert (await _counts())["tasks"] == baseline["tasks"]
    assert (await _counts())["grants"] == baseline["grants"]

    await _set_background_authorization(
        subject=subject, budget=Decimal("5"), enabled=True,
    )
    retried = await _dispatch(
        listing_id=listing_id, member_sequence=member, settings=settings,
    )
    assert retried and retried["state"] == "running"
    assert retried["cycle_id"] == blocked["cycle_id"]
    assert retried["task_id"]
    counts = await _counts()
    assert counts["tasks"] == baseline["tasks"] + 1
    assert counts["grants"] == baseline["grants"] + 1

    task_id = UUID(retried["task_id"])
    async with session_scope() as session:
        await TaskService(session).cancel(task_id)
    terminal_counts = await _counts()
    replay = await _dispatch(
        listing_id=listing_id, member_sequence=member, settings=settings,
    )
    assert replay and replay["cycle_id"] == retried["cycle_id"]
    assert replay["task_id"] == str(task_id)
    after = await _counts()
    assert after["tasks"] == terminal_counts["tasks"]
    assert after["grants"] == terminal_counts["grants"]


@pytest.mark.asyncio
async def test_authorized_local_result_without_verified_price_creates_no_task(p4_update_db):
    _, _, subject, _ = await saved()
    listing_id = int(subject.id)
    await _set_background_authorization(subject=subject, budget=Decimal("5"))
    member = await _member(listing_id=listing_id, context="company_research", key="no-price")
    settings = await _prepare_settings(priced=False)
    baseline = await _counts()
    result = await _dispatch(listing_id=listing_id, member_sequence=member, settings=settings)
    counts = await _counts()
    assert result and result["state"] == "blocked"
    assert "background_price_unavailable" in result["reason"], result
    assert counts["tasks"] == baseline["tasks"]
    assert counts["grants"] == baseline["grants"]


@pytest.mark.asyncio
async def test_authorized_zero_budget_local_result_does_not_create_paid_task(p4_update_db):
    _, _, subject, _ = await saved()
    listing_id = int(subject.id)
    await _set_background_authorization(subject=subject, budget=Decimal("0"))
    member = await _member(listing_id=listing_id, context="company_research", key="zero-budget")
    settings = await _prepare_settings(priced=True)
    baseline = await _counts()
    result = await _dispatch(listing_id=listing_id, member_sequence=member, settings=settings)
    counts = await _counts()
    assert result and result["state"] == "blocked"
    assert "budget" in result["reason"], result
    assert counts["tasks"] == baseline["tasks"]
    assert counts["grants"] == baseline["grants"]


@pytest.mark.asyncio
async def test_authorized_review_task_and_execution_grant_bind_exact_p4_batch(p4_update_db):
    _, _, subject, _ = await saved()
    listing_id = int(subject.id)
    await _set_background_authorization(subject=subject, budget=Decimal("5"))
    member = await _member(listing_id=listing_id, context="company_research", key="authorized")
    settings = await _prepare_settings(priced=True)
    result = await _dispatch(listing_id=listing_id, member_sequence=member, settings=settings)
    assert result and result["state"] == "running" and result["task_id"] and result["batch_id"], result
    task_id = UUID(result["task_id"])
    batch_id = UUID(result["batch_id"])
    async with session_scope() as session:
        task = await session.get(AgentTask, str(task_id))
        grant_row = await session.get(ExecutionGrantRow, str(task_id))
        assert task is not None and grant_row is not None
        grant = ExecutionGrant.model_validate(grant_row.payload)
        assert grant.mode == "background" and grant.research_kind == "review"
        assert grant.supplemental_update_batch_id == batch_id
        assert grant.supplemental_update_batch_hash
        batch = await SupplementalUpdatesRepository(session).get_change("local_owner", batch_id)
        assert batch is not None and batch.kind == "scope_handoff"
        assert batch.payload_hash == grant.supplemental_update_batch_hash
        assert batch.payload["member_sequences"] == [member]
        from investment_agent.investor.foundations import InvestorError

        with pytest.raises(InvestorError, match="supplemental_update_batch"):
            await validate_review_batch(
                session, owner_id="local_owner", batch_id=batch_id,
                batch_hash=batch.payload_hash, task_id=task_id,
            )
    counts = await _counts()
    assert counts["tasks"] == counts["grants"] == 2
    assert len(counts["batches"]) == 1


@pytest.mark.asyncio
async def test_real_import_p4_coordinator_p2_worker_settlement_dispatches_verified_review(
    p4_update_db,
):
    """A provider-free worker proves the actual receipt-to-paid-review bridge."""
    settings = await _prepare_settings(priced=True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600199")
        await activate_rule(session, metric_rule(listing_ref))
        await activate_rule(session, requirement_rule(listing_ref))
        case, first_ref = await seed_company_view(session, listing_ref)
    await _set_background_authorization(
        subject=listing_ref, budget=Decimal("5"), enabled=True,
    )
    baseline = await _counts()

    first_receipt_sequence = await _import_saved_evidence(
        listing_ref=listing_ref, evidence_ref=first_ref,
        settings=settings, key="first-real-import",
    )
    first_coordinator = await _enqueue_receipt_coordinator(first_receipt_sequence)
    assert await _run_local_update_job(settings, first_coordinator) == first_coordinator
    first_member = None
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_change("local_owner", first_receipt_sequence)
        assert source is not None
        members = await repo.list_changes(
            "local_owner", after_sequence=0, limit=100,
            kinds=("scope_member",), listing_id=int(listing_ref.id),
            context="company_research",
        )
        first_member = next(
            row for row in members
            if row.payload.get("source_sequence") == first_receipt_sequence
        )
    first_p2_run = await _p2_handoff_for_member(first_member.sequence)
    assert await _run_local_update_job(settings, first_p2_run) == first_p2_run
    await _settle_update_handoffs_under_lease(settings)

    # A later successful source check with the same exact saved refs/content
    # advances the source receipt but must not buy another review.
    await _make_local_source_stale(listing_ref)
    same_source_sequence = await _import_saved_evidence(
        listing_ref=listing_ref, evidence_ref=first_ref,
        settings=settings, key="same-source-revalidation",
    )
    same_source_coordinator = await _enqueue_receipt_coordinator(same_source_sequence)
    assert await _run_local_update_job(settings, same_source_coordinator) == same_source_coordinator
    async with session_scope() as session:
        members = await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", after_sequence=first_member.sequence, limit=100,
            kinds=("scope_member",), listing_id=int(listing_ref.id),
            context="company_research",
        )
        same_source_member = next(
            row for row in members
            if row.payload.get("source_sequence") == same_source_sequence
        )
    same_source_p2 = await _p2_handoff_for_member(same_source_member.sequence)
    assert await _run_local_update_job(settings, same_source_p2) == same_source_p2
    await _settle_update_handoffs_under_lease(settings)
    same_source_counts = await _counts()
    assert same_source_counts["tasks"] == baseline["tasks"]
    assert same_source_counts["grants"] == baseline["grants"]

    # Successful local P2 updates in candidate/account_fit never become
    # investor company/holding review cycles.
    for context in ("candidate", "account_fit"):
        local_sequence = await _import_saved_evidence(
            listing_ref=listing_ref, evidence_ref=first_ref, settings=settings,
            key=f"{context}-import", context=context,
        )
        coordinator = await _enqueue_receipt_coordinator(local_sequence)
        assert await _run_local_update_job(settings, coordinator) == coordinator
        async with session_scope() as session:
            members = await SupplementalUpdatesRepository(session).list_changes(
                "local_owner", after_sequence=same_source_member.sequence, limit=100,
                kinds=("scope_member",), listing_id=int(listing_ref.id), context=context,
            )
            local_member = next(
                row for row in members
                if row.payload.get("source_sequence") == local_sequence
            )
        p2_run = await _p2_handoff_for_member(local_member.sequence)
        assert await _run_local_update_job(settings, p2_run) == p2_run
        await _settle_update_handoffs_under_lease(settings)
        local_counts = await _counts()
        assert local_counts["tasks"] == baseline["tasks"]
        assert local_counts["grants"] == baseline["grants"]
        async with session_scope() as session:
            outcome = await SupplementalUpdatesRepository(session).get_latest_member_outcome(
                "local_owner", local_member.sequence,
            )
            assert outcome is not None and outcome.payload["state"] == "completed"

    second_ref = await _changed_evidence_ref(listing_ref, first_ref)
    second_receipt_sequence = await _import_saved_evidence(
        listing_ref=listing_ref, evidence_ref=second_ref,
        settings=settings, key="second-real-import",
    )
    second_coordinator = await _enqueue_receipt_coordinator(second_receipt_sequence)
    assert await _run_local_update_job(settings, second_coordinator) == second_coordinator
    async with session_scope() as session:
        members = await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", after_sequence=same_source_member.sequence, limit=100,
            kinds=("scope_member",), listing_id=int(listing_ref.id),
            context="company_research",
        )
        second_member = next(
            row for row in members
            if row.payload.get("source_sequence") == second_receipt_sequence
        )
    second_p2_run = await _p2_handoff_for_member(second_member.sequence)
    assert await _run_local_update_job(settings, second_p2_run) == second_p2_run
    await _settle_update_handoffs_under_lease(settings)

    counts = await _counts()
    review_cycles = [
        cycle for cycle in counts["cycles"]
        if cycle.case_id == str(case.id) and cycle.task_id
        and cycle.payload.get("supplemental_update_member_sequences") == [second_member.sequence]
    ]
    assert len(review_cycles) == 1
    cycle = review_cycles[0]
    assert cycle.state == "running"
    task_id = UUID(cycle.task_id)
    async with session_scope() as session:
        task = await session.get(AgentTask, str(task_id))
        grant_row = await session.get(ExecutionGrantRow, str(task_id))
        assert task is not None and grant_row is not None
        grant = ExecutionGrant.model_validate(grant_row.payload)
        assert grant.mode == "background" and grant.research_kind == "review"
        assert grant.supplemental_update_batch_id
        batch = await SupplementalUpdatesRepository(session).get_change(
            "local_owner", grant.supplemental_update_batch_id,
        )
        assert batch is not None and batch.kind == "scope_handoff"
        assert batch.payload["member_sequences"] == [second_member.sequence]
        outcome = await SupplementalUpdatesRepository(session).get_latest_member_outcome(
            "local_owner", second_member.sequence,
        )
        assert outcome is not None and outcome.payload["state"] == "completed"
        assert outcome.payload["run_id"] == second_p2_run
        assert outcome.source_manifest_hash
        assert batch.payload["member_source_manifest_hashes"] == {
            str(second_member.sequence): outcome.source_manifest_hash,
        }
        from investment_agent.supplemental_rules.jobs import authorize_supplemental_run

        _, authorized = await authorize_supplemental_run(session, second_p2_run)
        assert outcome.source_manifest_hash == authorized.manifest.manifest_hash
        assert batch.payload_hash == grant.supplemental_update_batch_hash
        await validate_review_batch(
            session, owner_id="local_owner", batch_id=batch.id,
            batch_hash=batch.payload_hash, task_id=task_id,
        )

    # Only the second, semantically changed source caused a review admission.
    assert counts["tasks"] == baseline["tasks"] + 1
    assert counts["grants"] == baseline["grants"] + 1


async def _finish_task_with_review(task_id: UUID, recommendation_ref: ObjectRef) -> None:
    from investment_agent.agent.contracts import TaskStatus

    now = metadata()["created_at"]
    async with session_scope() as session:
        row = await session.get(AgentTask, str(task_id))
        assert row is not None
        task = Task.model_validate(row.payload)
        review = ReviewEvent(**metadata(), trigger_refs=(), recommendation_ref=recommendation_ref,
            evidence_diff_refs=(), result="maintain", reasoning_revision=False,
            new_recommendation_ref=None, rationale="Fixture review settled",
            due_at=now, started_at=now, completed_at=now)
        review_ref = ObjectRef(kind="review", id=review.id, revision=1)
        case_id = await session.scalar(select(ReviewCycle.case_id).where(ReviewCycle.task_id == str(task_id)))
        session.add(PublicationArtifact(id=str(review.id), revision=1, kind="review",
            owner_id="local_owner", case_id=case_id, payload=review.model_dump(mode="json")))
        row.payload = task.model_copy(update={"status": TaskStatus.SUCCEEDED,
            "artifact_refs": (review_ref,), "stop_reason": StopReason.ANSWERED}).model_dump(mode="json")
        row.status = "succeeded"
        await session.flush()


@pytest.mark.asyncio
async def test_successful_cycle_reissues_exact_dirty_members_in_new_batch(p4_update_db):
    _, _, subject, published = await saved()
    listing_id = int(subject.id)
    await _set_background_authorization(subject=subject, budget=Decimal("5"))
    settings = await _prepare_settings(priced=True)
    first_member = await _member(listing_id=listing_id, context="company_research", key="first")
    first = await _dispatch(listing_id=listing_id, member_sequence=first_member, settings=settings)
    assert first and first["state"] == "running"
    baseline = await _counts()
    first_task = UUID(first["task_id"])
    second_member = await _member(listing_id=listing_id, context="company_research", key="dirty")
    dirty = await _dispatch(listing_id=listing_id, member_sequence=second_member, settings=settings)
    assert dirty and dirty["state"] == "running" and dirty["task_id"] == str(first_task)
    async with session_scope() as session:
        cycle = await session.scalar(select(ReviewCycle).where(ReviewCycle.task_id == str(first_task)))
        assert cycle is not None
        assert cycle.payload["dirty_update_member_sequences"] == [second_member]
    await _finish_task_with_review(first_task, published.recommendation_ref)
    async with session_scope() as session:
        assert await MonitoringService(session, settings).settle() == 1
    cycles = (await _counts())["cycles"]
    downstream = [cycle for cycle in cycles if cycle.id != first["cycle_id"]]
    assert len(downstream) == 1
    next_cycle = downstream[0]
    assert next_cycle.state == "running" and next_cycle.task_id
    assert next_cycle.payload["supplemental_update_member_sequences"] == [second_member]
    async with session_scope() as session:
        next_grant_row = await session.get(ExecutionGrantRow, next_cycle.task_id)
        assert next_grant_row is not None
        next_grant = ExecutionGrant.model_validate(next_grant_row.payload)
        assert next_grant.supplemental_update_batch_id
        next_batch = await SupplementalUpdatesRepository(session).get_change(
            "local_owner", next_grant.supplemental_update_batch_id)
        assert next_batch is not None and next_batch.payload["member_sequences"] == [second_member]
        from investment_agent.investor.foundations import InvestorError

        with pytest.raises(InvestorError, match="supplemental_update_batch"):
            await validate_review_batch(
                session, owner_id="local_owner", batch_id=next_batch.id,
                batch_hash=next_batch.payload_hash, task_id=UUID(next_cycle.task_id),
            )
    assert (await _counts())["tasks"] == baseline["tasks"] + 1


@pytest.mark.asyncio
async def test_same_member_replay_does_not_mark_dirty_or_duplicate_terminal_cycle(p4_update_db):
    _, _, subject, published = await saved()
    listing_id = int(subject.id)
    await _set_background_authorization(subject=subject, budget=Decimal("5"))
    settings = await _prepare_settings(priced=True)
    member = await _member(listing_id=listing_id, context="company_research", key="replay")
    first = await _dispatch(listing_id=listing_id, member_sequence=member, settings=settings)
    assert first and first["state"] == "running"
    first_task = UUID(first["task_id"])
    baseline = await _counts()

    replay_running = await _dispatch(
        listing_id=listing_id, member_sequence=member, settings=settings)
    assert replay_running and replay_running["cycle_id"] == first["cycle_id"]
    assert replay_running["task_id"] == str(first_task)
    async with session_scope() as session:
        cycle = await session.scalar(select(ReviewCycle).where(
            ReviewCycle.task_id == str(first_task)))
        assert cycle is not None
        assert cycle.payload.get("dirty_update_member_sequences", []) == []
    running_counts = await _counts()
    assert running_counts["tasks"] == baseline["tasks"]
    assert running_counts["grants"] == baseline["grants"]
    assert len(running_counts["batches"]) == len(baseline["batches"])

    await _finish_task_with_review(first_task, published.recommendation_ref)
    async with session_scope() as session:
        assert await MonitoringService(session, settings).settle() == 1
    replay_terminal = await _dispatch(
        listing_id=listing_id, member_sequence=member, settings=settings)
    assert replay_terminal and replay_terminal["cycle_id"] == first["cycle_id"]
    assert replay_terminal["task_id"] == str(first_task)
    after = await _counts()
    assert after["tasks"] == baseline["tasks"]
    assert after["grants"] == baseline["grants"]
    assert len(after["batches"]) == len(baseline["batches"])


@pytest.mark.asyncio
async def test_canceled_cycle_keeps_dirty_members_without_dispatching_paid_followup(p4_update_db):
    _, _, subject, _ = await saved()
    listing_id = int(subject.id)
    await _set_background_authorization(subject=subject, budget=Decimal("5"))
    settings = await _prepare_settings(priced=True)
    first_member = await _member(listing_id=listing_id, context="company_research", key="cancel-first")
    first = await _dispatch(listing_id=listing_id, member_sequence=first_member, settings=settings)
    assert first and first["state"] == "running"
    baseline = await _counts()
    task_id = UUID(first["task_id"])
    dirty_member = await _member(listing_id=listing_id, context="company_research", key="cancel-dirty")
    await _dispatch(listing_id=listing_id, member_sequence=dirty_member, settings=settings)
    async with session_scope() as session:
        await TaskService(session).cancel(task_id)
        assert await MonitoringService(session, settings).settle() == 1
    counts = await _counts()
    stopped = next(cycle for cycle in counts["cycles"] if cycle.id == first["cycle_id"])
    assert stopped.state == "stopped"
    assert stopped.payload["dirty_update_member_sequences"] == [dirty_member]
    assert counts["tasks"] == baseline["tasks"]
    assert counts["grants"] == baseline["grants"]
    assert len(counts["batches"]) == 1
