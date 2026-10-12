"""AG12 real file SQLite, numeric binding, publication and validity invariants."""
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.conversations import ConversationService, InvalidContextError
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
from investment_agent.agent.tasks import TaskBoundaryError, TaskService
from investment_agent.evidence.contracts import (
    CalculationAssumption,
    CalculationRecord,
    EvidenceRecord,
)
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Mutation, ObjectRef, Quantity
from investment_agent.investor.contracts import (
    CompanyRecommendation,
    MonitorCondition,
    MonitorSpec,
    NumericNode,
    TextNode,
)
from investment_agent.investor.enums import NumericRole, Sufficiency
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.publication import (
    FIXED_METHOD,
    PublicationCandidate,
    PublicationService,
)
from investment_agent.investor.publication_numbers import NumericResolver, format_quantity
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.publication import (
    PublicationArtifact,
    PublicationEvent,
    PublicationOutbox,
)
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from investment_agent.valuation.contracts import PercentileRequest
from investment_agent.valuation.service import ValuationService
from tests.support.publication import (  # noqa: F401
    base,
    fixture,
    market_candidate,
    metadata,
    prepared,
    publish,
    semantic,
    unchanged_review,
)
from tests.support.publication import (
    publication_db as publication_db,
)


@pytest.mark.asyncio
async def test_atomic_publish_replay_context_history_and_immutability(publication_db):
    candidate, _, subject = await fixture()
    prepared_action = await prepared(candidate)
    result = await publish(candidate, prepared_action)
    assert result.published and result.check.semantic_review_ref
    assert result.resolved_nodes["recommendation.claims.0.text.1"].text == "1234500000.00 CNY"
    assert await publish(candidate, prepared_action) == result
    async with session_scope() as session:
        svc = PublicationService(session)
        view = await svc.get_recommendation(result.recommendation_ref)
        assert view.status == "published" and not view.limitation_reasons
        assert view.recommendation.actor == "research_service" and view.recommendation.id != candidate.recommendation.id
        assert await session.scalar(select(func.count()).select_from(PublicationOutbox)) == 1
        assert await session.scalar(select(func.count()).select_from(PublicationEvent)) == 1
        scope = await ConversationService(session, base()).resolve_context((result.recommendation_ref, result.thesis_ref))
        assert subject in scope
        with pytest.raises(InvalidContextError):
            await ConversationService(session, base()).resolve_context((result.thesis_ref.model_copy(update={"revision": 2}),))
        row = await svc.artifact(result.thesis_ref)
        with pytest.raises(ValueError, match="immutable"):
            async with session.begin_nested():
                row.payload = {}
                await session.flush()


@pytest.mark.parametrize("failure", ["arabic", "chinese", "gap_number", "reason_number", "wrong_subject", "wrong_role", "wrong_path", "wrong_unit", "future", "unverified", "conflict", "method", "semantic", "semantic_issue", "missing_premise", "missing_dimension_gap", "missing_dependency", "insufficient_buy", "local", "review_before_cutoff"])
@pytest.mark.asyncio
async def test_failed_publication_retains_repairable_exact_draft(publication_db, failure):
    candidate, ref, subject = await fixture()
    rec, thesis = candidate.recommendation, candidate.thesis
    claim = rec.claims[0]
    if failure in {"arabic", "chinese"}:
        text = "现金提高20%" if failure == "arabic" else "未来五年增长三成"
        claim = claim.model_copy(update={"text": (TextNode(type="text", text=text),)})
    elif failure == "gap_number":
        rec = rec.model_copy(update={"gaps": ("未来2年未知",)})
    elif failure == "reason_number":
        claim = claim.model_copy(update={"impact_if_wrong": "损失两倍"})
    elif failure in {"wrong_role", "wrong_path", "wrong_unit"}:
        node = claim.text[1]
        if failure == "wrong_role":
            node = node.model_copy(update={"role": NumericRole.SCENARIO})
        elif failure == "wrong_path":
            node = node.model_copy(update={"ref": ref.model_copy(update={"field_path": "value.other"})})
        else:
            node = node.model_copy(update={"display_format": "percent"})
        claim = claim.model_copy(update={"text": (claim.text[0], node)})
    elif failure in {"wrong_subject", "future", "unverified", "conflict"}:
        async with session_scope() as session:
            row = await session.get(SavedEvidence, (str(ref.id), 1))
            payload = dict(row.payload)
            if failure == "wrong_subject":
                payload["subject_ref"] = subject.model_copy(update={"id": 999}).model_dump(mode="json")
            elif failure == "future":
                payload["available_from"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            elif failure == "unverified":
                payload["verification"] = "candidate"
            else:
                payload["conflict"] = "unresolved"
            # Simulated invalid source ingestion, not production immutable mutation.
            await session.execute(update(SavedEvidence).where(SavedEvidence.id == str(ref.id)).values(payload=payload))
    elif failure == "method":
        rec = rec.model_copy(update={"method_revision_id": FIXED_METHOD.model_copy(update={"id": uuid4()})})
    elif failure == "missing_premise":
        claim = claim.model_copy(update={"support_refs": ()})
    elif failure == "missing_dimension_gap":
        dimensions = list(rec.assessed_dimensions)
        dimensions[0] = dimensions[0].model_copy(update={"reason": None})
        rec = rec.model_copy(update={"assessed_dimensions": tuple(dimensions)})
        thesis = thesis.model_copy(update={"assessed_dimensions": tuple(dimensions)})
    elif failure == "missing_dependency":
        rec = rec.model_copy(update={"dependency_refs": ()})
    elif failure == "insufficient_buy":
        from investment_agent.investor.contracts import ConditionalAction
        rec = rec.model_copy(update={"sufficiency": Sufficiency.INSUFFICIENT, "conditional_actions": (ConditionalAction(action="buy", condition_refs=(), rationale_refs=()),)})
    elif failure == "review_before_cutoff":
        rec = rec.model_copy(update={"next_review_at": rec.as_of - timedelta(days=1)})
    rec = CompanyRecommendation.model_validate(rec.model_copy(update={"claims": (claim,)}).model_dump()) if failure != "review_before_cutoff" else rec.model_copy(update={"claims": (claim,)})
    thesis = thesis.model_copy(update={"claims": (claim,)})
    candidate = candidate.model_copy(update={"thesis": thesis, "recommendation": rec, "research_kind": "local_check" if failure == "local" else "company_research"})
    review = semantic(candidate)
    if failure == "semantic":
        review = review.model_copy(update={"candidate_hash": "other-candidate"})
    elif failure == "semantic_issue":
        review = review.model_copy(update={"assessments": (review.assessments[0].model_copy(update={"issues": ("Counter evidence omitted",)}),)})
    result = await publish(candidate, await prepared(candidate, review))
    assert not result.published and result.check.field_errors and result.draft_ref.kind == "draft"
    async with session_scope() as session:
        draft = await PublicationService(session).artifact(result.draft_ref)
        assert draft.payload["candidate"] == candidate.model_dump(mode="json")
        assert (await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)).current_recommendation_ref is None
        assert await session.scalar(select(func.count()).select_from(PublicationOutbox)) == 0


