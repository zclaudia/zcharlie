"""Independent service checks using real SQLite, saved rules and raw sources."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    EvaluationRequest,
    FrozenManifest,
    ResearchEvent,
    RuleDefinition,
)
from investment_agent.supplemental_rules.current_limits import application_limits
from investment_agent.supplemental_rules.service import SupplementalRuleService, validate_manifest


async def seed(session):
    now = datetime.now(UTC)
    session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Synthetic P2 Company")
    session.add(company)
    await session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600001")
    session.add(listing)
    await session.flush()
    reference = ObjectRef(kind="listing", id=listing.id, revision=1)
    rule = RuleDefinition(
        id=uuid4(), revision=1, name="Synthetic ROE check", rationale="Missing is unknown",
        source_refs=(), selector={"kind": "listing", "listing_ref": reference},
        contexts={"company_research"}, rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"factor_id": "roe", "operator": "<", "value": "0.05", "unit": "fraction"}]}},
        dimensions={"financial_cash"}, effects={"require_review"}, severity="warning",
        materiality="Synthetic fixture", valid_from=now-timedelta(days=1),
        review_due_at=now+timedelta(days=30), release_requirements=("Review exact evidence",),
    )
    store = SupplementalRulesRepository(session)
    await store.reserve_registry("local_owner")
    rule = await store.save_rule_revision("local_owner", rule)
    await store.put_rule_head("local_owner", rule.id, expected_head_revision=0,
        active_revision=1, active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    await store.advance_registry("local_owner", rules_delta=1, updated_at=now)
    await session.flush()
    return reference, rule


def request(reference):
    return EvaluationRequest(idempotency_key=uuid4(), listing_refs=(reference,),
        contexts={"company_research"}, mode="live")


async def test_real_freeze_save_retry_and_current_limits(db_session):
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    assert validate_manifest(frozen) == frozen
    assert len(frozen.pairs) == 1
    application = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    assert application.condition_result == "unknown"
    assert application.open_issue_refs == (application.reference,)
    again = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    assert again == application
    assert again.created_at == application.created_at
    limits = await application_limits(service, application)
    assert limits.current_status == "valid", limits


async def test_competing_frozen_predecessors_conflict_but_same_operation_recovers(db_session):
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    one, two = request(reference), request(reference)
    frozen_one = await service.freeze(one, as_of=datetime.now(UTC))
    frozen_two = await service.freeze(two, as_of=datetime.now(UTC))
    original = await service.save_pair(frozen_one, frozen_one.pairs[0], operation_id=one.idempotency_key)
    with pytest.raises(InvestorError) as caught:
        await service.save_pair(frozen_two, frozen_two.pairs[0], operation_id=two.idempotency_key)
    assert caught.value.detail.code == "item_binding_conflict"
    assert await service.save_pair(frozen_one, frozen_one.pairs[0], operation_id=one.idempotency_key) == original


async def test_replay_audit_does_not_advance_live_chain(db_session):
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    live = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    replay = await service.save_pair(frozen, frozen.pairs[0], operation_id=uuid4(), mode="historical_replay")
    assert replay.application_sequence > live.application_sequence
    assert (await service.latest_live(reference, "company_research"))[0].reference == live.reference
    next_frozen = await service.freeze(request(reference), as_of=datetime.now(UTC))
    assert next_frozen.pairs[0].prior_live_application_ref == live.reference


async def test_issue_identity_edit_preserves_old_problem_without_reopening_each_run(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    first_request = request(reference)
    frozen = await service.freeze(first_request, as_of=datetime.now(UTC))
    first = await service.save_pair(frozen, frozen.pairs[0], operation_id=first_request.idempotency_key)
    second_request = request(reference)
    second_frozen = await service.freeze(second_request, as_of=datetime.now(UTC))
    second = await service.save_pair(second_frozen, second_frozen.pairs[0], operation_id=second_request.idempotency_key)
    assert second.open_issue_refs == first.open_issue_refs
    changed = rule.model_copy(update={"revision": 2, "issue_key": "new-problem", "content_hash": None, "semantic_hash": None})
    changed = await service.store.save_rule_revision("local_owner", changed)
    await service.store.put_rule_head("local_owner", rule.id, expected_head_revision=1,
        active_revision=2, active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    next_request = request(reference)
    next_frozen = await service.freeze(next_request, as_of=datetime.now(UTC))
    latest = await service.save_pair(next_frozen, next_frozen.pairs[0], operation_id=next_request.idempotency_key)
    assert latest.series_key == first.series_key
    assert latest.previous_application_ref == second.reference
    assert latest.open_issue_refs == (first.reference, latest.reference)


async def test_retired_rule_old_issue_remains_and_zero_target_change_is_typed(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    first = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    await service.store.put_rule_head("local_owner", rule.id, expected_head_revision=1,
        active_revision=1, active_state="retired", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    later_request = request(reference)
    later = await service.freeze(later_request, as_of=datetime.now(UTC))
    assert later.pairs[0].rule_state == "retired"
    result = await service.save_pair(later, later.pairs[0], operation_id=later_request.idempotency_key)
    assert result.open_issue_refs == first.open_issue_refs
    assert result.effective_effects == ()
    assert result.assessment_status == "unresolved"


async def test_tampered_manifest_rejected_before_save(db_session):
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    with pytest.raises(InvestorError) as caught:
        await service.save_pair(frozen.model_copy(update={"manifest_hash": "a"*64}), frozen.pairs[0], operation_id=command.idempotency_key)
    assert caught.value.detail.code == "historical_manifest_unavailable"


async def test_irrelevant_generation_does_not_stale_but_new_relevant_event_does(db_session):
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    app = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    at = datetime.now(UTC)
    def event(subject):
        return ResearchEvent(id=uuid4(), revision=1, owner_id="local_owner", event_key=str(uuid4()),
            title="Synthetic event", event_type="user_report", jurisdiction="unknown",
            affected_products=(), affected_subjects=(subject,), policy_stage="unknown",
            acquired_at=at, available_from=at, source_refs=(), verification="user_report", conflict=False)
    unrelated = event("company:99999")
    await service.store.save_research_event("local_owner", unrelated, created_at=at)
    await service.store.advance_registry("local_owner", events_delta=1, updated_at=at)
    assert (await application_limits(service, app)).current_status == "valid"
    related = event(f"listing:{reference.id}")
    await service.store.save_research_event("local_owner", related, created_at=at)
    await service.store.advance_registry("local_owner", events_delta=1, updated_at=at)
    limits = await application_limits(service, app)
    assert limits.current_status == "stale"
    assert "supplemental_sources_changed" in limits.reasons
    current = await service.current(reference, "company_research")
    assert any(value.id == related.id for value in current.unbound_events)
    assert current.coverage_complete is False


async def test_current_get_performs_no_database_writes(db_session):
    from sqlalchemy import event
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    writes = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            writes.append(statement)
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        view = await service.current(reference, "company_research")
        assert len(view.applications) == 1
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert writes == []


async def test_real_manifest_byte_ceiling_is_enforced_independently_of_pair_count(db_session):
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    frozen = await service.freeze(request(reference), as_of=datetime.now(UTC))
    first = frozen.pairs[0]
    pairs = []
    for ordinal in range(2000):
        rule = first.rule.model_copy(update={"id": uuid4(), "content_hash": None, "semantic_hash": None})
        pair = first.model_copy(update={"ordinal": ordinal, "rule_ref": rule.reference, "rule": rule})
        pair = pair.model_copy(update={"input_hash": canonical_hash(pair.model_dump(mode="python", exclude={"input_hash"}))})
        pairs.append(pair)
    small = FrozenManifest.model_validate({**frozen.model_dump(mode="python"), "pairs": tuple(pairs[:100])})
    assert len(small.pairs) == 100
    with pytest.raises(ValidationError, match="manifest exceeds"):
        FrozenManifest.model_validate({**frozen.model_dump(mode="python"), "pairs": tuple(pairs)})


async def test_context_removed_in_new_revision_only_carries_old_issues(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    first_request = request(reference)
    frozen = await service.freeze(first_request, as_of=datetime.now(UTC))
    first = await service.save_pair(frozen, frozen.pairs[0], operation_id=first_request.idempotency_key)
    replacement = rule.model_copy(update={"revision": 2, "contexts": frozenset({"candidate"}), "content_hash": None, "semantic_hash": None})
    await service.store.save_rule_revision("local_owner", replacement)
    await service.store.put_rule_head("local_owner", rule.id, expected_head_revision=1,
        active_revision=2, active_state="active", draft_revision=None, draft_state=None,
        last_validation_revision=None, last_validation_hash=None, last_validation_payload=None)
    next_request = request(reference)
    next_frozen = await service.freeze(next_request, as_of=datetime.now(UTC))
    assert next_frozen.pairs[0].rule_state == "retired"
    next_app = await service.save_pair(next_frozen, next_frozen.pairs[0], operation_id=next_request.idempotency_key)
    assert next_app.open_issue_refs == first.open_issue_refs
    assert next_app.effective_effects == ()


async def test_current_issue_groups_merge_shared_identity_across_pages_without_losing_bases(db_session):
    reference, rule = await seed(db_session)
    service = SupplementalRuleService(db_session)
    for index, severity in enumerate(("warning", "critical")):
        changed = rule.model_copy(update={"id": rule.id if index == 0 else uuid4(),
            "revision": 2 if index == 0 else 1, "issue_key": "same-exposure",
            "severity": severity, "content_hash": None, "semantic_hash": None})
        await service.store.save_rule_revision("local_owner", changed)
        await service.store.put_rule_head("local_owner", changed.id,
            expected_head_revision=1 if index == 0 else 0,
            active_revision=changed.revision, active_state="active", draft_revision=None,
            draft_state=None, last_validation_revision=None, last_validation_hash=None,
            last_validation_payload=None)
    command = request(reference)
    manifest = await service.freeze(command, as_of=datetime.now(UTC))
    for pair in manifest.pairs:
        await service.save_pair(manifest, pair, operation_id=command.idempotency_key)
    first = await service.current(reference, "company_research", limit=1)
    assert first.cursor
    assert len(first.applications) == len(first.issue_groups) == 1
    group = first.issue_groups[0]
    assert len(group.application_refs) == len(group.rule_refs) == len(group.bases) == 2
    assert {basis.severity for basis in group.bases} == {"warning", "critical"}
    second = await service.current(reference, "company_research", limit=1, cursor=first.cursor)
    assert second.cursor is None
    assert second.issue_groups[0] == group
    assert second.applications[0].payload.id != first.applications[0].payload.id


def test_display_group_identity_merges_event_revisions_but_not_unrelated_unkeyed_rules():
    from investment_agent.supplemental_rules.issues import display_issue_key
    event_id = uuid4()
    common = {"company_id": 1, "issue_key": None, "dimension": "downside_counter_case"}
    assert display_issue_key(**common, event_ref=ObjectRef(kind="research_event", id=event_id, revision=1)) == display_issue_key(
        **common, event_ref=ObjectRef(kind="research_event", id=event_id, revision=2))
    assert display_issue_key(**common, event_ref=None, rule_id="one") != display_issue_key(
        **common, event_ref=None, rule_id="two")


async def test_deleted_source_stales_current_but_frozen_replay_retains_original(db_session):
    from investment_agent.storage.models.pit_snapshot import IndustryClassification
    reference, _ = await seed(db_session)
    now = datetime.now(UTC)
    classification = IndustryClassification(listing_id=reference.id, system="csrc",
        code="C39", name="Synthetic classification", valid_from=(now-timedelta(days=3)).date(),
        source="synthetic", captured_at=now-timedelta(days=2))
    db_session.add(classification)
    await db_session.flush()
    service = SupplementalRuleService(db_session)
    command = request(reference)
    frozen = await service.freeze(command, as_of=datetime.now(UTC))
    original = await service.save_pair(frozen, frozen.pairs[0], operation_id=command.idempotency_key)
    original_json = original.model_dump_json()
    await db_session.delete(classification)
    await db_session.flush()
    assert (await application_limits(service, original)).current_status == "stale"
    exact = await service.exact(original.reference)
    assert exact.payload.model_dump_json() == original_json
    replay = await service.save_pair(frozen, frozen.pairs[0], operation_id=uuid4(), mode="historical_replay")
    assert replay.frozen_pair.subject.source_manifest == original.frozen_pair.subject.source_manifest
    assert (await service.latest_live(reference, "company_research"))[0].reference == original.reference


async def test_unsupported_frozen_method_is_rejected_even_with_recomputed_manifest_hash(db_session):
    from investment_agent.supplemental_rules.service import manifest_digest
    reference, _ = await seed(db_session)
    service = SupplementalRuleService(db_session)
    frozen = await service.freeze(request(reference), as_of=datetime.now(UTC))
    changed = frozen.model_copy(update={"evaluator_version": "unavailable-version"})
    digest = manifest_digest(changed)
    changed = changed.model_copy(update={"input_hash": digest, "manifest_hash": digest})
    with pytest.raises(InvestorError) as caught:
        await service.save_pair(changed, changed.pairs[0], operation_id=uuid4())
    assert caught.value.detail.code == "historical_manifest_unavailable"
    assert await service.latest_live(reference, "company_research") == ()
