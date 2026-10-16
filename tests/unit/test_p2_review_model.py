from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from investment_agent.investor.common import ObjectRef
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.contracts import (
    EvaluationResult,
    EvidenceFacet,
    RuleDefinition,
)


@pytest.mark.parametrize("assessment_status", ["supported", "mitigated", "resolved"])
@pytest.mark.parametrize("semantic_status", ["mechanical", "pending"])
def test_unreviewed_result_cannot_claim_semantic_conclusion(
    assessment_status: str, semantic_status: str,
) -> None:
    not_applicable = EvidenceFacet(status="not_applicable")
    with pytest.raises(ValidationError, match="cannot claim semantic conclusions"):
        EvaluationResult(
            applicability="applicable",
            condition_result="triggered",
            evidence_status="pending",
            assessment_status=assessment_status,
            semantic_status=semantic_status,
            event_fact=not_applicable,
            company_exposure=not_applicable,
            impact_assessment=not_applicable,
        )


def test_reviewed_and_resolved_result_requires_coherent_review_and_closure() -> None:
    not_applicable = EvidenceFacet(status="not_applicable")
    issue = ObjectRef(kind="rule_application", id=uuid4(), revision=1)
    review = ObjectRef(kind="review", id=uuid4(), revision=1)
    base = {
        "applicability": "applicable",
        "condition_result": "not_triggered",
        "evidence_status": "verified",
        "semantic_status": "reviewed",
        "event_fact": not_applicable,
        "company_exposure": not_applicable,
        "impact_assessment": not_applicable,
    }
    with pytest.raises(ValidationError, match="requires an exact semantic review"):
        EvaluationResult(**base, assessment_status="resolved")
    with pytest.raises(ValidationError, match="resolved evaluation requires"):
        EvaluationResult(
            **base, assessment_status="resolved", semantic_review_ref=review,
        )
    with pytest.raises(ValidationError, match="both open and closed"):
        EvaluationResult(
            **base, assessment_status="unresolved", semantic_review_ref=review,
            open_issue_refs=(issue,), closure_refs=(issue,),
        )

    accepted = EvaluationResult(
        **base, assessment_status="resolved", semantic_review_ref=review,
        closure_refs=(issue,),
    )
    assert accepted.assessment_status == "resolved"


@pytest.mark.asyncio
async def test_resaving_existing_rule_revision_checks_storage_hash_columns(db_session) -> None:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    definition = RuleDefinition(
        id=uuid4(), revision=1, name="Synthetic", rationale="Hash identity regression",
        source_refs=(), selector={"kind": "company", "company_id": 7},
        contexts={"company_research"}, rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "revenue_growth", "operator": ">=", "value": "0.1"}
        ]}},
        dimensions={"business_demand"}, effects={"require_review"}, severity="warning",
        materiality="Synthetic", valid_from=now - timedelta(days=1),
        review_due_at=now + timedelta(days=30), release_requirements=("recheck",),
    )
    repo = SupplementalRulesRepository(db_session)
    saved = await repo.save_rule_revision("local_owner", definition)
    await db_session.execute(text(
        "DROP TRIGGER trg_supplemental_rule_revision_immutable_update"
    ))
    await db_session.execute(text(
        "UPDATE supplemental_rule_revision SET content_hash=:bad WHERE id=:id AND revision=1"
    ), {"bad": "f" * 64, "id": str(saved.id)})
    with pytest.raises(ValueError, match="recomputed content"):
        await repo.save_rule_revision("local_owner", definition)


@pytest.mark.asyncio
async def test_rule_schema_version_column_must_match_payload(db_session) -> None:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    definition = RuleDefinition(
        id=uuid4(), revision=1, name="Schema", rationale="Header integrity",
        source_refs=(), selector={"kind": "company", "company_id": 7},
        contexts={"company_research"}, rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "revenue_growth", "operator": ">=", "value": "0.1"}
        ]}},
        dimensions={"business_demand"}, effects={"require_review"}, severity="warning",
        materiality="Header integrity", valid_from=now, review_due_at=now + timedelta(days=30),
        release_requirements=("recheck",),
    )
    repo = SupplementalRulesRepository(db_session)
    saved = await repo.save_rule_revision("local_owner", definition)
    await db_session.execute(text("DROP TRIGGER trg_supplemental_rule_revision_immutable_update"))
    await db_session.execute(text("PRAGMA ignore_check_constraints=ON"))
    await db_session.execute(text(
        "UPDATE supplemental_rule_revision SET schema_version=2 WHERE id=:id"
    ), {"id": str(saved.id)})
    db_session.expunge_all()
    with pytest.raises(ValueError, match="schema_version column does not match payload"):
        await repo.get_rule_revision("local_owner", saved.reference)


@pytest.mark.asyncio
@pytest.mark.parametrize("column,value", [
    ("event_key", "tampered-event-key"),
    ("previous_ref", '{"kind":"research_event","id":"11111111-1111-4111-8111-111111111111","revision":1}'),
    ("published_at", "2026-10-15 11:00:00"),
    ("effective_from", "2026-10-15 11:00:00"),
    ("effective_to", "2026-10-17 11:00:00"),
    ("acquired_at", "2026-10-15 11:00:00"),
    ("available_from", "2026-10-15 11:00:00"),
])
async def test_event_duplicate_header_damage_is_not_hidden_as_unknown(
    db_session, column: str, value: str,
) -> None:
    from investment_agent.supplemental_rules.contracts import ResearchEvent

    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    identity = uuid4()
    event = ResearchEvent(
        id=identity, revision=1, owner_id="local_owner", event_key="synthetic-event",
        title="Synthetic report", event_type="user_report", jurisdiction="CN",
        affected_products=("Synthetic",), affected_subjects=(), policy_stage="unknown",
        published_at=now - timedelta(days=1), effective_from=now - timedelta(days=1),
        effective_to=now + timedelta(days=1), source_refs=(), verification="user_report",
        conflict=False, previous_ref=None, acquired_at=now, available_from=now,
    )
    repo = SupplementalRulesRepository(db_session)
    saved = await repo.save_research_event("local_owner", event, created_at=now)
    await db_session.execute(text("DROP TRIGGER trg_research_event_revision_immutable_update"))
    await db_session.execute(text(
        f"UPDATE research_event_revision SET {column}=:value WHERE id=:id AND revision=1"
    ), {"value": value, "id": str(identity)})
    db_session.expunge_all()
    with pytest.raises(ValueError, match=f"event {column} column does not match payload"):
        await repo.get_event_revision("local_owner", saved.reference)
    with pytest.raises(ValueError, match=f"event {column} column does not match payload"):
        await repo.list_events_visible_by(
            "local_owner", cutoff=now + timedelta(days=1),
            visible_at=now + timedelta(days=1), limit=10,
        )