@pytest.mark.asyncio
async def test_outer_rollback_and_action_recoverable(publication_db):
    candidate, _, _ = await fixture()
    claim, operation, review = await prepared(candidate)
    async with session_scope() as session:
        result = await PublicationService(session).publish(claim, operation, candidate, review, base=base())
        assert result.published
        await session.rollback()
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PublicationArtifact)) == 0
        assert (await session.get(ToolAction, str(operation))).state == "prepared"
    assert (await publish(candidate, (claim, operation, review))).published


@pytest.mark.parametrize("boundary", ["permission", "cancel", "lease", "disabled"])
@pytest.mark.asyncio
async def test_execution_boundary_rechecked_before_publication(publication_db, boundary):
    candidate, _, _ = await fixture()
    claim, operation, review = await prepared(candidate, permission=boundary != "permission")
    if boundary in {"cancel", "lease"}:
        async with session_scope() as session:
            if boundary == "cancel":
                await TaskService(session).cancel(claim.task_id)
            else:
                await session.execute(update(PipelineRun).where(PipelineRun.id == claim.lease.run_id).values(worker_id="other"))
    async with session_scope() as session:
        with pytest.raises((ExecutionDeniedError, TaskBoundaryError, LeaseLostError)):
            await PublicationService(session).publish(claim, operation, candidate, review, base=base().model_copy(update={"investor_agent_enabled": boundary != "disabled"}))
        assert await session.scalar(select(func.count()).select_from(PublicationOutbox)) == 0


@pytest.mark.asyncio
async def test_two_independent_sessions_cannot_overwrite_new_view(publication_db):
    candidate, _, _ = await fixture()
    first, second = await prepared(candidate), await prepared(candidate)
    results = await asyncio.gather(publish(candidate, first), publish(candidate, second), return_exceptions=True)
    assert sum(not isinstance(result, Exception) and result.published for result in results) == 1
    assert any((isinstance(result, InvestorError) and result.detail.code == "revision_conflict") or (not isinstance(result, Exception) and not result.published and "thesis.case_ref" in result.check.field_errors) for result in results)
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PublicationOutbox)) == 1


@pytest.mark.asyncio
async def test_expiry_exact_boundary_terminal_restriction_and_historical_projection(publication_db):
    candidate, _, _ = await fixture()
    result = await publish(candidate)
    async with session_scope() as session:
        svc = PublicationService(session)
        view = await svc.get_recommendation(result.recommendation_ref)
        assert (await svc.get_recommendation(result.recommendation_ref, at=view.recommendation.valid_until - timedelta(microseconds=1))).status == "published"
        assert (await svc.get_recommendation(result.recommendation_ref, at=view.recommendation.valid_until)).status == "expired"
        published_at = view.recommendation.created_at
        await svc.restrict(result.recommendation_ref, reason="Key counter evidence", withdrawn=True)
        assert (await svc.get_recommendation(result.recommendation_ref)).status == "withdrawn"
        assert (await svc.get_recommendation(result.recommendation_ref, at=published_at)).status == "published"
        await svc.restrict(result.recommendation_ref, reason="Late change")
        assert await session.scalar(select(func.count()).select_from(PublicationEvent)) == 2


