"""Trusted atomic publication. Caller commits; no model/network work under locks.

Rule checks are deterministic. A saved structured semantic review records the
argument's relevance and open problems; it is not a proof of investment quality.
"""
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import check_boundary
from investment_agent.agent.tasks import ActionResult, TaskClaim, TaskService, fingerprint
from investment_agent.config import Settings
from investment_agent.evidence.contracts import EvidenceRecord, ToolResult
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, Record, utc_timestamp
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Case,
    Claim,
    CompanyRecommendation,
    MonitorSpec,
    NumericNode,
    PublicationCheck,
    RecommendationEvent,
    RecommendationView,
    ReviewEvent,
    TextNode,
    ThesisRevision,
)
from investment_agent.investor.enums import RecommendationStatus
from investment_agent.investor.foundations import InvestorError, InvestorService, storage_conflicts
from investment_agent.investor.publication_numbers import (
    NUMBER,
    NumericResolver,
    ResolvedNode,
    resolve_assumption,
)
from investment_agent.industry_peers.contracts import PeerContextBinding
from investment_agent.industry_peers.resolver import (
    PeerContextResolver,
    peer_method_payload,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.publication import (
    PublicationArtifact,
    PublicationDependency,
    PublicationEvent,
    PublicationOutbox,
)
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository

FIXED_METHOD = ObjectRef(kind="method", id=uuid5(NAMESPACE_URL, "investment-agent/company-research.v1"), revision=1)


class SemanticAssessment(Contract):
    claim_id: UUID
    fact_relation: str
    counter_evidence_relevance: str
    materiality_reason: str
    issues: tuple[str, ...] = ()


class SemanticReview(Record):
    candidate_hash: str
    material_set_hash: str
    method_ref: ObjectRef
    assessments: tuple[SemanticAssessment, ...]
    checked_sources: tuple[ObjectRef, ...]
    unresolved_questions: tuple[str, ...]
    conclusion: Literal["pass", "revise"]
    reviewer: str
    # This is a research-step record, not AG20's human evaluation signature.


class PublicationCandidate(Contract):
    thesis: ThesisRevision
    recommendation: CompanyRecommendation
    monitors: tuple[MonitorSpec, ...] = ()
    research_kind: Literal["company_research", "local_check"] = "company_research"


class PublicationResult(Contract):
    published: bool
    draft_ref: ObjectRef | None
    thesis_ref: ObjectRef | None
    recommendation_ref: ObjectRef | None
    check: PublicationCheck
    check_ref: ObjectRef
    resolved_nodes: dict[str, ResolvedNode]


def candidate_hash(candidate: PublicationCandidate) -> str:
    return fingerprint(candidate.model_dump(mode="json"))


class PublicationPolicy:
    """Server-owned fixed method/version seam, not arbitrary model UUIDs."""
    def __init__(self, *, methods: tuple[ObjectRef, ...] = (FIXED_METHOD,), review_days: int = 7, validity_days: int = 14) -> None:
        if not 0 < review_days < validity_days:
            raise ValueError("review must precede validity")
        self.methods, self.review_days, self.validity_days = methods, review_days, validity_days

    def dates(self, as_of: datetime) -> tuple[datetime, datetime]:
        return as_of + timedelta(days=self.review_days), as_of + timedelta(days=self.validity_days)


class PublicationService:
    def __init__(self, session: AsyncSession, *, policy: PublicationPolicy | None = None) -> None:
        self.session, self.foundation = session, InvestorService(session)
        self.policy = policy or PublicationPolicy()

    async def _reserve(self, case_id: UUID) -> None:
        # A physical BEGIN before nested SAVEPOINT is essential for outer rollback.
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
        result = await self.session.execute(update(InvestorCase).where(
            InvestorCase.id == str(case_id), InvestorCase.owner_id == "local_owner"
        ).values(revision=InvestorCase.revision))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise InvestorError("not_found", "Case unavailable")

    async def artifact(self, ref: ObjectRef, *, owner: str = "local_owner") -> PublicationArtifact:
        row = await self.session.get(PublicationArtifact, (str(ref.id), ref.revision), populate_existing=True)
        if row is None or row.kind != ref.kind or row.owner_id != owner or ref.field_path is not None:
            raise InvestorError("not_found", "Publication reference unavailable")
        return row

    async def get_thesis(self, ref: ObjectRef) -> ThesisRevision:
        return ThesisRevision.model_validate((await self.artifact(ref)).payload)

    def _save(self, case_id: UUID, kind: str, value: Record | Contract, *, identifier: UUID | None = None) -> ObjectRef:
        identifier = identifier or getattr(value, "id", None) or uuid4()
        revision = getattr(value, "revision", 1)
        self.session.add(PublicationArtifact(id=str(identifier), revision=revision, kind=kind,
            owner_id="local_owner", case_id=str(case_id), payload=value.model_dump(mode="json")))
        # Checks/drafts are queried internally; reference schema deliberately stays stable.
        return ObjectRef(kind=kind, id=identifier, revision=revision)  # type: ignore[arg-type]

    async def _reference(self, resolver: NumericResolver, ref: ObjectRef, candidate: PublicationCandidate, *, peer: PeerContextResolver | None = None) -> None:
        if ref.kind == "evidence":
            await resolver.fact(ref)
        elif ref.kind == "calculation":
            if peer is not None and await peer_method_payload(self.session, "local_owner", ref) is not None:
                # WP12: the method entry validates its own records; the
                # generic single-company calculation path keeps its rules.
                await peer.verify(ref)
            else:
                await resolver.calculation(ref)
        elif ref.kind == "listing":
            if ref != resolver.subject:
                raise InvestorError("invalid_reference", "Listing identity differs")
        elif ref.kind == "method":
            if ref not in self.policy.methods:
                raise InvestorError("invalid_reference", "Unknown method version")
        elif ref.kind == "mandate":
            saved = await self.foundation.get_mandate("local_owner", ref.revision)
            if saved.id != ref.id or ref.field_path is not None:
                raise InvestorError("invalid_reference", "Mandate identity differs")
        elif ref.kind == "thesis":
            thesis = candidate.thesis
            paths = {None, *(f"assumptions.{h.hypothesis_id}" for h in thesis.assumptions)}
            if ref.id != thesis.id or ref.revision != thesis.revision or ref.field_path not in paths:
                raise InvestorError("invalid_reference", "Hypothesis/thesis reference is absent from candidate")
        elif ref.kind == "monitor":
            if not any(m.id == ref.id and m.revision == ref.revision for m in candidate.monitors):
                raise InvestorError("invalid_reference", "Monitor is absent from atomic publication")
        else:
            raise InvestorError("invalid_reference", "Reference kind is not usable research evidence")

    async def check(self, candidate: PublicationCandidate, semantic: SemanticReview, *, peer_context: PeerContextBinding | None = None) -> tuple[PublicationCheck, dict[str, ResolvedNode]]:
        thesis, recommendation = candidate.thesis, candidate.recommendation
        errors: dict[str, str] = {}
        resolved: dict[str, ResolvedNode] = {}
        case = await self.foundation.get_case("local_owner", thesis.case_ref.id)  # type: ignore[arg-type]
        subject = case.listing_ref
        resolver = NumericResolver(EvidenceService(self.session), subject, recommendation.as_of)
        peer = PeerContextResolver(self.session, binding=peer_context) if peer_context is not None else None
        now = datetime.now(UTC)
        if candidate.research_kind != "company_research":
            errors["research_kind"] = "Local checks do not replace the company's current view"
        if thesis.case_ref.kind != "case" or thesis.case_ref.field_path is not None or thesis.case_ref.revision != case.revision:
            errors["thesis.case_ref"] = "Case revision changed"
        if recommendation.case_ids != (case.id,):
            errors["recommendation.case_ids"] = "Company subject differs"
        expected_thesis = ObjectRef(kind="thesis", id=thesis.id, revision=thesis.revision)
        if recommendation.thesis_revision_refs != (expected_thesis,) or thesis.previous_ref != case.current_thesis_ref:
            errors["recommendation.thesis_revision_refs"] = "Candidate or previous thesis differs"
        if thesis.as_of != recommendation.as_of or recommendation.as_of > now or recommendation.valid_until <= now:
            errors["recommendation.as_of"] = "Research cutoff is future, inconsistent or expired"
        if not recommendation.as_of <= recommendation.next_review_at < recommendation.valid_until:
            errors["recommendation.next_review_at"] = "Review must fall between cutoff and expiry"
        if recommendation.valid_until > recommendation.as_of + timedelta(days=self.policy.validity_days):
            errors["recommendation.valid_until"] = "Validity exceeds configured limit"
        names = [d.dimension for d in thesis.assessed_dimensions]
        if len(names) != len(DIMENSIONS) or set(names) != set(DIMENSIONS) or thesis.assessed_dimensions != recommendation.assessed_dimensions:
            errors["assessed_dimensions"] = "Both objects must share the seven dimension states"
        for prefix, obj in (("thesis", thesis), ("recommendation", recommendation)):
            for index, dimension in enumerate(obj.assessed_dimensions):
                if dimension.status.value != "evidenced" and not dimension.reason:
                    errors[f"{prefix}.assessed_dimensions.{index}.reason"] = "Missing or qualitative state needs its specific gap"
                if dimension.status.value == "evidenced" and not dimension.refs:
                    errors[f"{prefix}.assessed_dimensions.{index}.refs"] = "Evidenced state requires saved sources"
        if thesis.method_ref != recommendation.method_revision_id or thesis.method_ref not in self.policy.methods:
            errors["method_revision_id"] = "Method version is unknown or inconsistent"
        mandate = await self.foundation.get_mandate("local_owner")
        if recommendation.mandate_revision_id != ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision):
            errors["mandate_revision_id"] = "Current mandate revision changed"
        directional = recommendation.business_view.value != "undetermined" or recommendation.price_view.value != "undetermined" or any(a.action.value != "wait" for a in recommendation.conditional_actions)
        if recommendation.sufficiency.value == "insufficient" and directional:
            errors["sufficiency"] = "Insufficient evidence only supports an undetermined view"
        if (recommendation.business_view.value == "undetermined" and recommendation.price_view.value == "undetermined") and any(a.action.value in {"buy", "sell"} for a in recommendation.conditional_actions):
            errors["conditional_actions"] = "Undetermined view does not support buy or sell"
        if directional and (not recommendation.counter_case or not thesis.counter_case):
            errors["counter_case"] = "Directional view needs relevant counter evidence"
        if not recommendation.claims:
            errors["claims"] = "A formal limited view still needs known facts or an evidenced gap"
        if directional and not any(c.materiality == "critical" for c in recommendation.claims):
            errors["claims"] = "Directional view requires explicit critical claims"
        if semantic.candidate_hash != candidate_hash(candidate) or semantic.material_set_hash != thesis.evidence_set_hash or semantic.method_ref != thesis.method_ref:
            errors["semantic_review"] = "Review does not bind these exact candidate/material/method versions"
        claim_ids = {c.id for c in (*thesis.claims, *recommendation.claims)}
        all_claims = (*thesis.claims, *recommendation.claims)
        if any(left.id == right.id and left != right for left in all_claims for right in all_claims):
            errors["claims.id"] = "A claim identity cannot denote different premises"
        reviewed = {a.claim_id for a in semantic.assessments}
        if reviewed != claim_ids or len(reviewed) != len(semantic.assessments) or semantic.conclusion != "pass":
            errors["semantic_review.assessments"] = "Review must assess every claim and resolve material problems"
        for index, assessment in enumerate(semantic.assessments):
            if not all((assessment.fact_relation.strip(), assessment.counter_evidence_relevance.strip(), assessment.materiality_reason.strip())) or assessment.issues:
                errors[f"semantic_review.assessments.{index}"] = "Concrete relevance/materiality review has unresolved issues"
        if directional and semantic.unresolved_questions:
            errors["semantic_review.unresolved_questions"] = "Directional reasoning has unresolved semantic questions"
        refs: list[tuple[str, ObjectRef]] = []
        for prefix, obj in (("thesis", thesis), ("recommendation", recommendation)):
            for i, claim in enumerate(obj.claims):
                path = f"{prefix}.claims.{i}"
                self._claim_gate(claim, path, errors, directional)
                for j, ref in enumerate((*claim.support_refs, *claim.opposition_refs, *claim.assumption_refs)):
                    refs.append((f"{path}.refs.{j}", ref))
            for i, d in enumerate(obj.assessed_dimensions):
                refs.extend((f"{prefix}.assessed_dimensions.{i}.refs", r) for r in d.refs)
            for i, h in enumerate(obj.assumptions):
                if h.check_by < obj.as_of or not all((h.scope.strip(), h.support_condition.strip(), h.falsification_condition.strip())):
                    errors[f"{prefix}.assumptions.{i}"] = "Hypothesis needs scope, test conditions and a future check"
                if h.last_check_ref:
                    refs.append((f"{prefix}.assumptions.{i}.last_check_ref", h.last_check_ref))
        refs.extend(("provenance", r) for r in recommendation.provenance)
        refs.extend(("conditions", r) for r in recommendation.conditions)
        refs.extend(("semantic_review.checked_sources", r) for r in semantic.checked_sources)
        for i, action in enumerate(recommendation.conditional_actions):
            if action.action.value != "wait" and (not action.condition_refs or not action.rationale_refs):
                errors[f"conditional_actions.{i}"] = "Directional action requires conditions and rationale"
            refs.extend((f"conditional_actions.{i}", r) for r in (*action.condition_refs, *action.rationale_refs))
        for i, dependency in enumerate(recommendation.dependency_refs):
            if dependency.ref.kind == "mandate" and not set(dependency.fields) <= {"goals", "scope_refs", "hard_constraints", "portfolio_constraints"}:
                errors[f"dependency_refs.{i}.fields"] = "Only relevant mandate fields are publication dependencies"
            if not dependency.fields:
                errors[f"dependency_refs.{i}"] = "Dependency needs relevant fields"
            refs.append((f"dependency_refs.{i}", dependency.ref))
        critical_refs = {r.model_copy(update={"field_path": None}) for c in (*thesis.claims, *recommendation.claims) if c.materiality == "critical" for r in (*c.support_refs, *c.opposition_refs, *c.assumption_refs)}
        declared = {d.ref.model_copy(update={"field_path": None}) for d in recommendation.dependency_refs if d.materiality == "critical"}
        if not critical_refs <= declared:
            errors["dependency_refs"] = "Every critical claim source must be a critical dependency"
        if not critical_refs <= {r.model_copy(update={"field_path": None}) for r in semantic.checked_sources}:
            errors["semantic_review.checked_sources"] = "Semantic review omitted critical sources"
        for ref in critical_refs:
            try:
                if ref.kind == "evidence":
                    await resolver.fact(ref, require_verified=True)
                elif ref.kind == "calculation" and peer is not None and await peer_method_payload(self.session, "local_owner", ref) is not None:
                    # WP12: the metric's whole input checklist runs in the method
                    # entry; critical sources additionally demand verified inputs.
                    await peer.verify(ref, require_verified_inputs=True)
                elif ref.kind == "calculation":
                    calculation = await resolver.calculation(ref, require_fresh=False)
                    for fact in calculation.fact_refs:
                        await resolver.fact(fact, require_verified=True)
            except (InvestorError, LookupError) as exc:
                errors[f"critical_source:{ref.kind}:{ref.id}"] = str(exc)
        for path, ref in refs:
            try:
                await self._reference(resolver, ref, candidate, peer=peer)
            except (InvestorError, LookupError) as exc:
                errors[path] = str(exc)
        monitor_ids = {ObjectRef(kind="monitor", id=m.id, revision=m.revision) for m in candidate.monitors}
        if set(recommendation.monitor_refs) != monitor_ids:
            errors["monitor_refs"] = "Monitor references differ from supplied specifications"
        for i, monitor in enumerate(candidate.monitors):
            if monitor.case_ref != thesis.case_ref or monitor.next_due_at < recommendation.as_of or (monitor.trigger == "field" and not monitor.conditions) or (monitor.trigger == "calendar" and bool(monitor.conditions)):
                errors[f"monitors.{i}"] = "Monitor needs bound case, conditions and valid schedule"
            if monitor.hypothesis_ref:
                try:
                    await self._reference(resolver, monitor.hypothesis_ref, candidate)
                except (InvestorError, LookupError) as exc:
                    errors[f"monitors.{i}.hypothesis_ref"] = str(exc)
            for j, condition in enumerate(monitor.conditions):
                refs_to_check = (condition.field_ref,) + ((condition.threshold_ref,) if condition.threshold_ref else ())
                if condition.operator != "changed" and condition.threshold_ref is None:
                    errors[f"monitors.{i}.conditions.{j}"] = "Comparison threshold missing"
                for ref in refs_to_check:
                    try:
                        await self._reference(resolver, ref, candidate)
                    except (InvestorError, LookupError) as exc:
                        errors[f"monitors.{i}.conditions.{j}"] = str(exc)
        # Scan all visible strings, including rationale/gaps/conditions; IDs, hashes,
        # method versions, references and other structural metadata are excluded.
        for path, text in visible_text(candidate, semantic):
            if NUMBER.search(text):
                errors[path] = "Unreferenced quantity; use a typed numeric or identity node"
        for prefix, obj in (("thesis", thesis), ("recommendation", recommendation)):
            contents = [(f"{prefix}.counter_case", obj.counter_case)]
            contents.extend((f"{prefix}.claims.{i}.text", c.text) for i, c in enumerate(obj.claims))
            for path, nodes in contents:
                for i, node in enumerate(nodes):
                    if not isinstance(node, TextNode):
                        node_claim = next((c for c in obj.claims if c.text is nodes), None)
                        if node_claim is not None and node.ref.model_copy(update={"field_path": None}) not in {r.model_copy(update={"field_path": None}) for r in (*node_claim.support_refs, *node_claim.assumption_refs)}:
                            errors[f"{path}.{i}.ref"] = "Claim node must bind its declared premise"
                        try:
                            if peer is not None and node.ref.kind == "calculation" and await peer_method_payload(self.session, "local_owner", node.ref) is not None:
                                # WP12: peer statistics render as output nodes
                                # through the method entry.
                                assert isinstance(node, NumericNode)
                                resolved[f"{path}.{i}"] = await peer.numeric_output(node)
                            else:
                                resolved[f"{path}.{i}"] = await resolver.resolve(node)
                        except (InvestorError, LookupError) as exc:
                            errors[f"{path}.{i}"] = str(exc)
        return PublicationCheck(rule_version="company-publication.v1", passed=() if errors else ("identity_scope", "references_time", "claims_numbers", "research_semantic_record", "company_scope"), failures=tuple(errors), field_errors=errors, semantic_review_ref=None), resolved

    @staticmethod
    def _claim_gate(claim: Claim, path: str, errors: dict[str, str], directional: bool) -> None:
        if not claim.text or not claim.impact_if_wrong.strip():
            errors[path] = "Claim needs visible text and impact if wrong"
        if claim.kind == "fact" and not claim.support_refs:
            errors[path + ".support_refs"] = "Fact needs saved verifiable evidence"
        if claim.kind == "fact" and any(r.kind != "evidence" for r in claim.support_refs):
            errors[path + ".support_refs"] = "Historical fact requires evidence sources"
        if claim.kind == "assumption" and not claim.assumption_refs:
            errors[path + ".assumption_refs"] = "Assumption needs saved scenario provenance"
        if claim.kind == "inference" and not (*claim.support_refs, *claim.assumption_refs):
            errors[path + ".support_refs"] = "Inference needs explicit saved premises"
        if claim.materiality == "critical" and (claim.verification != "verified" and (directional or claim.kind == "fact")):
            errors[path + ".verification"] = "Critical source conflict or pending verification blocks direction"

    @storage_conflicts
    async def publish(self, claim: TaskClaim, operation_id: UUID, candidate: PublicationCandidate, semantic: SemanticReview, *, base: Settings) -> PublicationResult:
        """Only trusted application calls this with a prepared action and claim.

        The exact parameters must already have been bound by prepare_action.
        Failure is a committed repairable draft/check, not a schema exception.
        """
        await self._reserve(candidate.thesis.case_ref.id)  # type: ignore[arg-type]
        tasks = TaskService(self.session)
        action = await tasks._action(claim.task_id, operation_id)
        parameters = {"candidate": candidate.model_dump(mode="json"), "semantic": semantic.model_dump(mode="json")}
        if action.parameters != parameters:
            raise InvestorError("idempotency_conflict", "Prepared publication parameters differ")
        async def write(session: AsyncSession, operation: UUID) -> ActionResult:
            await check_boundary(session, claim, base, tool="publish_recommendation", required_refs=(candidate.thesis.case_ref,))
            from investment_agent.investor.methods import task_method
            method = await task_method(session, claim.task_id)
            self.policy = PublicationPolicy(methods=(method.method_ref,), review_days=self.policy.review_days, validity_days=self.policy.validity_days)
            from investment_agent.agent.observation import effective_cutoff
            if candidate.recommendation.as_of > await effective_cutoff(session, claim.task_id):
                raise InvestorError("invalid_reference", "Publication exceeds trusted research observation cutoff")
            peer_context = await self._task_peer_context(claim.task_id)
            check, resolved = await self.check(candidate, semantic, peer_context=peer_context)
            from investment_agent.investor.methods import MethodConfig
            if method.config != MethodConfig():
                # Native completion commits this exact final progress before
                # publish. A trusted caller cannot bypass strengthening merely
                # by constructing a candidate with the selected method label.
                from investment_agent.agent.execution import ExecutionDeniedError
                from investment_agent.agent.research import ResearchProgress, validate_progress
                from investment_agent.storage.models.agent import ToolAction
                progress_action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(claim.task_id), ToolAction.intent_key == "completion:progress", ToolAction.state == "committed"))
                try:
                    if progress_action is None or not progress_action.result:
                        raise ValueError("Exact final research progress required")
                    await validate_progress(session, claim.task_id, ResearchProgress.model_validate(progress_action.result["progress"]), final=True, formal_directional=self._directional(candidate.recommendation))
                except (ExecutionDeniedError, InvestorError, ValueError, LookupError) as exc:
                    fields = {**check.field_errors, "method_steps": str(exc)}
                    check = check.model_copy(update={"field_errors": fields, "failures": tuple(fields), "passed": ()})
            current_limits = await self._dependency_limits(candidate.recommendation, datetime.now(UTC))
            if current_limits:
                field_errors = {**check.field_errors, "dependency_refs.current": "; ".join(current_limits)}
                check = check.model_copy(update={"field_errors": field_errors, "failures": tuple(field_errors), "passed": ()})
            case = await self.foundation.get_case("local_owner", candidate.thesis.case_ref.id)  # type: ignore[arg-type]

            now = datetime.now(UTC)
            review = semantic.model_copy(update={"id": uuid4(), "revision": 1, "actor": "research_service", "created_at": now, "origin_ref": ObjectRef(kind="task", id=claim.task_id, revision=1)})
            review_ref = self._save(case.id, "review", review)
            check = check.model_copy(update={"semantic_review_ref": review_ref})
            check_ref = self._save(case.id, "publication_check", check)
            if check.failures:
                draft_id = uuid4()
                self.session.add(PublicationArtifact(id=str(draft_id), revision=1, kind="draft", owner_id="local_owner", case_id=str(case.id), payload={"candidate": candidate.model_dump(mode="json"), "check": check.model_dump(mode="json")}))
                # A draft is a plan-like task artifact, never a current formal thesis.
                result = PublicationResult(published=False, draft_ref=ObjectRef(kind="draft", id=draft_id, revision=1), thesis_ref=None, recommendation_ref=None, check=check, check_ref=check_ref, resolved_nodes=resolved)
                return ActionResult(result.model_dump(mode="json"))
            # Allocate every formal identity before saving and remap all nested
            # object references together, including conditions and hypotheses.
            mappings = {
                ObjectRef(kind="thesis", id=candidate.thesis.id, revision=candidate.thesis.revision): ObjectRef(kind="thesis", id=uuid4(), revision=1),
                ObjectRef(kind="recommendation", id=candidate.recommendation.id, revision=candidate.recommendation.revision): ObjectRef(kind="recommendation", id=uuid4(), revision=1),
            }
            for monitor in candidate.monitors:
                mappings[ObjectRef(kind="monitor", id=monitor.id, revision=monitor.revision)] = ObjectRef(kind="monitor", id=uuid4(), revision=1)
            thesis_ref = mappings[ObjectRef(kind="thesis", id=candidate.thesis.id, revision=candidate.thesis.revision)]
            thesis = ThesisRevision.model_validate(remap_references(candidate.thesis.model_dump(mode="json"), mappings)).model_copy(update={"id": thesis_ref.id, "revision": 1, "actor": "research_service", "created_at": now, "origin_ref": ObjectRef(kind="task", id=claim.task_id, revision=1)})
            self._save(case.id, "thesis", thesis)
            monitor_refs = []
            for monitor in candidate.monitors:
                saved_ref = mappings[ObjectRef(kind="monitor", id=monitor.id, revision=monitor.revision)]
                saved = MonitorSpec.model_validate(remap_references(monitor.model_dump(mode="json"), mappings)).model_copy(update={"id": saved_ref.id, "revision": 1, "actor": "research_service", "created_at": now, "origin_ref": ObjectRef(kind="task", id=claim.task_id, revision=1)})
                monitor_refs.append(self._save(case.id, "monitor", saved))
            recommendation_ref = mappings[ObjectRef(kind="recommendation", id=candidate.recommendation.id, revision=candidate.recommendation.revision)]
            recommendation = CompanyRecommendation.model_validate(remap_references(candidate.recommendation.model_dump(mode="json"), mappings)).model_copy(update={"id": recommendation_ref.id, "revision": 1, "actor": "research_service", "created_at": now, "origin_ref": ObjectRef(kind="task", id=claim.task_id, revision=1), "thesis_revision_refs": (thesis_ref,), "monitor_refs": tuple(monitor_refs)})
            recommendation_ref = self._save(case.id, "recommendation", recommendation)
            for index, dependency in enumerate(recommendation.dependency_refs):
                self.session.add(PublicationDependency(recommendation_id=str(recommendation.id), ordinal=index, payload=dependency.model_dump(mode="json")))
            if case.current_recommendation_ref:
                old_view = await self.get_recommendation(case.current_recommendation_ref, at=now)
                if old_view.status not in {RecommendationStatus.WITHDRAWN, RecommendationStatus.SUPERSEDED}:
                    await self._event(case.current_recommendation_ref, "superseded", now, replacement=recommendation_ref)
            await self._event(recommendation_ref, "published", now)
            new_case = case.model_copy(update={"revision": case.revision + 1, "current_thesis_ref": thesis_ref, "current_recommendation_ref": recommendation_ref})
            row = await self.foundation.repository.case("local_owner", case.id)
            assert row is not None
            if not await self.foundation.repository.advance_case(row, case.revision, new_case.model_dump(mode="json")):
                raise InvestorError("revision_conflict", "Case revision changed")
            self.session.add(PublicationOutbox(id=str(uuid4()), owner_id="local_owner", notification_key=f"{case.id}:published:{recommendation.id}", state="pending", payload={"case_ref": ObjectRef(kind="case", id=case.id, revision=new_case.revision).model_dump(mode="json"), "recommendation_ref": recommendation_ref.model_dump(mode="json"), "muted": case.muted}))
            await check_boundary(session, claim, base, tool="publish_recommendation", required_refs=(candidate.thesis.case_ref,))
            result = PublicationResult(published=True, draft_ref=None, thesis_ref=thesis_ref, recommendation_ref=recommendation_ref, check=check, check_ref=check_ref, resolved_nodes=resolved)
            return ActionResult(result.model_dump(mode="json"), (thesis_ref, recommendation_ref))
        return PublicationResult.model_validate(await tasks.settle_action(claim, operation_id, write))

    async def _event(self, ref: ObjectRef, kind: Literal["published", "review_required", "expired", "superseded", "withdrawn", "extended"], at: datetime, *, replacement: ObjectRef | None = None, review_ref: ObjectRef | None = None, checked: tuple[ObjectRef, ...] = (), until: datetime | None = None, next_review: datetime | None = None) -> None:
        event = RecommendationEvent(id=uuid4(), revision=1, created_at=at, actor="research_service", origin_ref=None, recommendation_ref=ref, type=kind, review_ref=review_ref, checked_at=at, checked_dependency_refs=checked, next_review_at=next_review, effective_valid_until=until, replacement_ref=replacement)
        self.session.add(PublicationEvent(recommendation_id=str(ref.id), owner_id="local_owner", payload=event.model_dump(mode="json")))

    async def get_recommendation(self, ref: ObjectRef, *, at: datetime | None = None) -> RecommendationView:
        # Read-only API callers get a physical short snapshot. A writer caller
        # already holds BEGIN IMMEDIATE and sees its own atomic pending changes.
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN")
        row = await self.artifact(ref)
        recommendation = CompanyRecommendation.model_validate(row.payload)
        at = utc_timestamp(at or datetime.now(UTC))
        if recommendation.created_at > at:
            raise InvestorError("not_found", "Recommendation was not yet published")
        events = (await self.session.scalars(select(PublicationEvent).where(PublicationEvent.recommendation_id == str(ref.id), PublicationEvent.owner_id == row.owner_id).order_by(PublicationEvent.sequence))).all()
        status, until, replacement = RecommendationStatus.PUBLISHED, recommendation.valid_until, None
        next_review = recommendation.next_review_at
        limitations: list[str] = []
        for event_row in events:
            event = RecommendationEvent.model_validate(event_row.payload)
            if event.checked_at > at:
                continue
            if event.type == "extended":
                assert event.effective_valid_until is not None
                until = event.effective_valid_until
                next_review = event.next_review_at or next_review
            elif event.type != "published":
                limitations.append(event.type)
                if status not in {RecommendationStatus.SUPERSEDED, RecommendationStatus.WITHDRAWN}:
                    status = RecommendationStatus(event.type)
                if event.replacement_ref:
                    replacement = event.replacement_ref
        if at >= until:
            limitations.append("validity_expired")
            if status not in {RecommendationStatus.SUPERSEDED, RecommendationStatus.WITHDRAWN}:
                status = RecommendationStatus.EXPIRED
        # Case revisions are point-in-time snapshots; current pointers alone are
        # inappropriate for historical projection.
        from investment_agent.storage.models.investor import CaseRevision
        histories = (await self.session.scalars(select(CaseRevision).where(CaseRevision.case_id == row.case_id).order_by(CaseRevision.revision))).all()
        current_ref = None
        for history in histories:
            case = Case.model_validate(history.payload)
            current = case.current_recommendation_ref
            if current is not None:
                stored = await self.session.get(PublicationArtifact, (str(current.id), current.revision))
                if stored and CompanyRecommendation.model_validate(stored.payload).created_at <= at:
                    current_ref = current
        if current_ref != ref:
            limitations.append("not_current")
        dependency_limits = await self._dependency_limits(recommendation, at)
        dependency_limits.extend(await self._monitor_limits(recommendation, at))
        reasons = (await self.session.scalars(select(PublicationArtifact).where(PublicationArtifact.case_id == row.case_id, PublicationArtifact.kind == "limitation", PublicationArtifact.owner_id == row.owner_id))).all()
        for reason in reasons:
            if ObjectRef.model_validate(reason.payload["recommendation_ref"]) == ref and datetime.fromisoformat(reason.payload["at"]) <= at:
                dependency_limits.append(reason.payload["reason"])
        limitations.extend(dependency_limits)
        if limitations and status == RecommendationStatus.PUBLISHED:
            status = RecommendationStatus.REVIEW_REQUIRED
        return RecommendationView(recommendation=recommendation, status=status, effective_valid_until=until, effective_next_review_at=next_review, limitation_reasons=tuple(dict.fromkeys(limitations)), replacement_ref=replacement)

    async def _task_peer_context(self, task_id: UUID) -> PeerContextBinding | None:
        """The saved trusted peer-context binding of one task's grant, if any."""

        from investment_agent.agent.execution import ExecutionGrant
        from investment_agent.storage.models.agent import ExecutionGrantRow
        row = await self.session.get(ExecutionGrantRow, str(task_id))
        return ExecutionGrant.model_validate(row.payload).peer_context if row is not None else None

    async def saved_peer_binding(self, recommendation: CompanyRecommendation) -> PeerContextBinding | None:
        """The trusted saved binding behind a published recommendation.

        A formal opinion read has no current-session grant: the recheck
        validates against the publishing origin task's saved grant binding
        (design §统一方法入口), never against a model-supplied reference.
        """

        origin = recommendation.origin_ref
        if origin is None or origin.kind != "task":
            return None
        return await self._task_peer_context(origin.id)  # type: ignore[arg-type]

    async def peer_renewal_block(self, recommendation: CompanyRecommendation, ref: ObjectRef) -> InvestorError:
        """The WP13 structured renewal refusal for one critical peer dependency.

        An opinion resting on peer-context statistics is never attested
        "unchanged" and never extended (design §更新与历史可追溯性): peer
        statistics are fixed-sample descriptive computations whose inputs await
        backend re-verification, so only a new study bound to current results
        and a newly published opinion renews one. The error carries structured
        reasons - the dependency plus its current ``current_limits`` reasons
        (known change limits stay visible). Reading the old summary with the
        peer tool does not renew anything. The original opinion keeps its
        established validity dates and its known limitation reasons.
        """

        from investment_agent.industry_peers.resolver import RENEWAL_BLOCK_REASON
        binding = await self.saved_peer_binding(recommendation)
        reasons = [f"{RENEWAL_BLOCK_REASON}:{ref.id}"]
        if binding is None:
            reasons.append(f"{RENEWAL_BLOCK_REASON}:unbound:{ref.id}")
        else:
            try:
                limits = await PeerContextResolver(self.session, binding=binding).current_limits(ref)
                reasons.extend(f"{RENEWAL_BLOCK_REASON}:{reason}" for reason in limits)
            except (InvestorError, LookupError, ValueError) as exc:
                reasons.append(f"{RENEWAL_BLOCK_REASON}:unverifiable:{exc}")
        return InvestorError(
            "invalid_reference",
            "critical peer-context dependencies cannot support an unchanged review or an "
            f"extension ({'; '.join(reasons)}); run new research bound to current results and "
            "publish a new recommendation - the original opinion keeps its validity dates and "
            "known limitations",
        )

    async def _dependency_limits(self, recommendation: CompanyRecommendation, at: datetime) -> list[str]:
        case = await self.foundation.get_case("local_owner", recommendation.case_ids[0])
        resolver = NumericResolver(EvidenceService(self.session), case.listing_ref, at)
        limitations: list[str] = []
        for dependency in recommendation.dependency_refs:
            if dependency.materiality != "critical":
                continue
            ref = dependency.ref
            try:
                if ref.kind == "evidence":
                    evidence = await resolver.fact(ref, require_fresh=False)
                    limitations.extend(await self._evidence_changes(evidence, dependency.fields, at))
                elif ref.kind == "calculation" and await peer_method_payload(self.session, "local_owner", ref) is not None:
                    # WP12: peer dependencies recheck through the method entry
                    # (per real subject, current correction states).
                    limitations.extend(await self._peer_dependency_limits(recommendation, ref))
                elif ref.kind == "calculation":
                    calculation = await resolver.calculation(ref, require_fresh=False)
                    for fact in calculation.fact_refs:
                        evidence = await resolver.fact(fact, require_fresh=False)
                        limitations.extend(await self._evidence_changes(evidence, dependency.fields, at))
                elif ref.kind == "mandate":
                    original = await self.foundation.get_mandate("local_owner", ref.revision)
                    current = await self.foundation.get_mandate("local_owner")
                    while current.created_at > at and current.revision > 1:
                        current = await self.foundation.get_mandate("local_owner", current.revision - 1)
                    for field in dependency.fields:
                        if field not in {"goals", "scope_refs", "hard_constraints", "portfolio_constraints"}:
                            raise InvestorError("invalid_reference", "Unknown relevant mandate field")
                        if getattr(original, field) != getattr(current, field):
                            limitations.append(f"mandate_changed:{field}")
                elif ref.kind == "method":
                    if ref not in self.policy.methods:
                        from investment_agent.investor.methods import MethodService
                        await MethodService(self.session).known(ref)
                elif ref.kind == "monitor":
                    # Monitor specs are saved by this same publication; their
                    # conditions are evaluated by _monitor_limits on reads.
                    pass
                else:
                    raise InvestorError("invalid_reference", "Unsupported critical dependency kind")
            except (InvestorError, LookupError) as exc:
                limitations.append(f"dependency_unavailable:{ref.kind}:{ref.id}:{exc}")
        return limitations

    async def _peer_dependency_limits(self, recommendation: CompanyRecommendation, ref: ObjectRef) -> list[str]:
        """WP12 current_limits: the method-scoped dependency recheck.

        Renewal blocking for critical peer dependencies belongs to WP13; this
        answers the recheck against the saved origin-task binding with the
        current correction states.
        """

        binding = await self.saved_peer_binding(recommendation)
        if binding is None:
            return [f"peer_dependency_unbound:{ref.id}"]
        try:
            limits = await PeerContextResolver(self.session, binding=binding).current_limits(ref)
        except (InvestorError, LookupError, ValueError) as exc:
            return [f"peer_dependency_unavailable:{ref.id}:{exc}"]
        return list(limits)

    async def _evidence_changes(self, evidence: EvidenceRecord, fields: tuple[str, ...], at: datetime) -> list[str]:
        limits = []
        if evidence.document_provenance is not None:
            from investment_agent.evidence.documents import document_limits
            limits.extend(await document_limits(self.session, evidence, at))
        source = evidence.source_ref
        # Newly saved correction/conflict evidence is checked even before AG15
        # has consumed its notification event.
        rows = (await self.session.scalars(select(SavedEvidence).where(SavedEvidence.owner_id == "local_owner"))).all()
        for row in rows:
            other = EvidenceRecord.model_validate(row.payload)
            if max(other.available_from, other.acquired_at, other.published_at, other.created_at) > at or other.subject_ref != evidence.subject_ref:
                continue
            if evidence.id != other.id and any(r.id == evidence.id for r in other.correction_refs):
                limits.append("source_corrected")
            if other.period == evidence.period and other.locator == evidence.locator and other.conflict == "unresolved":
                limits.append("critical_source_conflict")
        from investment_agent.clock import SHANGHAI_TZ
        day = at.astimezone(SHANGHAI_TZ).date()
        if source.kind in {"financial_snapshot", "market_snapshot"}:
            latest = await self._snapshot_candidates(evidence, at)
            if latest.status in {"missing", "stale", "conflict"}:
                limits.append(f"critical_snapshot_{latest.status}")
            elif source.kind == "financial_snapshot" and latest.data:
                from investment_agent.factors.field_sources import (
                    beyond_tolerance,
                    default_field_sources,
                )
                field = await self._canonical_field(evidence)
                specification = default_field_sources().get(field)
                assert specification is not None
                for new in latest.data:
                    if new.value is None or new.value.value is None:
                        continue
                    if new.period != evidence.period:
                        limits.append("critical_financial_field_replaced")
                    elif evidence.value is not None and evidence.value.value is not None and beyond_tolerance(float(new.value.value), float(evidence.value.value), specification.tolerance):
                        limits.append("critical_financial_field_corrected")
            if source.kind == "financial_snapshot":
                overdue = await ReportOverdueRepository(self.session).open_as_of(int(evidence.subject_ref.id), day)
                if overdue:
                    limits.append("report_overdue")
        return limits

    async def _canonical_field(self, evidence: EvidenceRecord) -> str:
        source = evidence.source_ref
        field = source.field_path or ""
        if source.kind == "financial_snapshot":
            from investment_agent.factors.view import PROVIDER_FIELD_RENAMES
            original = await self.session.get(FinancialSnapshot, source.id)
            if original is None or original.version != source.revision:
                raise InvestorError("invalid_reference", "Financial source unavailable")
            return PROVIDER_FIELD_RENAMES.get(original.provider, {}).get(field.removeprefix("fields."), field.removeprefix("fields."))
        if source.kind == "market_snapshot":
            if await self.session.get(MarketSnapshot, source.id) is None:
                raise InvestorError("invalid_reference", "Market source unavailable")
            return field
        raise InvestorError("invalid_reference", "Snapshot source required")

    async def _snapshot_candidates(self, evidence: EvidenceRecord, at: datetime) -> ToolResult[tuple[EvidenceRecord, ...]]:
        from investment_agent.domain.a_share import format_listing_ticker
        from investment_agent.evidence.queries import SnapshotQueryService
        from investment_agent.storage.models.reference import Listing
        listing = await self.session.get(Listing, evidence.subject_ref.id)
        if listing is None:
            raise InvestorError("invalid_reference", "Snapshot listing unavailable")
        report_period = None
        if evidence.source_ref.kind == "market_snapshot":
            from investment_agent.clock import SHANGHAI_TZ
            from investment_agent.evidence.snapshots import materialize
            from investment_agent.storage.repositories.market_snapshot import (
                MarketSnapshotRepository,
            )
            day = at.astimezone(SHANGHAI_TZ).date()
            rows = await MarketSnapshotRepository(self.session).series_by_provider(int(listing.id), day - timedelta(days=400), day, at, adjust="none", include_backfill=False)
            # Choose the latest *public* trading day before AG05 compares sources.
            # A prematurely captured current-day close cannot hide yesterday.
            known_days = [day for day, providers in rows.items() if any(max(materialize(row, "close").available_from, materialize(row, "close").acquired_at) <= at for row in providers.values())]
            if known_days:
                report_period = max(known_days)
        return await SnapshotQueryService(self.session).get_snapshot(
            format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
            await self._canonical_field(evidence), at, report_period=report_period,
            max_age=timedelta(days=14) if evidence.source_ref.kind == "market_snapshot" else None)

    async def _monitor_limits(self, recommendation: CompanyRecommendation, at: datetime) -> list[str]:
        from decimal import Decimal

        from investment_agent.investor.publication_numbers import MONEY
        case = await self.foundation.get_case("local_owner", recommendation.case_ids[0])
        resolver = NumericResolver(EvidenceService(self.session), case.listing_ref, at)
        limits: list[str] = []
        async def value(ref: ObjectRef, *, latest: bool = False) -> tuple[Decimal, str]:
            if ref.kind == "evidence":
                evidence = await resolver.fact(ref, require_fresh=False)
                quantity = evidence.value
                if latest and evidence.source_ref.kind in {"financial_snapshot", "market_snapshot"}:
                    query = await self._snapshot_candidates(evidence, at)
                    if query.status != "ok" or not query.data:
                        raise InvestorError("invalid_reference", "Monitor has no current visible, conflict-free value")
                    same_source = [r for r in query.data if r.source_ref.kind == evidence.source_ref.kind]
                    chosen = same_source[0] if same_source else query.data[0]
                    quantity = chosen.value
            elif ref.kind == "calculation":
                calculation = await resolver.calculation(ref, require_fresh=False)
                path = ref.field_path or ""
                if path.startswith("assumptions."):
                    assumption = resolve_assumption(calculation, path)
                    quantity = assumption.value if assumption and assumption.rationale else None
                elif path.startswith("results."):
                    quantity = calculation.results.get(path[8:])
                else:
                    quantity = None
            else:
                quantity = None
            if quantity is None or quantity.value is None:
                raise InvestorError("invalid_reference", "Monitor value is missing")
            amount, unit = quantity.value, quantity.unit
            if quantity.currency not in {None, "CNY"}:
                raise InvestorError("invalid_reference", "Monitor currency unsupported")
            if unit in MONEY:
                amount, unit = amount * MONEY[unit], "CNY"
            if unit == "percent":
                amount, unit = amount / 100, "fraction"
            return amount, unit
        for ref in recommendation.monitor_refs:
            try:
                monitor = MonitorSpec.model_validate((await self.artifact(ref)).payload)
                if not monitor.active:
                    continue
                for condition in monitor.conditions:
                    current, unit = await value(condition.field_ref, latest=True)
                    if condition.operator == "changed":
                        previous, old_unit = await value(condition.field_ref)
                        triggered = unit != old_unit or current != previous
                    else:
                        if condition.threshold_ref is None:
                            raise InvestorError("invalid_reference", "Monitor threshold absent")
                        threshold, threshold_unit = await value(condition.threshold_ref)
                        if threshold_unit != unit:
                            raise InvestorError("invalid_reference", "Monitor comparison units differ")
                        triggered = {"lt": current < threshold, "le": current <= threshold, "eq": current == threshold, "ge": current >= threshold, "gt": current > threshold}[condition.operator]
                    if triggered:
                        limits.append(f"monitor_triggered:{ref.id}")
            except (InvestorError, LookupError) as exc:
                limits.append(f"monitor_unavailable:{ref.id}:{exc}")
        return limits

    @staticmethod
    def _directional(recommendation: CompanyRecommendation) -> bool:
        return recommendation.business_view.value != "undetermined" or recommendation.price_view.value != "undetermined" or any(action.action.value != "wait" for action in recommendation.conditional_actions)

    async def _check_method_renewal(self, task_id: UUID, recommendation: CompanyRecommendation) -> None:
        from investment_agent.investor.methods import MethodConfig, task_method
        binding = await task_method(self.session, task_id)
        if binding.config == MethodConfig():
            return
        from investment_agent.agent.research import ResearchProgress, validate_progress
        from investment_agent.storage.models.agent import ToolAction
        action = await self.session.scalar(select(ToolAction).where(ToolAction.task_id == str(task_id), ToolAction.intent_key == "completion:progress", ToolAction.state == "committed"))
        if action is None or not action.result:
            raise InvestorError("invalid_reference", "Exact final research progress required for method renewal")
        await validate_progress(self.session, task_id, ResearchProgress.model_validate(action.result["progress"]), final=True, formal_directional=self._directional(recommendation))

    @storage_conflicts
    async def extend(self, claim: TaskClaim, operation_id: UUID, ref: ObjectRef, review: ReviewEvent, *, expected_case_revision: int, base: Settings) -> RecommendationView:
        saved = await self.artifact(ref)
        case_id = UUID(saved.case_id)
        await self._reserve(case_id)
        tasks = TaskService(self.session)
        parameters = {"recommendation_ref": ref.model_dump(mode="json"), "review": review.model_dump(mode="json"), "expected_case_revision": expected_case_revision}
        action = await tasks._action(claim.task_id, operation_id)
        if action.parameters != parameters:
            raise InvestorError("idempotency_conflict", "Prepared extension parameters differ")
        async def write(session: AsyncSession, operation: UUID) -> ActionResult:
            await check_boundary(session, claim, base, tool="extend_recommendation", required_refs=(ObjectRef(kind="case", id=case_id, revision=expected_case_revision),))
            now = datetime.now(UTC)
            case = await self.foundation.get_case("local_owner", case_id)
            view = await self.get_recommendation(ref, at=now)
            # WP13: a critical peer dependency refuses the renewal with its
            # structured reasons before any generic unrestricted-view refusal -
            # the opinion keeps its validity and known limitations either way.
            for dependency in view.recommendation.dependency_refs:
                if (
                    dependency.materiality == "critical"
                    and dependency.ref.kind == "calculation"
                    and await peer_method_payload(self.session, "local_owner", dependency.ref) is not None
                ):
                    raise await self.peer_renewal_block(view.recommendation, dependency.ref)
            await self._check_method_renewal(claim.task_id, view.recommendation)
            from investment_agent.investor.monitoring import verify_unchanged_review
            await verify_unchanged_review(session, claim.task_id, review, self)
            required = tuple(d.ref for d in view.recommendation.dependency_refs if d.materiality == "critical")
            if case.revision != expected_case_revision or case.current_recommendation_ref != ref or view.status != RecommendationStatus.PUBLISHED:
                raise InvestorError("revision_conflict", "Only the still current, valid, unrestricted view can extend")
            if review.recommendation_ref != ref or review.result != "maintain" or review.reasoning_revision or review.new_recommendation_ref is not None or not set(required) <= set(review.evidence_diff_refs) or not review.started_at <= review.completed_at <= now:
                raise InvestorError("invalid_reference", "Unchanged review must bind every exact critical dependency")
            from investment_agent.agent.observation import effective_cutoff
            observation_cutoff = await effective_cutoff(session, claim.task_id)
            resolver = NumericResolver(EvidenceService(session), case.listing_ref, min(now, observation_cutoff))
            for checked_ref in review.evidence_diff_refs:
                if checked_ref not in required:
                    checked_record = await resolver.fact(checked_ref, require_fresh=False)
                    if max(checked_record.created_at, checked_record.acquired_at, checked_record.available_from) > review.completed_at:
                        raise InvestorError("invalid_reference", "Review cannot attest materials acquired after its completion")
            for dependency in view.recommendation.dependency_refs:
                if dependency.materiality != "critical" or dependency.ref.kind not in {"evidence", "calculation"}:
                    continue
                facts = (dependency.ref,) if dependency.ref.kind == "evidence" else (await resolver.calculation(dependency.ref, require_fresh=False)).fact_refs
                for fact in facts:
                    original = await resolver.fact(fact, require_fresh=False)
                    if original.source_ref.kind == "market_snapshot" and now - original.available_from > timedelta(days=14):
                        latest = await self._snapshot_candidates(original, min(now, observation_cutoff))
                        if latest.status != "ok" or not latest.data:
                            raise InvestorError("invalid_reference", "Fresh market verification required")
                        sources = {r.source_ref for r in latest.data}
                        proofs = [await resolver.fact(r, require_fresh=False) for r in review.evidence_diff_refs if r.kind == "evidence"]
                        if not any(proof.source_ref in sources and max(proof.created_at, proof.acquired_at, proof.available_from) <= review.completed_at for proof in proofs):
                            raise InvestorError("invalid_reference", "Old historical price requires an explicitly checked current snapshot")
            if review.completed_at < view.recommendation.created_at:
                raise InvestorError("invalid_reference", "Review predates publication")
            next_review, until = self.policy.dates(review.completed_at)
            if until <= view.effective_valid_until:
                raise InvestorError("invalid_reference", "Review does not extend the current validity")
            saved_review = review.model_copy(update={"id": uuid4(), "revision": 1, "created_at": now, "actor": "research_service"})
            review_ref = self._save(case_id, "review", saved_review)
            await self._event(ref, "extended", now, review_ref=review_ref, checked=review.evidence_diff_refs, until=until, next_review=next_review)
            new_case = case.model_copy(update={"revision": case.revision + 1})
            row = await self.foundation.repository.case("local_owner", case_id)
            assert row is not None
            if not await self.foundation.repository.advance_case(row, case.revision, new_case.model_dump(mode="json")):
                raise InvestorError("revision_conflict", "Case revision changed")
            await check_boundary(session, claim, base, tool="extend_recommendation", required_refs=(case.listing_ref,))
            await session.flush()
            result = await self.get_recommendation(ref)
            return ActionResult(result.model_dump(mode="json"), artifact_refs=(review_ref,))
        return RecommendationView.model_validate(await tasks.settle_action(claim, operation_id, write))

    async def restrict(self, ref: ObjectRef, *, reason: str, at: datetime | None = None, withdrawn: bool = False) -> None:
        """Trusted transaction seam for AG14 archive/revoke and AG15 changes.

        Not an HTTP/model capability. Caller checks user authorization and uses
        the same transaction as its case mutation/action receipt.
        """
        saved = await self.artifact(ref)
        await self._reserve(UUID(saved.case_id))
        at = utc_timestamp(at or datetime.now(UTC))
        view = await self.get_recommendation(ref, at=at)
        if view.status in {RecommendationStatus.WITHDRAWN, RecommendationStatus.SUPERSEDED}:
            return
        await self._event(ref, "withdrawn" if withdrawn else "review_required", at)
        self.session.add(PublicationArtifact(id=str(uuid4()), revision=1, kind="limitation", owner_id="local_owner", case_id=saved.case_id, payload={"recommendation_ref": ref.model_dump(mode="json"), "reason": reason, "at": at.isoformat()}))
        await self.session.flush()


