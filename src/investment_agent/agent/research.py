"""Layered research by the single native Agent and trusted completion policy.

Research notes live in the existing action ledger. They are readable artifacts,
not private framework histories and not formal company recommendations.
"""
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

from pydantic import model_validator
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.builtin_tools import (
    SnapshotParameters,
    builtin_registrations,
    listing_scope,
)
from investment_agent.agent.contracts import ResearchKind
from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
    ResearchValidationError,
    check_boundary,
)
from investment_agent.agent.executor import RunSpecification, TaskDisposition
from investment_agent.agent.observation import effective_cutoff
from investment_agent.agent.research_publication import (
    SemanticInput,
    registration,
    reviewed_candidate,
)
from investment_agent.agent.runtime import RunOutcome
from investment_agent.agent.tasks import ActionResult, TaskClaim, TaskService, fingerprint
from investment_agent.agent.tools import ToolRegistration
from investment_agent.config import Settings
from investment_agent.evidence.queries import SnapshotQueryService
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.investor.contracts import (
    DIMENSIONS,
    ContentNode,
    DimensionAssessment,
    EntityNode,
    NumericNode,
    ReviewEvent,
    TextNode,
)
from investment_agent.investor.controls import (
    ContextStamp,
    Operation,
    PrepareRequest,
    UserControlService,
)
from investment_agent.investor.enums import RecommendationStatus, StopReason
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import MethodConfig, enforce_steps, task_method
from investment_agent.investor.publication import (
    PublicationPolicy,
    PublicationService,
)
from investment_agent.investor.publication_numbers import NUMBER, NumericResolver
from investment_agent.portfolio.plan_contracts import PlanSemanticInput
from investment_agent.storage.models.agent import MessageRow, ToolAction
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.valuation.tools import valuation_registrations


class ResearchHypothesis(Contract):
    question: Text
    explanation: Text
    alternative: Text
    falsification_condition: Text
    evidence_priority: Text


class ResearchProgress(Contract):
    question: Text
    kind: ResearchKind
    content: tuple[ContentNode, ...] = ()
    hypotheses: tuple[ResearchHypothesis, ...] = ()
    evidence_steps: tuple[Text, ...] = ()
    completion_criteria: tuple[Text, ...] = ()
    stop_conditions: tuple[Text, ...] = ()
    dimensions: tuple[DimensionAssessment, ...] = ()
    fact_refs: tuple[ObjectRef, ...] = ()
    counter_refs: tuple[ObjectRef, ...] = ()
    inferences: tuple[Text, ...] = ()
    assumptions: tuple[Text, ...] = ()
    gaps: tuple[Text, ...] = ()
    completed_steps: tuple[Text, ...] = ()
    limitations: tuple[Text, ...] = ()
    answered: bool = False
    stopped_because: Text | None = None

    @model_validator(mode="after")
    def depth(self) -> "ResearchProgress":
        if self.kind in {"company_research", "review"}:
            if not self.hypotheses or not self.evidence_steps or not self.completion_criteria or not self.stop_conditions:
                raise ValueError("Full research requires falsifiable hypotheses and an evidence plan")
            if len(self.dimensions) != len(DIMENSIONS) or {d.dimension for d in self.dimensions} != set(DIMENSIONS):
                raise ValueError("Full research requires every dimension exactly once")
        if self.kind == "comparison" and not self.fact_refs and self.answered:
                raise ValueError("Comparison needs saved same-period evidence")
        return self


class ResearchOutput(Contract):
    progress: ResearchProgress
    semantic_review: SemanticInput | None = None
    portfolio_review: PlanSemanticInput | None = None
    review: ReviewEvent | None = None
    proposal: Operation | None = None
    extend: bool = False
    critical_counter_evidence: tuple[ObjectRef, ...] = ()


class CompanyResearchOutput(ResearchOutput):
    """Company runs do not need portfolio or user-control schemas in every request.

    Runtime validation/authorization still uses the complete ResearchOutput contract.
    Explicit control conversations and portfolio tasks retain their full schema.
    """
    portfolio_review: SkipJsonSchema[PlanSemanticInput | None] = None
    proposal: SkipJsonSchema[Operation | None] = None


class CompanyStudyOutput(CompanyResearchOutput):
    """A new company study publishes a new view, not a maintain/extend review."""
    review: SkipJsonSchema[ReviewEvent | None] = None
    extend: SkipJsonSchema[bool] = False


class LocalResearchProgress(ResearchProgress):
    """Bounded answers retain runtime checks without asking for a full plan."""

    hypotheses: SkipJsonSchema[tuple[ResearchHypothesis, ...]] = ()
    evidence_steps: SkipJsonSchema[tuple[Text, ...]] = ()
    completion_criteria: SkipJsonSchema[tuple[Text, ...]] = ()
    stop_conditions: SkipJsonSchema[tuple[Text, ...]] = ()
    dimensions: SkipJsonSchema[tuple[DimensionAssessment, ...]] = ()
    completed_steps: SkipJsonSchema[tuple[Text, ...]] = ()


class LocalResearchOutput(CompanyResearchOutput):
    progress: LocalResearchProgress
    semantic_review: SkipJsonSchema[SemanticInput | None] = None
    review: SkipJsonSchema[ReviewEvent | None] = None