@pytest.mark.asyncio
async def test_visible_correction_chain_and_candidate_background_versus_critical(publication_db):
    candidate, old, subject = await fixture()
    async with session_scope() as session:
        original = EvidenceRecord.model_validate((await session.get(SavedEvidence, (str(old.id), 1))).payload)
        corrected = original.model_copy(update={"id": uuid4(), "correction_refs": (old,), "value": Quantity(value="13", unit="亿元", currency="CNY"), "created_at": datetime.now(UTC)})
        session.add(SavedEvidence(id=str(corrected.id), revision=1, owner_id="local_owner", payload=corrected.model_dump(mode="json")))
        await session.flush()
        resolver = NumericResolver(EvidenceService(session), subject, datetime.now(UTC))
        with pytest.raises(InvestorError, match="corrected"):
            await resolver.fact(old)
        new_ref = ObjectRef(kind="evidence", id=corrected.id, revision=1)
        assert (await resolver.fact(new_ref)).value.value == Decimal(13)
        assert (await NumericResolver(EvidenceService(session), subject, candidate.recommendation.as_of).fact(old)).id == old.id


@pytest.mark.parametrize("unit,display,expected", [("亿元", "decimal", "1234500000.00 CNY"), ("fraction", "percent", "1234.50%"), ("percentage_points", "decimal", "12.35 percentage_points")])
def test_units_half_up(unit, display, expected):
    assert format_quantity(Quantity(value="12.345", unit=unit), display)[0] == expected


@pytest.mark.parametrize("text", ["一次性收益", "单元成本", "另一种解释"])
@pytest.mark.asyncio
async def test_finite_scanner_preserves_normal_prose(publication_db, text):
    candidate, _, _ = await fixture()
    candidate = candidate.model_copy(update={"recommendation": candidate.recommendation.model_copy(update={"gaps": (text,)})})
    assert (await publish(candidate)).published


@pytest.mark.asyncio
async def test_scenario_name_ambiguity_index_and_real_dcf_quality(publication_db):
    candidate, ref, subject = await fixture()
    from tests.support.valuation import FixtureQualification, seed
    async with session_scope() as session:
        request, qualification, subject = await seed(session)
        output = await ValuationService(session, FixtureQualification(qualification)).reverse_dcf("local_owner", request, uuid4())
        calculation_ref = ObjectRef.model_validate(output["calculation_ref"])
        resolver = NumericResolver(EvidenceService(session), subject, request.as_of)
        assert (await resolver.calculation(calculation_ref)).quality
        with pytest.raises(InvestorError, match="Ambiguous"):
            await resolver.resolve(NumericNode(type="numeric_claim", ref=calculation_ref.model_copy(update={"field_path": "assumptions.discount_rate"}), role="scenario_assumption", display_format="percent"))
        first = await resolver.resolve(NumericNode(type="numeric_claim", ref=calculation_ref.model_copy(update={"field_path": "assumptions.0"}), role="scenario_assumption", display_format="percent"))
        second = await resolver.resolve(NumericNode(type="numeric_claim", ref=calculation_ref.model_copy(update={"field_path": "assumptions.2"}), role="scenario_assumption", display_format="percent"))
        assert first.value != second.value


async def prepared_extension(claim, recommendation_ref, review, case_revision):
    async with session_scope() as session:
        action = await TaskService(session).prepare_action(claim, intent_key=f"extend:{uuid4()}", parameters={"recommendation_ref": recommendation_ref.model_dump(mode="json"), "review": review.model_dump(mode="json"), "expected_case_revision": case_revision})
    return UUID(action.operation_id)


@pytest.mark.asyncio
async def test_extension_keeps_original_body_and_historical_validity(publication_db):
    candidate, evidence_ref, _ = await fixture()
    preparation = await prepared(candidate)
    result = await publish(candidate, preparation)
    review = unchanged_review(result.recommendation_ref, (evidence_ref,))
    operation = await prepared_extension(preparation[0], result.recommendation_ref, review, 2)
    async with session_scope() as session:
        service = PublicationService(session)
        before = await service.get_recommendation(result.recommendation_ref)
        after = await service.extend(preparation[0], operation, result.recommendation_ref, review, expected_case_revision=2, base=base())
        assert after.recommendation == before.recommendation
        assert after.effective_valid_until > before.effective_valid_until
        assert after.effective_next_review_at > before.effective_next_review_at
        historical = await service.get_recommendation(result.recommendation_ref, at=before.recommendation.created_at)
        assert historical.effective_valid_until == before.recommendation.valid_until
        assert historical.effective_next_review_at == before.recommendation.next_review_at
        assert await service.extend(preparation[0], operation, result.recommendation_ref, review, expected_case_revision=2, base=base()) == after
        assert await session.scalar(select(func.count()).select_from(PublicationOutbox)) == 1


