from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.research_material import ResearchDataset, ResearchSourceBinding
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import ResearchEvent
from investment_agent.supplemental_rules.update_targets import (
    MAX_FROZEN_TARGET_LISTINGS,
    MAX_TARGET_LISTINGS,
    TARGET_CONTEXTS,
    freeze_target_snapshot,
    target_scopes,
)


@pytest.mark.asyncio
async def test_company_material_pages_are_separate_from_frozen_universe(
    db_session,
):
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Bounded material target")
    db_session.add(company)
    await db_session.flush()
    db_session.add_all([
        Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
        for index in range(1, 103)
    ])
    await db_session.flush()
    dataset = ResearchDataset(
        kind="filing_index", subject_kind="company", subject_id=company.id,
        source="fixture", schema_version=1, adapter_version="test", revision=1,
        content_hash="a" * 64, captured_at=datetime(2026, 10, 17, tzinfo=UTC), payload={},
    )
    db_session.add(dataset)
    await db_session.flush()
    receipt = await SupplementalUpdatesRepository(db_session).append_change(
        "local_owner", change_key="material:company:1", kind="material_revision",
        payload={"source_ref": {"kind": "research_dataset", "id": dataset.id, "revision": 1},
                 "content_hash": dataset.content_hash},
    )

    snapshot = await freeze_target_snapshot(db_session, receipt)
    assert len(snapshot["listing_ids"]) == 102
    assert snapshot["overflow_listing_id"] is None
    assert snapshot["complete"] is True
    assert snapshot["truncated"] is False
    assert snapshot["contexts"] == list(TARGET_CONTEXTS)

    first, first_complete = await target_scopes(
        db_session, receipt, target_snapshot=snapshot, offset=0,
    )
    second, second_complete = await target_scopes(
        db_session, receipt, target_snapshot=snapshot, offset=100,
    )
    assert len(first) == 100 * len(TARGET_CONTEXTS)
    assert len(second) == 2 * len(TARGET_CONTEXTS)
    assert first_complete is False and second_complete is True

    frozen_before_later_listing = max(snapshot["listing_ids"])
    db_session.add(Listing(
        company_id=company.id, market_code="SSE", ticker="999999",
    ))
    await db_session.flush()
    replayed, _ = await target_scopes(
        db_session, receipt, target_snapshot=snapshot, offset=0,
    )
    assert max(listing_id for listing_id, _ in replayed) <= frozen_before_later_listing


@pytest.mark.asyncio
async def test_large_company_target_uses_100_pages_and_recoverable_frozen_segments(db_session):
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Segmented material target")
    db_session.add(company)
    await db_session.flush()
    db_session.add_all([
        Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
        for index in range(1, MAX_FROZEN_TARGET_LISTINGS + 103)
    ])
    await db_session.flush()
    dataset = ResearchDataset(
        kind="filing_index", subject_kind="company", subject_id=company.id,
        source="fixture", schema_version=1, adapter_version="test", revision=1,
        content_hash="b" * 64, captured_at=datetime(2026, 10, 17, tzinfo=UTC), payload={},
    )
    db_session.add(dataset)
    await db_session.flush()
    receipt = await SupplementalUpdatesRepository(db_session).append_change(
        "local_owner", change_key="material:segmented-company", kind="material_revision",
        payload={"source_ref": {"kind": "research_dataset", "id": dataset.id, "revision": 1},
                 "content_hash": dataset.content_hash},
    )
    first_segment = await freeze_target_snapshot(db_session, receipt)
    assert len(first_segment["listing_ids"]) == MAX_FROZEN_TARGET_LISTINGS
    assert first_segment["truncated"] is True
    assert first_segment["complete"] is False
    assert first_segment["next_after_listing_id"] == first_segment["listing_ids"][-1]
    assert first_segment["overflow_listing_id"] == first_segment["listing_ids"][-1] + 1
    for offset in range(0, MAX_FROZEN_TARGET_LISTINGS, MAX_TARGET_LISTINGS):
        scopes, complete = await target_scopes(
            db_session, receipt, target_snapshot=first_segment, offset=offset,
        )
        assert len(scopes) == MAX_TARGET_LISTINGS * len(TARGET_CONTEXTS)
        assert complete is False
    beyond = await freeze_target_snapshot(
        db_session, receipt, high_water_id=first_segment["high_water_id"],
        after_listing_id=first_segment["next_after_listing_id"],
    )
    assert beyond["after_listing_id"] == first_segment["next_after_listing_id"]
    assert len(beyond["listing_ids"]) == 102
    assert beyond["truncated"] is False
    assert beyond["complete"] is True
    final, complete = await target_scopes(db_session, receipt, target_snapshot=beyond)
    assert len(final) == 100 * len(TARGET_CONTEXTS)
    assert complete is False
    final, complete = await target_scopes(
        db_session, receipt, target_snapshot=beyond, offset=100,
    )
    assert len(final) == 2 * len(TARGET_CONTEXTS)
    assert complete is True


