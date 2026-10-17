from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.common import ObjectRef
from investment_agent.jobs.base import ClaimedJob
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRuleHead,
    SupplementalRuleRevision,
)
from investment_agent.storage.repositories.pipeline_run import RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.update_bootstrap import scan_saved_inputs


async def _leased_job(db_session) -> ClaimedJob:
    now = datetime.now(UTC)
    run = PipelineRun(
        kind="supplemental_update_process", triggered_by="test", started_at=now,
        status="running", stage="claimed", idempotency_key="bootstrap-test",
        attempt_count=1, max_attempts=3, request_json={}, worker_id="bootstrap-test",
        claimed_at=now, heartbeat_at=now, metadata_json={},
    )
    db_session.add(run)
    await db_session.flush()
    return ClaimedJob(
        run_id=run.id, kind=run.kind,
        lease=RunLease(run.id, "bootstrap-test", 1), request_json={},
    )


async def _listing(db_session, ticker: str = "600001") -> Listing:
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name=f"Bootstrap {ticker}")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker=ticker)
    db_session.add(listing)
    await db_session.flush()
    return listing


async def _expire_audit(repo: SupplementalUpdatesRepository, feed_id: str, scope_key: str) -> None:
    state = await repo.get_source("local_owner", feed_id, scope_key)
    assert state is not None
    payload = {**state.payload, "last_audit_completed_at": (
        datetime.now(UTC) - timedelta(minutes=10)
    ).isoformat()}
    await repo.save_source(state.model_copy(update={
        "revision": state.revision + 1, "payload": payload,
    }), expected_revision=state.revision)


def _evidence_record(listing_id: int, *, created_at: datetime, digest: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=uuid4(), revision=1, created_at=created_at, actor="legacy", origin_ref=None,
        source_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
        source_kind="user_report", locator="saved_user_submission.text",
        content_hash=digest, subject_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
        period=None, published_at=created_at, acquired_at=created_at,
        available_from=created_at, verification="user_report", conflict="none",
        text="legacy evidence",
    )


@pytest.mark.asyncio
async def test_immutable_bootstrap_keyset_reconciles_missing_hooks_and_late_uuid_rows(db_session):
    listing = await _listing(db_session)
    now = datetime.now(UTC)
    repo = SupplementalUpdatesRepository(db_session)
    existing = _evidence_record(listing.id, created_at=now - timedelta(days=2), digest="old-evidence")
    missing = _evidence_record(listing.id, created_at=now - timedelta(days=1), digest="new-evidence")
    for item in (existing, missing):
        db_session.add(SavedEvidence(
            id=str(item.id), revision=item.revision, owner_id="local_owner",
            payload=item.model_dump(mode="json"),
        ))
    datasets = []
    for index, created_at in enumerate((now - timedelta(days=2), now - timedelta(days=1)), 1):
        row = ResearchDataset(
            kind="filing_index", subject_kind="listing", subject_id=listing.id,
            source="legacy", schema_version=1, adapter_version="legacy", revision=index,
            content_hash=f"{index:064x}", captured_at=created_at, payload={"index": index},
            created_at=created_at, updated_at=created_at,
        )
        db_session.add(row)
        datasets.append(row)
    await db_session.flush()
    await repo.append_change(
        "local_owner", change_key=f"evidence:{existing.id}:revision:1", kind="evidence_revision",
        acquired_at=existing.acquired_at, available_from=existing.available_from,
        payload={"source_ref": {"kind": "evidence", "id": str(existing.id), "revision": 1},
                 "content_hash": existing.content_hash},
    )
    job = await _leased_job(db_session)

    for _ in range(8):
        result = await scan_saved_inputs(db_session, repo, job, limit=1)
        if all(value == "caught_up" for value in result["feeds"].values()):
            break
    else:
        pytest.fail(f"bootstrap keyset did not finish its bounded initial pass: {result}")

    evidence_receipts = await repo.list_changes("local_owner", limit=100, kinds=("evidence_revision",))
    assert {row.change_key for row in evidence_receipts} == {
        f"evidence:{existing.id}:revision:1", f"evidence:{missing.id}:revision:1",
    }
    missing_receipt = next(row for row in evidence_receipts if str(missing.id) in row.change_key)
    assert missing_receipt.payload["bootstrap_initial"] is True
    dataset_receipts = await repo.list_changes("local_owner", limit=100, kinds=("material_revision",))
    assert len(dataset_receipts) == 2
    assert all(row.payload["bootstrap_initial"] is True for row in dataset_receipts)
    recent_audit = await scan_saved_inputs(db_session, repo, job, limit=1)
    assert recent_audit["feeds"]["bootstrap_saved_evidence"] == "caught_up"
    assert recent_audit["processed"] == 0

    # UUID ordering and an old created_at are not treated as an append cursor:
    # a later cycle wraps and discovers this row without duplicating earlier rows.
    late = _evidence_record(listing.id, created_at=now - timedelta(days=10), digest="late-evidence")
    db_session.add(SavedEvidence(
        id=str(late.id), revision=1, owner_id="local_owner", payload=late.model_dump(mode="json"),
    ))
    await db_session.flush()
    await _expire_audit(repo, "bootstrap_saved_evidence", "global")
    for _ in range(8):
        result = await scan_saved_inputs(db_session, repo, job, limit=1)
        if result["feeds"]["bootstrap_saved_evidence"] == "caught_up":
            break
    late_receipts = await repo.list_changes("local_owner", limit=100, kinds=("evidence_revision",))
    late_receipt = next(row for row in late_receipts if str(late.id) in row.change_key)
    assert late_receipt.payload["bootstrap_initial"] is False
    assert len([row for row in late_receipts if row.change_key.startswith("evidence:")]) == 3


