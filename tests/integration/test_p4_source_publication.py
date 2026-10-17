"""AF-P4-06: real prepare/review/publish chain with frozen local source coverage."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.publication import (
    RuleReviewItem,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.publication_limits import published_manifest_limits
from tests.integration.test_coverage_prepare import _drain_workers
from tests.integration.test_publication_closure import (
    _applications,
    _chain,
    _open_application_refs,
    _proposal,
    _publish,
    _publish_action,
    _reviewed,
)
from tests.support.research_closure import business_input, run_prepare


@pytest.fixture(autouse=True)
def _api_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "p4-test-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")


@pytest_asyncio.fixture
async def p4_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'p4-publish.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


async def _declare_complete_source(
    listing_ref: ObjectRef, evidence_ref: ObjectRef, *, session=None,
) -> int:
    """Persist an exact local source contract and its successful receipt."""
    from investment_agent.supplemental_rules.update_intake import LOCAL_CONTRACT_HASH

    now = datetime.now(UTC)
    owner = "local_owner"
    context = "company_research"
    feed_id = "controlled_local_v1"
    scope_key = f"listing:{listing_ref.id}:{context}"
    source_refs = [evidence_ref.model_dump(mode="json")]
    from investment_agent.supplemental_rules.update_intake import resolve_saved_sources
    from tests.support.research_closure import BOUND_SETTINGS

    async def write(db_session) -> int:
        sources = await resolve_saved_sources(
            db_session, listing_ref, (evidence_ref,), settings=BOUND_SETTINGS, now=now)
        repo = SupplementalUpdatesRepository(db_session)
        scope_hash = canonical_hash({
            "source_id": feed_id, "contract_version": "1", "contract_hash": LOCAL_CONTRACT_HASH,
            "scope_key": scope_key, "listing_ref": listing_ref,
        })
        payload = {"source_refs": source_refs, "source_refs_hash": canonical_hash(source_refs),
                   "source_id": feed_id, "contract_version": "1",
                   "contract_hash": LOCAL_CONTRACT_HASH, "scope_hash": scope_hash,
                   "origin": "controlled_import", "sources": sources,
                   "coverage_state": "complete"}
        state = await repo.ensure_source(owner, feed_id, scope_key, LOCAL_CONTRACT_HASH,
                                         listing_id=int(listing_ref.id), context=context)
        receipt = await repo.append_change(
            owner, change_key=f"p4-test-import:{listing_ref.id}", kind="evidence_revision",
            payload=payload, acquired_at=now, available_from=now, committed_at=now,
            listing_id=int(listing_ref.id), context=context)
        window_start = min(datetime.fromisoformat(item["available_from"]) for item in sources)
        await repo.save_source(state.model_copy(update={
            "revision": state.revision + 1, "contract_version": "1", "scope_hash": scope_hash,
            "window_start": window_start, "window_end": now,
            "complete_watermark": now, "coverage_state": "complete",
            "success_sequence": receipt.sequence, "success_receipt_hash": receipt.payload_hash,
            "source_refs": source_refs, "last_attempt_at": now, "last_success_at": now,
            "health_limit_seconds": 86400, "failure_code": None,
        }), expected_revision=state.revision)
        return receipt.sequence

    if session is not None:
        return await write(session)
    async with session_scope() as db_session:
        return await write(db_session)


async def _chain_with_source(ticker: str):
    """Admit the task only after its exact local source proof is visible."""
    from tests.support.research_closure import (
        activate_rule,
        admit_research,
        claim_task,
        commit_progress,
        metric_rule,
        requirement_rule,
        seed_company_view,
        seed_listing,
        set_runtime,
    )

    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker=ticker)
        await activate_rule(session, requirement_rule(listing_ref))
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        await _declare_complete_source(listing_ref, evidence_ref, session=session)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    return case, case_ref, evidence_ref, claim


async def _prepare_review_publish(case, case_ref, evidence_ref, claim, *, directional=False):
    view = "favorable" if directional else "undetermined"
    first = await run_prepare(claim, business_input(case, evidence_ref, business_view=view))
    assert first["coverage_manifest_ref"]
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, frozen = _proposal(requirement_ref) if directional else ((), None)
    result = await run_prepare(claim, business_input(
        case, evidence_ref, question_scope="Source coverage review", business_view=view,
        closure_proposals=proposals))
    review_items = (
        RuleReviewItem(application_ref=requirement_ref, disposition="resolved_with_closure",
                       proposal_hash=frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ) if directional else (
        RuleReviewItem(application_ref=requirement_ref, disposition="no_action_required"),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    )
    candidate, semantic = await _reviewed(claim, result, rule_reviews=review_items)
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    return result, candidate, semantic, operation, (requirement_ref, metric_ref)


@pytest.mark.usefixtures("p4_db")
async def test_real_prepare_semantic_publish_v2_keeps_open_issues_open():
    case, case_ref, evidence_ref, claim = await _chain_with_source(ticker="600801")
    result, candidate, semantic, operation, refs = await _prepare_review_publish(
        case, case_ref, evidence_ref, claim)

    assert result["coverage_manifest_ref"]
    async with session_scope() as session:
        manifest = await InvestmentAssessmentRepository(session).get_rule_coverage(
            "local_owner", ObjectRef.model_validate(result["coverage_manifest_ref"]))
        assert manifest.schema_version == 2
        assert manifest.update_source_binding.sources[0].source_refs == (evidence_ref,)
        limits = await published_manifest_limits(session, owner="local_owner", manifest=manifest,
                                                 at=datetime.now(UTC))
        assert not limits, (limits, manifest.update_source_binding.sources[0],
                            await SupplementalUpdatesRepository(session).get_source(
                                "local_owner", "controlled_local_v1",
                                manifest.update_source_binding.scope_key))
    assert any(entry.get("summary_kind") == "update_source_coverage"
               for entry in result["materials"])
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is True, published.check.field_errors
    apps = await _applications()
    for ref in refs:
        app = next(item for item in apps if item["id"] == str(ref.id))
        assert app["open_issue_refs"]  # normal publication cannot silently close a P3 issue
    assert await _applications(authorization="formal_review") == []


@pytest.mark.usefixtures("p4_db")
async def test_source_state_change_after_semantic_review_blocks_publish_and_preserves_issue():
    case, case_ref, evidence_ref, claim = await _chain_with_source(ticker="600802")
    async with session_scope() as session:
        source_state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "controlled_local_v1",
            f"listing:{case.listing_ref.id}:company_research")
        assert source_state is not None
        receipt_sequence = source_state.success_sequence
    result, candidate, semantic, operation, refs = await _prepare_review_publish(
        case, case_ref, evidence_ref, claim, directional=True)
    from investment_agent.supplemental_rules.update_intake import LOCAL_CONTRACT_HASH

    scope_key = f"listing:{case.listing_ref.id}:company_research"
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        state = await repo.get_source("local_owner", "controlled_local_v1", scope_key)
        assert state is not None and state.success_sequence == receipt_sequence
        await repo.save_source(state.model_copy(update={
            "revision": state.revision + 1, "coverage_state": "partial",
            "complete_watermark": None, "failure_code": "source_changed_during_review",
            "config_hash": LOCAL_CONTRACT_HASH,
        }), expected_revision=state.revision)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is False
    assert any("source_coverage" in key or "source_coverage" in str(error)
               for key, error in published.check.field_errors.items()), published.check.field_errors
    assert await _applications(authorization="formal_review") == []
    apps = await _applications()
    for ref in refs:
        app = next(item for item in apps if item["id"] == str(ref.id))
        assert app["open_issue_refs"]


@pytest.mark.usefixtures("p4_db")
async def test_declared_v1_source_scope_is_unknown_but_unrelated_listing_is_unaffected():
    from investment_agent.supplemental_rules.update_intake import LOCAL_CONTRACT_HASH

    case, case_ref, evidence_ref, claim = await _chain(ticker="600803")
    scope_key = f"listing:{case.listing_ref.id}:company_research"
    async with session_scope() as session:
        await SupplementalUpdatesRepository(session).ensure_source(
            "local_owner", "controlled_local_v1", scope_key, LOCAL_CONTRACT_HASH,
            listing_id=int(case.listing_ref.id), context="company_research")
    result, candidate, semantic, operation, refs = await _prepare_review_publish(
        case, case_ref, evidence_ref, claim, directional=True)
    async with session_scope() as session:
        manifest = await InvestmentAssessmentRepository(session).get_rule_coverage(
            "local_owner", ObjectRef.model_validate(result["coverage_manifest_ref"]))
        assert manifest is not None and manifest.schema_version == 1
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=datetime.now(UTC))
        assert "source_coverage_unknown:binding_missing" in limits
        unrelated = manifest.model_copy(update={
            "listing_ref": ObjectRef(kind="listing", id=case.listing_ref.id + 1000, revision=1),
        })
        assert not await published_manifest_limits(
            session, owner="local_owner", manifest=unrelated, at=datetime.now(UTC))
    blocked = await _publish(claim, operation, candidate, semantic)
    assert blocked.published is False
    assert any("source_coverage_unknown:binding_missing" in error
               for error in blocked.check.field_errors.values()), blocked.check.field_errors
    assert await _applications(authorization="formal_review") == []
    apps = await _applications()
    for ref in refs:
        app = next(item for item in apps if item["id"] == str(ref.id))
        assert app["open_issue_refs"]


@pytest.mark.usefixtures("p4_db")
async def test_source_import_after_task_cutoff_does_not_enter_old_prepared_manifest():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600805")
    from investment_agent.agent.observation import effective_cutoff

    async with session_scope() as session:
        original_cutoff = await effective_cutoff(session, claim.task_id)
    await _declare_complete_source(case.listing_ref, evidence_ref)
    result = await run_prepare(
        claim, business_input(case, evidence_ref, question_scope="Late source cutoff"))
    async with session_scope() as session:
        manifest = await InvestmentAssessmentRepository(session).get_rule_coverage(
            "local_owner", ObjectRef.model_validate(result["coverage_manifest_ref"]))
        assert manifest is not None and manifest.schema_version == 1
        assert manifest.effective_cutoff == original_cutoff
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=datetime.now(UTC))
        assert "source_coverage_unknown:binding_missing" in limits
        assert not any(
            item.get("summary_kind") == "update_source_coverage"
            and item.get("success_sequence")
            for item in result["materials"]
        )


@pytest.mark.usefixtures("p4_db")
async def test_post_admission_same_source_recheck_is_health_only_for_manifest():
    case, case_ref, evidence_ref, claim = await _chain_with_source(ticker="600806")
    from investment_agent.agent.observation import effective_cutoff

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        original_state = await repo.get_source(
            "local_owner", "controlled_local_v1",
            f"listing:{case.listing_ref.id}:company_research")
        assert original_state is not None
        original_receipt = await repo.get_change(
            "local_owner", original_state.success_sequence)
        assert original_receipt is not None
        original_cutoff = await effective_cutoff(session, claim.task_id)
        now = datetime.now(UTC)
        current_receipt = await repo.append_change(
            "local_owner", change_key="p4-post-admission-same-source-recheck",
            kind="evidence_revision", payload=original_receipt.payload,
            acquired_at=now, available_from=now, committed_at=now,
            listing_id=int(case.listing_ref.id), context="company_research")
        await repo.save_source(original_state.model_copy(update={
            "revision": original_state.revision + 1,
            "success_sequence": current_receipt.sequence,
            "success_receipt_hash": current_receipt.payload_hash,
            "window_end": now, "complete_watermark": now,
            "last_attempt_at": now, "last_success_at": now,
        }), expected_revision=original_state.revision)

    result = await run_prepare(
        claim, business_input(case, evidence_ref, question_scope="Health only recheck"))
    async with session_scope() as session:
        manifest = await InvestmentAssessmentRepository(session).get_rule_coverage(
            "local_owner", ObjectRef.model_validate(result["coverage_manifest_ref"]))
        assert manifest is not None and manifest.schema_version == 2
        entry = manifest.update_source_binding.sources[0]
        assert manifest.effective_cutoff == original_cutoff
        assert entry.success_sequence == original_state.success_sequence
        assert entry.success_receipt_hash == original_state.success_receipt_hash
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=datetime.now(UTC))
        assert not limits


@pytest.mark.usefixtures("p4_db")
async def test_same_saved_source_recheck_renews_health_but_changed_content_limits_old_manifest():
    case, case_ref, evidence_ref, claim = await _chain_with_source(ticker="600804")
    result, _candidate, _semantic, _operation, _refs = await _prepare_review_publish(
        case, case_ref, evidence_ref, claim)
    async with session_scope() as session:
        manifest = await InvestmentAssessmentRepository(session).get_rule_coverage(
            "local_owner", ObjectRef.model_validate(result["coverage_manifest_ref"]))
        assert manifest is not None and manifest.schema_version == 2
        repo = SupplementalUpdatesRepository(session)
        source = manifest.update_source_binding.sources[0]
        state = await repo.get_source(
            "local_owner", source.source_id, manifest.update_source_binding.scope_key)
        assert state is not None
        old_receipt = await repo.get_change("local_owner", source.success_sequence)
        assert old_receipt is not None
        now = datetime.now(UTC)
        same_receipt = await repo.append_change(
            "local_owner", change_key="p4-same-source-recheck",
            kind="evidence_revision", payload=old_receipt.payload,
            acquired_at=now, available_from=now, committed_at=now,
            listing_id=int(case.listing_ref.id), context="company_research")
        await repo.save_source(state.model_copy(update={
            "revision": state.revision + 1, "success_sequence": same_receipt.sequence,
            "success_receipt_hash": same_receipt.payload_hash,
            "window_end": now, "complete_watermark": now,
            "last_attempt_at": now, "last_success_at": now,
        }), expected_revision=state.revision)
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=now)
        assert not any(reason.startswith("source_coverage_") for reason in limits), limits
        stale_at = now + timedelta(seconds=source.health_limit_seconds)
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=stale_at)
        assert "source_coverage_stale:controlled_local_v1" in limits

        state = await repo.get_source(
            "local_owner", source.source_id, manifest.update_source_binding.scope_key)
        assert state is not None
        from investment_agent.supplemental_rules.coverage import build_update_source_binding

        future = now + timedelta(seconds=1)
        await repo.save_source(state.model_copy(update={
            "revision": state.revision + 1, "window_end": future,
            "complete_watermark": future, "last_success_at": future,
        }), expected_revision=state.revision)
        assert await build_update_source_binding(
            session, owner="local_owner", listing_ref=case.listing_ref,
            observed_at=now, knowledge_cutoff=manifest.effective_cutoff,
        ) is None
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=now)
        assert "source_coverage_unknown:controlled_local_v1" in limits

        state = await repo.get_source(
            "local_owner", source.source_id, manifest.update_source_binding.scope_key)
        assert state is not None
        changed_payload = {**old_receipt.payload, "sources": [{
            **old_receipt.payload["sources"][0], "content_hash": "b" * 64,
        }]}
        changed_receipt = await repo.append_change(
            "local_owner", change_key="p4-changed-source-recheck",
            kind="evidence_revision", payload=changed_payload,
            acquired_at=now, available_from=now, committed_at=now,
            listing_id=int(case.listing_ref.id), context="company_research")
        await repo.save_source(state.model_copy(update={
            "revision": state.revision + 1, "success_sequence": changed_receipt.sequence,
            "success_receipt_hash": changed_receipt.payload_hash,
        }), expected_revision=state.revision)
        limits = await published_manifest_limits(
            session, owner="local_owner", manifest=manifest, at=now)
        assert "source_coverage_unknown:controlled_local_v1" in limits