@pytest.mark.parametrize("limitation", ["expired", "restricted", "changed_dependency", "missing_checked"])
@pytest.mark.asyncio
async def test_cannot_extend_expired_limited_or_unchecked_view(publication_db, monkeypatch, limitation):
    candidate, evidence_ref, _ = await fixture()
    preparation = await prepared(candidate)
    result = await publish(candidate, preparation)
    review = unchanged_review(result.recommendation_ref, () if limitation == "missing_checked" else (evidence_ref,))
    operation = await prepared_extension(preparation[0], result.recommendation_ref, review, 2)
    if limitation == "expired":
        import investment_agent.investor.publication as publication
        class ExpiryTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return candidate.recommendation.valid_until
        monkeypatch.setattr(publication, "datetime", ExpiryTime)
    async with session_scope() as session:
        svc = PublicationService(session)
        if limitation == "restricted":
            await svc.restrict(result.recommendation_ref, reason="Counter evidence requires review")
        elif limitation == "changed_dependency":
            evidence = EvidenceRecord.model_validate((await session.get(SavedEvidence, (str(evidence_ref.id), 1))).payload)
            newer = evidence.model_copy(update={"id": uuid4(), "created_at": datetime.now(UTC), "correction_refs": (evidence_ref,)})
            session.add(SavedEvidence(id=str(newer.id), revision=1, owner_id="local_owner", payload=newer.model_dump(mode="json")))
            await session.flush()
        with pytest.raises(InvestorError):
            await svc.extend(preparation[0], operation, result.recommendation_ref, review, expected_case_revision=2, base=base())


@pytest.mark.asyncio
async def test_independent_concurrent_extensions_use_case_cas(publication_db):
    candidate, evidence_ref, _ = await fixture()
    preparation = await prepared(candidate)
    result = await publish(candidate, preparation)
    review = unchanged_review(result.recommendation_ref, (evidence_ref,))
    first = await prepared_extension(preparation[0], result.recommendation_ref, review, 2)
    second = await prepared_extension(preparation[0], result.recommendation_ref, review, 2)
    async def extend(operation):
        async with session_scope() as session:
            return await PublicationService(session).extend(preparation[0], operation, result.recommendation_ref, review, expected_case_revision=2, base=base())
    outcomes = await asyncio.gather(extend(first), extend(second), return_exceptions=True)
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert any(isinstance(outcome, InvestorError) for outcome in outcomes)


@pytest.mark.asyncio
async def test_new_view_supersedes_atomically_and_keeps_original_context(publication_db):
    candidate, _, subject = await fixture()
    first = await publish(candidate)
    new_thesis = candidate.thesis.model_copy(update={"id": uuid4(), "case_ref": candidate.thesis.case_ref.model_copy(update={"revision": 2}), "previous_ref": first.thesis_ref})
    candidate = candidate.model_copy(update={"thesis": new_thesis, "recommendation": candidate.recommendation.model_copy(update={"id": uuid4(), "thesis_revision_refs": (ObjectRef(kind="thesis", id=new_thesis.id, revision=1),)})})
    second = await publish(candidate)
    async with session_scope() as session:
        svc = PublicationService(session)
        assert (await svc.get_recommendation(first.recommendation_ref)).status == "superseded"
        assert (await svc.get_recommendation(first.recommendation_ref)).replacement_ref == second.recommendation_ref
        old = await svc.get_thesis(first.thesis_ref)
        assert (await svc.get_recommendation(first.recommendation_ref, at=old.created_at)).status == "published"
        assert subject in await ConversationService(session, base()).resolve_context((first.recommendation_ref,))
        assert await session.scalar(select(func.count()).select_from(PublicationOutbox)) == 2


@pytest.mark.asyncio
async def test_dependency_change_after_cutoff_rejects_publish_before_current_pointer(publication_db):
    candidate, old, _ = await fixture()
    preparation = await prepared(candidate)
    async with session_scope() as session:
        original = EvidenceRecord.model_validate((await session.get(SavedEvidence, (str(old.id), 1))).payload)
        newer = original.model_copy(update={"id": uuid4(), "created_at": datetime.now(UTC), "correction_refs": (old,)})
        session.add(SavedEvidence(id=str(newer.id), revision=1, owner_id="local_owner", payload=newer.model_dump(mode="json")))
    result = await publish(candidate, preparation)
    assert not result.published and "dependency_refs.current" in result.check.field_errors


@pytest.mark.asyncio
async def test_mandate_history_and_unrelated_budget_changes_do_not_invalidate(publication_db):
    from investment_agent.investor.contracts import Dependency
    candidate, _, _ = await fixture()
    mandate_ref = candidate.recommendation.mandate_revision_id
    candidate = candidate.model_copy(update={"recommendation": candidate.recommendation.model_copy(update={"dependency_refs": (*candidate.recommendation.dependency_refs, Dependency(ref=mandate_ref, materiality="critical", fields=("hard_constraints",)))})})
    result = await publish(candidate)
    async with session_scope() as session:
        foundation = InvestorService(session)
        original = await foundation.get_mandate("local_owner")
        changed = original.model_copy(update={"revision": 2, "authorization": original.authorization.model_copy(update={"max_leads_per_batch": 1})})
        await foundation.revise_mandate("local_owner", changed, Mutation(expected_revision=1, idempotency_key=uuid4()))
        assert (await PublicationService(session).get_recommendation(result.recommendation_ref)).status == "published"
        current = await foundation.get_mandate("local_owner")
        await foundation.revise_mandate("local_owner", current.model_copy(update={"revision": 3, "hard_constraints": ("No directional action",)}), Mutation(expected_revision=2, idempotency_key=uuid4()))
        svc = PublicationService(session)
        view = await svc.get_recommendation(result.recommendation_ref)
        assert view.status == "review_required" and "mandate_changed:hard_constraints" in view.limitation_reasons
        assert (await svc.get_recommendation(result.recommendation_ref, at=view.recommendation.created_at)).status == "published"


