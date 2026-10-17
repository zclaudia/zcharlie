"""AF-P3-04 admission binding: bounded enumeration, idempotency, honest unknowns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.binding import (
    BindingScanOverflowError,
    _match_rows,
    binding_identity,
    create_task_binding,
    get_binding,
    get_task_binding,
)
from investment_agent.supplemental_rules.contracts import FrozenSubject
from tests.support.research_closure import activate_rule, metric_rule, seed_listing


def _subject(listing_ref: ObjectRef) -> FrozenSubject:
    return FrozenSubject(listing_ref=listing_ref, company_id=70, ticker="600101", name="Synthetic",
        as_of=datetime.now(UTC), input_status="partial", selector_inputs={}, factor_values={},
        evidence=(), source_manifest=(), diagnostics=())


async def test_binding_freezes_rule_set_and_is_idempotent(db_session):
    listing_ref = await seed_listing(db_session)
    rule = await activate_rule(db_session, metric_rule(listing_ref))
    task_id = uuid4()
    method_ref = ObjectRef(kind="method", id=uuid4(), revision=1)

    binding = await create_task_binding(db_session, owner="local_owner", task_id=task_id,
        listing_ref=listing_ref, research_kind="company_research", method_ref=method_ref,
        as_of=datetime.now(UTC))

    assert binding.id == binding_identity("local_owner", task_id)
    assert binding.task_ref == ObjectRef(kind="task", id=task_id, revision=1)
    assert binding.research_kind == "company_research"
    assert [entry.rule_ref for entry in binding.rule_set] == [rule.reference]
    assert binding.rule_set_hash != "0" * 64
    assert binding.events_visible_until is not None
    assert binding.events_visible_until <= binding.admission_as_of
    assert binding.events_visible_unknown is False
    assert binding.matching_manifest_hash is not None

    # A rule activated after admission never enters the original binding.
    await activate_rule(db_session, metric_rule(listing_ref))
    again = await create_task_binding(db_session, owner="local_owner", task_id=task_id,
        listing_ref=listing_ref, research_kind="company_research", method_ref=method_ref,
        as_of=datetime.now(UTC))
    assert again == binding
    assert len(again.rule_set) == 1


async def test_binding_deterministic_across_tasks_and_empty_set_is_legal(db_session):
    listing_ref = await seed_listing(db_session)
    first = await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
        listing_ref=listing_ref, research_kind="review",
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))
    second = await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
        listing_ref=listing_ref, research_kind="review",
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))
    # No rules exist: both tasks carry the legal empty set with equal hashes,
    # machine-distinguishable from legacy tasks that have no binding row.
    assert first.rule_set == () and second.rule_set == ()
    assert first.rule_set_hash == second.rule_set_hash
    assert first.matching_manifest_hash is None
    stored = await get_task_binding(db_session, owner="local_owner", task_id=first.task_ref.id)
    assert stored is not None and stored.id == first.id
    assert await get_task_binding(db_session, owner="local_owner", task_id=uuid4()) is None


async def test_unknown_category_match_is_recorded_not_guessed(db_session):
    from investment_agent.supplemental_rules.contracts import RuleDefinition

    listing_ref = await seed_listing(db_session)
    base = metric_rule(listing_ref).model_dump(mode="python")
    rule = RuleDefinition.model_validate({**base,
        "id": uuid4(), "content_hash": None, "semantic_hash": None,
        "selector": {"kind": "category", "expression": {"operator": "any", "terms": [
            {"kind": "industry", "system": "sw2021", "scheme_version": "2021.1", "code": "999999",
             "source_ref": {"kind": "evidence", "id": str(uuid4()), "revision": 1}}]}},
    })
    await activate_rule(db_session, rule)
    rows = _match_rows((rule,), _subject(listing_ref))
    # Missing classification evidence stays unknown; never not_applicable.
    assert rows[0]["status"] == "unknown"
    assert any("selector_source" in diagnostic or "classification" in diagnostic
               for diagnostic in rows[0]["diagnostics"])
    binding = await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
        listing_ref=listing_ref, research_kind="company_research",
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))
    assert [entry.rule_ref for entry in binding.rule_set] == [rule.reference]


async def test_owner_mismatch_reads_absent_and_write_rejects(db_session):
    listing_ref = await seed_listing(db_session)
    binding = await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
        listing_ref=listing_ref, research_kind="company_research",
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))
    # The only supported owner validates every read; anything else is refused.
    with pytest.raises(ValueError, match="unsupported owner"):
        await get_task_binding(db_session, owner="other_owner", task_id=binding.task_ref.id)
    with pytest.raises(ValueError, match="unsupported owner"):
        await get_binding(db_session, owner="other_owner", reference=binding.reference)
    with pytest.raises(ValueError, match="unsupported owner"):
        await create_task_binding(db_session, owner="other_owner", task_id=uuid4(),
            listing_ref=listing_ref, research_kind="company_research",
            method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))


async def test_rule_scan_overflow_fails_loudly(db_session, monkeypatch):
    listing_ref = await seed_listing(db_session)

    async def overflow(self, owner_id, *, limit, after_rule_id=None):
        return (), True

    monkeypatch.setattr(SupplementalRulesRepository, "list_active_rules", overflow)
    with pytest.raises(BindingScanOverflowError):
        await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
            listing_ref=listing_ref, research_kind="company_research",
            method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))


async def test_event_visibility_truncation_is_flagged(db_session, monkeypatch):
    listing_ref = await seed_listing(db_session)

    async def truncated(self, owner_id, *, cutoff, visible_at, limit):
        return (), True

    monkeypatch.setattr(SupplementalRulesRepository, "list_events_visible_by", truncated)
    binding = await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
        listing_ref=listing_ref, research_kind="company_research",
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))
    assert binding.events_visible_unknown is True


async def test_expired_rules_do_not_enter_the_binding(db_session):
    listing_ref = await seed_listing(db_session)
    now = datetime.now(UTC)
    expired = metric_rule(listing_ref).model_copy(update={
        "id": uuid4(), "content_hash": None, "semantic_hash": None,
        "valid_from": now - timedelta(days=10),
        "valid_until": now - timedelta(days=1),
        "review_due_at": now - timedelta(days=2),
    })
    await activate_rule(db_session, expired)
    review_due = metric_rule(listing_ref).model_copy(update={
        "id": uuid4(), "content_hash": None, "semantic_hash": None,
        "valid_from": now - timedelta(days=10),
        "review_due_at": now - timedelta(hours=1),
    })
    await activate_rule(db_session, review_due)
    binding = await create_task_binding(db_session, owner="local_owner", task_id=uuid4(),
        listing_ref=listing_ref, research_kind="company_research",
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), as_of=datetime.now(UTC))
    # Expired rules leave; past-review-due rules stay bound (active+review_due).
    assert [entry.rule_ref.id for entry in binding.rule_set] == [review_due.id]