def visible_text(candidate: PublicationCandidate, semantic: SemanticReview) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for prefix, obj in (("thesis", candidate.thesis), ("recommendation", candidate.recommendation)):
        for name in ("change_reason", "question_scope"):
            if hasattr(obj, name):
                texts.append((f"{prefix}.{name}", getattr(obj, name)))
        for i, claim in enumerate(obj.claims):
            texts.append((f"{prefix}.claims.{i}.impact_if_wrong", claim.impact_if_wrong))
            texts.extend((f"{prefix}.claims.{i}.text.{j}", node.text) for j, node in enumerate(claim.text) if isinstance(node, TextNode))
        texts.extend((f"{prefix}.counter_case.{j}", node.text) for j, node in enumerate(obj.counter_case) if isinstance(node, TextNode))
        for i, dimension in enumerate(obj.assessed_dimensions):
            if dimension.reason:
                texts.append((f"{prefix}.assessed_dimensions.{i}.reason", dimension.reason))
        for i, assumption in enumerate(obj.assumptions):
            for name in ("question", "scope", "support_condition", "falsification_condition"):
                texts.append((f"{prefix}.assumptions.{i}.{name}", getattr(assumption, name)))
    texts.extend((f"recommendation.gaps.{i}", text) for i, text in enumerate(candidate.recommendation.gaps))
    for i, monitor in enumerate(candidate.monitors):
        texts.extend((f"monitors.{i}.conditions.{j}.period_basis", condition.period_basis) for j, condition in enumerate(monitor.conditions))
    for i, assessment in enumerate(semantic.assessments):
        for name in ("fact_relation", "counter_evidence_relevance", "materiality_reason"):
            texts.append((f"semantic_review.assessments.{i}.{name}", getattr(assessment, name)))
    texts.extend((f"semantic_review.unresolved_questions.{i}", text) for i, text in enumerate(semantic.unresolved_questions))
    return texts


def remap_references(value: Any, mappings: dict[ObjectRef, ObjectRef]) -> Any:
    if isinstance(value, list):
        return [remap_references(item, mappings) for item in value]
    if isinstance(value, dict):
        if set(value) >= {"kind", "id", "revision"}:
            ref = ObjectRef.model_validate(value)
            bare = ref.model_copy(update={"field_path": None})
            if bare in mappings:
                return mappings[bare].model_copy(update={"field_path": ref.field_path}).model_dump(mode="json")
        return {key: remap_references(item, mappings) for key, item in value.items()}
    return value
