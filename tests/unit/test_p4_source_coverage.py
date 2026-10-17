from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.settings.registry import RuntimeSettings
from investment_agent.storage.models import Base
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash, canonical_json
from investment_agent.supplemental_rules.contracts import (
    CoverageReceiptRef,
    PollWindowSourceProof,
    RuleCoverageManifestV2,
    RuleCoverageManifestV3,
    SourceCoverageBindingV3,
    UpdateSourceBinding,
    UpdateSourceEntry,
    parse_rule_coverage_manifest,
)
from investment_agent.supplemental_rules.coverage import (
    build_update_source_binding,
    rule_summary_entries,
)
from investment_agent.supplemental_rules.publication_limits import _source_coverage_limits
from tests.support.supplemental_rules import NOW, synthetic_coverage_manifest

SOURCE_ID = "controlled_local_v1"
LISTING = ObjectRef(kind="listing", id=7, revision=1)
CONTRACT_HASH = canonical_hash({
    "source_id": SOURCE_ID,
    "version": "1",
    "allowed_kinds": ["disclosure_original", "evidence", "research_dataset"],
    "coverage": "explicit_saved_refs",
    "health_limit_seconds": 86400,
})
SCOPE_KEY = "listing:7:company_research"
SCOPE_HASH = canonical_hash({
    "source_id": SOURCE_ID,
    "contract_version": "1",
    "contract_hash": CONTRACT_HASH,
    "scope_key": SCOPE_KEY,
    "listing_ref": LISTING,
})


def test_p4_update_switches_default_closed() -> None:
    assert Settings(api_key="synthetic").supplemental_updates_enabled is False
    assert Settings(api_key="synthetic").supplemental_disclosure_polling_enabled is False
    assert RuntimeSettings().supplemental_updates_enabled is False
    assert RuntimeSettings().supplemental_disclosure_polling_enabled is False


def _v2(
    *, sequence: int = 1, receipt_hash: str = "d" * 64,
    source_refs: tuple[ObjectRef, ...] | None = None, complete: str = "complete",
) -> RuleCoverageManifestV2:
    source = UpdateSourceEntry(
        source_id=SOURCE_ID,
        contract_version="1",
        contract_hash=CONTRACT_HASH,
        scope_hash=SCOPE_HASH,
        success_sequence=sequence,
        success_receipt_hash=receipt_hash,
        source_refs=source_refs or (ObjectRef(kind="evidence", id=uuid4(), revision=1),),
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        complete_watermark=NOW,
        completeness=complete,
        last_success_at=NOW,
        health_limit_seconds=86400,
    )
    binding = UpdateSourceBinding(
        listing_ref=LISTING,
        context="company_research",
        scope_key=SCOPE_KEY,
        sources=(source,),
        source_policy_hash=canonical_hash(((SOURCE_ID, CONTRACT_HASH, SCOPE_HASH),)),
        observed_at=NOW,
    )
    data = synthetic_coverage_manifest().model_dump(mode="python")
    data.update({"schema_version": 2, "update_source_binding": binding, "manifest_hash": "pending"})
    data["manifest_hash"] = canonical_hash({k: v for k, v in data.items() if k != "manifest_hash"})
    return RuleCoverageManifestV2.model_validate(data)