@pytest.mark.asyncio
async def test_real_ag10_percentile_quality_resolves_and_candidate_inputs_stay_candidate(publication_db):
    from tests.support.valuation import AS_OF, seed_bars
    async with session_scope() as session:
        listing, rows = await seed_bars(session)
        output = await ValuationService(session).percentile("local_owner", PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric="pe"), uuid4())
        ref = ObjectRef.model_validate(output["calculation_ref"])
        subject = ObjectRef(kind="listing", id=listing.id, revision=1)
        resolver = NumericResolver(EvidenceService(session), subject, AS_OF)
        record = await resolver.calculation(ref)
        assert record.quality and "short_history" in record.errors
        node = await resolver.resolve(NumericNode(type="numeric_claim", ref=ref.model_copy(update={"field_path": "results.percentile"}), role="calculation_output", display_format="percent"))
        assert "short_history" in node.quality
        fact = await resolver.fact(record.fact_refs[0])
        assert fact.verification == "candidate"
        with pytest.raises(InvestorError, match="unverified"):
            await resolver.fact(record.fact_refs[0], require_verified=True)


@pytest.mark.asyncio
async def test_monitor_internal_refs_are_remapped_as_one_publication(publication_db):
    from investment_agent.investor.contracts import ConditionalAction, Dependency
    candidate, evidence, _ = await fixture()
    async with session_scope() as session:
        calculation = CalculationRecord(**metadata(), method="fixture_threshold", method_version="fixture.v1", fact_refs=(evidence,), assumption_refs=(), input_hash="fixture", as_of=candidate.recommendation.as_of, applicable=True, status="ok", results={}, assumptions=(CalculationAssumption(name="review_threshold", value=Quantity(value="1", unit="CNY"), provenance="agent_scenario", rationale="A fictional threshold for monitoring"),))
        session.add(SavedCalculation(id=str(calculation.id), revision=1, owner_id="local_owner", payload=calculation.model_dump(mode="json")))
    threshold = ObjectRef(kind="calculation", id=calculation.id, revision=1, field_path="assumptions.0")
    monitor = MonitorSpec(**metadata(), case_ref=candidate.thesis.case_ref, hypothesis_ref=None, conditions=(MonitorCondition(field_ref=evidence, operator="lt", threshold_ref=threshold, period_basis="annual"),), next_due_at=candidate.recommendation.next_review_at, required=True, active=True)
    monitor_ref = ObjectRef(kind="monitor", id=monitor.id, revision=1)
    rec = candidate.recommendation.model_copy(update={"monitor_refs": (monitor_ref,), "conditions": (monitor_ref,), "conditional_actions": (ConditionalAction(action="wait", condition_refs=(monitor_ref,), rationale_refs=(evidence,)),), "dependency_refs": (*candidate.recommendation.dependency_refs, Dependency(ref=monitor_ref, materiality="critical", fields=("conditions",)))})
    candidate = candidate.model_copy(update={"recommendation": rec, "monitors": (monitor,)})
    result = await publish(candidate)
    assert result.published
    async with session_scope() as session:
        svc = PublicationService(session)
        view = await svc.get_recommendation(result.recommendation_ref)
        rec = view.recommendation
        assert rec.monitor_refs[0] != monitor_ref
        assert rec.conditions == rec.monitor_refs == rec.conditional_actions[0].condition_refs
        assert rec.dependency_refs[-1].ref == rec.monitor_refs[0]
        assert (await svc.artifact(rec.conditions[0])).kind == "monitor"
        assert view.status == "published"


@pytest.mark.asyncio
async def test_withdrawn_old_view_stays_terminal_when_new_view_published(publication_db):
    candidate, _, _ = await fixture()
    first = await publish(candidate)
    async with session_scope() as session:
        await PublicationService(session).restrict(first.recommendation_ref, reason="Withdrawn at user request", withdrawn=True)
    thesis = candidate.thesis.model_copy(update={"id": uuid4(), "case_ref": candidate.thesis.case_ref.model_copy(update={"revision": 2}), "previous_ref": first.thesis_ref})
    candidate = candidate.model_copy(update={"thesis": thesis, "recommendation": candidate.recommendation.model_copy(update={"id": uuid4(), "thesis_revision_refs": (ObjectRef(kind="thesis", id=thesis.id, revision=1),)})})
    second = await publish(candidate)
    assert second.published
    async with session_scope() as session:
        old = await PublicationService(session).get_recommendation(first.recommendation_ref)
        assert old.status == "withdrawn"
        events = (await session.scalars(select(PublicationEvent).where(PublicationEvent.recommendation_id == str(first.recommendation_ref.id)))).all()
        assert [event.payload["type"] for event in events] == ["published", "withdrawn"]