METHOD_INSTRUCTIONS = """Use the exact task-bound company-research method and finite strengthening config supplied by the controller. Enforce each enabled check; method configuration never relaxes source, permission, budget or publication rules.
Answer only the authorized research kind and exact security scope. With no
security scope explain concepts or ask for an exact listing; never guess a ticker.
Company research and company-view publication do not require a personal investment
mandate. Answer business, price, evidence and risk questions within the admitted
scope; never infer the user's holdings, suitability or position sizes. Personal
allocation advice requires explicitly admitted portfolio_action authority and a
current mandate; company research cannot grant background execution permission.
For explanations and local checks inspect the relevant saved material directly,
then return a bounded answer, exact source references and limitations. Do not
start a full company-research plan or populate every research dimension for a
local question. The controller saves the final answer; an extra
save_research_progress call is unnecessary for a short explanation/local check.
For a local check gather a small relevant batch, then answer concisely from it;
do not keep collecting to fill every dimension. Avoid repeating the same prose
across content, inferences, gaps and limitations. Omit optional empty fields.
Never replace a global company view. For complete company research and formal
review first call save_research_progress with the question, falsifiable hypotheses,
strongest alternative explanations, prioritized evidence steps, completion and stop
criteria; report all seven dimensions as evidenced, qualitative or missing. Save
actual progress before expensive further work. Facts, inferences and assumptions
must be distinct. The final progress still requires the saved question, kind, hypotheses,
evidence_steps, completion_criteria, stop_conditions and all seven dimensions;
retain these required fields when returning the concise final answer.
For full research/review an initial plan is not a final answer: execute save_research_progress
as a tool call, then inspect available material using get_snapshot/capture_snapshot.
Do not merely describe future tool calls in a final JSON answer. If you must stop,
give an explicit stopped_because and concrete remaining gaps. entity_ref accepts
only the exact authorized listing reference; omit internal case/method/task/message
IDs from display content.
Missing materials mean unknown, not unfavorable. User preferences
and approval/disapproval affect explanation and constraints, never company evidence.
Price-only changes cannot supply a new business claim. Comparisons require the same
report period, canonical field, unit and basis; otherwise show limitations without
ranking. search_saved_sources searches only already saved authorized listing material:
search relevance, titles and summaries are discovery leads, never verified claims.
Return exact saved evidence/source/locator/hash citations. Same original hash or
republication cannot add independent corroboration. Respect explicit coverage caps,
parse availability and fixed task cutoff; a later parse needs a new message.
Use compare_saved_sources for exact saved numeric facts; it explicitly refuses
mismatched period, unit, currency, metric, reporting scope or restricted sources.
get_evidence accepts only saved evidence UUID refs, never listing/case refs.
Prefer capture_snapshot directly for a known stored field: it reads and saves in one call,
returning exact saved evidence and source material together. No preceding get_snapshot
or subsequent get_evidence is needed for the same returned material. Batch a small
set of decision-relevant fields in the same round, then analyze before gathering more.
Missing or candidate provider data must remain a gap, not a verified fact.
Capture snapshot material with capture_snapshot before citing saved evidence.
Trusted target_identities supplies the authorized company's canonical ticker for
snapshot tools. It is identity metadata, not evidence. Text question/gaps contain
qualitative descriptions or company names only; exact security codes and numeric
facts belong in progress.content entity_ref/numeric_claim nodes, never hidden in text.
This applies to EVERY display text field, including question, content text,
inferences, assumptions, gaps, completed_steps, limitations and stopped_because.
Never copy the trusted observation timestamp, a report date or ticker into these
text fields. The controller already retains the observation cutoff and identity.
Use qualitative wording such as "within the task's observation cutoff" instead.
中文展示文本只写定性描述与公司名；不要复述日期、证券代码、数值或中文数词。
具体数值只能通过已保存证据的 numeric_claim 引用展示；日期和身份保留在结构化元数据中。
Cite exact saved refs; never grant yourself verified status.
An undetermined company view with explicit known material, risks and unresolved
gaps is a valid completed study. Do not confuse completing this question with
proving a favorable business or price direction. When all relevant sources remain
pending, report them as pending context and leave directions undetermined; never
retain a directional conclusion by relabelling its critical premises as context.
Keep the prepared claim set small and the final answer concise. Do not echo raw
page text or the prepared candidate JSON in final output; return only the final
progress and the exact semantic review needed for this question.
When reusable_sources are supplied, they have already been read at this task's
cutoff. Use them directly, preserving their exact refs and quality. Do not repeat
search, capture_snapshot or get_evidence for those same sources. Save the plan,
inspect only specific decision-relevant gaps or changes, then prepare the view
and return final progress plus semantic review. Do not add optional valuation
calculations when their assumptions or qualification are unavailable. A complete
study can explicitly record missing dimensions; never manufacture their evidence.
critical_counter_evidence is only for exact sources already checked and included
in progress.counter_refs, not a list of speculative risks or missing data.
For a numeric_claim with role historical_fact, use the exact saved evidence UUID
and revision with ref.field_path="value" (never null). The bare refs returned in
fact_refs/materials identify the record, not its numeric value field. Use
display_format="percent" for fraction/percent values and "decimal" for other
quantities; never invent a unit or rewrite a source value.
For calculation_output use ref.kind="calculation", the saved calculation UUID
and ref.field_path="results.<exact result key>" (plural results), for example
"results.percentile". Copy keys from the saved results map; never use "result."
or invent a calculation field.
For capture_snapshot, only materials with eligibility.fact_reference_allowed=true may enter fact_refs,
counter_refs, dimensions.refs or numeric_claim. Missing/conflicted materials explain
gaps only; missing data is never counter evidence. Copy a returned numeric_claim
template when available; its field path and display format are server-selected.
Historical percentile only describes historical position, not cheapness. The real
FCFE qualification is unavailable: do not substitute CFO-capex or fabricate DCF.
Before publishing perform an explicit semantic review of every critical claim,
its support, opposing material, effect if wrong and unresolved questions, binding
candidate/material hashes and fixed method. Call prepare_company_view to receive
the server-built candidate, exact material and preparation_ref; inspect them.
A claim containing calculation results is inference,
not historical fact; every numeric/entity node must also declare its exact ref
in that claim's premises. Review issues must be resolved by revising the claim
or its evidence before conclusion=pass; unresolved research gaps remain explicit.
Return semantic_review bound to that preparation_ref
only for full company research/revision; the controller may save a failed draft.
A maintain review preserves the existing thesis; extend only when still valid and
all exact critical dependencies were checked. Expired/restricted views need a new
publication. Changed reasoning with unchanged evidence is reasoning_revision.
Critical local counter evidence must be reported with exact saved sources so the
controller restricts the affected recommendation and records formal review required.
Stop after repeated requests without new dependencies, two rounds without new
material or resolved questions, unavailable critical sources or budget exhaustion.
Save concrete gaps, completed work and stopping reason. A proposed user operation
is only a proposal; you cannot approve it or modify authority. For explicitly admitted portfolio_action research use get_portfolio_context, calculate_portfolio_options and prepare_portfolio_plan, after saving portfolio research progress. Alternatives are mutually exclusive and include a no_action comparison. get_portfolio_context includes exact user-confirmed purposes; bind its current purpose_ref in calculations, associate each trading cash movement with an existing purpose_id (this does not assert purpose ownership of holdings), and respect fee-inclusive purpose budgets and required cash. Include the exact purpose version in checked_sources. Purposes are planning envelopes, never extra cash or broker reservations. You may propose changes in prose but only the user can confirm them; never relax short-term reserves using a favorable company view. Exact current company views are required for company-thesis actions; typed user mandate exceptions support only risk-reducing reduce/exit/wait with a research gap. Model scenario rules are assumptions and never grant executable sizing. Missing fields stay explicit. Submit portfolio_review with exact preparation and assess every action. The controller publishes via the prepared-action claim transaction. You cannot write ledger facts, alter mandates, approve decisions, reserve funds or execute trades.
"""


def classify(text: str, requested: str = "auto") -> ResearchKind:
    """Trusted server-side default research depth, chosen from message keywords.

    The Chinese/English keyword heuristic is only a conservative default: an
    explicit ``MessageInput.research_kind`` always overrides it, and the kind
    actually admitted is reported back on task progress.
    """
    if requested != "auto":
        return cast(ResearchKind, requested)
    lowered = text.lower()
    if any(word in lowered for word in ("不要研究", "不需要研究", "don't research", "do not research")):
        return "local_check"
    if any(word in lowered for word in ("完整研究", "全面研究", "完整公司", "形成公司观点", "深入研究", "研究这家公司", "company research", "full research")):
        return "company_research"
    if any(word in lowered for word in ("复核观点", "复核意见", "review recommendation", "观点复核")):
        return "review"
    if any(word in lowered for word in ("比较", "对比", "compare")):
        return "comparison"
    if any(word in lowered for word in ("是什么", "什么意思", "解释", "explain")):
        return "explanation"
    return "local_check"


