"""Synthetic fixtures for P2/P3 contract and capacity tests; no service imports."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.supplemental_rules.canonical import canonical_hash, stable_series_key
from investment_agent.supplemental_rules.contracts import (
    BindingRuleEntry,
    EvaluationResult,
    EvidenceFacet,
    FrozenPair,
    FrozenSubject,
    ResearchRuleBinding,
    RuleApplication,
    RuleCoverageManifest,
    RuleDefinition,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "supplemental_rules_v1" / "expected.json"

BINDING_TASK_ID = UUID("22222222-2222-4222-8222-222222222222")
BINDING_LISTING = ObjectRef(kind="listing", id=7, revision=1)
BINDING_METHOD = ObjectRef(kind="method", id=UUID("11111111-1111-4111-8111-111111111111"), revision=1)
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
HASH = "a" * 64


def _canonical_hash_minus(data: dict[str, Any], field: str) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != field})


def synthetic_binding_entry() -> BindingRuleEntry:
    return BindingRuleEntry(
        rule_ref=synthetic_rule().reference,
        semantic_hash=HASH,
        selector_hash="b" * 64,
    )


def synthetic_binding(
    *, rule_entries: tuple[BindingRuleEntry, ...] = (), task_id: UUID = BINDING_TASK_ID
) -> ResearchRuleBinding:
    """One valid admission binding; an empty rule set is legal and explicit."""

    data: dict[str, Any] = {
        "schema_version": 1,
        "id": uuid4(),
        "revision": 1,
        "owner_id": "local_owner",
        "task_ref": ObjectRef(kind="task", id=task_id, revision=1),
        "listing_ref": BINDING_LISTING,
        "research_kind": "company_research",
        "method_ref": BINDING_METHOD,
        "admission_as_of": NOW,
        "rules_generation": 1,
        "events_generation": 0,
        "rule_set": rule_entries,
        "rule_set_hash": canonical_hash(rule_entries),
        "matching_manifest_hash": None,
        "events_visible_until": None,
        "events_visible_unknown": False,
        "input_hash": HASH,
        "created_at": NOW + timedelta(minutes=1),
        "content_hash": "pending",
    }
    data["content_hash"] = _canonical_hash_minus(data, "content_hash")
    return ResearchRuleBinding.model_validate(data)


def synthetic_coverage_manifest(
    *, stage: str = "prepared", rule_entries: tuple[Any, ...] = (), **overrides: Any
) -> RuleCoverageManifest:
    """Build a prepared manifest; published needs its stage fields via overrides."""

    data: dict[str, Any] = {
        "schema_version": 1,
        "id": uuid4(),
        "revision": 1,
        "stage": stage,
        "owner_id": "local_owner",
        "task_ref": ObjectRef(kind="task", id=BINDING_TASK_ID, revision=1),
        "task_id": BINDING_TASK_ID,
        "binding_ref": synthetic_binding().reference,
        "listing_ref": BINDING_LISTING,
        "case_ref": None,
        "admission_as_of": NOW,
        "effective_cutoff": NOW + timedelta(minutes=30),
        "rule_entries": rule_entries,
        "open_issue_refs": (),
        "closure_proposals": (),
        "scan_capped": False,
        "scan_known_total": None,
        "scan_uncovered_note": None,
        "material_set_hash": None,
        "input_hash": HASH,
        "created_at": NOW + timedelta(hours=1),
        "manifest_hash": "pending",
        "prepared_ref": None,
        "prepared_manifest_hash": None,
        "semantic_review_ref": None,
        "closure_application_refs": (),
        "thesis_ref": None,
        "recommendation_ref": None,
        "limits_recheck": (),
        "committed_at": None,
    }
    data.update(overrides)
    data["manifest_hash"] = _canonical_hash_minus(data, "manifest_hash")
    return RuleCoverageManifest.model_validate(data)


def expected_cases() -> dict[str, dict[str, Any]]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {case["id"]: case["expected"] for case in document["cases"]}


CLOSURE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "research_closure_v1" / "expected.json"


def closure_expectations() -> dict[str, Any]:
    """Independent P3 freeze expectations; never imports a service."""

    return json.loads(CLOSURE_FIXTURE.read_text(encoding="utf-8"))


def synthetic_manifest_stress_shape(*, pair_count: int = 2000) -> dict[str, Any]:
    """Return a deterministic stress shape; this is not a valid/complete P2 DTO.

    The padding deliberately exercises UTF-8 serialization size. It is not an
    entropy model and does not establish the largest legal manifest.
    """

    if not 0 <= pair_count <= 2000:
        raise ValueError("pair_count must be in 0..2000")
    return {
        "schema_version": 1,
        "owner_id": "local_owner",
        "as_of": "2026-10-16T00:00:00+00:00",
        "evaluated_at": "2026-10-16T00:00:01+00:00",
        "rules_generation": 1,
        "events_generation": 1,
        "listing_refs": [{"kind": "listing", "id": i + 1, "revision": 1} for i in range(min(100, pair_count))],
        "contexts": ["company_research"],
        "pairs": [
            {
                "ordinal": i,
                "listing_ref": {"kind": "listing", "id": (i % 100) + 1, "revision": 1},
                "context": "company_research",
                "rule_ref": {"kind": "supplemental_rule", "id": f"{i:08x}-0000-4000-8000-000000000000", "revision": 1},
                "subject": {"ticker": f"T{i:04d}", "company_id": i + 1, "selector_inputs": {"source": "synthetic", "captured_at": "2026-10-16T00:00:00+00:00"}, "factor_values": {}, "evidence": [], "source_manifest": []},
                "match_status": "unknown",
                "input_hash": f"{i:064x}",
                "matching_manifest_hash": f"{i:064x}",
                "padding": "x" * 1500,
            }
            for i in range(pair_count)
        ],
        "scan_diagnostics": ["synthetic upper bound"],
    }


def synthetic_rule() -> RuleDefinition:
    """One deterministic synthetic rule definition for application fixtures."""

    return RuleDefinition(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        revision=1,
        name="Synthetic binding rule",
        rationale="Synthetic fixture rule",
        source_refs=(),
        selector={"kind": "listing", "listing_ref": {"kind": "listing", "id": 7, "revision": 1}},
        contexts={"company_research"},
        rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "revenue_growth", "operator": ">=", "value": "0.10", "unit": "ratio"}
        ]}},
        dimensions={"business_demand"},
        effects={"require_review"},
        severity="warning",
        materiality="Synthetic materiality",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        review_due_at=datetime(2027, 1, 1, tzinfo=UTC),
        release_requirements=("Review source data",),
    )


def synthetic_application(
    *, overrides: dict[str, Any] | None = None, result_overrides: dict[str, Any] | None = None
) -> RuleApplication:
    """Build one valid mechanical RuleApplication; overrides keep hash coherence."""

    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    rule = synthetic_rule()
    listing_ref = ObjectRef(kind="listing", id=7, revision=1)
    subject = FrozenSubject(
        listing_ref=listing_ref, company_id=70, ticker="600007", name="Synthetic",
        as_of=now, input_status="partial", selector_inputs={}, factor_values={},
        evidence=(), source_manifest=(), diagnostics=("synthetic missing fact",),
    )
    pair = FrozenPair(
        ordinal=0, listing_ref=listing_ref, context="company_research", rule_ref=rule.reference,
        rule=rule, subject=subject, selected_event=None, prior_live_application_ref=None,
        prior_open_issue_refs=(), prior_issue_keys=(), prior_input_hash=None,
        input_hash="a" * 64, rule_state="active", match_status="applicable",
        matching_manifest_hash="b" * 64, diagnostics=(),
    )
    facet = EvidenceFacet(status="pending", summary=None, evidence_refs=(), unknowns=("not checked",), claims=())
    result = EvaluationResult(
        applicability="applicable", condition_result="triggered", evidence_status="pending",
        assessment_status="unresolved", semantic_status="mechanical", effective_effects=(),
        diagnostics=("synthetic",), open_issue_refs=(), closure_refs=(),
        event_fact=facet, company_exposure=facet, impact_assessment=facet,
    )
    if result_overrides:
        # model_copy does not coerce; re-validate so nested refs become typed.
        result = EvaluationResult.model_validate(
            result.model_copy(update=result_overrides).model_dump(mode="python")
        )
    values: dict[str, Any] = {
        "id": uuid4(),
        "operation_id": uuid4(),
        "owner_id": "local_owner",
        "listing_ref": listing_ref,
        "company_id": 70,
        "context": "company_research",
        "rule_ref": rule.reference,
        "rules_generation": 1,
        "events_generation": 0,
        "matching_manifest_hash": "b" * 64,
        "event_ref": None,
        "as_of": now,
        "evaluated_at": now + timedelta(seconds=1),
        "valid_until": rule.valid_until,
        "review_due_at": rule.review_due_at,
        "matcher_version": "test-matcher-1",
        "evaluator_version": "test-evaluator-1",
        "dependencies": (),
        "series_key": stable_series_key("local_owner", 7, rule.id, "company_research"),
        "application_sequence": 1,
        "application_mode": "live",
        "origin_ref": None,
        "created_at": now + timedelta(seconds=1),
        "actor": "operator",
        "previous_application_ref": None,
        "frozen_pair": pair,
        "source_manifest_hash": "c" * 64,
        "evaluation_purpose": "coverage",
        "applicability": "applicable",
        "condition_result": "triggered",
        "evidence_status": "pending",
        "assessment_status": "unresolved",
        "semantic_status": "mechanical",
        "semantic_review_ref": None,
        "effective_effects": (),
        "open_issue_refs": (),
        "closure_refs": (),
        "result": result,
        "payload": {"synthetic": True},
    }
    if overrides:
        values.update(overrides)
    values["input_hash"] = canonical_hash({
        "pair": values["frozen_pair"],
        "manifest_hash": values["source_manifest_hash"],
        "mode": values["application_mode"],
        "purpose": values["evaluation_purpose"],
    })
    return RuleApplication.model_validate(values)
