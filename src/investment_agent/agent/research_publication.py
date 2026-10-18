"""Business-only publication preparation for one Agent's explicit review step."""
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
    ResearchValidationError,
)
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
    RuleReviewItem,
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


class ClosureProposalInput(Contract):
    """Model-side closure proposal; the server freezes it into prepared coverage."""

    target_application_refs: tuple[ObjectRef, ...] = Field(min_length=1)
    evidence_refs: tuple[ObjectRef, ...] = ()
    claim_key: Text | None = None
    rationale: Text

    @model_validator(mode="after")
    def proposal_targets(self) -> "ClosureProposalInput":
        for ref in self.target_application_refs:
            if ref.kind != "rule_application" or ref.field_path is not None:
                raise ValueError("closure proposals must target exact rule applications")
        for ref in self.evidence_refs:
            if ref.field_path is not None or ref.revision is None:
                raise ValueError("closure evidence must cite exact revisions")
        return self


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
    closure_proposals: tuple[ClosureProposalInput, ...] = ()


class SemanticInput(Contract):
    preparation_ref: ObjectRef
    assessments: tuple[SemanticAssessment, ...]
    checked_sources: tuple[ObjectRef, ...]
    unresolved_questions: tuple[Text, ...]
    conclusion: Literal["pass", "revise"]
    rule_reviews: tuple[RuleReviewItem, ...] = ()
    coverage_manifest_ref: ObjectRef | None = None
    coverage_manifest_hash: str | None = None


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
    cutoff = await effective_cutoff(session, task_id)
    resolver = NumericResolver(EvidenceService(session), case.listing_ref, cutoff)
    # WP12: the peer-context method entry verifies its own calculations and
    # provides the review material; other methods keep the original expansion.
    from investment_agent.industry_peers.resolver import PeerContextResolver, peer_method_payload
    peer_resolver = PeerContextResolver(session, binding=grant.peer_context) if grant.peer_context is not None else None
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
                elif ref.kind == "calculation" and peer_resolver is not None and await peer_method_payload(session, grant.owner, ref) is not None:
                    # WP13: the method entry runs the complete input checklist
                    # and returns the fixed model-facing digest; the digest's
                    # manifest_hash (delivered inside it) binds the material
                    # set hash to the full manifest and audit, and `verified`
                    # stays the honest checklist answer - a digest never
                    # promotes a pending source.
                    material = await peer_resolver.review_material(ref)
                    materials.append(material.material)
                    verified = verified and material.verified
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
    pending = [f"claims.{i}" for i, c in enumerate(claims) if c.materiality == "critical" and c.verification != "verified"]
    if pending:
        # Return bounded native correction feedback before echoing an invalid
        # candidate and its full material set into all subsequent requests.
        # The publication gate still rechecks the eventual prepared revision.
        raise ResearchValidationError("research_publication_pending_premises", "claims",
            "服务端核查出的待核验关键主张为：" + ", ".join(pending) + "。你提交的 verification 不会提升来源状态；关键主张的每条支持、反对或假设来源均需真实核验且无冲突，可将已核验事实与候选背景拆开。请修订 prepare_company_view：无法证实方向时将 business_view 与 price_view 设为 undetermined，明确已读材料、核验缺口及风险；只有不再支撑方向的候选材料可作为明确标注的 context。不得把原方向保留并仅降低主张的重要性，也不得自行提升来源核验状态。")
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
    from investment_agent.industry_peers.resolver import DIGEST_SCHEMA

    summaries: list[dict[str, Any]] = []
    for material_record in materials:
        if material_record.get("summary_schema") == DIGEST_SCHEMA:
            # WP13 fixed peer formatter: the digest *is* the bounded summary
            # (its manifest_hash inside binds the material set hash to the
            # complete input manifest and exclusion audit; its summary_hash
            # records the model-visible digest separately). No re-projection.
            summaries.append(material_record)
        elif "fact_refs" in material_record:
            summaries.append({key: material_record[key] for key in ("id", "revision", "method_version", "as_of", "results", "quality", "assumptions", "errors", "input_hash") if key in material_record})
        else:
            summaries.append({key: material_record[key] for key in ("id", "revision", "subject_ref", "period", "source_ref", "content_hash", "verification", "conflict", "value", "text", "published_at", "acquired_at", "available_from") if key in material_record})
    # AF-P3-05: a bound task appends its rule-coverage digest to the same
    # bounded review window and persists one prepared manifest per prepare.
    # Coverage semantics stay independent of the material evidence hash.
    coverage: dict[str, Any] = {}
    from investment_agent.supplemental_rules.binding import get_task_binding

    binding = await get_task_binding(session, owner=grant.owner, task_id=task_id)
    if binding is not None:
        from investment_agent.storage.repositories.investment_assessment import (
            InvestmentAssessmentRepository,
        )
        from investment_agent.storage.repositories.supplemental_updates import (
            SupplementalUpdatesRepository,
        )
        from investment_agent.supplemental_rules.coverage import (
            build_prepared_manifest,
            build_update_source_binding,
            coverage_complete,
            coverage_limitations,
            ensure_binding_evaluation,
            rule_summary_entries,
        )

        await ensure_binding_evaluation(session, binding=binding, listing_ref=case.listing_ref)
        source_repo = SupplementalUpdatesRepository(session)
        source_states = await source_repo.list_sources(
            grant.owner, int(case.listing_ref.id), "company_research"
        )
        source_binding = await build_update_source_binding(
            session, owner=grant.owner, listing_ref=case.listing_ref,
            context="company_research", observed_at=now, knowledge_cutoff=cutoff,
        )
        manifest = await build_prepared_manifest(
            session, binding=binding, case_ref=business.case_ref, listing_ref=case.listing_ref,
            cutoff=cutoff, closure_proposals=business.closure_proposals,
            material_set_hash=material_hash,
            update_source_binding=source_binding,
        )
        saved = await InvestmentAssessmentRepository(session).save_rule_coverage(grant.owner, manifest)
        summaries.extend(rule_summary_entries(saved))
        source_summary = None
        if source_states and source_binding is None:
            source_summary = {
                "summary_kind": "update_source_coverage",
                "state": "unknown",
                "listing_ref": case.listing_ref.model_dump(mode="json"),
                "context": "company_research",
                "scope_key": f"listing:{case.listing_ref.id}:company_research",
                "sources": [{
                    "source_id": state.feed_id,
                    "contract_version": state.contract_version,
                    "contract_hash": state.config_hash,
                    "scope_hash": state.scope_hash,
                    "coverage_state": state.coverage_state,
                    "success_sequence": state.success_sequence,
                    "success_receipt_hash": state.success_receipt_hash,
                    "last_attempt_at": state.last_attempt_at.isoformat() if state.last_attempt_at else None,
                    "last_success_at": state.last_success_at.isoformat() if state.last_success_at else None,
                    "health_limit_seconds": state.health_limit_seconds,
                    "failure_code": state.failure_code,
                    "retry_at": state.retry_at.isoformat() if state.retry_at else None,
                } for state in source_states[:16]],
            }
            summaries.append(source_summary)
        coverage = {
            "coverage_manifest_ref": saved.reference.model_dump(mode="json"),
            "coverage_manifest_hash": saved.manifest_hash,
            "coverage_complete": coverage_complete(saved),
            "coverage_limitations": list(coverage_limitations(saved)),
        }
        if source_summary is not None:
            coverage["source_coverage"] = source_summary
        elif source_binding is not None:
            from investment_agent.supplemental_rules.contracts import SavedRefsSourceProof

            coverage["source_coverage"] = {
                "summary_kind": "update_source_coverage",
                "state": "complete",
                "source_policy_hash": source_binding.source_policy_hash,
                "scope_key": source_binding.scope_key,
                "sources": [
                    source.entry.source_id if isinstance(source, SavedRefsSourceProof)
                    else source.source_id
                    for source in source_binding.sources
                ],
            }
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
        method_revision_id=method.method_ref, provenance=tuple(dict.fromkeys(r for c in claims for r in (*c.support_refs, *c.opposition_refs, *c.assumption_refs))),
        business_view=business.business_view, price_view=business.price_view, conditional_actions=business.conditional_actions)
    candidate = PublicationCandidate(thesis=thesis, recommendation=recommendation, monitors=monitors)
    ref = ObjectRef(kind="publication_preparation", id=operation, revision=1)
    output = {"preparation_ref": ref.model_dump(mode="json"), "candidate": candidate.model_dump(mode="json"),
        "candidate_hash": candidate_hash(candidate), "material_set_hash": material_hash,
        "materials": summaries, "material_count": len(materials), "material_review_blocked": review_blocked, "material_limit": "At most thirty-two exact sources and bounded summaries; narrow scope or save a material gap when exceeded", "review_required": "Assess every exact claim ID and relevant opposing material before final output"}
    output["publication_constraints"] = {
        "pending_critical_claim_ids": [str(c.id) for c in claims if c.materiality == "critical" and c.verification != "verified"],
        "policy": "Critical historical facts and directional claims require verified, conflict-free premises. If a direction cannot be substantiated, keep that business/price view undetermined and explain the remaining gaps. Pending material may remain explicitly labelled context; never upgrade its verification. Publication rechecks all rules after semantic review.",
    }
    if coverage:
        # Only bound tasks carry coverage fields; the legacy receipt stays
        # byte-identical to the pre-P3 shape.
        output.update(coverage)
    return ActionResult(output, artifact_refs=(ref,))


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
    # The prepare receipt is the only trusted coverage source: the model can
    # supply per-rule dispositions, never the manifest binding itself.
    coverage_ref_data = action.result.get("coverage_manifest_ref")
    coverage_ref = ObjectRef.model_validate(coverage_ref_data) if coverage_ref_data else None
    semantic = SemanticReview(id=uuid5(UUID(action.operation_id), "semantic"), revision=1,
        actor="research_service", created_at=datetime.now(UTC), origin_ref=ObjectRef(kind="task", id=task_id, revision=1),
        candidate_hash=action.result["candidate_hash"], material_set_hash=action.result["material_set_hash"],
        method_ref=method.method_ref, assessments=review.assessments, checked_sources=review.checked_sources,
        unresolved_questions=review.unresolved_questions, conclusion=review.conclusion,
        rule_reviews=review.rule_reviews,
        coverage_manifest_ref=coverage_ref,
        coverage_manifest_hash=action.result.get("coverage_manifest_hash") if coverage_ref else None,
        reviewer="single_agent_explicit_review")
    return candidate, semantic