async def authorized_identities(session: AsyncSession, refs: tuple[ObjectRef, ...]) -> list[dict[str, Any]]:
    """PK-only metadata for trusted targets; never an identity discovery API."""
    from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
    from investment_agent.storage.models.reference import Company, Listing

    result = []
    for ref in refs:
        if ref.kind != "listing":
            continue
        item: dict[str, Any] = {"listing_ref": ref.model_dump(mode="json"), "status": "unavailable"}
        listing = await session.get(Listing, int(ref.id))
        company = await session.get(Company, listing.company_id) if listing else None
        if listing and company:
            ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
            parsed = parse_a_share_ticker(ticker)
            if parsed and parsed[1].market_code == listing.market_code:
                item.update(status="available", canonical_name=company.canonical_name, canonical_ticker=ticker)
        result.append(item)
    return result


async def recovered_evidence(session: AsyncSession, claim: TaskClaim, grant: ExecutionGrant) -> dict[str, Any] | None:
    """Rebuild same-scope continuation from exact owned evidence, never private drafts."""
    from investment_agent.agent.context import pack_evidence
    from investment_agent.storage.models.agent import ExecutionGrantRow
    service = TaskService(session)
    task = await service.get_task(claim.task_id)
    if not task.parent_task_id or grant.research_kind not in {"company_research", "review"}:
        return None
    method = await task_method(session, claim.task_id)
    cutoff = await effective_cutoff(session, claim.task_id)
    parent_id: UUID | None = task.parent_task_id
    visited: set[UUID] = set()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    unavailable: list[dict[str, Any]] = []
    while parent_id and len(visited) < 4:
        if parent_id in visited:
            raise ExecutionDeniedError("parent_research_context_cycle")
        visited.add(parent_id)
        parent = await service.get_task(parent_id)
        row = await session.get(ExecutionGrantRow, str(parent_id))
        if row is None:
            raise ExecutionDeniedError("parent_research_context_unavailable")
        previous = ExecutionGrant.model_validate(row.payload)
        if (previous.owner != grant.owner or previous.research_kind != grant.research_kind or
                set(previous.scope_refs) != set(grant.scope_refs) or
                (await task_method(session, parent_id)).method_ref != method.method_ref):
            raise ExecutionDeniedError("parent_research_context_mismatch")
        for ref in parent.artifact_refs:
            if ref.kind != "evidence":
                continue
            saved = await session.get(SavedEvidence, (str(ref.id), ref.revision))
            if saved is None or saved.owner_id != grant.owner:
                raise ExecutionDeniedError("parent_evidence_unavailable")
            subject = ObjectRef.model_validate(saved.payload["subject_ref"])
            if subject not in grant.scope_refs:
                raise ExecutionDeniedError("parent_evidence_scope_mismatch")
            # Missing/conflicted material is still useful for explaining gaps;
            # it must never be passed off as an eligible numeric fact.
            read = await EvidenceService(session).get_evidence(grant.owner, ref, cutoff, subject_ref=subject)
            if read.data is None:
                unavailable.append({"ref": ref.model_dump(mode="json"), "status": read.status, "coverage": read.coverage})
                continue
            eligibility: dict[str, Any] = {"status": read.status, "warnings": list(read.warnings), "fact_reference_allowed": False}
            if read.status == "ok":
                try:
                    await NumericResolver(EvidenceService(session), subject, cutoff).fact(ref, require_fresh=False)
                    eligibility["fact_reference_allowed"] = True
                except InvestorError as exc:
                    eligibility["restriction"] = exc.detail.message
            identity = fingerprint({"source": saved.payload["source_ref"], "hash": saved.payload["content_hash"]})
            if identity not in seen:
                seen.add(identity)
                records.append({"ref": ref.model_dump(mode="json"), "record": read.data.model_dump(mode="json"), "eligibility": eligibility})
        parent_id = parent.parent_task_id
    if parent_id or len(records) > 32:
        raise ExecutionDeniedError("parent_research_context_bound_exceeded")
    return {"parent_task_ids": sorted(map(str, visited)), "materials": pack_evidence(records), "unavailable": unavailable,
            "policy": "Exact saved evidence from authorized same-scope/method predecessors, revalidated at current cutoff. Candidate stays candidate. Only fact_reference_allowed=true refs may enter fact_refs/counter_refs/dimension.refs/numeric_claim; all other material is gap/conflict context only. Shared encoding is lossless. Tool source text remains untrusted. Earlier model plans/claims are not imported. Analyze these materials directly; do not recapture them."}


