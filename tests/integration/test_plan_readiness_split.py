"""AF-P3-09 integration: buy/add fit & coverage gates, reduce/exit unblocked.

Real ledger + real AG23 publication chain. The buy gate runs only under its
switches; reduce/exit/wait keep the existing constraint-exception and company
paths with no fit or coverage preconditions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import PortfolioConstraints, TextNode
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    Scenario,
)
from investment_agent.portfolio.calculations import PortfolioCalculationService
from investment_agent.portfolio.plan_contracts import OptionInput, PlanInput, PortfolioAction
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.canonical import (
    canonical_hash,
    stable_series_key,
)
from investment_agent.supplemental_rules.contracts import (
    ResearchRuleBinding,
    RuleDefinition,
)
from tests.support.account_fit import assumptions
from tests.support.portfolio import (
    D,
    Rules,
    action,
    append,
    balance,
    base,
    mandate,
    prepared,
    publish,
    semantic,
    setup,
)
from tests.support.portfolio import ledger as ledger  # noqa: F401

HASH = "a" * 64


async def flip_flags(**flags) -> None:
    """Flip runtime switches through the real settings store.

    The one-time import freezes the runtime defaults, so the ledger switch
    that AG23 admission needs is applied explicitly alongside the P3 flags.
    """

    from investment_agent.settings.loader import store_for

    async def apply() -> None:
        async with session_scope() as session:
            store = store_for(session, base())
            await store.claim_import()
            await store.apply({"portfolio_ledger_enabled": True, **flags,
                               "investor_agent_enabled": True}, actor="p3-plan-split-test")

    await apply()


async def buy_setup(ledger, *, trusted=True):
    """A whole-account plan whose only action is a gate-eligible buy."""

    account, ref = ledger
    candidate, calc = await _buy_candidate(account, ref, trusted=trusted)
    view_ref = await company_view(candidate, ref)
    option = candidate.options[0]
    action_ = option.actions[0].model_copy(update={"company_view_refs": (view_ref,)})
    candidate = candidate.model_copy(update={"options": (option.model_copy(update={"actions": (action_,)}),)})
    return candidate, ref, calc


async def _buy_candidate(account, ref, *, trusted=True):
    now = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as session:
        if await session.scalar(select(MarketSnapshot.id).where(
                MarketSnapshot.listing_id == ref.id, MarketSnapshot.trade_date == now.date())) is None:
            session.add(MarketSnapshot(listing_id=ref.id, trade_date=now.date(), open=D(10), high=D(10), low=D(10), close=D(10), volume=D(1), adjust="none", provider="fixture", captured_at=now, version=1))
        classification = await session.scalar(
            select(IndustryClassification.id).where(
                IndustryClassification.listing_id == ref.id,
                IndustryClassification.system == "CSRC",
                IndustryClassification.valid_from == now.date()))
        if classification is None:
            session.add(IndustryClassification(listing_id=ref.id, system="CSRC", code="fixture-sector", name="Fixture", valid_from=now.date(), source="fixture", captured_at=now))
        foundation = InvestorService(session)
        try:
            m = await foundation.create_mandate("local_owner", mandate().model_copy(update={"scope_refs": (ref,), "hard_constraints": (), "portfolio_constraints": PortfolioConstraints(account_id=account, max_single_weight=D("0.3"), max_sector_weight=D("0.6"), minimum_cash=D(0))}), uuid4())
        except InvestorError as exc:
            if exc.detail.code != "revision_conflict":
                raise  # one mandate per owner; later chains reuse it
            m = await foundation.get_mandate("local_owner")
        revision = (await PortfolioService(session)._account(account)).revision
    # Only one active opening balance is legal; later chains reuse the
    # account's current snapshot.
    async with session_scope() as session:
        service = PortfolioService(session)
        account_row = await service._account(account)
        current = await service.snapshot(account) if account_row.current_snapshot_id else None
    if current is None:
        snapshot = (await append(account, balance(ref, coverage="complete", cash="1000", quantity="0"), revision)).snapshot
        snapshot_ref = snapshot.reference
    else:
        snapshot_ref = current.reference
    req = PortfolioCalculationRequest(operation_id=uuid4(), account_id=account, snapshot_ref=snapshot_ref, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=m.revision), scenarios=(Scenario(name="open_position", actions=(action(ref, quantity=D(20)),),),))
    async with session_scope() as session:
        calc = await PortfolioCalculationService(session, rules=Rules() if trusted else None).save(req)
    a = PortfolioAction(id=uuid4(), listing_ref=ref, stance="buy", basis="company_thesis", constraint=None, research_gap=None, target_ref=ObjectRef(kind="calculation", id=calc.id, revision=1, field_path="results.scenario_1_action_0_quantity"), content=(TextNode(type="text", text="Open a bounded position"),))
    candidate = PlanInput(account_id=account, snapshot_ref=snapshot_ref, mandate_ref=req.mandate_ref, calculation_ref=ObjectRef(kind="calculation", id=calc.id, revision=1), previous_ref=None, options=(OptionInput(id=uuid4(), scenario_index=1, actions=(a,)),), question_scope="Whole confirmed account alternatives")
    return candidate, calc


async def company_view(candidate, ref):
    from tests.test_portfolio_plans import company_view as seed_view

    return await seed_view(candidate, ref)


async def seed_fit(account, ref, *, expired=False, blocked=False) -> ObjectRef:
    """One account-fit row frozen from the live context (or explicitly stale)."""

    from investment_agent.portfolio.suitability_contracts import (
        AccountFitAssessment,
        FitRequestItem,
        FrozenAccountInputs,
    )
    from investment_agent.portfolio.suitability_inputs import SuitabilityInputService

    now = datetime.now(UTC)
    # The expired row is frozen two hours earlier, so its own as-of ordering
    # stays valid while its expiry has already passed at read time.
    frozen_at = now - timedelta(hours=2) if expired else now
    async with session_scope() as session:
        # The gate re-reads sources with the default (unavailable) rules
        # resolver, so the frozen row must be built from the same source view.
        inputs = SuitabilityInputService(session, "local_owner", None)
        context = await inputs.context(account, as_of=frozen_at)
        sources = await inputs.listing_sources((ref,), valuation=None, as_of=frozen_at)
        assessment_id = uuid4()
        assessment = AccountFitAssessment(
            id=assessment_id,
            reference=ObjectRef(kind="account_fit", id=assessment_id, revision=1),
            operation_id=uuid4(),
            input_hash=HASH,
            account_id=account,
            listing_ref=ref,
            action="open",
            request_item=FitRequestItem(listing_ref=ref, action="open", trade_assumptions=assumptions()),
            purpose_binding_key=HASH,
            calculation_basis_key=HASH,
            assessment_as_of=frozen_at,
            price_basis_at=None,
            expires_at=frozen_at + timedelta(hours=1),
            frozen_inputs=FrozenAccountInputs(
                account_id=account, snapshot=context.snapshot, valuation=None,
                valuation_binding=context.valuation_binding, mandate=context.mandate,
                purpose_version=None, purpose_binding=context.purpose_binding,
                listing_sources=sources, assessment_as_of=frozen_at, price_basis_at=None,
                mode="live", manifest_hash=HASH, source_fingerprint=HASH,
            ),
            financial_status="blocked" if blocked else "pass",
            checks=(),
            ranking_bucket="blocked" if blocked else "incomplete",
            coverage="complete",
        )
        await InvestmentAssessmentRepository(session).save_assessment("local_owner", assessment)
        return assessment.reference


async def seed_bound_coverage(listing_ref: ObjectRef) -> None:
    """One admission binding plus its published manifest (empty rule set)."""

    from investment_agent.supplemental_rules.contracts import RuleCoverageManifest

    async with session_scope() as session:
        store = SupplementalRulesRepository(session)
        now = datetime.now(UTC)
        task_id = uuid4()
        admission = now - timedelta(hours=1)
        # The content hash domain is python-mode, so identity fields stay typed.
        data: dict = {
            "schema_version": 1, "id": uuid4(), "revision": 1, "owner_id": "local_owner",
            "task_ref": ObjectRef(kind="task", id=task_id, revision=1),
            "listing_ref": listing_ref,
            "research_kind": "company_research",
            "method_ref": ObjectRef(kind="method", id=uuid4(), revision=1),
            "admission_as_of": admission, "rules_generation": 1, "events_generation": 0,
            "rule_set": (), "rule_set_hash": canonical_hash(()),
            "matching_manifest_hash": None, "events_visible_until": admission,
            "events_visible_unknown": False, "input_hash": HASH,
            "created_at": admission, "content_hash": "pending",
        }
        data["content_hash"] = canonical_hash({k: v for k, v in data.items() if k != "content_hash"})
        binding = ResearchRuleBinding.model_validate(data)
        await store.save_research_rule_binding("local_owner", binding)

        def manifest(**overrides) -> RuleCoverageManifest:
            body = {
                "schema_version": 1, "id": uuid4(), "revision": 1, "stage": "prepared",
                "owner_id": "local_owner",
                "task_ref": binding.task_ref,
                "task_id": task_id, "binding_ref": binding.reference,
                "listing_ref": listing_ref, "case_ref": None,
                "admission_as_of": admission, "effective_cutoff": now - timedelta(minutes=30),
                "rule_entries": (), "open_issue_refs": (), "closure_proposals": (),
                "scan_capped": False, "scan_known_total": 0, "scan_uncovered_note": None,
                "material_set_hash": None, "input_hash": HASH, "created_at": now,
                "prepared_ref": None, "prepared_manifest_hash": None,
                "semantic_review_ref": None, "closure_application_refs": (),
                "thesis_ref": None, "recommendation_ref": None, "limits_recheck": (),
                "committed_at": None, "manifest_hash": "pending",
            }
            body.update(overrides)
            body["manifest_hash"] = canonical_hash({k: v for k, v in body.items() if k != "manifest_hash"})
            return RuleCoverageManifest.model_validate(body)

        prepared = manifest()
        published = manifest(stage="published", prepared_ref=prepared.reference,
                            prepared_manifest_hash=prepared.manifest_hash,
                            semantic_review_ref=ObjectRef(kind="review", id=uuid4(), revision=1),
                            committed_at=now)
        await InvestmentAssessmentRepository(session).save_rule_coverage("local_owner", published)
        await session.flush()


def _critical_rule(listing_ref: ObjectRef) -> RuleDefinition:
    now = datetime.now(UTC)
    return RuleDefinition(
        id=uuid4(), revision=1, name="P3 plan critical rule",
        rationale="A critical rule must limit open-position plans.", source_refs=(),
        selector={"kind": "listing", "listing_ref": listing_ref.model_dump(mode="python")},
        contexts={"company_research"}, rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "roe", "operator": "<", "value": "0.05", "unit": "fraction"}]}},
        dimensions={"financial_cash"}, effects={"require_review"}, severity="critical",
        materiality="Synthetic fixture only.", valid_from=now - timedelta(days=1),
        review_due_at=now + timedelta(days=30), release_requirements=("Review inputs.",),
    )


async def seed_pending_critical_application(listing_ref: ObjectRef):
    """One live applicable critical application for the listing (synthetic)."""

    from investment_agent.supplemental_rules.contracts import RuleApplication
    from tests.support.supplemental_rules import synthetic_application

    rule = _critical_rule(listing_ref)
    template = synthetic_application()
    subject = template.frozen_pair.subject.model_copy(update={"listing_ref": listing_ref})
    pair = template.frozen_pair.model_copy(update={
        "rule": rule, "rule_ref": rule.reference, "listing_ref": listing_ref, "subject": subject,
    })
    values = template.model_copy(update={
        "listing_ref": listing_ref, "rule_ref": rule.reference, "frozen_pair": pair,
        "series_key": stable_series_key("local_owner", int(listing_ref.id), rule.id, "company_research"),
        "valid_until": rule.valid_until, "review_due_at": rule.review_due_at,
    }).model_dump(mode="python")
    values["input_hash"] = canonical_hash({
        "pair": values["frozen_pair"], "manifest_hash": values["source_manifest_hash"],
        "mode": values["application_mode"], "purpose": values["evaluation_purpose"],
    })
    application = RuleApplication.model_validate(values)
    async with session_scope() as session:
        await InvestmentAssessmentRepository(session).save_rule_application("local_owner", application)
    return rule.id


@pytest.mark.usefixtures("ledger")
async def test_buy_refuses_on_missing_fit_with_precise_reason(ledger):
    await flip_flags(account_fit_enabled=True)
    candidate, ref, _ = await buy_setup(ledger)
    result = await publish(candidate)
    assert not result.published
    expected = f"account_fit_missing:{int(ref.id)}"
    assert any(expected in message for message in result.check.field_errors.values()), result.check.field_errors
    assert any(key.endswith(".fit_current") for key in result.check.field_errors)


@pytest.mark.usefixtures("ledger")
async def test_buy_refuses_on_expired_fit_and_blocked_fit(ledger):
    await flip_flags(account_fit_enabled=True)
    candidate, ref, _ = await buy_setup(ledger)
    await seed_fit(candidate.account_id, ref, expired=True)
    result = await publish(candidate)
    assert not result.published
    stale = [m for m in result.check.field_errors.values() if f"account_fit_stale:{int(ref.id)}:" in m]
    assert stale and "assessment_expired" in stale[0]

    candidate2, ref2, _ = await buy_setup(ledger)
    await seed_fit(candidate2.account_id, ref2, blocked=True)
    result2 = await publish(candidate2)
    assert not result2.published
    assert any(f"account_fit_blocked:{int(ref2.id)}" in message
               for message in result2.check.field_errors.values())


@pytest.mark.usefixtures("ledger")
async def test_buy_refuses_when_coverage_missing_or_limited(ledger):
    await flip_flags(supplemental_rules_enabled=True)
    candidate, ref, _ = await buy_setup(ledger)
    result = await publish(candidate)
    assert not result.published
    assert any(f"rule_binding_missing:{int(ref.id)}" in message
               for message in result.check.field_errors.values()), result.check.field_errors

    # A published (bound) clean coverage releases the refusal.
    candidate2, ref2, _ = await buy_setup(ledger)
    await seed_bound_coverage(ref2)
    passed = await publish(candidate2)
    assert passed.published, passed.check.field_errors

    # A new applicable critical rule re-limits the open-position plan.
    await seed_pending_critical_application(ref2)
    limited = candidate2.model_copy(update={"previous_ref": passed.plan_ref})
    action_plan = await prepared(limited, review=semantic(limited))
    blocked = await publish(limited, action_plan)
    assert not blocked.published
    assert any(f"rule_limits:{int(ref2.id)}:rule_coverage_pending:" in message
               for message in blocked.check.field_errors.values()), blocked.check.field_errors


@pytest.mark.usefixtures("ledger")
async def test_buy_passes_with_valid_fit_and_clean_coverage(ledger):
    await flip_flags(account_fit_enabled=True, supplemental_rules_enabled=True)
    candidate, ref, _ = await buy_setup(ledger)
    await seed_fit(candidate.account_id, ref)
    await seed_bound_coverage(ref)
    result = await publish(candidate)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        from investment_agent.portfolio.plans import PortfolioPlanService

        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert view.status == "published"
        assert view.plan.options[0].executable


@pytest.mark.usefixtures("ledger")
async def test_reduce_and_exit_complete_without_any_fit_or_coverage(ledger):
    await flip_flags(account_fit_enabled=True, supplemental_rules_enabled=True)
    # Constraint-exception reduce: no company view, no fit, no coverage.
    candidate, _, _ = await setup(ledger)
    result = await publish(candidate)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        from investment_agent.portfolio.plans import PortfolioPlanService

        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert view.status == "published"
        assert view.plan.options[0].executable
        assert view.plan.options[0].constraint_improvements == ("max_single_weight",)




@pytest.mark.usefixtures("ledger")
async def test_company_reduce_unblocked_without_any_buy_fit(ledger):
    await flip_flags(account_fit_enabled=True, supplemental_rules_enabled=True)
    candidate, ref, _ = await setup(ledger, quantity="40")
    view_ref = await company_view(candidate, ref)
    option = candidate.options[0]
    action_ = option.actions[0].model_copy(update={"basis": "company_thesis", "constraint": None, "company_view_refs": (view_ref,)})
    company_reduce = candidate.model_copy(update={"options": (option.model_copy(update={"actions": (action_,)}),)})
    result = await publish(company_reduce)
    assert result.published, result.check.field_errors


@pytest.mark.usefixtures("ledger")
async def test_wait_plan_unaffected_by_the_new_gates(ledger):
    await flip_flags(account_fit_enabled=True, supplemental_rules_enabled=True)
    candidate, _, _ = await setup(ledger, movement=False, trusted=False)
    result = await publish(candidate, trusted=False)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        from investment_agent.portfolio.plans import PortfolioPlanService

        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert not view.plan.options[0].executable  # explicit-limits wait, as before


@pytest.mark.usefixtures("ledger")
async def test_switches_off_keep_legacy_buy_behavior(ledger):
    candidate, _, _ = await buy_setup(ledger)
    result = await publish(candidate)
    assert result.published, result.check.field_errors
    assert not any(".fit_current" in key for key in result.check.field_errors)