@pytest.mark.asyncio
async def test_critical_opposition_is_dependency_and_review_requirement(publication_db):
    candidate, evidence, _ = await fixture()
    opposing = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    claim = candidate.recommendation.claims[0].model_copy(update={"opposition_refs": (opposing,)})
    candidate = candidate.model_copy(update={"thesis": candidate.thesis.model_copy(update={"claims": (claim,)}), "recommendation": candidate.recommendation.model_copy(update={"claims": (claim,)})})
    result = await publish(candidate)
    assert not result.published and "dependency_refs" in result.check.field_errors


@pytest.mark.asyncio
async def test_market_monitor_excludes_intraday_future_close_and_respects_threshold(publication_db):
    from investment_agent.evidence.snapshots import materialize
    from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
    candidate, evidence_ref, evidence, old_bar = await market_candidate()
    async with session_scope() as session:
        calculation = CalculationRecord(**metadata(), method="fixture_threshold", method_version="fixture.v1", fact_refs=(evidence_ref,), assumption_refs=(), input_hash="fixture", as_of=candidate.recommendation.as_of, applicable=True, status="ok", results={}, assumptions=(CalculationAssumption(name="review_threshold", value=Quantity(value="20", unit="CNY/share"), provenance="agent_scenario", rationale="A fictional review threshold"),))
        session.add(SavedCalculation(id=str(calculation.id), revision=1, owner_id="local_owner", payload=calculation.model_dump(mode="json")))
    threshold = ObjectRef(kind="calculation", id=calculation.id, revision=1, field_path="assumptions.0")
    monitor = MonitorSpec(**metadata(), case_ref=candidate.thesis.case_ref, hypothesis_ref=None, conditions=(MonitorCondition(field_ref=evidence_ref, operator="gt", threshold_ref=threshold, period_basis="point_in_time"),), next_due_at=candidate.recommendation.next_review_at, required=True, active=True)
    candidate = candidate.model_copy(update={"monitors": (monitor,), "recommendation": candidate.recommendation.model_copy(update={"monitor_refs": (ObjectRef(kind="monitor", id=monitor.id, revision=1),)})})
    result = await publish(candidate)
    assert result.published
    # Keep the observation after the actual publication, regardless of when
    # the suite is run; a fixed wall date eventually becomes pre-publication.
    day = datetime.now(UTC).date() + timedelta(days=1)
    # The value was captured in the morning but represents an unavailable close.
    morning = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=2)
    async with session_scope() as session:
        row = await MarketSnapshotRepository(session).insert(listing_id=old_bar.listing_id, trade_date=day, open=Decimal(30), high=Decimal(30), low=Decimal(30), close=Decimal(30), volume=Decimal(100), pe_ttm=Decimal(5), pb_mrq=Decimal(1), provider="baostock", captured_at=morning)
        assert materialize(row, "close").available_from > morning
    async with session_scope() as session:
        svc = PublicationService(session)
        assert (await svc.get_recommendation(result.recommendation_ref, at=morning)).status == "published"
        late = await svc.get_recommendation(result.recommendation_ref, at=morning + timedelta(hours=6))
        assert late.status == "review_required" and any("monitor_triggered" in reason for reason in late.limitation_reasons)


@pytest.mark.asyncio
async def test_no_new_price_automatic_invalidation_without_triggered_condition(publication_db):
    from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
    candidate, _, evidence, old_bar = await market_candidate()
    result = await publish(candidate)
    assert result.published
    async with session_scope() as session:
        await MarketSnapshotRepository(session).insert(listing_id=old_bar.listing_id, trade_date=date(2026, 10, 10), open=Decimal(11), high=Decimal(11), low=Decimal(11), close=Decimal(11), volume=Decimal(100), pe_ttm=Decimal(5), pb_mrq=Decimal(1), provider="baostock", captured_at=datetime(2026, 10, 10, 9, tzinfo=UTC))
    async with session_scope() as session:
        assert (await PublicationService(session).get_recommendation(result.recommendation_ref, at=datetime.now(UTC))).status == "published"


@pytest.mark.asyncio
async def test_publication_read_holds_consistent_snapshot_across_concurrent_event(publication_db, monkeypatch):
    async with publication_db.connect() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    candidate, _, _ = await fixture()
    result = await publish(candidate)
    ready, updated = asyncio.Event(), asyncio.Event()
    original = PublicationService._dependency_limits
    async def delayed(self, recommendation, at):
        ready.set()
        await updated.wait()
        return await original(self, recommendation, at)
    monkeypatch.setattr(PublicationService, "_dependency_limits", delayed)
    async def read():
        async with session_scope() as session:
            return await PublicationService(session).get_recommendation(result.recommendation_ref)
    reader = asyncio.create_task(read())
    await ready.wait()
    # Append the actual event directly: the writer must not call delayed reader.
    async with session_scope() as session:
        svc = PublicationService(session)
        await svc._reserve(candidate.thesis.case_ref.id)
        await svc._event(result.recommendation_ref, "review_required", datetime.now(UTC))
    updated.set()
    assert (await reader).status == "published"
    monkeypatch.setattr(PublicationService, "_dependency_limits", original)
    async with session_scope() as session:
        assert (await PublicationService(session).get_recommendation(result.recommendation_ref)).status == "review_required"