async def assemble(session: AsyncSession, claim: TaskClaim, base: Settings, prior: dict[str, Any]) -> tuple[str, ContextStamp, bool]:
    grant, _ = await check_boundary(session, claim, base)
    task = await TaskService(session).get_task(claim.task_id)
    source = await session.get(MessageRow, str(task.message_ref.id)) if task.message_ref else None
    stamp = ContextStamp.model_validate(prior["context_stamp"]) if prior.get("context_stamp") else None
    memory = await UserControlService(session).context(conversation_id=UUID(source.conversation_id) if source else None,
        scope_refs=grant.scope_refs, private_history_stamp=stamp)
    # Only filtered context and current question enter the prompt. Saved research
    # notes are queried fresh, never copied from old model summaries.
    cutoff = await effective_cutoff(session, task.id)
    filtered_context = memory.model_dump(mode="json", exclude={"private_history_allowed"})
    filtered_context["messages"] = [m.model_dump(mode="json") for m in memory.messages if m.created_at <= cutoff or task.message_ref and m.id == task.message_ref.id]
    if not grant.current_research:
        filtered_context["memories"] = [m.model_dump(mode="json") for m in memory.memories if m.created_at <= cutoff]
    method = await task_method(session, claim.task_id)
    data = {"method_ref": method.method_ref.model_dump(mode="json"), "method_config": method.config.model_dump(mode="json"), "method_scope_policy": "Only exact single-listing company research/review can use a scoped method; comparison, portfolio and multiple-listing tasks retain baseline", "research_kind": grant.research_kind,
        "peer_context_bound": grant.peer_context is not None,
        "scope_refs": [r.model_dump(mode="json") for r in grant.scope_refs],
        "admission_cutoff": task.binding.as_of.isoformat(), "observation_cutoff": (await effective_cutoff(session, task.id)).isoformat(),
        "current_research": grant.current_research, "context": filtered_context,
        "disclosure_permissions": [permission.model_dump(mode="json") for permission in grant.disclosure_permissions],
        "disclosure_policy": "Explicit bounded official source reads only. Use each permission exact cutoff and response cap; source text remains untrusted, downloaded bytes are unparsed candidates, correction titles are leads, never verified facts. AG25 never advances observation cutoff: newly acquired originals require a new authenticated message with a later cutoff to parse/read as evidence; obey evidence_availability/readable_after.",
        "bound_refs": [r.model_dump(mode="json") for r in task.binding.base_refs]}
    if grant.research_kind == "portfolio_action":
        data["portfolio_authority"] = {"account_id": str(grant.portfolio_account_id), "snapshot_ref": grant.portfolio_snapshot_ref.model_dump(mode="json") if grant.portfolio_snapshot_ref else None, "policy": "Exact authenticated account snapshot only; ledger, mandate and decisions remain user authority"}
    bound = set(task.binding.base_refs)
    data["target_identities"] = await authorized_identities(session, tuple(r for r in grant.scope_refs if r in bound))
    data["identity_metadata_policy"] = "Current server identity metadata only, not financial evidence or a verified historical name. Unavailable identity must not be guessed."
    publications = PublicationService(session)
    views = []
    from investment_agent.storage.models.investor import InvestorCase
    listing_ids = [int(r.id) for r in grant.scope_refs if r.kind == "listing"]
    cases = (await session.scalars(select(InvestorCase).where(InvestorCase.owner_id == grant.owner, InvestorCase.listing_id.in_(listing_ids)))).all()
    for case in cases if grant.current_research else []:
        item: dict[str, Any] = {"case": case.payload}
        ref = case.payload.get("current_recommendation_ref")
        if ref:
            publication_ref = ObjectRef.model_validate(ref)
            saved_publication = await publications.artifact(publication_ref)
            if datetime.fromisoformat(saved_publication.payload["created_at"].replace("Z", "+00:00")) <= cutoff:
                recommendation = await publications.get_recommendation(publication_ref, at=cutoff)
                if grant.research_kind in {"company_research", "review"}:
                    from investment_agent.agent.company_context import (
                        prior_company_view,
                        reuse_company_sources,
                    )
                    item["recommendation"] = prior_company_view(recommendation.recommendation, publication_ref)
                    item["recommendation"]["status"] = recommendation.status
                    item["recommendation"]["limitation_reasons"] = list(recommendation.limitation_reasons)
                    item["recommendation"]["effective_valid_until"] = recommendation.effective_valid_until.isoformat()
                    item["recommendation"]["effective_next_review_at"] = recommendation.effective_next_review_at.isoformat()
                    item["recommendation"]["replacement_ref"] = recommendation.replacement_ref.model_dump(mode="json") if recommendation.replacement_ref else None
                    item["reusable_sources"] = await reuse_company_sources(session, owner=grant.owner,
                        subject=ObjectRef(kind="listing", id=case.listing_id, revision=1),
                        cutoff=cutoff, recommendation=recommendation.recommendation)
                else:
                    item["recommendation"] = recommendation.model_dump(mode="json")
            else:
                item["case"] = {"listing_ref": case.payload["listing_ref"], "historical_context_unavailable": True}
        views.append(item)
    data["company_context"] = views
    if source and grant.research_kind == "company_research" and any(v.get("reusable_sources", {}).get("records") for v in views):
        from investment_agent.agent.company_plan import reuse_question_plan
        reused_plan = await reuse_question_plan(session, claim, grant, source, method.method_ref)
        if reused_plan:
            data["reused_research_plan"] = reused_plan
    recovered = await recovered_evidence(session, claim, grant)
    if recovered is not None:
        data["recovered_evidence"] = recovered
    if not grant.current_research:
        data["historical_context_limit"] = "Current case heads and opinions omitted because a reliable historical case projection is unavailable"
    instructions = METHOD_INSTRUCTIONS
    if grant.research_kind != "portfolio_action":
        instructions = instructions.split("For explicitly admitted portfolio_action")[0]
    if grant.research_kind in {"company_research", "review"}:
        from investment_agent.agent.company_context import COMPANY_INSTRUCTIONS
        instructions = COMPANY_INSTRUCTIONS
    return instructions + "\nTrusted current context:\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":")), memory.stamp, memory.private_history_allowed