@pytest.mark.asyncio
async def test_due_source_boundary_freezes_all_binding_members_and_pages(db_session):
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Due binding target")
    db_session.add(company)
    await db_session.flush()
    db_session.add_all([
        Listing(company_id=company.id, market_code="SSE", ticker=f"D{index:05d}")
        for index in range(105)
    ])
    await db_session.flush()
    binding = ResearchSourceBinding(
        subject_kind="company", subject_id=company.id, source="fixture",
        source_symbol="OLD", valid_from=date(2020, 1, 1), valid_to=date(2026, 1, 1),
        verified_at=datetime(2025, 1, 1, tzinfo=UTC), verified_by="test", payload={},
    )
    db_session.add(binding)
    await db_session.flush()
    due_at = datetime(2026, 1, 1, tzinfo=UTC)
    receipt = await SupplementalUpdatesRepository(db_session).append_change(
        "local_owner", change_key=f"due:binding:{binding.id}:valid_to", kind="due_boundary",
        payload={"source_ref": {"kind": "research_source_binding", "id": binding.id,
                                 "revision": 1},
                 "boundary": "material_binding_valid_to", "due_at": due_at.isoformat()},
        available_from=due_at,
    )
    snapshot = await freeze_target_snapshot(db_session, receipt)
    assert len(snapshot["listing_ids"]) == 105
    assert snapshot["complete"] is True
    page, complete = await target_scopes(db_session, receipt, target_snapshot=snapshot)
    assert len(page) == 100 * len(TARGET_CONTEXTS)
    assert complete is False
    page, complete = await target_scopes(db_session, receipt, target_snapshot=snapshot, offset=100)
    assert len(page) == 5 * len(TARGET_CONTEXTS)
    assert complete is True


@pytest.mark.asyncio
async def test_target_snapshot_rejects_receipt_mismatch(db_session):
    repo = SupplementalUpdatesRepository(db_session)
    receipt = await repo.append_change(
        "local_owner", change_key="bootstrap:target", kind="bootstrap", payload={},
    )
    snapshot = await freeze_target_snapshot(db_session, receipt)
    other = await repo.append_change(
        "local_owner", change_key="bootstrap:other", kind="bootstrap", payload={},
    )
    scopes, complete = await target_scopes(db_session, other, target_snapshot=snapshot)
    assert scopes == []
    assert complete is False


@pytest.mark.asyncio
async def test_material_target_rejects_unknown_or_missing_exact_source(db_session):
    repo = SupplementalUpdatesRepository(db_session)
    receipt = await repo.append_change(
        "local_owner", change_key="material:unknown", kind="material_revision",
        payload={"source_ref": {"kind": "external_url", "id": "https://example.invalid", "revision": 1}},
    )
    snapshot = await freeze_target_snapshot(db_session, receipt)
    assert snapshot["listing_ids"] == []
    assert snapshot["complete"] is False


@pytest.mark.asyncio
async def test_evidence_target_keeps_legacy_non_hex_hash_contract(db_session):
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Legacy evidence target")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600001")
    db_session.add(listing)
    await db_session.flush()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    evidence_id = str(uuid4())
    record = EvidenceRecord(
        id=evidence_id, revision=1, created_at=now, actor="user", origin_ref=None,
        source_ref=ObjectRef(kind="research_dataset", id=1, revision=1),
        source_kind="research_dataset", locator="legacy.fixture", content_hash="p3-fixture",
        subject_ref=ObjectRef(kind="listing", id=listing.id, revision=1), period=None,
        published_at=now, acquired_at=now, available_from=now,
        verification="candidate", conflict="none", text="saved source",
    )
    db_session.add(SavedEvidence(
        id=evidence_id, revision=1, owner_id="local_owner",
        payload=record.model_dump(mode="json"),
    ))
    await db_session.flush()
    receipt = await SupplementalUpdatesRepository(db_session).append_change(
        "local_owner", change_key="evidence:legacy-hash", kind="evidence_revision",
        payload={"source_ref": {"kind": "evidence", "id": evidence_id, "revision": 1},
                 "content_hash": "p3-fixture"},
    )
    snapshot = await freeze_target_snapshot(db_session, receipt)
    scopes, complete = await target_scopes(db_session, receipt, target_snapshot=snapshot)
    assert scopes == [(listing.id, "company_research"), (listing.id, "candidate"),
                      (listing.id, "account_fit"), (listing.id, "holding_review")]
    assert complete is True


