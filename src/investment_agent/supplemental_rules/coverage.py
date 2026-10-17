"""Prepared rule-coverage manifests and publish-time rechecks (AF-P3-05).

Construction only reads completed live applications inside the caller's
prepare/publish transaction; it never evaluates rules inline. Unknown and
unbound-new are first-class outcomes, never silently collapsed into coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef, Text
from investment_agent.investor.foundations import InvestorError
from investment_agent.supplemental_rules.admission import SupplementalRuleAdmission
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    MAX_PAIRS_PER_MANIFEST,
    Context,
    CoverageRuleEntry,
    EvaluationAdmission,
    EvaluationRequest,
    PreparedClosureProposal,
    ResearchRuleBinding,
    RuleApplication,
    RuleCoverageManifest,
)
from investment_agent.supplemental_rules.service import SupplementalRuleService

COVERAGE_CONTEXT: Context = "company_research"


class ClosureProposalLike(Protocol):
    """The investor-side model proposal; the server freezes it with a hash."""

    @property
    def target_application_refs(self) -> tuple[ObjectRef, ...]: ...

    @property
    def evidence_refs(self) -> tuple[ObjectRef, ...]: ...

    @property
    def claim_key(self) -> str | None: ...

    @property
    def rationale(self) -> str: ...


def closure_proposal_hash(
    *, target_application_refs: tuple[ObjectRef, ...], evidence_refs: tuple[ObjectRef, ...],
    claim_key: str | None, rationale: str,
) -> str:
    """The frozen proposal digest domain (must mirror the DTO validator)."""

    return canonical_hash({
        "target_application_refs": target_application_refs,
        "evidence_refs": evidence_refs,
        "claim_key": claim_key,
        "rationale": rationale,
    })


def freeze_closure_proposal(proposal: ClosureProposalLike) -> PreparedClosureProposal:
    """Map a model-side closure proposal onto the immutable prepared DTO."""

    targets = tuple(proposal.target_application_refs)
    evidence = tuple(proposal.evidence_refs)
    claim_key = proposal.claim_key
    rationale = proposal.rationale
    return PreparedClosureProposal.model_validate({
        "target_application_refs": targets,
        "evidence_refs": evidence,
        "claim_key": claim_key,
        "rationale": rationale,
        "proposal_hash": closure_proposal_hash(
            target_application_refs=targets, evidence_refs=evidence,
            claim_key=claim_key, rationale=rationale,
        ),
    })


async def ensure_binding_evaluation(
    session: AsyncSession, *, binding: ResearchRuleBinding, listing_ref: ObjectRef
) -> EvaluationAdmission:
    """Admit one deterministic coverage evaluation for the binding scope.

    The idempotency key derives from the binding, so repeated prepare calls
    replay the original admission receipt instead of enqueueing again. A zero
    pair evaluation is legal. The P2 switch check stays inside admission: a
    bound task cannot use a disabled switch to skip its chain.
    """

    from investment_agent.settings.loader import load_settings

    effective = await load_settings(session)
    operation_id = uuid5(NAMESPACE_URL, f"coverage_evaluation:{binding.id}")
    request = EvaluationRequest(
        idempotency_key=operation_id, listing_refs=(listing_ref,),
        contexts=frozenset({COVERAGE_CONTEXT}), mode="live", evaluation_purpose="coverage",
    )
    return await SupplementalRuleAdmission(session, settings=effective).accept_evaluation(request)


async def _latest_live(
    service: SupplementalRuleService, listing_ref: ObjectRef
) -> tuple[tuple[RuleApplication, ...], bool]:
    """Bounded latest live applications; capacity overflow is reported, not hidden."""

    try:
        return await service.latest_live(listing_ref, COVERAGE_CONTEXT), False
    except InvestorError as exc:
        if exc.detail.code != "source_capacity_exceeded":
            raise
        return (
            await service.latest_live(listing_ref, COVERAGE_CONTEXT, allow_partial=True),
            True,
        )


async def build_prepared_manifest(
    session: AsyncSession,
    *,
    binding: ResearchRuleBinding,
    case_ref: ObjectRef | None,
    listing_ref: ObjectRef,
    cutoff: datetime,
    closure_proposals: tuple[ClosureProposalLike, ...] = (),
    material_set_hash: str | None = None,
) -> RuleCoverageManifest:
    """Build one prepared coverage manifest for the binding's rule set.

    Each bound rule yields exactly one entry: its latest live application, an
    evidenced not-applicable, an explicit unknown, or unbound-new for a rule
    activated after admission that currently matches. Missing evaluations stay
    unknown; nothing here blocks prepare.
    """

    if cutoff < binding.admission_as_of:
        raise InvestorError("invalid_reference", "研究截止不能早于受理绑定时点")
    service = SupplementalRuleService(session)
    live_apps, scan_capped = await _latest_live(service, listing_ref)
    by_rule = {app.rule_ref.id: app for app in live_apps}
    entries: list[Any] = []
    for entry in sorted(binding.rule_set, key=lambda item: (str(item.rule_ref.id), item.rule_ref.revision)):
        app = by_rule.get(entry.rule_ref.id)
        if app is None:
            entries.append({"rule_ref": entry.rule_ref, "semantic_hash": entry.semantic_hash,
                            "status": "unknown", "unknown_reason": "coverage_evaluation_missing"})
        elif app.rule_ref != entry.rule_ref:
            entries.append({"rule_ref": entry.rule_ref, "semantic_hash": entry.semantic_hash,
                            "status": "unknown", "unknown_reason": "binding_rule_revision_superseded"})
        elif app.applicability == "not_applicable":
            entries.append({"rule_ref": entry.rule_ref, "semantic_hash": entry.semantic_hash,
                            "status": "not_applicable",
                            "basis": f"rule_application:{app.id}:selector_not_applicable"})
        elif app.applicability == "unknown":
            entries.append({"rule_ref": entry.rule_ref, "semantic_hash": entry.semantic_hash,
                            "status": "unknown",
                            "unknown_reason": ";".join(
                                ("evaluation_match_unknown", *app.frozen_pair.diagnostics))})
        else:
            entries.append({"rule_ref": entry.rule_ref, "semantic_hash": entry.semantic_hash,
                            "status": "application", "application_ref": app.reference})
    # A capped scan keeps every uncovered binding entry explicitly unknown and
    # reports its incompleteness via scan_capped/scan_uncovered_note below.
    bound_ids = {entry.rule_ref.id for entry in binding.rule_set}
    for app in live_apps:
        if app.rule_ref.id in bound_ids or app.applicability != "applicable":
            continue
        rule = app.frozen_pair.rule
        entries.append({"rule_ref": app.rule_ref,
                        "semantic_hash": rule.semantic_hash or canonical_hash(rule.model_dump(mode="python")),
                        "status": "unbound_new"})
    rule_entries = tuple(CoverageRuleEntry.model_validate(item) for item in entries)
    open_issue_refs: list[ObjectRef] = []
    for app in live_apps:
        for ref in app.open_issue_refs:
            if ref not in open_issue_refs:
                open_issue_refs.append(ref)
    open_issue_refs = open_issue_refs[:MAX_PAIRS_PER_MANIFEST]
    proposals = tuple(freeze_closure_proposal(proposal) for proposal in closure_proposals)
    now = datetime.now(UTC)
    task_ref = ObjectRef(kind="task", id=binding.task_ref.id, revision=1)
    input_hash = canonical_hash({
        "schema_version": 1,
        "binding_ref": binding.reference,
        "task_ref": task_ref,
        "listing_ref": listing_ref,
        "case_ref": case_ref,
        "admission_as_of": binding.admission_as_of,
        "effective_cutoff": cutoff,
        "rule_entries": rule_entries,
        "open_issue_refs": tuple(open_issue_refs),
        "closure_proposals": proposals,
        "scan_capped": scan_capped,
        "scan_known_total": len(live_apps),
        "scan_uncovered_note": "live application scan exceeded the bounded limit" if scan_capped else None,
        "material_set_hash": material_set_hash,
    })
    data: dict[str, Any] = {
        "schema_version": 1,
        "id": uuid4(),
        "revision": 1,
        "stage": "prepared",
        "owner_id": binding.owner_id,
        "task_ref": task_ref,
        "task_id": binding.task_ref.id,
        "binding_ref": binding.reference,
        "listing_ref": listing_ref,
        "case_ref": case_ref,
        "admission_as_of": binding.admission_as_of,
        "effective_cutoff": cutoff,
        "rule_entries": rule_entries,
        "open_issue_refs": tuple(open_issue_refs),
        "closure_proposals": proposals,
        "scan_capped": scan_capped,
        "scan_known_total": len(live_apps),
        "scan_uncovered_note": "live application scan exceeded the bounded limit" if scan_capped else None,
        "material_set_hash": material_set_hash,
        "input_hash": input_hash,
        "created_at": now,
        # Published-stage defaults are part of the hashed domain and must be
        # present before the manifest hash is computed.
        "prepared_ref": None,
        "prepared_manifest_hash": None,
        "semantic_review_ref": None,
        "closure_application_refs": (),
        "thesis_ref": None,
        "recommendation_ref": None,
        "limits_recheck": (),
        "committed_at": None,
        "manifest_hash": "pending",
    }
    data["manifest_hash"] = canonical_hash(
        {key: value for key, value in data.items() if key != "manifest_hash"}
    )
    return RuleCoverageManifest.model_validate(data)


def coverage_complete(manifest: RuleCoverageManifest) -> bool:
    """True only when every entry resolved to an application or an evidenced miss."""

    return not manifest.scan_capped and all(
        entry.status in {"application", "not_applicable"} for entry in manifest.rule_entries
    )


def coverage_limitations(manifest: RuleCoverageManifest, *, limit: int = 32) -> tuple[Text, ...]:
    """Bounded honest gaps of one prepared manifest for the prepare receipt."""

    items: list[Text] = []
    for entry in manifest.rule_entries:
        marker = None
        if entry.status == "unknown":
            marker = f"coverage_unknown:{entry.rule_ref.id}"
        elif entry.status == "unbound_new":
            marker = f"coverage_unbound_new:{entry.rule_ref.id}"
        if marker is not None and marker not in items:
            items.append(marker)
        if len(items) >= limit:
            break
    overflow = sum(
        1 for entry in manifest.rule_entries
        if entry.status in {"unknown", "unbound_new"}
    ) - len(items)
    if overflow > 0:
        items.append(f"coverage_gaps_truncated:{overflow}_more")
    if manifest.scan_capped:
        items.append("rule_coverage_scan_incomplete")
    return tuple(items)


def rule_summary_entries(manifest: RuleCoverageManifest) -> list[dict[str, Any]]:
    """Fixed-shape, bounded rule/issue/coverage digest for the review window."""

    summaries: list[dict[str, Any]] = [{
        "summary_kind": "rule_coverage",
        "manifest_ref": manifest.reference.model_dump(mode="json"),
        "manifest_hash": manifest.manifest_hash,
        "binding_ref": manifest.binding_ref.model_dump(mode="json"),
        "admission_as_of": manifest.admission_as_of.isoformat(),
        "effective_cutoff": manifest.effective_cutoff.isoformat(),
        "scan_capped": manifest.scan_capped,
        "scan_known_total": manifest.scan_known_total,
        "scan_uncovered_note": manifest.scan_uncovered_note,
        "open_issue_refs": [ref.model_dump(mode="json") for ref in manifest.open_issue_refs],
        "closure_proposals": [{
            "target_application_refs": [ref.model_dump(mode="json") for ref in proposal.target_application_refs],
            "evidence_refs": [ref.model_dump(mode="json") for ref in proposal.evidence_refs],
            "claim_key": proposal.claim_key,
            "rationale": proposal.rationale,
            "proposal_hash": proposal.proposal_hash,
        } for proposal in manifest.closure_proposals],
    }]
    for entry in manifest.rule_entries:
        item: dict[str, Any] = {
            "rule_ref": entry.rule_ref.model_dump(mode="json"),
            "semantic_hash": entry.semantic_hash,
            "status": entry.status,
        }
        if entry.application_ref is not None:
            item["application_ref"] = entry.application_ref.model_dump(mode="json")
        if entry.basis is not None:
            item["basis"] = entry.basis
        if entry.unknown_reason is not None:
            item["unknown_reason"] = entry.unknown_reason
        summaries.append(item)
    return summaries


async def rule_coverage_publish_limits(
    session: AsyncSession, *, binding: ResearchRuleBinding, manifest: RuleCoverageManifest,
    listing_ref: ObjectRef,
) -> tuple[Text, ...]:
    """Publish-time current restriction recheck for one prepared manifest.

    Thin delegate: the single bounded recheck predicate lives in
    ``publication_limits`` so publish-time and read-time answers can never
    drift. Never enqueues research.
    """

    if listing_ref != manifest.listing_ref:
        raise InvestorError("invalid_reference", "覆盖清单与发布主体不一致")
    from investment_agent.supplemental_rules.publication_limits import published_manifest_limits

    return await published_manifest_limits(session, owner=manifest.owner_id, manifest=manifest)
