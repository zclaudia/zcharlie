"""Business-only publication preparation for one Agent's explicit review step."""
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant
from investment_agent.agent.observation import effective_cutoff
from investment_agent.agent.tasks import ActionResult, fingerprint
from investment_agent.agent.tools import ToolRegistration
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.investor.contracts import (
    Claim,
    CompanyRecommendation,
    ConditionalAction,
    ContentNode,
    Dependency,
    DimensionAssessment,
    Hypothesis,
    MonitorCondition,
    MonitorSpec,
    ThesisRevision,
)
from investment_agent.investor.enums import AssumptionSource, BusinessView, PriceView, Sufficiency
from investment_agent.investor.methods import task_method
from investment_agent.investor.publication import (
    PublicationCandidate,
    PublicationService,
    SemanticAssessment,
    SemanticReview,
    candidate_hash,
)
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.storage.models.agent import ExecutionGrantRow, ToolAction


class HypothesisInput(Contract):
    key: Text
    question: Text
    scope: Text
    support_condition: Text
    falsification_condition: Text
    source: AssumptionSource = AssumptionSource.AGENT_SCENARIO
    status: Literal["untested", "supported", "weakened", "falsified"] = "untested"
    last_check_ref: ObjectRef | None = None


class MonitorInput(Contract):
    hypothesis_key: Text | None = None
    trigger: Literal["field", "calendar"] = "calendar"
    conditions: tuple[MonitorCondition, ...] = ()
    required: bool = True


class CompanyViewInput(Contract):
    case_ref: ObjectRef
    question_scope: Text
    change_reason: Text
    assessed_dimensions: tuple[DimensionAssessment, ...]
    claims: tuple[Claim, ...]
    counter_case: tuple[ContentNode, ...]
    gaps: tuple[Text, ...]
    dependencies: tuple[Dependency, ...]
    business_view: BusinessView
    price_view: PriceView
    sufficiency: Sufficiency
    conditional_actions: tuple[ConditionalAction, ...] = ()
    hypotheses: tuple[HypothesisInput, ...] = ()
    condition_hypothesis_keys: tuple[Text, ...] = ()
    condition_refs: tuple[ObjectRef, ...] = ()
    monitors: tuple[MonitorInput, ...] = ()


class SemanticInput(Contract):
    preparation_ref: ObjectRef
    assessments: tuple[SemanticAssessment, ...]
    checked_sources: tuple[ObjectRef, ...]
    unresolved_questions: tuple[Text, ...]
    conclusion: Literal["pass", "revise"]


async def scope(session: AsyncSession, business: CompanyViewInput) -> tuple[ObjectRef, ...]:
    del session
    return (business.case_ref,)


