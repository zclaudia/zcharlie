from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.supplemental_rules.contracts import (
    FrozenPair,
    FrozenSubject,
    ListingSelector,
    MetricCondition,
    MetricConditionDefinition,
    MetricLeaf,
    ResearchEvent,
    ResearchRequirementDefinition,
    RuleDefinition,
)
from investment_agent.supplemental_rules.evaluation import evaluate_pair
from investment_agent.supplemental_rules.issues import carry_open_issue_refs, deduplicate_issue_rows
from investment_agent.supplemental_rules.metric_evaluation import evaluate_metric

NOW = datetime(2026, 10, 16, 4, tzinfo=UTC)
LISTING = ObjectRef(kind="listing", id=123, revision=1)


def _subject(factors: dict, *, evidence=()) -> FrozenSubject:
    return FrozenSubject(
        listing_ref=LISTING, company_id=12, ticker="600001.SH", name="Example",
        as_of=NOW, input_status="ready",
        selector_inputs={"factor_metadata": [{"name": key, "unit": "ratio"} for key in factors]},
        factor_values=factors, evidence=tuple(evidence), source_manifest=(),
    )


def _factor(value, quality="ok", flags=(), inputs_ref=None):
    return {"value": value, "quality": quality, "version": "v1", "flags": list(flags),
            "inputs_ref": inputs_ref or {}}


def test_three_value_all_any_and_all_not_applicable_semantics() -> None:
    subject = _subject({"known": _factor(0.2), "missing": _factor(None, "missing"),
                        "na": _factor(None, "not_applicable")})
    def gt(name: str, threshold: str) -> MetricLeaf:
        return MetricLeaf(factor_id=name, operator=">", value=Decimal(threshold))
    all_false_unknown = evaluate_metric(subject, MetricCondition(kind="all", conditions=(
        gt("known", "0.5"), gt("missing", "0.1"))))
    assert all_false_unknown.status == "not_triggered"
    any_true_unknown = evaluate_metric(subject, MetricCondition(kind="any", conditions=(
        gt("known", "0.1"), gt("missing", "0.1"))))
    assert any_true_unknown.status == "triggered"
    all_na = evaluate_metric(subject, MetricCondition(kind="all", conditions=(
        gt("na", "0.1"),)))
    assert all_na.status == "not_applicable"


def test_nested_metric_groups_keep_expected_all_any_truth() -> None:
    subject = _subject({"low": _factor(0.1), "high": _factor(0.9),
                        "missing": _factor(None, "missing"), "na": _factor(None, "not_applicable")})
    low_true = MetricLeaf(factor_id="low", operator=">", value=Decimal("0"))
    low_false = MetricLeaf(factor_id="low", operator=">", value=Decimal("0.5"))
    high_true = MetricLeaf(factor_id="high", operator=">", value=Decimal("0.5"))
    high_false = MetricLeaf(factor_id="high", operator=">", value=Decimal("1"))
    missing = MetricLeaf(factor_id="missing", operator=">", value=Decimal("0"))
    na = MetricLeaf(factor_id="na", operator=">", value=Decimal("0"))
    nested_false = MetricCondition(kind="all", conditions=(
        MetricCondition(kind="any", conditions=(low_false, low_false)), high_true))
    assert evaluate_metric(subject, nested_false).status == "not_triggered"
    nested_true = MetricCondition(kind="any", conditions=(
        MetricCondition(kind="all", conditions=(low_true, high_true)), high_false))
    assert evaluate_metric(subject, nested_true).status == "triggered"
    nested_unknown = MetricCondition(kind="all", conditions=(
        MetricCondition(kind="all", conditions=(missing, na)), high_true))
    assert evaluate_metric(subject, nested_unknown).status == "unknown"
    nested_na = MetricCondition(kind="any", conditions=(
        MetricCondition(kind="all", conditions=(na, na)),))
    assert evaluate_metric(subject, nested_na).status == "not_applicable"


def test_float_boundary_conflict_stale_and_fallback_are_unknown() -> None:
    subject = _subject({
        "boundary": _factor(0.1),
        "conflicted": _factor(1.0, inputs_ref={"financial": [{"sources": {
            "roe": {"verification": "conflict", "conflicts": [{"field": "roe"}]}}}]}),
        "stale": _factor(1.0, flags=("stale_price",)),
        "fallback_na": _factor(None, "not_applicable", flags=("industry_fallback",)),
    })
    boundary = evaluate_metric(subject, MetricCondition(kind="all", conditions=(
        MetricLeaf(factor_id="boundary", operator=">=", value=Decimal("0.1")),)))
    assert boundary.status == "unknown"
    for name in ("conflicted", "stale", "fallback_na"):
        result = evaluate_metric(subject, MetricCondition(kind="all", conditions=(
            MetricLeaf(factor_id=name, operator=">", value=Decimal("0")),)))
        assert result.status == "unknown", name


def _rule(definition, rule_type: str, *, event_ref=None) -> RuleDefinition:
    return RuleDefinition(
        id=uuid4(), revision=1, name="test", rationale="test purpose", source_refs=(),
        selector=ListingSelector(kind="listing", listing_ref=LISTING),
        contexts=frozenset({"company_research"}), rule_type=rule_type,
        definition=definition, dimensions=frozenset({"downside_counter_case"}), effects=frozenset({"require_review"}),
        severity="warning", materiality="test", event_ref=event_ref,
        valid_from=NOW - timedelta(days=2), review_due_at=NOW + timedelta(days=2),
        release_requirements=("review exact evidence",),
    )