async def validate_progress(session: AsyncSession, task_id: UUID, progress: ResearchProgress, *, method_config: MethodConfig | None = None, final: bool = False, formal_directional: bool = False) -> tuple[str, tuple[ObjectRef, ...]]:
    from investment_agent.storage.models.agent import ExecutionGrantRow
    saved = await session.get(ExecutionGrantRow, str(task_id))
    if saved is None:
        raise ExecutionDeniedError("trusted_admission_required")
    grant = ExecutionGrant.model_validate(saved.payload)
    if progress.kind != grant.research_kind:
        raise ResearchValidationError("research_depth_outside_admission", "progress.kind", "研究类型必须与可信任务授权一致。")
    cutoff = await effective_cutoff(session, task_id)
    subjects = {r for r in grant.scope_refs if r.kind == "listing"}
    # WP12: the peer-context method entry validates its own calculations
    # against the trusted binding; every other method keeps the generic path.
    from investment_agent.industry_peers.resolver import PeerContextResolver, peer_method_payload
    peer_resolver = None
    peer_calc_refs: list[ObjectRef] = []
    if grant.peer_context is not None:
        peer_resolver = PeerContextResolver(session, binding=grant.peer_context)
    ref_fields = {ref: f"progress.dimensions.{i}.refs.{j}" for i, d in enumerate(progress.dimensions) for j, ref in enumerate(d.refs)}
    ref_fields.update({ref: f"progress.counter_refs.{i}" for i, ref in enumerate(progress.counter_refs)})
    ref_fields.update({ref: f"progress.fact_refs.{i}" for i, ref in enumerate(progress.fact_refs)})
    refs = tuple(ref_fields)
    records = []
    for ref in refs:
        if grant.research_kind == "portfolio_action" and ref.kind == "calculation":
            from investment_agent.agent.portfolio_tools import progress_calculation
            await progress_calculation(session, grant, ref)
            continue
        model = SavedEvidence if ref.kind == "evidence" else SavedCalculation if ref.kind == "calculation" else None
        row: Any = await session.get(model, (str(ref.id), ref.revision)) if model else None
        if row is None or row.owner_id != grant.owner:
            raise ResearchValidationError("research_reference_unavailable", ref_fields[ref], "引用必须来自当前已保存、可定位的 evidence/calculation；身份引用不是证据。")
        peer_payload = await peer_method_payload(session, grant.owner, ref) if ref.kind == "calculation" else None
        if peer_payload is not None:
            # WP12 method branch: the trusted binding owns the target identity;
            # every result's real multi-company dependencies are verified per
            # real subject. A peer-method calculation never falls through to
            # the single-subject reconstruction below.
            if peer_resolver is None:
                raise ExecutionDeniedError("peer_calculation_not_bound")
            try:
                subject = await peer_resolver.bind_subject(ref)
            except InvestorError as exc:
                raise ResearchValidationError("research_reference_validation_failed", ref_fields[ref], exc.detail.message) from exc
            peer_calc_refs.append(ref)
        elif ref.kind == "evidence":
            subject = ObjectRef.model_validate(row.payload["subject_ref"])
        else:
            # Reconstruct calculation scope from saved evidence facts.
            fact_rows = [await session.get(SavedEvidence, (str(f["id"]), f["revision"])) for f in row.payload.get("fact_refs", [])]
            fact_subjects = {ObjectRef.model_validate(f.payload["subject_ref"]) for f in fact_rows if f is not None}
            if len(fact_subjects) != 1:
                raise ExecutionDeniedError("calculation_subject_unavailable")
            subject = next(iter(fact_subjects))
        if subject not in subjects:
            raise ResearchValidationError("research_reference_scope_mismatch", ref_fields[ref], "证据主体必须与本任务明确授权的证券一致。")
        resolver = NumericResolver(EvidenceService(session), subject, cutoff)
        try:
            if ref.kind == "evidence":
                records.append(await resolver.fact(ref, require_fresh=False))
            elif peer_payload is None:
                await resolver.calculation(ref, require_fresh=False)
        except InvestorError as exc:
            raise ResearchValidationError("research_reference_validation_failed", ref_fields[ref], exc.detail.message) from exc
    if progress.kind == "comparison" and progress.answered:
        keys = {(r.period.model_dump_json() if r.period else None, r.source_ref.field_path, r.value.unit if r.value else None) for r in records}
        malformed = any(r.period is None or r.source_ref.field_path is None or r.value is None for r in records)
        # WP12 narrow branch: one authorized target whose comparison cites the
        # bound peer-context calculations validates by the fixed-sample
        # statistics (verified above); the model need not gather peer raw
        # evidence. Every other multi-company comparison keeps the original
        # two-subject same-period rule, and mixing arbitrary peer facts in is
        # still refused by the scope check above.
        peer_backed = peer_resolver is not None and len(subjects) == 1 and bool(peer_calc_refs)
        if peer_backed:
            if records and (len(keys) != 1 or malformed):
                raise ExecutionDeniedError("comparison_period_or_basis_differs")
        elif len(keys) != 1 or len({r.subject_ref for r in records}) < 2 or malformed:
            raise ExecutionDeniedError("comparison_period_or_basis_differs")
    config = method_config if method_config is not None else (await task_method(session, task_id)).config
    await enforce_steps(session, config, progress, cutoff, final=final, formal_directional=formal_directional)
    text_fragments = [("progress.question", progress.question)]
    for name in ("inferences", "assumptions", "gaps", "completed_steps", "limitations"):
        text_fragments.extend((f"progress.{name}.{index}", text) for index, text in enumerate(getattr(progress, name)))
    if progress.stopped_because:
        text_fragments.append(("progress.stopped_because", progress.stopped_because))
    text_fragments.extend((f"progress.content.{index}.text", node.text) for index, node in enumerate(progress.content) if isinstance(node, TextNode))
    for field, text in text_fragments:
        if NUMBER.search(text):
            raise ResearchValidationError("research_numeric_text_requires_reference", field,
                "该文本字段仅写无量化描述；具体数字或证券身份请放入 progress.content 的 numeric_claim/entity_ref 引用节点，保留来源、单位和期间。")
    rendered = []
    node_refs = []
    for index, node in enumerate(progress.content):
        if isinstance(node, TextNode):
            rendered.append(node.text)
        else:
            if grant.research_kind == "portfolio_action" and node.ref.kind == "calculation":
                from investment_agent.agent.portfolio_tools import progress_calculation
                from investment_agent.investor.enums import NumericRole
                from investment_agent.investor.publication_numbers import format_quantity
                from investment_agent.portfolio.plans import PortfolioNumericResolver
                calculation = await progress_calculation(session, grant, node.ref)
                path = next((p for p in calculation.details["result_paths"] if p["field_path"] == node.ref.field_path), None)
                if not isinstance(node, NumericNode) or node.role != NumericRole.OUTPUT or path is None:
                    raise ExecutionDeniedError("portfolio_progress_exact_output_required")
                listing_ref = ObjectRef.model_validate(path["listing_ref"]) if path.get("listing_ref") else None
                if listing_ref is not None and listing_ref not in subjects:
                    raise ExecutionDeniedError("portfolio_progress_listing_outside_scope")
                quantity = PortfolioNumericResolver(calculation, grant.portfolio_account_id).quantity(node.ref, scenario=path["scenario_index"], listing=listing_ref)  # type: ignore[arg-type]
                rendered.append(format_quantity(quantity, node.display_format)[0])
                node_refs.append(node.ref)
                continue
            if isinstance(node, EntityNode) or node.ref.kind == "listing":
                subject = node.ref.model_copy(update={"field_path": None})
            elif node.ref.kind == "calculation" and await peer_method_payload(session, grant.owner, node.ref) is not None:
                # WP12: peer results render through the method entry as output
                # nodes; the entity scope below never sees a peer identity.
                if peer_resolver is None:
                    raise ExecutionDeniedError("peer_calculation_not_bound")
                if not isinstance(node, NumericNode):
                    raise ResearchValidationError("peer_numeric_output_required", f"progress.content.{index}.ref", "同行参照结果以 numeric_claim 输出节点（calculation_output 角色）引用。")
                try:
                    rendered.append((await peer_resolver.numeric_output(node)).text)
                except InvestorError as exc:
                    raise ResearchValidationError("research_content_reference_validation_failed", f"progress.content.{index}.ref", exc.detail.message) from exc
                node_refs.append(node.ref)
                continue
            else:
                model = SavedEvidence if node.ref.kind == "evidence" else SavedCalculation
                row = await session.get(model, (str(node.ref.id), node.ref.revision))
                if row is None or row.owner_id != grant.owner:
                    raise ResearchValidationError("research_reference_unavailable", f"progress.content.{index}.ref", "引用必须来自当前已保存、可定位的 evidence/calculation；身份引用不是证据。")
                if node.ref.kind == "evidence":
                    subject = ObjectRef.model_validate(row.payload["subject_ref"])
                else:
                    fact_rows = [await session.get(SavedEvidence, (str(f["id"]), f["revision"])) for f in row.payload.get("fact_refs", [])]
                    identities = {ObjectRef.model_validate(f.payload["subject_ref"]) for f in fact_rows if f is not None}
                    if len(identities) != 1:
                        raise ExecutionDeniedError("calculation_subject_unavailable")
                    subject = next(iter(identities))
            if subject not in subjects:
                raise ResearchValidationError("research_reference_scope_mismatch", f"progress.content.{index}.ref", "证据主体必须与本任务明确授权的证券一致。")
            if (isinstance(node, NumericNode) and node.ref.kind == "evidence"
                    and node.role.value == "historical_fact" and node.ref.field_path is None):
                raise ResearchValidationError("research_numeric_reference_format", f"progress.content.{index}.ref.field_path",
                    'historical_fact 的 numeric_claim 必须使用 ref.field_path="value"；保留已保存证据的原 UUID 与 revision，不要改写数值。')
            if isinstance(node, NumericNode) and node.ref.kind == "calculation" and node.role.value == "calculation_output":
                allowed_paths = ["results." + key for key in row.payload.get("results", {})]
                if node.ref.field_path not in allowed_paths:
                    raise ResearchValidationError("research_numeric_reference_format", f"progress.content.{index}.ref.field_path",
                        "calculation_output 必须使用已保存结果的精确路径：" + ", ".join(allowed_paths) + "；保留原计算 UUID 与 revision，不要改写结果。")
            try:
                rendered.append((await NumericResolver(EvidenceService(session), subject, cutoff).resolve(node)).text)
            except InvestorError as exc:
                raise ResearchValidationError("research_content_reference_validation_failed", f"progress.content.{index}.ref", exc.detail.message) from exc
            node_refs.append(node.ref)
    if not rendered:
        rendered = ["研究进展已保存。"]
    for title, values in (("分析推断", progress.inferences), ("假设", progress.assumptions), ("材料缺口", progress.gaps), ("已完成", progress.completed_steps), ("局限", progress.limitations)):
        if values:
            rendered.append(title + "：" + "；".join(values))
    if progress.stopped_because:
        rendered.append("停止原因：" + progress.stopped_because)
    # Expose candidate verification explicitly; a saved citation is not verified.
    qualities = sorted({r.verification for r in records if r.verification != "verified"})
    if qualities:
        rendered.append("来源质量：" + ", ".join(qualities))
    return "\n".join(rendered), tuple(dict.fromkeys((*refs, *node_refs)))


