from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.contracts import ResearchEvent, RuleDefinition

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def _rule(identity=None, revision: int = 1) -> RuleDefinition:
    return RuleDefinition(
        id=identity or uuid4(), revision=revision, name="Synthetic", rationale="Fixture",
        source_refs=(), selector={"kind": "listing", "listing_ref": {"kind": "listing", "id": 7, "revision": 1}},
        contexts={"company_research"}, rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "revenue_growth", "operator": ">=", "value": "0.10", "unit": "ratio"}
        ]}},
        dimensions={"business_demand"}, effects={"require_review"}, severity="warning",
        materiality="Synthetic fixture", valid_from=NOW-timedelta(days=1),
        review_due_at=NOW+timedelta(days=30), release_requirements=("recheck",),
    )


@pytest.mark.asyncio
async def test_registry_reservation_zero_view_and_rule_head_repository(db_session) -> None:
    repo = SupplementalRulesRepository(db_session)
    assert await repo.get_registry_head("local_owner") == await repo.get_registry_head("local_owner")
    zero = await repo.get_registry_head("local_owner")
    assert zero.registry_revision == zero.rules_generation == zero.events_generation == 0
    reserved = await repo.reserve_registry("local_owner", reserved_at=NOW)
    assert reserved.registry_revision == 0

    rule = _rule()
    saved = await repo.save_rule_revision("local_owner", rule)
    assert saved.content_hash and saved.semantic_hash
    exact = await repo.get_rule_revision("local_owner", saved.reference)
    assert exact == saved
    head = await repo.put_rule_head(
        "local_owner", saved.id, expected_head_revision=0,
        active_revision=None, active_state=None, draft_revision=1, draft_state="draft",
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None,
    )
    assert head.head_revision == 1 and head.draft_revision == 1
    with pytest.raises(ValueError, match="conflict"):
        await repo.put_rule_head(
            "local_owner", saved.id, expected_head_revision=0,
            active_revision=None, active_state=None, draft_revision=1, draft_state="draft",
            last_validation_revision=None, last_validation_hash=None, last_validation_payload=None,
        )
    await repo.append_rule_event(
        "local_owner", saved.id, operation_id=uuid4(), command="draft_created",
        rule_revision=1, payload={"status": "draft"}, created_at=NOW,
    )
    events, more = await repo.list_rule_events("local_owner", saved.id, limit=10)
    assert not more and len(events) == 1 and events[0].sequence == 1


@pytest.mark.asyncio
async def test_public_event_repository_backdates_neither_acquisition_nor_visibility(db_session) -> None:
    repo = SupplementalRulesRepository(db_session)
    identity = uuid4()
    old = NOW - timedelta(days=100)
    event = ResearchEvent(
        id=identity, revision=1, owner_id="local_owner", event_key="synthetic-event",
        title="Synthetic report", event_type="user_report", jurisdiction="CN",
        affected_products=("Synthetic",), affected_subjects=(), policy_stage="unknown",
        published_at=old, effective_from=None, effective_to=None,
        acquired_at=old, available_from=old, source_refs=(), verification="user_report",
        conflict=False, previous_ref=None,
    )
    saved = await repo.save_research_event("local_owner", event, created_at=NOW)
    assert saved.acquired_at == NOW and saved.available_from == NOW
    exact_ref = ObjectRef(kind="research_event", id=identity, revision=1)
    assert await repo.get_event_revision("local_owner", exact_ref) == saved
    before, truncated = await repo.list_events_visible_by(
        "local_owner", cutoff=NOW-timedelta(seconds=1), visible_at=NOW+timedelta(days=1), limit=10,
    )
    assert not truncated and before == ()
    after, truncated = await repo.list_events_visible_by(
        "local_owner", cutoff=NOW, visible_at=NOW, limit=10,
    )
    assert not truncated and after == (saved,)

    verified = saved.model_copy(update={"id": uuid4(), "verification": "verified"})
    with pytest.raises(ValueError, match="user_report"):
        await repo.save_research_event("local_owner", verified, created_at=NOW)