def _pair(rule, subject, *, state="active", prior=(), event=None) -> FrozenPair:
    return FrozenPair(
        ordinal=0, listing_ref=LISTING, context="company_research",
        rule_ref=ObjectRef(kind="supplemental_rule", id=rule.id, revision=rule.revision),
        rule=rule, subject=subject, selected_event=event,
        prior_live_application_ref=prior[0] if prior else None,
        prior_open_issue_refs=tuple(prior), input_hash="a" * 64, rule_state=state,
        match_status="applicable", matching_manifest_hash="b" * 64,
    )


def test_suspended_trigger_cannot_clear_prior_issue_or_assert_new_effect() -> None:
    definition = MetricConditionDefinition(kind="metric_condition", condition=MetricCondition(
        kind="all", conditions=(MetricLeaf(factor_id="known", operator=">", value=Decimal("0.5")),)))
    rule = _rule(definition, "metric_condition")
    open_ref = ObjectRef(kind="rule_application", id=uuid4(), revision=1)
    result = evaluate_pair(_pair(rule, _subject({"known": _factor(1.0)}), state="suspended", prior=(open_ref,)))
    assert result.condition_result == "unknown"
    assert result.open_issue_refs == (open_ref,)
    assert result.closure_refs == ()
    assert result.effective_effects == ()
    assert result.semantic_review_ref is None


def test_user_report_is_pending_requirement_and_cannot_satisfy_semantics() -> None:
    definition = ResearchRequirementDefinition(
        kind="research_requirement", question="Is the issue resolved?",
        required_evidence=("resolution",), completion_criteria="a decision is documented",
        falsification_criteria="an adverse ruling remains", check_by=NOW - timedelta(days=1),
        incomplete_effects=("require_review",),
    )
    rule = _rule(definition, "research_requirement")
    user_report = {"id": str(uuid4()), "revision": 1, "locator": "resolution",
                   "source_kind": "user_report", "verification": "user_report", "conflict": "none"}
    result = evaluate_pair(_pair(rule, _subject({}, evidence=(user_report,))))
    assert result.condition_result == "triggered"
    assert result.evidence_status == "pending"
    assert result.assessment_status == "unresolved"
    assert result.semantic_status == "pending"
    assert result.semantic_review_ref is None
    assert [effect.effect for effect in result.effective_effects] == ["require_review"]


def test_verified_event_fact_does_not_claim_exposure_or_reviewed_impact() -> None:
    event_ref = ObjectRef(kind="research_event", id=uuid4(), revision=1)
    event = ResearchEvent(
        id=event_ref.id, revision=1, owner_id="local_owner", event_key="policy-x", title="Policy",
        event_type="policy", jurisdiction="CN", affected_products=(), affected_subjects=(),
        policy_stage="effective",
        published_at=NOW - timedelta(days=1), effective_from=NOW - timedelta(days=1),
        acquired_at=NOW, available_from=NOW, source_refs=(), verification="verified",
        conflict=False,
    )
    from investment_agent.supplemental_rules.contracts import EventImpactDefinition
    definition = EventImpactDefinition(
        kind="event_impact", event_ref=event_ref, affected_scope=("product x",),
        exposure_evidence=("operating data",), impact_path="path", release_requirements=("repeal",),
    )
    rule = _rule(definition, "event_impact", event_ref=event_ref)
    result = evaluate_pair(_pair(rule, _subject({}), event=event))
    assert result.event_fact.status == "verified"
    assert result.condition_result == "unknown"
    assert result.company_exposure.status == "unknown"
    assert result.impact_assessment.status == "unknown"
    assert result.semantic_status == "pending"
    assert result.assessment_status == "inconclusive"
    assert result.semantic_review_ref is None


def test_open_issue_refs_are_stable_and_display_dedup_preserves_measures() -> None:
    old_ref = ObjectRef(kind="rule_application", id=uuid4(), revision=1)
    newer_ref = ObjectRef(kind="rule_application", id=uuid4(), revision=1)
    assert carry_open_issue_refs((old_ref,), (newer_ref, old_ref)) == carry_open_issue_refs((newer_ref, old_ref))
    same_a = {"company_id": 7, "issue_key": "x", "dimension": "risk",
              "application_refs": [old_ref.model_dump(mode="json")], "effects": ["review"]}
    same_b = {"company_id": 7, "issue_key": "x", "dimension": "risk",
              "application_refs": [newer_ref.model_dump(mode="json")], "effects": ["limit"]}
    different = {"company_id": 7, "issue_key": "different-measure", "dimension": "risk",
                 "application_refs": [], "effects": ["review"]}
    grouped = deduplicate_issue_rows((same_a, same_b, different))
    assert len(grouped) == 2
    merged = next(row for row in grouped if row["issue_key"] == "x")
    assert len(merged["application_refs"]) == 2
    assert set(merged["effects"]) == {"review", "limit"}
    assert all("severity" not in row for row in grouped)