async def progress_scope(session: AsyncSession, business: Any) -> tuple[ObjectRef, ...]:
    del session, business
    return ()


async def save_progress(session: AsyncSession, operation: UUID, business: Any) -> ActionResult:
    assert isinstance(business, ResearchProgress)
    action = await session.get(ToolAction, str(operation))
    assert action is not None
    text, refs = await validate_progress(session, UUID(action.task_id), business)
    ref = ObjectRef(kind="research_progress", id=operation, revision=1)
    return ActionResult({"progress": business.model_dump(mode="json"), "text": text,
        "refs": [r.model_dump(mode="json") for r in refs], "artifact_ref": ref.model_dump(mode="json")}, artifact_refs=(ref,))


async def capture(session: AsyncSession, operation: UUID, business: Any) -> ActionResult:
    assert isinstance(business, SnapshotParameters)
    result = await SnapshotQueryService(session).capture_snapshot("local_owner", business.ticker, business.field, business.as_of, operation, report_period=business.report_period)
    payload = result.model_dump(mode="json")
    # Read the exact saved revision, not a second upstream/current snapshot query.
    # Keep every source, missing field and conflict; capture does not verify anything.
    payload["materials"] = []
    for ref in result.refs:
        row = await session.get(SavedEvidence, (str(ref.id), ref.revision))
        assert row is not None
        subject = ObjectRef.model_validate(row.payload["subject_ref"])
        material: dict[str, Any] = {"ref": ref.model_dump(mode="json"), "record": row.payload,
            "eligibility": {"fact_reference_allowed": False}}
        try:
            record = await NumericResolver(EvidenceService(session), subject, business.as_of).fact(ref)
            material["eligibility"]["fact_reference_allowed"] = True
            if record.value is not None and record.value.value is not None:
                display = "percent" if record.value.unit in {"fraction", "percent"} else "decimal"
                node = NumericNode(type="numeric_claim", ref=ref.model_copy(update={"field_path": "value"}),
                    role="historical_fact", display_format=display)
                await NumericResolver(EvidenceService(session), subject, business.as_of).resolve(node)
                material["numeric_claim"] = node.model_dump(mode="json")
        except InvestorError as exc:
            material["eligibility"]["restriction"] = exc.detail.message
        payload["materials"].append(material)
    return ActionResult(payload, artifact_refs=result.refs)


def registrations() -> tuple[ToolRegistration, ...]:
    from investment_agent.agent.disclosure_tools import registrations as disclosure_registrations
    from investment_agent.agent.document_tools import registrations as document_registrations
    from investment_agent.agent.peer_tools import registrations as peer_registrations
    from investment_agent.agent.portfolio_tools import registrations as portfolio_registrations
    return (*builtin_registrations(), *valuation_registrations(), registration(), *portfolio_registrations(), *disclosure_registrations(), *document_registrations(), *peer_registrations(),
        ToolRegistration("capture_snapshot", SnapshotParameters, listing_scope, database=capture, description="Read and save one stored field directly (no get_snapshot prerequisite): close, roe, operating_cash_flow, capital_expenditure, net_profit_parent, net_profit_total, revenue_main, revenue_total. Returns exact saved evidence refs AND records with source/period/unit/cutoff, missing values and conflicts. Never upgrades candidate verification."),
        ToolRegistration("save_research_progress", ResearchProgress, progress_scope, database=save_progress))


