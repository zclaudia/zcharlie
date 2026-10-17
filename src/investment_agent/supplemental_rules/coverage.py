"""Prepared rule-coverage manifests and publish-time rechecks (AF-P3-05).

Construction only reads completed live applications inside the caller's
prepare/publish transaction; it never evaluates rules inline. Unknown and
unbound-new are first-class outcomes, never silently collapsed into coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef, OwnerId, Text
from investment_agent.investor.foundations import InvestorError
from investment_agent.supplemental_rules.admission import SupplementalRuleAdmission
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    MAX_PAIRS_PER_MANIFEST,
    Context,
    CoverageRuleEntry,
    EvaluationAdmission,
    EvaluationRequest,
    PollWindowSourceProof,
    PreparedClosureProposal,
    ResearchRuleBinding,
    RuleApplication,
    RuleCoverage,
    RuleCoverageManifest,
    RuleCoverageManifestV2,
    RuleCoverageManifestV3,
    SavedRefsSourceProof,
    SourceCoverageBindingV3,
    UpdateSourceBinding,
    UpdateSourceEntry,
)
from investment_agent.supplemental_rules.service import SupplementalRuleService

COVERAGE_CONTEXT: Context = "company_research"


async def _build_saved_refs_source_binding(
    session: AsyncSession, *, owner: OwnerId, listing_ref: ObjectRef,
    context: Context = COVERAGE_CONTEXT, observed_at: datetime | None = None,
    knowledge_cutoff: datetime | None = None,
    source_ids: set[str] | None = None,
) -> UpdateSourceBinding | None:
    """Freeze only current, complete source states backed by durable receipts.

    A scope with no declared sources remains on P3 schema 1. Any declared but
    incomplete scope also stays unbound; publication limits then report the
    declared scope as unknown and prevent a directional conclusion.
    """

    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
    )

    if listing_ref.kind != "listing" or listing_ref.field_path is not None:
        raise InvestorError("invalid_reference", "来源覆盖需要精确上市主体")
    repo = SupplementalUpdatesRepository(session)
    listing_id = int(listing_ref.id)
    scope_key = f"listing:{listing_id}:{context}"
    states = await repo.list_sources(owner, listing_id, context, for_update=True)
    if source_ids is not None:
        states = [state for state in states if state.feed_id in source_ids]
    if not states or len(states) > 16:
        return None
    observed_at = observed_at or datetime.now(UTC)
    knowledge_cutoff = knowledge_cutoff or observed_at
    observed_at = observed_at.astimezone(UTC)
    knowledge_cutoff = knowledge_cutoff.astimezone(UTC)
    entries: list[UpdateSourceEntry] = []
    for state in states:
        if (
            state.scope_key != scope_key
            or state.listing_id != listing_id
            or state.context != context
            or state.coverage_state != "complete"
            or state.success_sequence < 1
            or state.success_receipt_hash is None
            or state.window_start is None
            or state.window_end is None
            or state.complete_watermark is None
            or state.complete_watermark < state.window_end
            or state.window_start > observed_at
            or state.window_end > observed_at
            or state.complete_watermark > observed_at
            or state.last_success_at is None
            or state.last_success_at > observed_at
            or state.health_limit_seconds <= 0
            or observed_at >= state.last_success_at.astimezone(UTC)
            + timedelta(seconds=state.health_limit_seconds)
            or not state.source_refs
        ):
            return None
        current_receipt = await repo.get_change(owner, state.success_sequence)
        if (
            current_receipt is None
            or current_receipt.payload_hash != state.success_receipt_hash
            or current_receipt.listing_id != listing_id
            or current_receipt.context != context
            or current_receipt.committed_at is None
            or current_receipt.committed_at.astimezone(UTC)
            != state.last_success_at.astimezone(UTC)
            or current_receipt.committed_at.astimezone(UTC) > observed_at
            or current_receipt.acquired_at is not None
            and current_receipt.acquired_at.astimezone(UTC) > observed_at
            or current_receipt.available_from is not None
            and current_receipt.available_from.astimezone(UTC) > observed_at
            or canonical_hash(current_receipt.payload.get("source_refs"))
            != canonical_hash(state.source_refs)
            or current_receipt.payload.get("source_id") != state.feed_id
            or current_receipt.payload.get("contract_version") != state.contract_version
            or current_receipt.payload.get("contract_hash") != state.config_hash
            or current_receipt.payload.get("scope_hash") != state.scope_hash
            or current_receipt.payload.get("source_refs_hash")
            != canonical_hash(state.source_refs)
        ):
            return None
        refs = tuple(ObjectRef.model_validate(ref) for ref in state.source_refs)
        # Operational freshness may have been renewed after task admission.
        # Select a historical proof that was already committed and visible at
        # the knowledge cutoff, and verify those immutable refs against their
        # current exact-version content before freezing them into the manifest.
        try:
            from investment_agent.settings.loader import load_settings
            from investment_agent.supplemental_rules.update_intake import (
                resolve_saved_sources,
            )

            resolved = await resolve_saved_sources(
                session, listing_ref, refs,
                settings=await load_settings(session), now=knowledge_cutoff,
                owner_id=owner,
            )
        except InvestorError:
            return None
        if not resolved or any(
            item.get("status") not in {"ok", "candidate"}
            or datetime.fromisoformat(item["acquired_at"]).astimezone(UTC) > knowledge_cutoff
            or datetime.fromisoformat(item["available_from"]).astimezone(UTC) > knowledge_cutoff
            for item in resolved
        ):
            return None
        expected_source_rows = [ref.model_dump(mode="json") for ref in refs]
        from investment_agent.storage.models.supplemental_updates import (
            SupplementalChangeReceiptRow,
        )

        proof_sequence = await session.scalar(
            select(SupplementalChangeReceiptRow.sequence).where(
                SupplementalChangeReceiptRow.owner_id == owner,
                SupplementalChangeReceiptRow.kind == "evidence_revision",
                SupplementalChangeReceiptRow.listing_id == listing_id,
                SupplementalChangeReceiptRow.context == context,
                SupplementalChangeReceiptRow.committed_at <= knowledge_cutoff,
                SupplementalChangeReceiptRow.acquired_at <= knowledge_cutoff,
                SupplementalChangeReceiptRow.available_from <= knowledge_cutoff,
                SupplementalChangeReceiptRow.payload["source_id"].as_string() == state.feed_id,
                SupplementalChangeReceiptRow.payload["contract_version"].as_string()
                == state.contract_version,
                SupplementalChangeReceiptRow.payload["contract_hash"].as_string()
                == state.config_hash,
                SupplementalChangeReceiptRow.payload["scope_hash"].as_string()
                == state.scope_hash,
                SupplementalChangeReceiptRow.payload["source_refs_hash"].as_string()
                == canonical_hash(state.source_refs),
                SupplementalChangeReceiptRow.payload["coverage_state"].as_string()
                == "complete",
            ).order_by(
                SupplementalChangeReceiptRow.committed_at.desc(),
                SupplementalChangeReceiptRow.sequence.desc(),
            ).limit(1)
        )
        proof = await repo.get_change(owner, proof_sequence) if proof_sequence else None
        if proof is None:
            return None
        proof_sources = proof.payload.get("sources")
        if (
            canonical_hash(proof.payload.get("source_refs"))
            != canonical_hash(expected_source_rows)
            or proof.payload.get("source_id") != state.feed_id
            or proof.payload.get("contract_version") != state.contract_version
            or proof.payload.get("contract_hash") != state.config_hash
            or proof.payload.get("scope_hash") != state.scope_hash
            or proof.payload.get("source_refs_hash") != canonical_hash(state.source_refs)
            or not isinstance(proof_sources, list)
            or not proof_sources
            or proof_sources != resolved
        ):
            return None
        proof_available = [
            datetime.fromisoformat(item["available_from"]).astimezone(UTC)
            for item in proof_sources if isinstance(item, dict)
            and isinstance(item.get("available_from"), str)
        ]
        if len(proof_available) != len(proof_sources):
            return None
        window_start = min(proof_available)
        window_end = proof.committed_at.astimezone(UTC) if proof.committed_at else None
        if window_end is None or window_start > window_end:
            return None
        entries.append(UpdateSourceEntry(
            source_id=state.feed_id,
            contract_version=state.contract_version,
            contract_hash=state.config_hash,
            scope_hash=state.scope_hash,
            success_sequence=proof.sequence,
            success_receipt_hash=proof.payload_hash,
            source_refs=refs,
            window_start=window_start,
            window_end=window_end,
            complete_watermark=window_end,
            completeness="complete",
            last_success_at=window_end,
            health_limit_seconds=state.health_limit_seconds,
        ))
    ordered = tuple(sorted(entries, key=lambda entry: entry.source_id))
    policy_hash = canonical_hash(tuple(sorted(
        (entry.source_id, entry.contract_hash, entry.scope_hash) for entry in ordered
    )))
    return UpdateSourceBinding(
        listing_ref=listing_ref,
        context=context,
        scope_key=scope_key,
        sources=ordered,
        source_policy_hash=policy_hash,
        observed_at=observed_at,
    )


async def build_update_source_binding(
    session: AsyncSession, *, owner: OwnerId, listing_ref: ObjectRef,
    context: Context = COVERAGE_CONTEXT, observed_at: datetime | None = None,
    knowledge_cutoff: datetime | None = None,
) -> UpdateSourceBinding | SourceCoverageBindingV3 | None:
    """Build the legacy V2 saved-ref binding or typed V3 mixed-source binding."""
    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
    )

    observed_at = observed_at or datetime.now(UTC)
    knowledge_cutoff = knowledge_cutoff or observed_at
    repo = SupplementalUpdatesRepository(session)
    states = await repo.list_sources(owner, int(listing_ref.id), context, for_update=True)
    # A mixed binding may not silently omit any source declared for this scope.
    # The V3 manifest has the same hard source-count ceiling as V2, so reject
    # the whole scope before selecting a subset of source families.
    if len(states) > 16:
        return None
    poll_states = [state for state in states if state.feed_id == "cninfo_szse_v1"]
    if not poll_states:
        return await _build_saved_refs_source_binding(
            session, owner=owner, listing_ref=listing_ref, context=context,
            observed_at=observed_at, knowledge_cutoff=knowledge_cutoff,
            source_ids={state.feed_id for state in states},
        )
    if any(state.coverage_state != "complete" for state in poll_states):
        return None
    saved_states = [state for state in states if state.feed_id != "cninfo_szse_v1"]
    saved_binding = await _build_saved_refs_source_binding(
        session, owner=owner, listing_ref=listing_ref, context=context,
        observed_at=observed_at, knowledge_cutoff=knowledge_cutoff,
        source_ids={state.feed_id for state in saved_states},
    ) if saved_states else None
    if saved_states and saved_binding is None:
        return None
    sources: list[Any] = []
    if saved_binding is not None:
        sources.extend(SavedRefsSourceProof(entry=entry) for entry in saved_binding.sources)
    from investment_agent.storage.repositories.supplemental_updates import (
        load_poll_window_proof,
    )
    for state in poll_states:
        proof = await load_poll_window_proof(
            session, owner=owner, listing_id=int(listing_ref.id), context=context,
            cutoff=knowledge_cutoff, expected_source_state=state,
        )
        if proof is None:
            return None
        poll_proof = PollWindowSourceProof.model_validate(proof)
        if poll_proof.protocol_profile == "fixture" and not session.info.get(
            "allow_disclosure_fixture_proofs", False
        ):
            return None
        if poll_proof.protocol_profile == "live":
            from investment_agent.evidence.disclosure_profiles import approved_contract

            current_at = datetime.now(UTC)
            if (poll_proof.protocol_status != "live_validated"
                or not approved_contract(poll_proof.contract_hash, now=current_at)):
                return None
            from investment_agent.supplemental_rules.disclosure_authorization import (
                get_current_authorization,
            )

            try:
                active_grant = await get_current_authorization(
                    session, poll_proof.grant_ref, owner_id=owner, now=current_at,
                )
            except Exception:
                return None
            if active_grant.authorization_hash != poll_proof.grant_hash:
                return None
        try:
            target_end = datetime.fromisoformat(
                state.payload["coverage_target_end"].replace("Z", "+00:00")
            ).astimezone(UTC)
            history_frontier = datetime.fromisoformat(
                state.payload["history_frontier"].replace("Z", "+00:00")
            ).astimezone(UTC)
        except (KeyError, AttributeError, TypeError, ValueError):
            return None
        if (
            state.payload.get("history_gap") is True
            or history_frontier < target_end
            or poll_proof.continuous_index_watermark is None
            or poll_proof.continuous_original_watermark is None
            or poll_proof.continuous_complete_watermark is None
            or poll_proof.continuous_complete_watermark < target_end
        ):
            return None
        sources.append(poll_proof)
    if not sources or len(sources) > 16:
        return None
    sources.sort(key=lambda item: item.entry.source_id if isinstance(item, SavedRefsSourceProof) else item.source_id)
    policy_hash = canonical_hash(tuple(sorted(
        (item.entry.source_id, item.entry.contract_hash, item.entry.scope_hash)
        if isinstance(item, SavedRefsSourceProof)
        else (item.source_id, item.contract_hash, item.scope_hash)
        for item in sources
    )))
    return SourceCoverageBindingV3(
        listing_ref=listing_ref, context=context,
        scope_key=f"listing:{listing_ref.id}:{context}", sources=tuple(sources),
        source_policy_hash=policy_hash, observed_at=observed_at,
    )


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
    update_source_binding: UpdateSourceBinding | SourceCoverageBindingV3 | None = None,
) -> RuleCoverage:
    """Build one prepared coverage manifest for the binding's rule set.

    Each bound rule yields exactly one entry: its latest live application, an
    evidenced not-applicable, an explicit unknown, or unbound-new for a rule
    activated after admission that currently matches. Missing evaluations stay
    unknown; nothing here blocks prepare.
    """

    if cutoff < binding.admission_as_of:
        raise InvestorError("invalid_reference", "研究截止不能早于受理绑定时点")
    if update_source_binding is not None and update_source_binding.listing_ref != listing_ref:
        raise InvestorError("invalid_reference", "来源覆盖主体与研究主体不一致")
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
    schema_version = (
        3 if isinstance(update_source_binding, SourceCoverageBindingV3)
        else 2 if update_source_binding is not None else 1
    )
    source_binding_field = (
        "source_coverage_binding"
        if isinstance(update_source_binding, SourceCoverageBindingV3)
        else "update_source_binding"
    )
    input_payload: dict[str, Any] = {
        "schema_version": schema_version,
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
    }
    if update_source_binding is not None:
        input_payload[source_binding_field] = update_source_binding
    input_hash = canonical_hash(input_payload)
    data: dict[str, Any] = {
        "schema_version": schema_version,
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
    if update_source_binding is not None:
        data[source_binding_field] = update_source_binding
    data["manifest_hash"] = canonical_hash(
        {key: value for key, value in data.items() if key != "manifest_hash"}
    )
    if schema_version == 2:
        return RuleCoverageManifestV2.model_validate(data)
    if schema_version == 3:
        return RuleCoverageManifestV3.model_validate(data)
    return RuleCoverageManifest.model_validate(data)


def coverage_complete(manifest: RuleCoverage) -> bool:
    """True only when every entry resolved to an application or an evidenced miss."""

    return not manifest.scan_capped and all(
        entry.status in {"application", "not_applicable"} for entry in manifest.rule_entries
    )


def coverage_limitations(manifest: RuleCoverage, *, limit: int = 32) -> tuple[Text, ...]:
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


def rule_summary_entries(manifest: RuleCoverage) -> list[dict[str, Any]]:
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
    if isinstance(manifest, (RuleCoverageManifestV2, RuleCoverageManifestV3)):
        if isinstance(manifest, RuleCoverageManifestV3):
            summaries.append({
                "summary_kind": "update_source_coverage",
                "listing_ref": manifest.source_coverage_binding.listing_ref.model_dump(mode="json"),
                "context": manifest.source_coverage_binding.context,
                "scope_key": manifest.source_coverage_binding.scope_key,
                "source_policy_hash": manifest.source_coverage_binding.source_policy_hash,
                "sources": [{
                    "source_id": source.entry.source_id if isinstance(source, SavedRefsSourceProof) else source.source_id,
                    "proof_type": source.proof_type,
                    "contract_hash": source.entry.contract_hash if isinstance(source, SavedRefsSourceProof) else source.contract_hash,
                    "scope_hash": source.entry.scope_hash if isinstance(source, SavedRefsSourceProof) else source.scope_hash,
                    "window_start": (source.entry.window_start if isinstance(source, SavedRefsSourceProof) else source.publication_window_start).isoformat(),
                    "window_end": (source.entry.window_end if isinstance(source, SavedRefsSourceProof) else source.publication_window_end).isoformat(),
                    "complete_watermark": (source.entry.complete_watermark if isinstance(source, SavedRefsSourceProof) else source.complete_watermark).isoformat(),
                    "completeness": "complete",
                } for source in manifest.source_coverage_binding.sources],
            })
        else:
            summaries.append({
            "summary_kind": "update_source_coverage",
            "listing_ref": manifest.update_source_binding.listing_ref.model_dump(mode="json"),
            "context": manifest.update_source_binding.context,
            "scope_key": manifest.update_source_binding.scope_key,
            "source_policy_hash": manifest.update_source_binding.source_policy_hash,
            "sources": [{
                "source_id": source.source_id,
                "contract_version": source.contract_version,
                "contract_hash": source.contract_hash,
                "scope_hash": source.scope_hash,
                "success_sequence": source.success_sequence,
                "success_receipt_hash": source.success_receipt_hash,
                "source_refs_count": len(source.source_refs),
                "source_refs_hash": canonical_hash(source.source_refs),
                "source_refs_preview": [
                    ref.model_dump(mode="json") for ref in source.source_refs[:8]
                ],
                "source_refs_truncated": len(source.source_refs) > 8,
                "window_start": source.window_start.isoformat(),
                "window_end": source.window_end.isoformat(),
                "complete_watermark": source.complete_watermark.isoformat(),
                "completeness": source.completeness,
                "last_success_at": source.last_success_at.isoformat(),
                "health_limit_seconds": source.health_limit_seconds,
            } for source in manifest.update_source_binding.sources],
            })
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
    session: AsyncSession, *, binding: ResearchRuleBinding, manifest: RuleCoverage,
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
