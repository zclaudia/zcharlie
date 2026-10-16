"""Review regressions against real stored P2 applications and read projections."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.supplemental_rules.contracts import ResearchEvent, RuleDefinition
from investment_agent.supplemental_rules.current_limits import application_limits
from investment_agent.supplemental_rules.service import SupplementalRuleService
from tests.unit.test_supplemental_service import request, seed


async def replace_rule(service, rule, **updates):
    changed = RuleDefinition.model_validate({**rule.model_dump(mode="python"), **updates,
        "revision": rule.revision + 1, "content_hash": None, "semantic_hash": None})
    changed = await service.store.save_rule_revision("local_owner", changed)
    head = await service.store.get_rule_head("local_owner", rule.id)
    await service.store.put_rule_head("local_owner", rule.id,
        expected_head_revision=head.head_revision, active_revision=changed.revision,
        active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    return changed


async def test_research_deadline_invalidates_old_application_and_refresh_restores_currency(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    before = datetime.now(UTC)
    due = before + timedelta(hours=1)
    await replace_rule(service, rule, rule_type="research_requirement", definition={
        "kind": "research_requirement", "question": "Check saved source",
        "required_evidence": ["saved_fact"], "completion_criteria": "Verified answer",
        "falsification_criteria": "Contradictory fact", "check_by": due,
        "incomplete_effects": ["require_review"],
    })
    command = request(reference)
    frozen = await service.freeze(command, as_of=before)
    app = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    after = due + timedelta(seconds=1)
    current = await service.freeze(request(reference), as_of=after)
    limits = await application_limits(service, app, manifest=current, now=after)
    assert limits.current_status == "stale"
    assert "research_check_due" in limits.reasons
    refreshed = await service.save_pair(current, current.pairs[0], operation_id=uuid4())
    assert refreshed.condition_result == "triggered"
    assert (await application_limits(service, refreshed, manifest=current, now=after)).current_status == "valid"


async def test_other_listing_rule_cannot_pollute_input_or_hide_unbound_event(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    baseline = await service.freeze(request(reference), as_of=datetime.now(UTC))
    company = Company(canonical_name="Different company")
    db_session.add(company)
    await db_session.flush()
    other = Listing(company_id=company.id, market_code="SSE", ticker="600002")
    db_session.add(other)
    await db_session.flush()
    other_ref = ObjectRef(kind="listing", id=other.id, revision=1)
    unrelated = RuleDefinition.model_validate({**rule.model_dump(mode="python"),
        "id": uuid4(), "source_refs": (other_ref,),
        "selector": {"kind": "listing", "listing_ref": other_ref},
        "definition": {"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "pe_ttm", "operator": ">", "value": "20"}]}},
        "content_hash": None, "semantic_hash": None})
    await service.store.save_rule_revision("local_owner", unrelated)
    await service.store.put_rule_head("local_owner", unrelated.id, expected_head_revision=0,
        active_revision=1, active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    changed = await service.freeze(request(reference), as_of=datetime.now(UTC))
    assert len(changed.pairs) == 2  # Coverage keeps the explicit not-applicable member.
    assert next(p for p in changed.pairs if p.rule_ref.id == unrelated.id).match_status == "not_applicable"
    assert service.scope_hash(changed, reference, "company_research") == service.scope_hash(baseline, reference, "company_research")
    at = datetime.now(UTC)
    event = ResearchEvent(id=uuid4(), revision=1, owner_id="local_owner", event_key="reported-for-first",
        title="Related event", event_type="user_report", jurisdiction="unknown", affected_products=(),
        affected_subjects=(f"listing:{reference.id}",), policy_stage="unknown", acquired_at=at,
        available_from=at, source_refs=(), verification="user_report", conflict=False)
    event = await service.store.save_research_event("local_owner", event, created_at=at)
    await replace_rule(service, unrelated, rule_type="event_impact", event_ref=event.reference,
        definition={"kind": "event_impact", "event_ref": event.reference,
            "affected_scope": ["Synthetic scope"], "exposure_evidence": ["saved_fact"],
            "impact_path": "Needs evidence", "release_requirements": ["Review source"]})
    unbound = await service.freeze(request(reference), as_of=datetime.now(UTC))
    assert event.id in {item.id for item in unbound.unbound_events}


async def test_capacity_fallback_paginates_existing_issues_and_validates_cursor(db_session, monkeypatch):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    other = rule.model_copy(update={"id": uuid4(), "content_hash": None, "semantic_hash": None})
    await service.store.save_rule_revision("local_owner", other)
    await service.store.put_rule_head("local_owner", other.id, expected_head_revision=0,
        active_revision=1, active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    for pair in frozen.pairs:
        await service.save_pair(frozen, pair, operation_id=command.idempotency_key)

    async def over_capacity(*args, **kwargs):
        raise InvestorError("source_capacity_exceeded", "Controlled source ceiling")

    monkeypatch.setattr(service, "freeze", over_capacity)
    first = await service.current(reference, "company_research", limit=1)
    assert first.cursor and not first.coverage_complete
    second = await service.current(reference, "company_research", limit=1, cursor=first.cursor)
    assert not second.cursor and not second.coverage_complete
    assert first.applications[0].payload.reference != second.applications[0].payload.reference
    assert all(view.current_limits.coverage_pending for view in (*first.applications, *second.applications))
    with pytest.raises(InvestorError) as error:
        await service.current(reference, "company_research", cursor="not-a-cursor")
    assert error.value.detail.code == "invalid_cursor"


async def test_current_does_not_hash_the_full_scope_for_each_application(db_session, monkeypatch):
    from investment_agent.supplemental_rules import service as module
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    for _ in range(4):
        other = rule.model_copy(update={"id": uuid4(), "content_hash": None, "semantic_hash": None})
        await service.store.save_rule_revision("local_owner", other)
        await service.store.put_rule_head("local_owner", other.id, expected_head_revision=0,
            active_revision=1, active_state="active", draft_revision=None, draft_state=None,
            last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    for pair in frozen.pairs:
        await service.save_pair(frozen, pair, operation_id=command.idempotency_key)
    calls = 0
    original = module.scope_digest

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Each member must reuse the current projection's scope digest")

    monkeypatch.setattr(module, "scope_digest", counted)
    monkeypatch.setattr(service, "scope_hash", forbidden)
    view = await service.current(reference, "company_research")
    assert len(view.applications) == 5
    assert calls <= 3


async def test_event_effective_boundaries_require_refresh_of_the_old_frozen_state(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    before = datetime.now(UTC)
    starts, ends = before + timedelta(hours=1), before + timedelta(hours=2)
    event = ResearchEvent(id=uuid4(), revision=1, owner_id="local_owner", event_key="synthetic-policy",
        title="Synthetic event", event_type="policy", jurisdiction="unknown",
        affected_products=(), affected_subjects=(f"listing:{reference.id}",), policy_stage="enacted",
        acquired_at=before, available_from=before, source_refs=(), verification="user_report",
        effective_from=starts, effective_to=ends, conflict=False)
    event = await service.store.save_research_event("local_owner", event, created_at=before)
    await replace_rule(service, rule, rule_type="event_impact", event_ref=event.reference,
        definition={"kind": "event_impact", "event_ref": event.reference,
            "affected_scope": ["Synthetic scope"], "exposure_evidence": ["saved_fact"],
            "impact_path": "Needs evidence", "release_requirements": ["Review source"]})
    command = request(reference)
    frozen = await service.freeze(command, as_of=before)
    app = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    after_start = starts + timedelta(seconds=1)
    current = await service.freeze(request(reference), as_of=after_start)
    assert "event_effective_started" in (await application_limits(service, app, manifest=current, now=after_start)).reasons
    refreshed = await service.save_pair(current, current.pairs[0], operation_id=uuid4())
    after_end = ends + timedelta(seconds=1)
    ended = await service.freeze(request(reference), as_of=after_end)
    assert "event_effective_ended" in (await application_limits(service, refreshed, manifest=ended, now=after_end)).reasons