class ResearchPolicy:
    def __init__(self, base: Settings) -> None:
        self.base = base

    def specification(self) -> RunSpecification:
        return RunSpecification(output_type=ResearchOutput, instructions=METHOD_INSTRUCTIONS,
            phase="research:v1", completion=self.complete, context_builder=assemble, output_check=self.validate_output)

    async def validate_output(self, session: AsyncSession, claim: TaskClaim, output: Any) -> None:
        """Read-only native feedback; completion still rechecks before any write."""
        assert isinstance(output, ResearchOutput)
        await validate_progress(session, claim.task_id, output.progress, final=True)
        progress = output.progress
        if not set(output.critical_counter_evidence) <= set(progress.counter_refs):
            raise ResearchValidationError("research_counter_references_mismatch", "critical_counter_evidence",
                "关键反证必须逐项出现在已读回并核查的 progress.counter_refs；潜在风险或缺失材料应写入缺口，不得冒充已核查反证。请修正引用清单后重新提交。")
        if output.semantic_review is not None and output.semantic_review.conclusion == "pass":
            candidate, semantic = await reviewed_candidate(session, claim.task_id, output.semantic_review)
            if any(c.materiality == "critical" and c.verification != "verified" for c in candidate.thesis.claims):
                raise ResearchValidationError("research_publication_pending_premises", "semantic_review.preparation_ref",
                    "准备的观点仍依赖待核验的关键来源，不能通过发布审查。请重新调用 prepare_company_view，依据实际来源修订观点，再审查新的 preparation_ref。无法证实方向时应明确暂不判断，说明已读材料、风险和缺口；不得只改审查结论或把支撑方向的关键前提降为背景。")
            method = await task_method(session, claim.task_id)
            publication = PublicationService(session, policy=PublicationPolicy(methods=(method.method_ref,)))
            check = await publication.preflight(claim.task_id, candidate, semantic)
            if check.failures:
                paths = ", ".join(check.field_errors)
                current = check.field_errors.get("dependency_refs.current", "")
                dependency_hint = ("关键财务依赖已被更新报告期替代；需要当前经营判断时重新取证。若本次只陈述历史事实且不据此判断方向，应同时据实修订主张与依赖的重要性。" if "critical_financial_field_replaced" in current else "")
                raise ResearchValidationError("research_publication_requires_revision", "semantic_review",
                    "发布预检未通过，具体字段：" + paths + "。历史 fact 只能引用 evidence；含计算结果的主张应按实际推理使用 inference，所有内容节点必须声明对应依据。审查需具体说明事实关系、反证相关性和影响；未解决问题不能同时标为 pass。失效的关键依赖需重新取证并修订观点，不能保留原方向而仅降低重要性。" + dependency_hint + "候选观点变更须重新 prepare_company_view 并审查新的 preparation_ref；确实未能解决时提交 revise，保留草稿及具体缺口。")
        has_progress = await session.scalar(select(ToolAction.operation_id).where(ToolAction.task_id == str(claim.task_id), ToolAction.state == "committed").limit(1))
        if progress.kind in {"company_research", "review"} and not progress.stopped_because and not (progress.fact_refs or progress.counter_refs or any(d.refs for d in progress.dimensions) or has_progress):
            raise ResearchValidationError("research_plan_is_not_final_answer", "progress.stopped_because",
                "尚未回答且未说明停止原因。研究计划请通过 save_research_progress 工具保存，再调用 get_snapshot/capture_snapshot 取证；确实无法继续时，明确停止原因和实际缺口。不要把初始计划作为最终答案。")

    async def complete(self, session: AsyncSession, claim: TaskClaim, outcome: RunOutcome) -> TaskDisposition:
        from investment_agent.agent.conversations import ConversationService
        output = outcome.output
        assert isinstance(output, ResearchOutput)
        grant, _ = await check_boundary(session, claim, self.base)
        service = TaskService(session)
        task = await service.get_task(claim.task_id)
        text, _ = await validate_progress(session, claim.task_id, output.progress, final=True)
        parameters: dict[str, Any] = {"tool": "save_research_progress", "business": output.progress.model_dump(mode="json"), "phase": "research:v1"}
        action = await service.prepare_action(claim, intent_key="completion:progress", parameters=parameters, effect="database")
        await service.settle_action(claim, UUID(action.operation_id), lambda s, op: save_progress(s, op, output.progress))
        refs = list((await service.get_task(claim.task_id)).artifact_refs)
        method = await task_method(session, claim.task_id)
        publication = PublicationService(session, policy=PublicationPolicy(methods=(method.method_ref,)))
        # A local answer never publishes. Critical counter material restricts
        # the affected current view before any further review or response.
        if output.critical_counter_evidence:
            checked = output.progress.counter_refs
            if not set(output.critical_counter_evidence) <= set(checked):
                raise ExecutionDeniedError("counter_evidence_not_checked")
            from investment_agent.storage.models.investor import InvestorCase
            ids = [int(r.id) for r in grant.scope_refs if r.kind == "listing"]
            cases = (await session.scalars(select(InvestorCase).where(InvestorCase.owner_id == grant.owner, InvestorCase.listing_id.in_(ids)))).all()
            for affected_case in cases:
                if affected_case.payload.get("current_recommendation_ref"):
                    current = ObjectRef.model_validate(affected_case.payload["current_recommendation_ref"])
                    view = await publication.get_recommendation(current)
                    subjects = set()
                    for ref in output.critical_counter_evidence:
                        row = await session.get(SavedEvidence, (str(ref.id), ref.revision))
                        if row:
                            subjects.add(ObjectRef.model_validate(row.payload["subject_ref"]))
                    if any(r.id == affected_case.listing_id for r in subjects):
                        await publication.restrict(current, reason="critical_counter_evidence:formal_review_required")
                        await service.append_event(claim.task_id, claim.attempt_id, "progress", {"summary": "Critical counter evidence restricted the current opinion; formal review required"})
                        text += "\n关键反证已使原观点受限，需要正式复核。"
        published = False
        if output.portfolio_review is not None:
            if output.semantic_review is not None or output.review is not None or grant.research_kind != "portfolio_action":
                raise ExecutionDeniedError("portfolio_completion_outside_admission")
            from investment_agent.agent.portfolio_tools import reviewed
            from investment_agent.portfolio.plans import PortfolioPlanService
            portfolio_candidate, portfolio_semantic = await reviewed(session, claim.task_id, output.portfolio_review)
            parameters = {"candidate": portfolio_candidate.model_dump(mode="json"), "semantic": portfolio_semantic.model_dump(mode="json")}
            action = await service.prepare_action(claim, intent_key="completion:portfolio-publish", parameters=parameters, effect="database")
            portfolio_result = await PortfolioPlanService(session).publish(claim, UUID(action.operation_id), portfolio_candidate, portfolio_semantic, base=self.base)
            refs.extend(r for r in (portfolio_result.plan_ref, portfolio_result.draft_ref, portfolio_result.check_ref) if r)
            published = portfolio_result.published
            text += "\n" + ("组合互斥方案已发布；采纳不预留资金或提交订单。" if published else "组合发布检查未通过；具体限制已保存。")
        if output.semantic_review is not None:
            if grant.research_kind not in {"company_research", "review"}:
                raise ExecutionDeniedError("publication_requires_full_research_and_semantic_review")
            candidate, semantic = await reviewed_candidate(session, claim.task_id, output.semantic_review)
            if candidate.thesis.method_ref != method.method_ref or candidate.recommendation.method_revision_id != method.method_ref:
                raise ExecutionDeniedError("task_bound_research_method_required")
            cutoff = await effective_cutoff(session, claim.task_id)
            if candidate.recommendation.as_of > cutoff:
                raise ExecutionDeniedError("publication_cutoff_outside_observation")
            parameters = {"candidate": candidate.model_dump(mode="json"), "semantic": semantic.model_dump(mode="json")}
            action = await service.prepare_action(claim, intent_key="completion:publish", parameters=parameters, effect="database")
            result = await publication.publish(claim, UUID(action.operation_id), candidate, semantic, base=self.base)
            refs.extend(r for r in (result.draft_ref, result.thesis_ref, result.recommendation_ref, result.check_ref) if r)
            published = result.published
            text += "\n" + ("正式公司观点已发布。" if published else "发布审查未通过，已保存草稿与具体检查结果。")
        elif output.review is not None:
            if grant.research_kind != "review":
                raise ExecutionDeniedError("review_outside_admission")
            review = output.review
            if review.completed_at > datetime.now(UTC):
                raise ExecutionDeniedError("review_cutoff_outside_observation")
            saved = await publication.artifact(review.recommendation_ref)
            case = await publication.foundation.get_case("local_owner", UUID(saved.case_id))
            await check_boundary(session, claim, self.base, required_refs=(case.listing_ref,))
            view = await publication.get_recommendation(review.recommendation_ref)
            required = {d.ref for d in view.recommendation.dependency_refs if d.materiality == "critical"}
            if review.result == "maintain" and (not required <= set(review.evidence_diff_refs) or review.reasoning_revision):
                raise ExecutionDeniedError("maintain_review_requires_exact_critical_dependencies")
            resolver = NumericResolver(EvidenceService(session), case.listing_ref, await effective_cutoff(session, claim.task_id))
            verified_facts = {source.model_copy(update={"field_path": None}) for item in view.recommendation.claims
                if item.kind == "fact" and item.materiality == "critical"
                for source in (*item.support_refs, *item.opposition_refs)}
            for ref in review.evidence_diff_refs:
                if ref.kind == "evidence":
                    await resolver.fact(ref, require_verified=ref.model_copy(update={"field_path": None}) in verified_facts, require_fresh=False)
                elif ref.kind == "calculation":
                    await resolver.calculation(ref, require_fresh=False)
                else:
                    raise ExecutionDeniedError("review_evidence_reference_required")
            if review.result == "maintain" and output.extend:
                parameters = {"recommendation_ref": review.recommendation_ref.model_dump(mode="json"), "review": review.model_dump(mode="json"), "expected_case_revision": case.revision}
                action = await service.prepare_action(claim, intent_key="completion:extend", parameters=parameters, effect="database")
                try:
                    await publication.extend(claim, UUID(action.operation_id), review.recommendation_ref, review, expected_case_revision=case.revision, base=self.base)
                    published = True
                except InvestorError:
                    text += "\n原观点无法续期，需要发布新观点；复核进展已保留。"
            else:
                if review.result == "maintain":
                    from investment_agent.investor.monitoring import verify_unchanged_review
                    await verify_unchanged_review(session, claim.task_id, review, publication)
                # Save the real review record without cloning the original thesis.
                now = datetime.now(UTC)
                trusted_review = review.model_copy(update={"id": uuid5(claim.task_id, "review"), "revision": 1, "created_at": now, "actor": "research_service", "origin_ref": task.message_ref})
                ref = publication._save(case.id, "review", trusted_review)
                refs.append(ref)
                if review.result in {"withdraw", "revise", "inconclusive"}:
                    await publication.restrict(review.recommendation_ref, reason="formal_review:" + review.result, withdrawn=review.result == "withdraw")
                    published = review.result == "withdraw"
                else:
                    view = await publication.get_recommendation(review.recommendation_ref)
                    published = view.status == RecommendationStatus.PUBLISHED
        if output.proposal is not None:
            proposal = await UserControlService(session).prepare(PrepareRequest(parameters=output.proposal,
                idempotency_key=uuid5(claim.task_id, "proposal"), expires_at=datetime.now(UTC)+timedelta(hours=1)), claim=claim, base=self.base)
            refs.append(ObjectRef(kind="proposal", id=proposal.id, revision=proposal.revision))
            text += "\n操作建议已保存，等待用户决定。"
        refs = list(dict.fromkeys(refs))
        if task.message_ref:
            await ConversationService(session, self.base).save_assistant(claim, text=text, content=output.progress.content, artifact_refs=tuple(refs))
        # Completion requires a deliverable, not merely a framework success.
        full = grant.research_kind in {"company_research", "review", "portfolio_action"}
        # A published bounded company view can answer the question while
        # explaining why further evidence gathering has reached its limit.
        # That explanation does not negate a successfully checked deliverable.
        company_delivered = grant.research_kind in {"company_research", "review"} and output.semantic_review is not None and published
        complete = output.progress.answered and (published if full else bool(output.progress.content)) and (company_delivered or not output.progress.stopped_because)
        current_task = await service.get_task(claim.task_id)
        await service.checkpoint(claim, checkpoint=(await service.get_checkpoint(claim.task_id))[0].get("data", {}), artifact_refs=tuple(dict.fromkeys((*current_task.artifact_refs, *refs))))
        return TaskDisposition("succeeded" if complete else "partial", StopReason.ANSWERED if complete else StopReason.EVIDENCE_BLOCKED)