@pytest.mark.asyncio
async def test_mutable_classification_hash_change_is_journaled_as_missed_change(db_session):
    listing = await _listing(db_session)
    now = datetime.now(UTC)
    classification = IndustryClassification(
        listing_id=listing.id, system="csrc", code="A", name="初始分类",
        valid_from=date(2020, 1, 1), valid_to=None, source="legacy", captured_at=now,
    )
    db_session.add(classification)
    await db_session.flush()
    repo = SupplementalUpdatesRepository(db_session)
    job = await _leased_job(db_session)
    first = await scan_saved_inputs(db_session, repo, job, limit=10)
    receipts = await repo.list_changes("local_owner", limit=20, kinds=("bootstrap",))
    assert len(receipts) == 1
    assert receipts[0].payload["bootstrap_initial"] is True
    initial_hash = receipts[0].payload["source_digest"]

    classification.name = "更新分类"
    await db_session.flush()
    await _expire_audit(repo, "bootstrap_industry_classification", "cursor")
    second = await scan_saved_inputs(db_session, repo, job, limit=10)
    receipts = await repo.list_changes("local_owner", limit=20, kinds=("bootstrap",))
    assert len(receipts) == 2
    assert receipts[-1].payload["bootstrap_initial"] is False
    assert receipts[-1].payload["previous_digest"] == initial_hash
    assert receipts[-1].payload["source_digest"] != initial_hash
    assert first["feeds"]["bootstrap_industry_classification"] == "caught_up"
    assert second["feeds"]["bootstrap_industry_classification"] == "caught_up"


@pytest.mark.asyncio
async def test_rule_and_event_prejournal_baseline_is_marked_initial(db_session):
    now = datetime.now(UTC)
    rule_id = str(uuid4())
    db_session.add(SupplementalRuleRevision(
        id=rule_id, revision=1, owner_id="local_owner", schema_version=1,
        content_hash="a" * 64, semantic_hash="b" * 64, payload={},
    ))
    await db_session.flush()
    db_session.add(SupplementalRuleHead(
        owner_id="local_owner", rule_id=rule_id, head_revision=1,
        active_revision=1, active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None,
    ))
    event_id = str(uuid4())
    db_session.add(ResearchEventRevision(
        id=event_id, revision=1, owner_id="local_owner", previous_ref=None,
        event_key="legacy:event", event_aliases=[], published_at=None,
        effective_from=None, effective_to=None, acquired_at=now - timedelta(days=4),
        available_from=now - timedelta(days=3), created_at=now - timedelta(days=3),
        content_hash="c" * 64, payload={"title": "legacy"},
    ))
    await db_session.flush()
    repo = SupplementalUpdatesRepository(db_session)
    job = await _leased_job(db_session)
    for _ in range(4):
        result = await scan_saved_inputs(db_session, repo, job, limit=1)
        if result["feeds"]["bootstrap_rule_revision"] == "caught_up" and result["feeds"]["bootstrap_research_event"] == "caught_up":
            break
    rules = await repo.list_changes("local_owner", limit=20, kinds=("rule_revision", "rule_lifecycle"))
    events = await repo.list_changes("local_owner", limit=20, kinds=("research_event",))
    assert {item.kind for item in rules} == {"rule_revision", "rule_lifecycle"}
    assert all(item.payload["bootstrap_initial"] is True for item in rules + events)
    assert len(events) == 1
