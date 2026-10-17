"""AF-P3-07 integration: dependency re-resolution, time/event limits, maintain.

Every scenario drives the real bound chain (admission binding, prepare, real
evaluation worker, review, publish) and then reads the published opinion
through ``get_recommendation``. Reads must never enqueue research.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import CompanyRecommendation
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.monitoring import MonitoringService
from investment_agent.investor.publication import (
    PublicationService,
    RuleReviewItem,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.publication import PublicationArtifact
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
    RunLease,
)
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.admission import (
    SUPPLEMENTAL_RULE_EVALUATE,
    SupplementalRuleAdmission,
)
from investment_agent.supplemental_rules.contracts import (
    EvaluationRequest,
    ResearchEvent,
    RuleDefinition,
)
from tests.integration.test_coverage_prepare import _drain_workers
from tests.integration.test_publication_closure import (
    _publish,
    _publish_action,
    _reviewed,
)
from tests.support.publication import unchanged_review
from tests.support.research_closure import (
    BASE_SETTINGS,
    activate_rule,
    admit_research,
    business_input,
    claim_task,
    commit_progress,
    metric_rule,
    requirement_rule,
    run_prepare,
    seed_company_view,
    seed_listing,
    set_runtime,
)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "p3-test-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")


@pytest_asyncio.fixture
async def limits_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def critical_rule(listing_ref: ObjectRef, *, rule_id=None, review_due_at=None) -> RuleDefinition:
    """A critical metric rule that deterministically matches one listing."""

    now = datetime.now(UTC)
    return RuleDefinition(
        id=rule_id or uuid4(), revision=1, name="P3 critical pending rule",
        rationale="A critical activated rule must limit old opinions.", source_refs=(),
        selector={"kind": "listing", "listing_ref": listing_ref.model_dump(mode="python")},
        contexts={"company_research"}, rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "roe", "operator": "<", "value": "0.05", "unit": "fraction"}]}},
        dimensions={"financial_cash"}, effects={"require_review"}, severity="critical",
        materiality="Synthetic P3 fixture only.", valid_from=now - timedelta(days=1),
        review_due_at=review_due_at or now + timedelta(days=30),
        release_requirements=("Review exact saved inputs.",),
    )


def event_rule(listing_ref: ObjectRef, event_ref: ObjectRef, *, effective_from=None) -> RuleDefinition:
    now = datetime.now(UTC)
    return RuleDefinition(
        id=uuid4(), revision=1, name="P3 event impact rule",
        rationale="An announced policy event requires review.", source_refs=(),
        selector={"kind": "listing", "listing_ref": listing_ref.model_dump(mode="python")},
        contexts={"company_research"}, rule_type="event_impact",
        definition={"kind": "event_impact", "event_ref": event_ref.model_dump(mode="python"),
                    "affected_scope": ("listing",), "exposure_evidence": ("saved",),
                    "impact_path": "review", "release_requirements": ("Review the event.",)},
        dimensions={"business_demand"}, effects={"require_review"}, severity="critical",
        materiality="Synthetic P3 fixture only.", valid_from=now - timedelta(days=1),
        review_due_at=now + timedelta(days=30), release_requirements=("Review the event.",),
        event_ref=event_ref,
    )


async def save_event(listing_ref: ObjectRef, *, event_id=None, revision=1, previous=None,
                     session=None, effective_from=None) -> ObjectRef:
    """Persist one user_report event revision through the real repository."""

    if session is not None:
        return await _save_event(session, listing_ref, event_id=event_id, revision=revision,
                                 previous=previous, effective_from=effective_from)
    async with session_scope() as owned:
        return await _save_event(owned, listing_ref, event_id=event_id, revision=revision,
                                 previous=previous, effective_from=effective_from)


async def _save_event(session, listing_ref: ObjectRef, *, event_id, revision, previous,
                      effective_from) -> ObjectRef:
    store = SupplementalRulesRepository(session)
    now = datetime.now(UTC)
    identifier = event_id or uuid4()
    event = ResearchEvent(
        id=identifier, revision=revision, owner_id="local_owner",
        event_key=f"p3-limits-event-{identifier}", title="Synthetic P3 event",
        event_type="policy", jurisdiction="CN", affected_products=(),
        affected_subjects=(f"listing:{listing_ref.id}",), policy_stage="proposed",
        published_at=now - timedelta(hours=1), effective_from=effective_from,
        acquired_at=now, available_from=now, source_refs=(),
        verification="user_report", conflict=False, previous_ref=previous,
    )
    saved = await store.save_research_event("local_owner", event, created_at=now)
    await store.advance_registry("local_owner", events_delta=1, updated_at=now)
    await session.flush()
    return ObjectRef(kind="research_event", id=saved.id, revision=saved.revision)


async def revise_rule_to_other_listing(rule_id: UUID) -> None:
    """Publish revision 2 of the rule with a selector for another listing."""

    async with session_scope() as session:
        other = await seed_listing(session, ticker="600999")
        store = SupplementalRulesRepository(session)
        now = datetime.now(UTC)
        revised = critical_rule(other, rule_id=rule_id).model_copy(update={"revision": 2})
        await store.reserve_registry("local_owner")
        saved = await store.save_rule_revision("local_owner", revised)
        await store.put_rule_head("local_owner", saved.id, expected_head_revision=1,
            active_revision=2, active_state="active", draft_revision=None,
            draft_state=None, last_validation_revision=None, last_validation_hash=None,
            last_validation_payload=None)
        await store.advance_registry("local_owner", rules_delta=1, updated_at=now)
        await session.flush()


async def suspend_rule(rule_id: UUID) -> None:
    async with session_scope() as session:
        store = SupplementalRulesRepository(session)
        head = await store.get_rule_head("local_owner", rule_id)
        assert head is not None
        await store.reserve_registry("local_owner")
        await store.put_rule_head("local_owner", rule_id, expected_head_revision=head.head_revision,
            active_revision=head.active_revision, active_state="suspended", draft_revision=None,
            draft_state=None, last_validation_revision=None, last_validation_hash=None,
            last_validation_payload=None)
        await store.advance_registry("local_owner", rules_delta=0, updated_at=datetime.now(UTC))
        await session.flush()


async def run_listing_evaluation(listing_ref: ObjectRef) -> None:
    """Admit one live evaluation through the real boundary, then drain it."""

    async with session_scope() as session:
        request = EvaluationRequest(idempotency_key=uuid4(), listing_refs=(listing_ref,),
                                    contexts={"company_research"}, mode="live")
        await SupplementalRuleAdmission(session, settings=Settings(_env_file=None)).accept_evaluation(request)
    assert await _drain_workers() >= 1


async def evaluation_runs() -> int:
    async with session_scope() as session:
        return int(await session.scalar(
            select(func.count()).select_from(PipelineRun)
            .where(PipelineRun.kind == SUPPLEMENTAL_RULE_EVALUATE)
        ))


async def live_application_refs() -> tuple[ObjectRef, ...]:
    async with session_scope() as session:
        rows = list((await session.scalars(select(InvestmentAssessment).where(
            InvestmentAssessment.kind == "rule_application"))).all())
    return tuple(ObjectRef(kind="rule_application", id=UUID(row.id), revision=1) for row in rows)


async def current_view(ref: ObjectRef):
    async with session_scope() as session:
        return await PublicationService(session).get_recommendation(ref)


@pytest.mark.usefixtures("limits_db")
async def test_new_critical_rule_limits_old_opinion_and_reads_enqueue_nothing():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600521")
        requirement = requirement_rule(listing_ref)
        metric = metric_rule(listing_ref)
        for rule in (requirement, metric):
            await activate_rule(session, rule)
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is True, published.check.field_errors
    ref = published.recommendation_ref

    # A new critical rule activates after publication and its evaluation runs.
    async with session_scope() as session:
        fresh = await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)
    runs_before = await evaluation_runs()

    view = await current_view(ref)
    assert view.status.value == "review_required"
    assert view.limitation_reasons == (f"rule_coverage_pending:{fresh.id}",)
    # The read itself never enqueues research.
    assert await evaluation_runs() == runs_before

    # Consumer-side binding honesty marker: the subject is bound.
    async with session_scope() as session:
        state = await InvestmentAssessmentRepository(session).rule_binding_state("local_owner", listing_ref)
    assert state.state == "bound" and state.reasons == ()
    assert state.manifest is not None and state.manifest.stage == "published"
    assert state.binding is not None


@pytest.mark.usefixtures("limits_db")
async def test_confirmed_irrelevant_rule_releases_the_limit():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600522")
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True
    ref = published.recommendation_ref
    # The critical rule activates after publication, so it is not covered.
    async with session_scope() as session:
        rule = await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)
    limited = await current_view(ref)
    assert limited.limitation_reasons == (f"rule_coverage_pending:{rule.id}",)

    # Deterministic re-match: the rule is revised onto another listing, so the
    # newest live application for this series records not_applicable.
    await revise_rule_to_other_listing(rule.id)
    await run_listing_evaluation(listing_ref)
    released = await current_view(ref)
    assert released.status.value == "published", released.limitation_reasons
    assert released.limitation_reasons == ()
    assert await evaluation_runs() >= 2  # both evaluations came from the scenario, never from a read


@pytest.mark.usefixtures("limits_db")
async def test_new_event_uuid_and_new_revision_limit_old_opinion():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600523")
        event_ref = await save_event(listing_ref, session=session)
        await activate_rule(session, event_rule(listing_ref, event_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True, published.check.field_errors
    ref = published.recommendation_ref
    assert (await current_view(ref)).limitation_reasons == ()

    # A brand-new relevant event UUID limits the old conclusion.
    fresh = await save_event(listing_ref)
    limited = await current_view(ref)
    assert f"unbound_event:{fresh.id}" in limited.limitation_reasons
    assert limited.status.value == "review_required"

    # A new revision of the anchored event is NOT shadowed by the old anchor.
    revised = await save_event(listing_ref, event_id=event_ref.id, revision=2,
                               previous=event_ref)
    assert revised.revision == 2
    both = await current_view(ref)
    assert f"unbound_event:{event_ref.id}" in both.limitation_reasons
    assert f"unbound_event:{fresh.id}" in both.limitation_reasons
    assert both.limitation_reasons.count(f"unbound_event:{event_ref.id}") == 1


@pytest.mark.usefixtures("limits_db")
async def test_time_boundaries_limit_at_the_instant_without_background_jobs():
    now = datetime.now(UTC)
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600524")
        # The event becomes effective shortly after publication: the covered
        # application anchors it, and the read itself flags the crossing.
        event_ref = await save_event(listing_ref, session=session,
                                     effective_from=now + timedelta(seconds=3))
        # Both boundaries cross shortly after publication; no job runs.
        due_soon = critical_rule(listing_ref, review_due_at=now + timedelta(seconds=3))
        await activate_rule(session, due_soon)
        await activate_rule(session, event_rule(listing_ref, event_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True, published.check.field_errors
    ref = published.recommendation_ref
    before = await current_view(ref)
    assert before.limitation_reasons == ()

    # The read path itself limits the instant the boundary passes.
    await _drain_workers()  # settle anything pending; still no new evaluation
    import asyncio
    await asyncio.sleep(3.2)
    crossed = await current_view(ref)
    assert f"rule_review_due:{due_soon.id}" in crossed.limitation_reasons
    assert f"event_effective_started:{event_ref.id}" in crossed.limitation_reasons


@pytest.mark.usefixtures("limits_db")
async def test_scan_overflow_reports_explicit_limitation():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600525")
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True
    ref = published.recommendation_ref

    real = InvestmentAssessmentRepository.list_latest_live_rule_application_payloads

    async def truncated(self, owner_id, listing_id, context, *, limit=2000):
        payloads, _ = await real(self, owner_id, listing_id, context, limit=limit)
        return payloads, True

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(InvestmentAssessmentRepository,
                        "list_latest_live_rule_application_payloads", truncated)
        limited = await current_view(ref)
    assert "rule_coverage_scan_incomplete" in limited.limitation_reasons
    assert limited.status.value == "review_required"


@pytest.mark.usefixtures("limits_db")
async def test_extend_refuses_with_limits_and_passes_without():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600526")
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True
    ref = published.recommendation_ref

    async with session_scope() as session:
        row = await session.get(PublicationArtifact, (str(ref.id), 1))
        recommendation = CompanyRecommendation.model_validate(row.payload)
    required = tuple(d.ref for d in recommendation.dependency_refs if d.materiality == "critical")
    assert any(item.kind == "rule_coverage" for item in required)
    review = unchanged_review(ref, required)
    async with session_scope() as session:
        grant = ExecutionGrant(mode="interactive", source="authenticated_message",
            prompt="Extend the opinion", tool_names=("extend_recommendation",),
            scope_refs=(ObjectRef(kind="case", id=case.id, revision=case.revision + 1),))
        task2 = await admit(session, base=BASE_SETTINGS, input_key=f"p3-extend-{uuid4()}",
            binding=ContextBinding(context_refs=(case_ref,), base_refs=(case_ref,),
                                   as_of=datetime.now(UTC)), grant=grant)
        run = await PipelineRunRepository(session).claim_next(worker_id=str(task2.id), kinds=["agent_turn"])
        extend_claim = await TaskService(session).claim(task2.id, RunLease(run.id, str(task2.id), run.attempt_count))
        action = await TaskService(session).prepare_action(extend_claim, intent_key=f"extend:{uuid4()}",
            parameters={"recommendation_ref": ref.model_dump(mode="json"),
                        "review": review.model_dump(mode="json"), "expected_case_revision": 2})
    operation = UUID(action.operation_id)

    async def try_extend():
        async with session_scope() as session:
            return await PublicationService(session).extend(
                extend_claim, operation, ref, review, expected_case_revision=2, base=BASE_SETTINGS)

    # With a current rule limitation the mechanical extension is refused.
    fresh = None
    async with session_scope() as session:
        fresh = await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)
    with pytest.raises(InvestorError, match="rule-coverage"):
        await try_extend()

    # The confirmed-irrelevant revision releases the limit; the prepared
    # extension is then repairable without a new attestation.
    await revise_rule_to_other_listing(fresh.id)
    await run_listing_evaluation(listing_ref)
    extended = await try_extend()
    assert extended.status.value == "published"
    assert extended.effective_valid_until > recommendation.valid_until


@pytest.mark.usefixtures("limits_db")
async def test_maintain_scan_aggregates_rule_limitations():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600527")
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True
    fresh = None
    async with session_scope() as session:
        fresh = await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)

    async with session_scope() as session:
        scanned = await MonitoringService(session, BASE_SETTINGS).scan()
    assert scanned >= 1
    async with session_scope() as session:
        cycle = await session.scalar(select(ReviewCycle).order_by(ReviewCycle.id))
        assert cycle is not None
        assert f"rule_coverage_pending:{fresh.id}" in cycle.payload["reasons"]


@pytest.mark.usefixtures("limits_db")
async def test_suspend_does_not_clear_the_pending_limit():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600528")
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=tuple(
        RuleReviewItem(application_ref=ref, disposition="no_action_required")
        for ref in await live_application_refs()))
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    ref = published.recommendation_ref
    async with session_scope() as session:
        rule = await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)
    assert (await current_view(ref)).limitation_reasons == (f"rule_coverage_pending:{rule.id}",)

    await suspend_rule(rule.id)
    await run_listing_evaluation(listing_ref)
    still = await current_view(ref)
    assert still.limitation_reasons == (f"rule_coverage_pending:{rule.id}",)


@pytest.mark.usefixtures("limits_db")
async def test_legacy_conclusion_keeps_no_rule_limits_and_missing_marker():
    await set_runtime(False)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600529")
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    result = await run_prepare(claim, business_input(case, evidence_ref))
    assert "coverage_manifest_ref" not in result
    candidate, semantic = await _reviewed(claim, result)
    published = await _publish(claim, await _publish_action(claim, case_ref, candidate, semantic),
                               candidate, semantic)
    assert published.published is True
    ref = published.recommendation_ref

    # Rules and evaluations appear afterwards (the switch is back on; reads of
    # a published conclusion ignore the switch). A legacy opinion has no rule
    # dependency, so the publication side never adds rule restrictions.
    await set_runtime(True)
    async with session_scope() as session:
        await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)
    view = await current_view(ref)
    assert view.status.value == "published"
    assert view.limitation_reasons == ()

    # The consumer-side marker says explicitly that no binding exists.
    async with session_scope() as session:
        state = await InvestmentAssessmentRepository(session).rule_binding_state("local_owner", listing_ref)
    assert state.state == "unknown"
    assert state.reasons == ("rule_binding_missing",)
    assert state.manifest is None and state.binding is None