async def research_artifacts(session: AsyncSession, task_id: UUID) -> tuple[dict[str, Any], ...]:
    task = await TaskService(session).get_task(task_id)
    refs = {str(r.id) for r in task.artifact_refs if r.kind == "research_progress"}
    rows = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id), ToolAction.operation_id.in_(refs), ToolAction.state == "committed"))).all()
    by_id = {row.operation_id: row.result for row in rows if row.result is not None}
    return tuple(by_id[str(ref.id)] for ref in task.artifact_refs if ref.kind == "research_progress" and str(ref.id) in by_id)


async def check_progress_round(session: AsyncSession, claim: TaskClaim) -> None:
    """Persist a bounded no-progress guard at each native model boundary."""
    from investment_agent.agent.execution import NoProgressError
    service = TaskService(session)
    row = await service.fence(claim)
    task = await service.get_task(claim.task_id)
    actions = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(claim.task_id), ToolAction.state == "committed"))).all()
    material: set[str] = set()
    discoveries: set[str] = set()
    resolved = []
    for action in actions:
        result = action.result or {}
        if action.parameters.get("tool") == "search_saved_sources":
            # Discovery is a real step before readback, not verified support.
            # Distinct queries/timestamps/copy refs cannot manufacture progress
            # when they point to the same immutable original fragment.
            for hit in result.get("matches", []):
                if hit.get("effective_status") == "ok" and hit.get("fragment_hash") and hit.get("evidence_ref"):
                    discoveries.add(fingerprint({"subject": hit.get("subject_ref"),
                        "original": hit.get("original_hash") or hit.get("source_ref"),
                        "fragment": hit["fragment_hash"]}))
        if result.get("status") == "ok" and action.parameters.get("tool") not in {"save_research_progress"}:
            def collect(value: Any, tool_name: str = action.parameters.get("tool", "")) -> None:
                if isinstance(value, dict):
                    if value.get("content_hash") and value.get("source_ref"):
                        material.add(fingerprint({"source": value["source_ref"], "hash": value["content_hash"]}))
                    if value.get("calculation_ref"):
                        material.add(fingerprint(value["calculation_ref"]))
                    if value.get("snapshot_ref") and tool_name == "get_portfolio_context":
                        material.add(fingerprint(value["snapshot_ref"]))
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            if action.parameters.get("tool") in {"get_snapshot", "get_evidence", "get_event", "compare_stocks", "capture_snapshot", "historical_valuation_percentile", "reverse_dcf_fcfe", "get_portfolio_context", "calculate_portfolio_options", "get_industry_peer_context"}:
                collect(result)
        if action.parameters.get("tool") == "save_research_progress":
            resolved.extend(result.get("progress", {}).get("completed_steps", []))
    data = dict(row.checkpoint.get("data", {}))
    dependencies = sorted(data.get("deferred_results", {}))
    signature = fingerprint({"dependencies": dependencies, "material": sorted(material), "discoveries": sorted(discoveries), "resolved": sorted(set(resolved)), "refs": [r.model_dump(mode="json") for r in task.artifact_refs if r.kind in {"evidence", "calculation", "publication_preparation"}]})
    last = data.get("progress_guard")
    stagnant = last["stagnant"] + 1 if last and last["signature"] == signature else 0
    if stagnant >= 2:
        raise NoProgressError("no_progress_two_model_rounds")
    data["progress_guard"] = {"signature": signature, "stagnant": stagnant}
    # Persist through the CAS checkpoint channel, never a direct row write.
    await service.patch_checkpoint(claim, data=data)