@pytest.mark.parametrize("change,limited", [("unrelated", False), ("small_backup", False), ("different_canonical", False), ("conflict", True), ("new_period", True)])
@pytest.mark.asyncio
async def test_financial_changes_match_canonical_field_and_registered_tolerance(publication_db, change, limited):
    from investment_agent.evidence.snapshots import materialize
    from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
    from tests.support.evidence import AS_OF, PERIOD, seed
    async with session_scope() as session:
        listing, other, original_row = await seed(session)
        evidence = materialize(original_row, "net_profit_total")
        # This is a saved source candidate; no upgrade to verified facts.
        saved_ref = await EvidenceService(session).save_evidence("local_owner", evidence, uuid4())
        original = (await EvidenceService(session).get_evidence("local_owner", saved_ref, AS_OF)).data
        repository = FinancialSnapshotRepository(session)
        fields = {"net_profit": 123456789, "roe": .4}
        provider = "baostock"
        period = PERIOD
        if change == "small_backup":
            provider, fields = "akshare", {"net_profit_total": 123456790}
        elif change == "different_canonical":
            # iFinD's ambiguous stored net_profit means parent, not total.
            provider, fields = "ifind", {"net_profit": 999999999}
        elif change == "conflict":
            provider, fields = "akshare", {"net_profit_total": 999999999}
        elif change == "new_period":
            period = date(2026, 6, 30)
        captured = AS_OF + timedelta(days=1)
        await repository.insert(listing_id=listing.id, report_period=period, period_kind="semi" if change == "new_period" else "annual", fields=fields, provider=provider, captured_at=captured, availability="actual", available_from=date(2026, 9, 30))
        assert bool(await PublicationService(session)._evidence_changes(original, ("value",), AS_OF + timedelta(days=2))) is limited
        assert not await PublicationService(session)._evidence_changes(original, ("value",), AS_OF)


@pytest.mark.parametrize("checked_latest", [False, True])
@pytest.mark.asyncio
async def test_extension_retains_old_historical_price_requires_explicit_current_check(publication_db, monkeypatch, checked_latest):
    import investment_agent.investor.publication as publication
    from investment_agent.evidence.snapshots import materialize
    from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
    candidate, evidence_ref, old_evidence, old_bar = await market_candidate()
    preparation = await prepared(candidate)
    result = await publish(candidate, preparation)
    # Just before validity expiry, the historical source price is older than
    # the freshness policy; a current same-value capture is available.
    at = candidate.recommendation.valid_until - timedelta(hours=1)
    async with session_scope() as session:
        row = await MarketSnapshotRepository(session).insert(listing_id=old_bar.listing_id, trade_date=date(2026, 10, 14), open=Decimal(10), high=Decimal(10), low=Decimal(10), close=Decimal(10), volume=Decimal(100), pe_ttm=Decimal(5), pb_mrq=Decimal(1), provider="baostock", captured_at=at - timedelta(days=1))
        record = materialize(row, "close")
        session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        latest_ref = ObjectRef(kind="evidence", id=record.id, revision=1)
    class CheckedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(publication, "datetime", CheckedTime)
    checked = (evidence_ref, latest_ref) if checked_latest else (evidence_ref,)
    review = unchanged_review(result.recommendation_ref, checked).model_copy(update={"started_at": at - timedelta(minutes=1), "completed_at": at})
    # This is a later review task with its own trusted current observation
    # binding, not the original publication task seeing future captures.
    async with session_scope() as session:
        later = await admit(session, base=base(), input_key=str(uuid4()),
            binding=ContextBinding(context_refs=(), base_refs=(), as_of=at),
            grant=ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Later review",
                scope_refs=(candidate.thesis.case_ref,), tool_names=("extend_recommendation",)))
        later_run = await PipelineRunRepository(session).claim_next(worker_id="later-review")
        later_claim = await TaskService(session).claim(later.id, RunLease(later_run.id, "later-review", later_run.attempt_count))
    operation = await prepared_extension(later_claim, result.recommendation_ref, review, 2)
    async with session_scope() as session:
        svc = PublicationService(session)
        view = await svc.get_recommendation(result.recommendation_ref)
        assert view.status == "published"
        if checked_latest:
            extended = await svc.extend(later_claim, operation, result.recommendation_ref, review, expected_case_revision=2, base=base())
            assert extended.recommendation.as_of == candidate.recommendation.as_of
            assert extended.recommendation.valid_until == candidate.recommendation.valid_until
            assert extended.effective_valid_until > extended.recommendation.valid_until
        else:
            with pytest.raises(InvestorError, match="explicitly checked"):
                await svc.extend(later_claim, operation, result.recommendation_ref, review, expected_case_revision=2, base=base())


@pytest.mark.asyncio
async def test_exact_publication_version_and_owner_queries(publication_db):
    candidate, _, _ = await fixture()
    result = await publish(candidate)
    async with session_scope() as session:
        service = PublicationService(session)
        with pytest.raises(InvestorError, match="unavailable"):
            await service.get_recommendation(result.recommendation_ref.model_copy(update={"revision": 2}))
        with pytest.raises(InvestorError, match="unavailable"):
            await service.artifact(result.thesis_ref, owner="another_owner")
        check = await service.artifact(result.check_ref)
        assert check.kind == "publication_check" and check.payload["passed"]
        assert "private_history" not in str(check.payload)