@pytest.mark.asyncio
async def test_p4_source_state_is_scope_local_and_rechecked_without_a_timer(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("API_KEY", "p4-source-test-key")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source-coverage.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        repo = SupplementalUpdatesRepository(session)
        source_refs = (ObjectRef(kind="evidence", id=uuid4(), revision=1),)
        source_rows = [ref.model_dump(mode="json") for ref in source_refs]
        source_hash = canonical_hash(source_rows)
        sources = [{"ref": source_rows[0], "content_hash": "a" * 64,
                    "status": "ok", "acquired_at": NOW.isoformat(),
                    "available_from": NOW.isoformat()}]
        receipt = await repo.append_change(
            "local_owner", change_key="source-import:one", kind="evidence_revision",
            payload={"scope": SCOPE_KEY, "source_refs": source_rows,
                     "source_refs_hash": source_hash, "source_id": SOURCE_ID,
                     "contract_version": "1", "contract_hash": CONTRACT_HASH,
                     "scope_hash": SCOPE_HASH, "coverage_state": "complete",
                     "sources": sources},
            acquired_at=NOW, available_from=NOW,
            committed_at=NOW,
            listing_id=7,
            context="company_research",
        )
        manifest = _v2(sequence=receipt.sequence, receipt_hash=receipt.payload_hash, source_refs=source_refs)
        source = manifest.update_source_binding.sources[0]
        state = await repo.ensure_source(
            "local_owner", SOURCE_ID, SCOPE_KEY, CONTRACT_HASH,
            listing_id=7, context="company_research",
        )
        state = state.model_copy(update={
            "revision": 1,
            "contract_version": "1",
            "scope_hash": SCOPE_HASH,
            "window_start": source.window_start,
            "window_end": source.window_end,
            "complete_watermark": source.complete_watermark,
            "coverage_state": "complete",
            "success_sequence": receipt.sequence,
            "success_receipt_hash": receipt.payload_hash,
            "source_refs": [ref.model_dump(mode="json") for ref in source.source_refs],
            "last_attempt_at": NOW,
            "last_success_at": NOW,
            "health_limit_seconds": 86400,
        })
        await repo.save_source(state, expected_revision=0)
        await session.commit()

    async with factory() as session:
        prepared_binding = await build_update_source_binding(
            session, owner="local_owner", listing_ref=LISTING,
            context="company_research", observed_at=NOW + timedelta(hours=1),
        )
        # The exact reference is intentionally absent from this synthetic DB;
        # prepare must fail closed rather than freeze a fake local source proof.
        assert prepared_binding is None
        assert await _source_coverage_limits(
            session, owner="local_owner", manifest=manifest, at=NOW + timedelta(hours=1)
        ) == ()
        # No timer is involved: elapsed time alone makes the exact source scope stale.
        assert await _source_coverage_limits(
            session, owner="local_owner", manifest=manifest, at=NOW + timedelta(hours=25)
        ) == (f"source_coverage_stale:{SOURCE_ID}",)
    await engine.dispose()


def test_v2_parser_requires_the_server_frozen_binding_and_keeps_v1_shape() -> None:
    v1 = synthetic_coverage_manifest()
    archived_payload = v1.model_dump(mode="json")
    assert parse_rule_coverage_manifest(archived_payload) == v1
    assert "update_source_binding" not in archived_payload

    v2 = _v2()
    parsed = parse_rule_coverage_manifest(v2.model_dump(mode="json"))
    assert parsed == v2
    assert parsed.manifest_hash == v2.manifest_hash
    source_summary = next(item for item in rule_summary_entries(v2) if item["summary_kind"] == "update_source_coverage")
    assert source_summary["source_policy_hash"] == v2.update_source_binding.source_policy_hash
    assert source_summary["sources"][0]["source_refs_count"] == len(v2.update_source_binding.sources[0].source_refs)
    assert source_summary["sources"][0]["source_refs_preview"] == [
        ref.model_dump(mode="json") for ref in v2.update_source_binding.sources[0].source_refs[:8]
    ]
    missing = v2.model_dump(mode="json")
    del missing["update_source_binding"]
    with pytest.raises(ValueError):
        parse_rule_coverage_manifest(missing)


@pytest.mark.asyncio
async def test_v3_mixed_scope_fails_closed_when_declared_saved_source_is_incomplete(monkeypatch):
    poll = SimpleNamespace(feed_id="cninfo_szse_v1", coverage_state="complete")
    local = SimpleNamespace(feed_id=SOURCE_ID, coverage_state="failed")

    async def list_sources(self, owner, listing_id, context, *, for_update=False):
        return [poll, local]

    async def missing_saved_binding(*args, **kwargs):
        return None

    monkeypatch.setattr(SupplementalUpdatesRepository, "list_sources", list_sources)
    monkeypatch.setattr(
        "investment_agent.supplemental_rules.coverage._build_saved_refs_source_binding",
        missing_saved_binding,
    )
    session = SimpleNamespace(info={})
    assert await build_update_source_binding(
        session, owner="local_owner", listing_ref=LISTING,
        context="company_research", observed_at=NOW,
    ) is None


def _poll_proof(**changes) -> PollWindowSourceProof:
    start = NOW - timedelta(days=7)
    final = CoverageReceiptRef(sequence=20, id=uuid4(), payload_hash="e" * 64)
    page = CoverageReceiptRef(sequence=19, id=uuid4(), payload_hash="f" * 64)
    values = {
        "proof_type": "poll_window_v1", "source_id": "cninfo_szse_v1",
        "protocol_profile": "fixture", "protocol_status": "fixture_validated",
        "contract_version": "1", "contract_hash": "a" * 64,
        "scope_hash": canonical_hash({"source_id": "cninfo_szse_v1", "contract_version": "1",
            "contract_hash": "a" * 64, "scope_key": SCOPE_KEY, "listing_ref": LISTING}),
        "grant_ref": uuid4(), "grant_hash": "b" * 64, "poll_ref": uuid4(),
        "poll_id": uuid4(),
        "final_ref": final, "child_receipts": (page,),
            "predecessor_snapshot_ref": None, "predecessor_snapshot_refs": (),
            "predecessor_resolution": "none",
            "predecessor_pending_ids_hash": canonical_hash([]),
        "publication_window_start": start, "publication_window_end": NOW,
        "history_start": start,
        "issuer_id": "fixture_issuer",
        "history_basis_hash": "1" * 64,
        "index_watermark": NOW, "original_watermark": NOW,
        "complete_watermark": NOW, "page_count": 1, "document_count": 0,
        "continuous_index_watermark": NOW,
        "continuous_original_watermark": NOW,
        "continuous_complete_watermark": NOW,
        "eligible_count": 0, "filtered_count": 0, "page_chain_root": "c" * 64,
        "document_manifest_hash": "d" * 64, "observation_at": NOW + timedelta(minutes=1),
        "acquired_at": NOW, "available_from": NOW, "committed_at": NOW,
        "last_success_at": NOW, "health_limit_seconds": 3600, "history_epoch": uuid4(),
        "epoch_ordinal": 1, "semantic_generation": 0, "coverage_anchor": "9" * 64,
    }
    values.update(changes)
    return PollWindowSourceProof.model_validate(values)


def test_v3_poll_proof_accepts_empty_window_but_rejects_incomplete_or_future_watermarks() -> None:
    proof = _poll_proof()
    binding = SourceCoverageBindingV3(
        listing_ref=LISTING, context="company_research", scope_key=SCOPE_KEY,
        sources=(proof,), source_policy_hash=canonical_hash(((
            proof.source_id, proof.contract_hash, proof.scope_hash,
        ),)), observed_at=NOW + timedelta(minutes=2),
    )
    assert binding.sources[0].document_count == 0
    with pytest.raises(ValueError, match="original"):
        _poll_proof(original_watermark=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="complete watermark"):
        _poll_proof(complete_watermark=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future"):
        _poll_proof(acquired_at=NOW + timedelta(minutes=2),
                    available_from=NOW + timedelta(minutes=2),
                    committed_at=NOW + timedelta(minutes=2),
                    last_success_at=NOW + timedelta(minutes=2))
    with pytest.raises(ValueError, match="first-source"):
        _poll_proof(source_id="other_feed")


def test_v3_manifest_is_discriminated_and_hashes_the_typed_poll_proof() -> None:
    proof = _poll_proof()
    binding = SourceCoverageBindingV3(
        listing_ref=LISTING, context="company_research", scope_key=SCOPE_KEY,
        sources=(proof,), source_policy_hash=canonical_hash(((
            proof.source_id, proof.contract_hash, proof.scope_hash,
        ),)), observed_at=NOW + timedelta(minutes=2),
    )
    data = synthetic_coverage_manifest().model_dump(mode="python")
    data.update({"schema_version": 3, "source_coverage_binding": binding, "manifest_hash": "pending"})
    data["manifest_hash"] = canonical_hash({key: value for key, value in data.items() if key != "manifest_hash"})
    v3 = RuleCoverageManifestV3.model_validate(data)
    parsed = parse_rule_coverage_manifest(v3.model_dump(mode="json"))
    assert parsed == v3
    assert parsed.manifest_hash == v3.manifest_hash
    unknown = v3.model_dump(mode="json")
    unknown["schema_version"] = 4
    with pytest.raises(ValueError, match="unsupported"):
        parse_rule_coverage_manifest(unknown)


@pytest.mark.asyncio
async def test_archived_v1_prepared_and_published_payloads_keep_their_hashes(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v1-coverage.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prepared = synthetic_coverage_manifest()
    async with factory() as session:
        repo = InvestmentAssessmentRepository(session)
        await repo.save_rule_coverage("local_owner", prepared)
        published = await repo.save_published_rule_coverage(
            "local_owner",
            prepared=prepared,
            manifest_id=uuid4(),
            semantic_review_ref=ObjectRef(kind="review", id=uuid4(), revision=1),
            closure_application_refs=(),
            thesis_ref=ObjectRef(kind="thesis", id=uuid4(), revision=1),
            recommendation_ref=ObjectRef(kind="recommendation", id=uuid4(), revision=1),
            limits_recheck=(),
            committed_at=NOW + timedelta(hours=2),
        )
        await session.commit()
    async with factory() as session:
        repo = InvestmentAssessmentRepository(session)
        for expected in (prepared, published):
            stored = await repo.get_rule_coverage("local_owner", expected.reference)
            assert stored is not None
            assert stored.schema_version == 1
            assert stored.manifest_hash == expected.manifest_hash
            assert canonical_json(stored) == canonical_json(expected)
            row = await session.get(InvestmentAssessment, (str(expected.id), 1))
            assert row is not None
            assert row.manifest_hash == expected.manifest_hash
            assert canonical_json(parse_rule_coverage_manifest(row.payload)) == canonical_json(expected)
    await engine.dispose()


@pytest.mark.asyncio
async def test_v2_published_stage_preserves_source_binding_and_prepared_review_hash(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v2-coverage.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prepared = _v2()
    async with factory() as session:
        repo = InvestmentAssessmentRepository(session)
        await repo.save_rule_coverage("local_owner", prepared)
        published = await repo.save_published_rule_coverage(
            "local_owner",
            prepared=prepared,
            manifest_id=uuid4(),
            semantic_review_ref=ObjectRef(kind="review", id=uuid4(), revision=1),
            closure_application_refs=(),
            thesis_ref=ObjectRef(kind="thesis", id=uuid4(), revision=1),
            recommendation_ref=ObjectRef(kind="recommendation", id=uuid4(), revision=1),
            limits_recheck=(),
            committed_at=NOW + timedelta(hours=2),
        )
        await session.commit()
    async with factory() as session:
        repo = InvestmentAssessmentRepository(session)
        saved = await repo.get_rule_coverage("local_owner", published.reference)
        assert isinstance(saved, RuleCoverageManifestV2)
        assert saved.prepared_manifest_hash == prepared.manifest_hash
        assert saved.update_source_binding == prepared.update_source_binding
        assert parse_rule_coverage_manifest(saved.model_dump(mode="json")) == saved
        row = await session.get(InvestmentAssessment, (str(saved.id), 1))
        assert row is not None and row.manifest_hash == saved.manifest_hash
    await engine.dispose()


@pytest.mark.asyncio
async def test_declared_source_scope_blocks_unbound_v1_but_unrelated_scope_is_untouched(db_session) -> None:
    repo = SupplementalUpdatesRepository(db_session)
    await repo.ensure_source(
        "local_owner", SOURCE_ID, SCOPE_KEY, CONTRACT_HASH,
        listing_id=7, context="company_research",
    )
    v1 = synthetic_coverage_manifest()
    assert await _source_coverage_limits(
        db_session, owner="local_owner", manifest=v1, at=NOW
    ) == ("source_coverage_unknown:binding_missing",)
    unrelated = v1.model_copy(update={"listing_ref": ObjectRef(kind="listing", id=8, revision=1)})
    assert await _source_coverage_limits(
        db_session, owner="local_owner", manifest=unrelated, at=NOW
    ) == ()