@pytest.mark.asyncio
async def test_company_event_fanout_uses_pages_without_losing_targets(db_session):
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Bounded event target")
    db_session.add(company)
    await db_session.flush()
    db_session.add_all([
        Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
        for index in range(1, 103)
    ])
    await db_session.flush()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    event = ResearchEvent(
        id=uuid4(), revision=1, owner_id="local_owner", event_key="event:company-cap",
        title="Candidate event", event_type="user_report", jurisdiction="CN",
        affected_products=(), affected_subjects=(f"company:{company.id}",),
        policy_stage="unknown", acquired_at=now, available_from=now, source_refs=(),
        verification="user_report", conflict=False,
    )
    stored = await SupplementalRulesRepository(db_session).save_research_event(
        "local_owner", event, created_at=now,
    )
    receipt = await SupplementalUpdatesRepository(db_session).append_change(
        "local_owner", change_key="event:company-cap", kind="research_event",
        payload={"source_ref": stored.reference.model_dump(mode="json")},
    )
    snapshot = await freeze_target_snapshot(db_session, receipt)
    assert len(snapshot["listing_ids"]) == 102
    assert snapshot["overflow_listing_id"] is None
    assert snapshot["complete"] is True
    scopes, complete = await target_scopes(db_session, receipt, target_snapshot=snapshot)
    assert len(scopes) == 100 * len(TARGET_CONTEXTS)
    assert complete is False
    next_page, next_complete = await target_scopes(
        db_session, receipt, target_snapshot=snapshot, offset=100,
    )
    assert len(next_page) == 2 * len(TARGET_CONTEXTS)
    assert next_complete is True


@pytest.mark.asyncio
async def test_controlled_import_of_company_material_targets_only_declared_listing(db_session):
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Imported company material")
    db_session.add(company)
    await db_session.flush()
    listings = [
        Listing(company_id=company.id, market_code="SSE", ticker=f"{index:06d}")
        for index in range(1, 4)
    ]
    db_session.add_all(listings)
    await db_session.flush()
    dataset = ResearchDataset(
        kind="filing_index", subject_kind="company", subject_id=company.id,
        source="fixture", schema_version=1, adapter_version="test", revision=1,
        content_hash="c" * 64, captured_at=datetime(2026, 10, 17, tzinfo=UTC), payload={},
    )
    db_session.add(dataset)
    await db_session.flush()
    source_ref = {"kind": "research_dataset", "id": dataset.id, "revision": 1,
                  "field_path": None}
    source = {"ref": source_ref, "content_hash": dataset.content_hash,
              "status": "candidate", "acquired_at": "2026-10-17T00:00:00+00:00",
              "available_from": "2026-10-17T00:00:00+00:00"}
    listing_ref = {"kind": "listing", "id": listings[2].id, "revision": 1,
                   "field_path": None}
    manifest_hash = canonical_hash({
        "listing_ref": listing_ref, "context": "company_research", "sources": [source],
    })
    receipt = await SupplementalUpdatesRepository(db_session).append_change(
        "local_owner", change_key="import:exact-company-scope", kind="evidence_revision",
        listing_id=listings[2].id, context="company_research",
        payload={"origin": "controlled_import", "source_refs": [source_ref],
                 "sources": [source], "listing_refs": [listing_ref],
                 "contexts": ["company_research"], "manifest_hash": manifest_hash,
                 "coverage": "explicit_saved_refs"},
    )
    snapshot = await freeze_target_snapshot(db_session, receipt)
    scopes, complete = await target_scopes(db_session, receipt, target_snapshot=snapshot)
    assert scopes == [(listings[2].id, "company_research")]
    assert complete is True