@pytest.mark.asyncio
async def test_rule_repository_recomputes_immutable_payload_hashes(db_session) -> None:
    repo = SupplementalRulesRepository(db_session)
    rule = await repo.save_rule_revision("local_owner", _rule())
    await db_session.execute(text("DROP TRIGGER trg_supplemental_rule_revision_immutable_update"))
    await db_session.execute(text(
        "UPDATE supplemental_rule_revision SET payload=json_set(payload, '$.name', 'tampered') WHERE id=:id"
    ), {"id": str(rule.id)})
    db_session.expunge_all()
    with pytest.raises(ValueError, match="recomputed content"):
        await repo.get_rule_revision("local_owner", rule.reference)


@pytest.mark.asyncio
async def test_rule_application_storage_branch_is_exact_and_preserves_p1_shape(db_session) -> None:
    from investment_agent.supplemental_rules.canonical import canonical_hash, stable_series_key
    from investment_agent.supplemental_rules.contracts import (
        EvaluationResult,
        EvidenceFacet,
        FrozenPair,
        FrozenSubject,
        RuleApplication,
    )

    rule = _rule()
    listing_ref = ObjectRef(kind="listing", id=7, revision=1)
    subject = FrozenSubject(
        listing_ref=listing_ref, company_id=70, ticker="600007", name="Synthetic",
        as_of=NOW, input_status="partial", selector_inputs={}, factor_values={},
        evidence=(), source_manifest=(), diagnostics=("synthetic missing fact",),
    )
    pair_hash = "a" * 64
    pair = FrozenPair(
        ordinal=0, listing_ref=listing_ref, context="company_research", rule_ref=rule.reference,
        rule=rule, subject=subject, selected_event=None, prior_live_application_ref=None,
        prior_open_issue_refs=(), prior_issue_keys=(), prior_input_hash=None, input_hash=pair_hash,
        rule_state="active", match_status="applicable", matching_manifest_hash="b" * 64,
        diagnostics=(),
    )
    source_manifest_hash = "c" * 64
    application_input_hash = canonical_hash({
        "pair": pair, "manifest_hash": source_manifest_hash, "mode": "live", "purpose": "coverage",
    })
    pending_facet = EvidenceFacet(status="pending", summary=None, evidence_refs=(), unknowns=("not checked",), claims=())
    result = EvaluationResult(
        applicability="applicable", condition_result="triggered", evidence_status="pending",
        assessment_status="unresolved", semantic_status="mechanical", effective_effects=(),
        diagnostics=("synthetic",), open_issue_refs=(), closure_refs=(), event_fact=pending_facet,
        company_exposure=pending_facet, impact_assessment=pending_facet,
    )
    application = RuleApplication(
        id=uuid4(), operation_id=uuid4(), owner_id="local_owner", listing_ref=listing_ref,
        company_id=70, context="company_research", rule_ref=rule.reference,
        rules_generation=1, events_generation=0, matching_manifest_hash="b"*64,
        event_ref=None, as_of=NOW, evaluated_at=NOW+timedelta(seconds=1), valid_until=None,
        review_due_at=NOW+timedelta(days=30), input_hash=application_input_hash, matcher_version="test-matcher-1",
        evaluator_version="test-evaluator-1", dependencies=(), series_key=stable_series_key(
            "local_owner", 7, rule.id, "company_research"
        ), application_sequence=1, application_mode="live", origin_ref=None,
        created_at=NOW+timedelta(seconds=1), actor="operator", previous_application_ref=None,
        frozen_pair=pair, source_manifest_hash=source_manifest_hash, evaluation_purpose="coverage",
        applicability="applicable", condition_result="triggered", evidence_status="pending",
        assessment_status="unresolved", semantic_status="mechanical", semantic_review_ref=None,
        effective_effects=(), open_issue_refs=(), closure_refs=(), result=result, payload={"synthetic": True},
    )
    repo = InvestmentAssessmentRepository(db_session)
    saved = await repo.save_rule_application("local_owner", application)
    exact = await repo.get_exact("local_owner", application.reference, expected_kind="rule_application")
    assert exact == saved
    row = await db_session.get(InvestmentAssessment, (str(application.id), 1))
    assert row is not None
    assert row.account_id is None and row.series_key == application.series_key
    assert row.application_sequence == 1 and row.application_mode == "live"
