"""AF-P3-04 integration: the real admit boundary writes (or skips) bindings."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.supplemental_rules import (
    ResearchRuleBindingRow,
)
from investment_agent.supplemental_rules.binding import get_task_binding
from tests.support.research_closure import (
    BASE_SETTINGS,
    BOUND_SETTINGS,
    activate_rule,
    admit_research,
    metric_rule,
    seed_listing,
)


async def test_switch_on_binds_company_research_and_review_tasks(db_session):
    listing_ref = await seed_listing(db_session)
    rule = await activate_rule(db_session, metric_rule(listing_ref))
    research = await admit_research(db_session, listing_ref, research_kind="company_research")
    review = await admit_research(db_session, listing_ref, research_kind="review")
    bound = await get_task_binding(db_session, owner="local_owner", task_id=research.id)
    assert bound is not None
    assert [entry.rule_ref for entry in bound.rule_set] == [rule.reference]
    review_binding = await get_task_binding(db_session, owner="local_owner", task_id=review.id)
    assert review_binding is not None
    assert review_binding.rule_set_hash == bound.rule_set_hash
    rows = list((await db_session.scalars(select(ResearchRuleBindingRow))).all())
    assert len(rows) == 2


async def test_switch_off_admits_legacy_without_binding_rows(db_session):
    listing_ref = await seed_listing(db_session)
    await activate_rule(db_session, metric_rule(listing_ref))
    task = await admit_research(db_session, listing_ref, settings=BASE_SETTINGS)
    assert await get_task_binding(db_session, owner="local_owner", task_id=task.id) is None
    assert list((await db_session.scalars(select(ResearchRuleBindingRow))).all()) == []


async def test_non_research_kinds_never_bind_even_when_enabled(db_session):
    listing_ref = await seed_listing(db_session)
    await activate_rule(db_session, metric_rule(listing_ref))
    task = await admit_research(db_session, listing_ref, research_kind="explanation")
    assert await get_task_binding(db_session, owner="local_owner", task_id=task.id) is None


async def test_same_input_key_readmission_reuses_the_original_binding(db_session):
    listing_ref = await seed_listing(db_session)
    await activate_rule(db_session, metric_rule(listing_ref))
    input_key = f"p3-retry-{uuid4()}"
    as_of = datetime.now(UTC).replace(microsecond=0)
    task = await admit_research(db_session, listing_ref, input_key=input_key, as_of=as_of)
    original = await get_task_binding(db_session, owner="local_owner", task_id=task.id)
    assert original is not None
    # New rule activated between the two admissions must not leak in.
    await activate_rule(db_session, metric_rule(listing_ref))
    retried = await admit_research(db_session, listing_ref, input_key=input_key, as_of=as_of)
    assert retried.id == task.id
    again = await get_task_binding(db_session, owner="local_owner", task_id=task.id)
    assert again == original and len(again.rule_set) == 1


async def test_binder_failure_denies_admission(db_session):
    # A non-A-share subject cannot be frozen; with an active rule the binder
    # must fail the admission instead of silently writing an unbound task.
    db_session.add(Market(code="HKEX", name="HKEX", timezone="Asia/Hong_Kong", currency="HKD"))
    company = Company(canonical_name="P3 unsupported issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="HKEX", ticker="00001")
    db_session.add(listing)
    await db_session.flush()
    listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
    await activate_rule(db_session, metric_rule(listing_ref))
    with pytest.raises(ExecutionDeniedError, match="research_rule_binding_failed"):
        await admit_research(db_session, listing_ref, research_kind="company_research")
    assert list((await db_session.scalars(select(ResearchRuleBindingRow))).all()) == []


async def test_multi_subject_admission_is_denied_while_bound(db_session):
    from datetime import UTC, datetime

    from investment_agent.agent.contracts import ContextBinding
    from investment_agent.agent.execution import ExecutionGrant, admit

    first = await seed_listing(db_session, ticker="600201")
    second = await seed_listing(db_session, ticker="600202")
    await activate_rule(db_session, metric_rule(first))
    grant = ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Compare both",
        tool_names=(), scope_refs=(first, second), research_kind="company_research")
    with pytest.raises(ExecutionDeniedError, match="research_rule_binding_subject_unavailable"):
        await admit(db_session, base=BOUND_SETTINGS, input_key=f"p3-multi-{uuid4()}",
            binding=ContextBinding(context_refs=(first,), base_refs=(first,), as_of=datetime.now(UTC)),
            grant=grant)


async def test_unsupported_subject_with_no_rules_still_binds_empty(db_session):
    # No candidate rules means no matching and no subject freeze: the legal
    # empty-set binding records "no effective rules at admission".
    db_session.add(Market(code="HKEX", name="HKEX", timezone="Asia/Hong_Kong", currency="HKD"))
    company = Company(canonical_name="P3 empty binding issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="HKEX", ticker="00002")
    db_session.add(listing)
    await db_session.flush()
    task = await admit_research(db_session, ObjectRef(kind="listing", id=listing.id, revision=1))
    binding = await get_task_binding(db_session, owner="local_owner", task_id=task.id)
    assert binding is not None and binding.rule_set == ()