async def prepare(session: AsyncSession, operation: UUID, business: CompanyViewInput) -> ActionResult:
    from investment_agent.investor.foundations import InvestorError, InvestorService
    action = await session.get(ToolAction, str(operation))
    assert action is not None
    grant_row = await session.get(ExecutionGrantRow, action.task_id)
    assert grant_row is not None
    grant = ExecutionGrant.model_validate(grant_row.payload)
    if grant.research_kind not in {"company_research", "review"}:
        raise ExecutionDeniedError("full_research_publication_preparation_required")
    task_id = UUID(action.task_id)
    method = await task_method(session, task_id)
    if not await preparation_ready(session, task_id):
        raise ExecutionDeniedError("persisted_research_plan_required_before_publication_preparation")
    foundation = InvestorService(session)
    case = await foundation.get_case("local_owner", business.case_ref.id)  # type: ignore[arg-type]
    if business.case_ref != ObjectRef(kind="case", id=case.id, revision=case.revision):
        raise ExecutionDeniedError("publication_case_revision_changed")
    mandate = await foundation.get_mandate("local_owner")
    cutoff = await effective_cutoff(session, task_id)
    resolver = NumericResolver(EvidenceService(session), case.listing_ref, cutoff)
    materials = []
    claims = []
    for index, claim in enumerate(business.claims):
        verified = True
        for ref in (*claim.support_refs, *claim.opposition_refs, *claim.assumption_refs):
            try:
                if ref.kind == "evidence":
                    record = await resolver.fact(ref, require_fresh=False)
                    materials.append(record.model_dump(mode="json"))
                    verified = verified and record.verification == "verified" and record.conflict != "unresolved"
                elif ref.kind == "calculation":
                    calculation = await resolver.calculation(ref, require_fresh=False)
                    materials.append(calculation.model_dump(mode="json"))
                    for fact in calculation.fact_refs:
                        record = await resolver.fact(fact, require_fresh=False)
                        materials.append(record.model_dump(mode="json"))
                        verified = verified and record.verification == "verified" and record.conflict != "unresolved"
                else:
                    verified = False
            except (InvestorError, ValueError, LookupError):
                verified = False
        # No model can promote a source to verified. Empty facts stay pending.
        if claim.kind == "fact" and not claim.support_refs:
            verified = False
        claims.append(claim.model_copy(update={"id": uuid5(operation, f"claim:{index}"), "verification": "verified" if verified else "pending"}))
    now = datetime.now(UTC)
    origin = ObjectRef(kind="task", id=task_id, revision=1)
    metadata: dict[str, Any] = {"revision": 1, "actor": "research_service", "created_at": now, "origin_ref": origin}
    next_review, valid_until = PublicationService(session).policy.dates(cutoff)
    thesis_id = uuid5(operation, "thesis")
    hypotheses = tuple(Hypothesis(**metadata, id=uuid5(operation, "hypothesis-record:" + h.key),
        hypothesis_id=uuid5(operation, "hypothesis:" + h.key), question=h.question, source=h.source,
        scope=h.scope, support_condition=h.support_condition, falsification_condition=h.falsification_condition,
        check_by=next_review, status=h.status, last_check_ref=h.last_check_ref) for h in business.hypotheses)
    hypothesis_refs = {source.key: ObjectRef(kind="thesis", id=thesis_id, revision=1,
        field_path=f"assumptions.{value.hypothesis_id}") for source, value in zip(business.hypotheses, hypotheses, strict=True)}
    if len(hypothesis_refs) != len(hypotheses) or not set(business.condition_hypothesis_keys) <= hypothesis_refs.keys() or any(m.hypothesis_key and m.hypothesis_key not in hypothesis_refs for m in business.monitors):
        raise ExecutionDeniedError("publication_hypothesis_keys_invalid")
    monitors = tuple(MonitorSpec(**metadata, id=uuid5(operation, f"monitor:{i}"), case_ref=business.case_ref,
        hypothesis_ref=hypothesis_refs.get(m.hypothesis_key) if m.hypothesis_key else None,
        conditions=m.conditions, trigger=m.trigger, next_due_at=next_review, required=m.required, active=True)
        for i, m in enumerate(business.monitors))
    material_records = {(r["id"], r["revision"]): r for r in materials}
    materials = sorted(material_records.values(), key=lambda r: (r["id"], r["revision"]))
    material_hash = fingerprint(materials)
    summaries: list[dict[str, Any]] = []
    for material_record in materials:
        if "fact_refs" in material_record:
            summaries.append({key: material_record[key] for key in ("id", "revision", "method_version", "as_of", "results", "quality", "assumptions", "errors", "input_hash") if key in material_record})
        else:
            summaries.append({key: material_record[key] for key in ("id", "revision", "subject_ref", "period", "source_ref", "content_hash", "verification", "conflict", "value", "text", "published_at", "acquired_at", "available_from") if key in material_record})
    review_blocked = len(summaries) > 32 or len(json.dumps(summaries, ensure_ascii=False)) > 64000
    if review_blocked:
        summaries = []  # Explicitly blocked, never an incomplete passing review.

    previous_ref = case.current_thesis_ref
    reason = business.change_reason
    if previous_ref:
        previous = await PublicationService(session).artifact(previous_ref)
        if previous.payload.get("evidence_set_hash") == material_hash:
            reason = "reasoning_revision"
    thesis = ThesisRevision(**metadata, id=thesis_id, case_ref=business.case_ref,
        previous_ref=previous_ref, change_reason=reason, question_scope=business.question_scope,
        assessed_dimensions=business.assessed_dimensions, claims=tuple(claims), assumptions=hypotheses,
        counter_case=business.counter_case, evidence_set_hash=material_hash, method_ref=method.method_ref, as_of=cutoff)
    recommendation = CompanyRecommendation(**metadata, id=uuid5(operation, "recommendation"), scope="company_view",
        case_ids=(case.id,), thesis_revision_refs=(ObjectRef(kind="thesis", id=thesis.id, revision=1),),
        as_of=cutoff, question_scope=business.question_scope, assessed_dimensions=business.assessed_dimensions,
        sufficiency=business.sufficiency, claims=tuple(claims), counter_case=business.counter_case,
        assumptions=hypotheses, gaps=business.gaps, conditions=(*business.condition_refs, *(hypothesis_refs[k] for k in business.condition_hypothesis_keys)), valid_until=valid_until,
        next_review_at=next_review, monitor_refs=tuple(ObjectRef(kind="monitor", id=m.id, revision=1) for m in monitors), dependency_refs=business.dependencies,
        mandate_revision_id=ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision),
        method_revision_id=method.method_ref, provenance=tuple(dict.fromkeys(r for c in claims for r in (*c.support_refs, *c.opposition_refs, *c.assumption_refs))),
        business_view=business.business_view, price_view=business.price_view, conditional_actions=business.conditional_actions)
    candidate = PublicationCandidate(thesis=thesis, recommendation=recommendation, monitors=monitors)
    ref = ObjectRef(kind="publication_preparation", id=operation, revision=1)
    return ActionResult({"preparation_ref": ref.model_dump(mode="json"), "candidate": candidate.model_dump(mode="json"),
        "candidate_hash": candidate_hash(candidate), "material_set_hash": material_hash,
        "materials": summaries, "material_count": len(materials), "material_review_blocked": review_blocked, "material_limit": "At most thirty-two exact sources and bounded summaries; narrow scope or save a material gap when exceeded", "review_required": "Assess every exact claim ID and relevant opposing material before final output"}, artifact_refs=(ref,))


