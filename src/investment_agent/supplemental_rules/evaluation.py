"""Mechanical evaluation of one frozen P2 rule pair.

This module deliberately cannot manufacture semantic reviews or close an old
issue. It evaluates saved mechanics and preserves history for later readers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from investment_agent.investor.common import ObjectRef
from investment_agent.supplemental_rules.contracts import (
    EffectiveEffect,
    EvaluationResult,
    EventImpactDefinition,
    EvidenceFacet,
    FrozenPair,
    MetricConditionDefinition,
    ResearchEvent,
    ResearchRequirementDefinition,
)
from investment_agent.supplemental_rules.issues import carry_open_issue_refs
from investment_agent.supplemental_rules.metric_evaluation import evaluate_metric

EVALUATOR_VERSION = "supplemental-evaluator-v1"
ConditionStatus = Literal["triggered", "not_triggered", "unknown", "not_applicable"]
EvidenceStatus = Literal["verified", "pending", "conflicted", "not_applicable"]
AssessmentStatus = Literal["clear", "unresolved", "supported", "mitigated", "resolved", "inconclusive"]
SemanticStatus = Literal["mechanical", "pending", "reviewed"]


def _evidence_ref(item: dict[str, Any]) -> ObjectRef | None:
    if item.get("id") is None or item.get("revision") is None:
        return None
    try:
        return ObjectRef.model_validate({
            "kind": "evidence", "id": item["id"], "revision": int(item["revision"]),
        })
    except Exception:
        return None


def _metric_basis_refs(pair: FrozenPair, factors: Iterable[str]) -> tuple[ObjectRef, ...]:
    refs: dict[str, ObjectRef] = {}
    pattern = re.compile(r"^(market_snapshot|financial_snapshot):(\d+)$")

    def visit(value: Any) -> None:
        if isinstance(value, str):
            match = pattern.fullmatch(value)
            if match:
                kind, row_id = match.groups()
                ref = ObjectRef.model_validate({"kind": kind, "id": int(row_id), "revision": 1})
                refs[ref.model_dump_json(exclude_none=True)] = ref
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list | tuple):
            for child in value:
                visit(child)

    for factor in factors:
        raw = pair.subject.factor_values.get(factor)
        if isinstance(raw, dict):
            visit(raw.get("inputs_ref", {}))
    return tuple(refs[key] for key in sorted(refs))


def _requirement_basis_refs(pair: FrozenPair, requirements: Iterable[str]) -> tuple[ObjectRef, ...]:
    refs = {
        ref.model_dump_json(exclude_none=True): ref
        for item in pair.subject.evidence
        if item.get("locator") in set(requirements)
        if item.get("verification") == "verified" and item.get("conflict") == "none"
        if _evidence_read_state(pair, item) == "ok"
        if (ref := _evidence_ref(item)) is not None
    }
    return tuple(refs[key] for key in sorted(refs))


def _evidence_read_state(pair: FrozenPair, item: dict[str, Any]) -> str:
    """Return the exact evidence adapter's frozen read state.

    An immutable EvidenceRecord may remain ``verified`` and conflict-free
    after a later saved correction makes its current ToolResult conflicted.
    The manifest carries that current-limits result without rewriting the
    original evidence payload.
    """
    ref = _evidence_ref(item)
    if ref is None:
        return "missing"
    ref_data = ref.model_dump(mode="json")
    manifest = next((row for row in pair.subject.source_manifest
        if row.get("kind") == "saved_evidence" and row.get("ref") == ref_data), None)
    if manifest is None:
        return "missing"
    status = manifest.get("read_status")
    warnings = manifest.get("read_warnings", ())
    if status == "conflict":
        return "conflicted"
    if status != "ok" or warnings:
        return "pending"
    return "ok"


def _condition_factors(condition: Any) -> tuple[str, ...]:
    factors: set[str] = set()

    def visit(node: Any) -> None:
        if getattr(node, "kind", None) == "factor":
            factors.add(str(node.factor_id))
        else:
            for child in getattr(node, "conditions", ()):
                visit(child)

    visit(condition)
    return tuple(sorted(factors))


def _existing_evidence_status(
    pair: FrozenPair, requirements: Iterable[str]
) -> tuple[EvidenceStatus, tuple[ObjectRef, ...], tuple[str, ...]]:
    evidence = list(pair.subject.evidence)
    refs: list[ObjectRef] = []
    missing: list[str] = []
    conflicted: list[str] = []
    for required in requirements:
        matched = [item for item in evidence if item.get("locator") == required]
        if not matched:
            missing.append(required)
            continue
        read_states = {id(item): _evidence_read_state(pair, item) for item in matched}
        if any(item.get("conflict") != "none" or read_states[id(item)] == "conflicted"
               for item in matched):
            conflicted.append(required)
            continue
        verified = [item for item in matched
                    if item.get("verification") == "verified"
                    and read_states[id(item)] == "ok"]
        if not verified:
            missing.append(required)
            continue
        refs.extend(ref for ref in (_evidence_ref(item) for item in verified) if ref is not None)
    if conflicted:
        return "conflicted", tuple(refs), tuple(f"evidence_conflicted:{item}" for item in conflicted)
    if missing:
        return "pending", tuple(refs), tuple(f"evidence_required:{item}" for item in missing)
    return "verified", tuple(refs), ()


def _facets_not_applicable() -> tuple[EvidenceFacet, EvidenceFacet, EvidenceFacet]:
    facet = EvidenceFacet(status="not_applicable")
    return facet, facet, facet


def _event_fact(event: ResearchEvent | None) -> EvidenceFacet:
    if event is None:
        return EvidenceFacet(status="unknown", unknowns=("selected_research_event_missing",))
    if event.conflict:
        return EvidenceFacet(status="conflicted", unknowns=("event_revision_has_unresolved_conflict",))
    if event.verification == "verified":
        return EvidenceFacet(status="verified", evidence_refs=event.source_refs,
                             summary="Saved event fact is verified; this does not establish company exposure or impact.")
    return EvidenceFacet(status="pending", evidence_refs=event.source_refs,
                         unknowns=(f"event_verification={event.verification}",))


def evaluate_pair(
    pair: FrozenPair,
    *,
    prior_open_issue_refs: tuple[ObjectRef, ...] = (),
) -> EvaluationResult:
    """Produce one typed mechanical result while inheriting all open history."""
    rule = pair.rule
    old_refs = carry_open_issue_refs(
        prior_open_issue_refs or pair.prior_open_issue_refs
    )
    diagnostics = list(pair.diagnostics)
    inactive = pair.rule_state in {"suspended", "retired", "expired"}
    if inactive:
        diagnostics.append(f"rule_state:{pair.rule_state}; no new requirement asserted")
    elif pair.rule_state == "review_due":
        diagnostics.append("rule_review_due")

    if pair.match_status == "not_applicable":
        facts = _facets_not_applicable()
        return EvaluationResult(
            applicability="not_applicable", condition_result="not_applicable",
            evidence_status="not_applicable",
            assessment_status="unresolved" if old_refs else "clear",
            semantic_status="mechanical", effective_effects=(), diagnostics=tuple(diagnostics),
            open_issue_refs=old_refs, closure_refs=(), event_fact=facts[0],
            company_exposure=facts[1], impact_assessment=facts[2], semantic_review_ref=None,
        )
    if pair.match_status == "unknown":
        diagnostics.append("selector_applicability_unknown")
        return EvaluationResult(
            applicability="unknown", condition_result="unknown", evidence_status="pending",
            assessment_status="unresolved" if old_refs else "inconclusive",
            semantic_status="pending", effective_effects=(), diagnostics=tuple(diagnostics),
            open_issue_refs=old_refs, closure_refs=(),
            event_fact=EvidenceFacet(status="unknown", unknowns=("selector_unknown",)),
            company_exposure=EvidenceFacet(status="unknown", unknowns=("selector_unknown",)),
            impact_assessment=EvidenceFacet(status="unknown", unknowns=("selector_unknown",)),
            semantic_review_ref=None,
        )

    condition_result: ConditionStatus = "unknown"
    evidence_status: EvidenceStatus = "pending"
    assessment: AssessmentStatus
    semantic: SemanticStatus
    effective_effects: tuple[EffectiveEffect, ...] = ()
    event_fact, exposure, impact = _facets_not_applicable()

    if isinstance(rule.definition, MetricConditionDefinition):
        metric = evaluate_metric(pair.subject, rule.definition.condition)
        condition_result = metric.status
        diagnostics.extend(metric.unknowns)
        # Factor quality 'ok' describes deterministic computation, not source
        # verification. P2 retains the evidence status as pending unless an
        # independent saved evidence reference says otherwise.
        evidence_status = "pending"
        if condition_result == "not_applicable":
            evidence_status = "not_applicable"
        elif condition_result == "triggered" and not inactive:
            effective_effects = _effects(
                rule.effects,
                "Metric condition was mechanically satisfied by the frozen factor values.",
                _metric_basis_refs(pair, _condition_factors(rule.definition.condition)),
            )
        assessment = _assessment(condition_result, old_refs)
        semantic = "mechanical"

    elif isinstance(rule.definition, ResearchRequirementDefinition):
        status, refs, gaps = _existing_evidence_status(
            pair, rule.definition.required_evidence
        )
        evidence_status = status
        diagnostics.extend(gaps)
        # Evidence presence can be checked mechanically, but completion and
        # falsification criteria are semantic. Never infer a completed answer.
        check_due = pair.subject.as_of >= rule.definition.check_by
        if check_due:
            diagnostics.append("research_requirement_check_by_reached")
        if status == "conflicted" or (status == "pending" and check_due):
            condition_result = "triggered"
            assessment = "unresolved"
            if not inactive:
                effective_effects = _effects(
                    rule.definition.incomplete_effects,
                    "Required source evidence is missing, unverified, or conflicted at check_by.",
                    _requirement_basis_refs(pair, rule.definition.required_evidence),
                )
        else:
            condition_result = "unknown"
            assessment = "unresolved" if old_refs else "inconclusive"
        semantic = "pending"
        event_fact, exposure, impact = _facets_not_applicable()

    elif isinstance(rule.definition, EventImpactDefinition):
        event = pair.selected_event
        event_fact = _event_fact(event)
        if event is None:
            condition_result = "unknown"
            evidence_status = "pending"
            diagnostics.append("event_revision_unavailable_at_cutoff")
        elif event.conflict:
            condition_result = "unknown"
            evidence_status = "conflicted"
        else:
            evidence_status = "verified" if event.verification == "verified" else "pending"
        # Event verification establishes only the event fact. The rule's
        # company exposure and impact conditions remain unknown until there is
        # subject-bound evidence and an authorized semantic assessment.
        condition_result = "unknown"
        exposure = EvidenceFacet(status="unknown",
            unknowns=("company_exposure_requires_subject_bound_evidence",))
        impact = EvidenceFacet(status="unknown",
            unknowns=("impact_assessment_requires_semantic_review",))
        if event is not None and event.effective_from is not None and event.effective_from > pair.subject.as_of:
            diagnostics.append("event_not_yet_effective_at_cutoff")
        if event is not None and event.effective_to is not None and event.effective_to <= pair.subject.as_of:
            diagnostics.append("event_effective_interval_ended")
        assessment = "unresolved" if old_refs else "inconclusive"
        semantic = "pending"
        effective_effects = ()

    else:  # pragma: no cover - strict typed definition discriminator.
        raise TypeError(f"unsupported definition type: {type(rule.definition).__name__}")

    if inactive:
        if condition_result == "triggered":
            diagnostics.append("trigger_observed_but_inactive_rule_not_applied")
        condition_result = "unknown"
        evidence_status = "pending"
        assessment = "unresolved" if old_refs else "inconclusive"
        semantic = "pending"
        effective_effects = ()
    elif pair.rule_state == "review_due":
        if "require_review" not in rule.effects:
            effective_effects = (*effective_effects, EffectiveEffect(
                effect="require_review", basis_refs=(), reason="Rule review deadline has passed."
            ))
        if assessment == "clear":
            assessment = "inconclusive"
        if "review_due" not in diagnostics:
            diagnostics.append("rule_review_due")
    return EvaluationResult(
        applicability="applicable", condition_result=condition_result,
        evidence_status=evidence_status, assessment_status=assessment,
        semantic_status=semantic, effective_effects=effective_effects,
        diagnostics=tuple(dict.fromkeys(diagnostics)), open_issue_refs=old_refs,
        closure_refs=(), event_fact=event_fact, company_exposure=exposure,
        impact_assessment=impact, semantic_review_ref=None,
    )


def _assessment(condition_result: ConditionStatus, prior_refs: tuple[ObjectRef, ...]) -> AssessmentStatus:
    if prior_refs:
        return "unresolved"
    if condition_result == "triggered":
        return "unresolved"
    if condition_result == "not_triggered":
        return "clear"
    return "inconclusive"


def _effects(
    effects: Any, reason: str, basis_refs: tuple[ObjectRef, ...] = (),
) -> tuple[EffectiveEffect, ...]:
    return tuple(EffectiveEffect(effect=effect, basis_refs=basis_refs, reason=reason)
                 for effect in sorted(effects))