@pytest.mark.asyncio
async def test_schema_error_is_not_publication_business_failure(publication_db):
    from pydantic import ValidationError
    candidate, _, _ = await fixture()
    payload = candidate.model_dump(mode="json")
    payload["recommendation"]["position_proposal"] = {"target": "invented"}
    with pytest.raises(ValidationError):
        PublicationCandidate.model_validate(payload)


@pytest.mark.asyncio
async def test_ag18_historical_body_keeps_original_numeric_source_after_correction(publication_db):
    from investment_agent.investor.presentation import recommendation_body
    candidate, fact_ref, subject = await fixture()
    published = await publish(candidate)
    async with session_scope() as session:
        svc = InvestorService(session)
        original = await recommendation_body(svc, published.recommendation_ref)
        node = original["resolved_nodes"]["recommendation.claims.0.text.1"]
        assert node["text"] == "1234500000.00 CNY"
        assert node["subject_ref"] == subject.model_dump(mode="json")
        saved = await session.get(SavedEvidence, (str(fact_ref.id), 1))
        corrected = EvidenceRecord.model_validate(saved.payload).model_copy(update={
            "id": uuid4(), "created_at": datetime.now(UTC), "published_at": datetime.now(UTC),
            "acquired_at": datetime.now(UTC), "available_from": datetime.now(UTC),
            "value": Quantity(value="99", unit="亿元", currency="CNY"), "correction_refs": (fact_ref,)})
        session.add(SavedEvidence(id=str(corrected.id), revision=1, owner_id="local_owner", payload=corrected.model_dump(mode="json")))
    async with session_scope() as session:
        current = await recommendation_body(InvestorService(session), published.recommendation_ref)
        assert current["resolved_nodes"] == original["resolved_nodes"]
        assert current["view"]["status"] == "review_required"
        assert current["body_as_of"] == original["body_as_of"]
        assert not current["render_errors"]


@pytest.mark.asyncio
async def test_ag18_authenticated_body_calculation_and_case_history(publication_db):
    from httpx import ASGITransport, AsyncClient

    from investment_agent.api.app import create_app
    candidate, fact, _ = await fixture()
    published = await publish(candidate)
    record = CalculationRecord(**metadata(), method="reverse_dcf_fcfe", method_version="v1", fact_refs=(fact,),
        assumption_refs=(), input_hash="missing-fixture", as_of=datetime.now(UTC), applicable=False,
        status="missing_inputs", results={}, errors=("unverified_fcfe",), details={"unknown_input": None})
    async with session_scope() as session:
        session.add(SavedCalculation(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
    from investment_agent.api.auth import get_bootstrap_settings
    app = create_app(settings=base())
    app.dependency_overrides[get_bootstrap_settings] = base
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        paths = [f"/investor/recommendations/{published.recommendation_ref.id}/body",
            f"/investor/calculations/{record.id}", f"/investor/cases/{candidate.recommendation.case_ids[0]}/recommendations"]
        for path in paths:
            assert (await client.get(path)).status_code == 401
        headers = {"X-API-Key": "ci-inert-api-key"}
        body = await client.get(paths[0], headers=headers)
        assert body.status_code == 200
        assert body.json()["resolved_nodes"]["recommendation.claims.0.text.1"]["ref"]["id"] == str(fact.id)
        calc = await client.get(paths[1], headers=headers)
        assert calc.json() == record.model_dump(mode="json")
        assert calc.json()["results"] == {} and calc.json()["details"]["unknown_input"] is None
        history = await client.get(paths[2], headers=headers)
        assert history.json()[0]["ref"] == published.recommendation_ref.model_dump(mode="json")
        assert (await client.get(paths[0]+"?revision=2", headers=headers)).status_code == 404
        assert (await client.get(paths[2]+"?limit=101", headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_typed_portfolio_mandate_dependency_invalidates_on_user_revision(publication_db):
    from investment_agent.investor.contracts import Dependency, PortfolioConstraints
    candidate, _, _ = await fixture()
    mandate_ref = candidate.recommendation.mandate_revision_id
    candidate = candidate.model_copy(update={"recommendation": candidate.recommendation.model_copy(update={"dependency_refs": (*candidate.recommendation.dependency_refs, Dependency(ref=mandate_ref, materiality="critical", fields=("portfolio_constraints",)))})})
    result = await publish(candidate)
    assert result.published
    async with session_scope() as session:
        foundation = InvestorService(session)
        original = await foundation.get_mandate("local_owner")
        constraints = PortfolioConstraints(account_id=uuid4(), max_single_weight=Decimal("0.1"))
        await foundation.revise_mandate("local_owner", original.model_copy(update={"revision": 2, "portfolio_constraints": constraints}), Mutation(expected_revision=1, idempotency_key=uuid4()))
        svc = PublicationService(session)
        view = await svc.get_recommendation(result.recommendation_ref)
        assert view.status == "review_required"
        assert "mandate_changed:portfolio_constraints" in view.limitation_reasons
        assert (await svc.get_recommendation(result.recommendation_ref, at=view.recommendation.created_at)).status == "published"