async def preparation_ready(session: AsyncSession, task_id: UUID) -> bool:
    """Schema availability mirrors the existing server prerequisite; grants stay intact."""
    from investment_agent.agent.research import ResearchProgress
    rows = (await session.scalars(select(ToolAction).where(
        ToolAction.task_id == str(task_id), ToolAction.state == "committed"
    ))).all()
    for action in rows:
        if action.parameters.get("tool") == "save_research_progress" and action.result:
            progress = ResearchProgress.model_validate(action.result["progress"])
            if progress.kind in {"company_research", "review"}:
                return True
    return False


def registration() -> ToolRegistration:
    return ToolRegistration("prepare_company_view", CompanyViewInput, scope, database=prepare, ready=preparation_ready)  # type: ignore[arg-type]


async def reviewed_candidate(session: AsyncSession, task_id: UUID, review: SemanticInput) -> tuple[PublicationCandidate, SemanticReview]:
    if review.preparation_ref.kind != "publication_preparation" or review.preparation_ref.revision != 1:
        raise ExecutionDeniedError("publication_preparation_required")
    action = await session.get(ToolAction, str(review.preparation_ref.id))
    if action is None or action.task_id != str(task_id) or action.state != "committed" or action.parameters.get("tool") != "prepare_company_view" or not action.result:
        raise ExecutionDeniedError("publication_preparation_unavailable")
    if action.result.get("material_review_blocked"):
        raise ExecutionDeniedError("publication_material_review_window_exceeded")
    method = await task_method(session, task_id)
    candidate = PublicationCandidate.model_validate(action.result["candidate"])
    if candidate.thesis.method_ref != method.method_ref or candidate.recommendation.method_revision_id != method.method_ref:
        raise ExecutionDeniedError("task_bound_research_method_required")
    semantic = SemanticReview(id=uuid5(UUID(action.operation_id), "semantic"), revision=1,
        actor="research_service", created_at=datetime.now(UTC), origin_ref=ObjectRef(kind="task", id=task_id, revision=1),
        candidate_hash=action.result["candidate_hash"], material_set_hash=action.result["material_set_hash"],
        method_ref=method.method_ref, assessments=review.assessments, checked_sources=review.checked_sources,
        unresolved_questions=review.unresolved_questions, conclusion=review.conclusion, reviewer="single_agent_explicit_review")
    return candidate, semantic
