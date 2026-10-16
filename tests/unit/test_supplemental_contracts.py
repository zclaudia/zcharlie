from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_agent.supplemental_rules.canonical import (
    canonical_hash,
    mutation_request_hash,
    rule_content_hash,
    rule_semantic_hash,
    stable_series_key,
)
from investment_agent.supplemental_rules.contracts import (
    CategorySelector,
    EvaluationRequest,
    FrozenManifest,
    MetricCondition,
    MetricLeaf,
    RuleDefinition,
)
from tests.support.supplemental_rules import (
    FIXTURE,
    expected_cases,
    synthetic_manifest_stress_shape,
)


def _rule(*, revision: int = 1, name: str = "Policy") -> RuleDefinition:
    return RuleDefinition(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        revision=revision,
        name=name,
        rationale="Synthetic rule fixture",
        source_refs=(),
        selector={"kind": "company", "company_id": 7},
        contexts={"company_research"},
        rule_type="metric_condition",
        definition={
            "kind": "metric_condition",
            "condition": {"kind": "all", "conditions": [
                {"kind": "factor", "factor_id": "revenue_growth", "operator": ">=", "value": "0.10", "unit": "ratio"}
            ]},
        },
        dimensions={"business_demand"},
        effects={"require_review"},
        severity="warning",
        materiality="Synthetic materiality",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        review_due_at=datetime(2027, 1, 1, tzinfo=UTC),
        release_requirements=("Review source data",),
    )


def test_rule_definition_is_strict_and_keeps_decimal_thresholds() -> None:
    rule = _rule()
    assert rule.definition.kind == rule.rule_type
    assert rule.definition.condition.conditions[0].value == Decimal("0.10")
    assert rule.reference.kind == "supplemental_rule"
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate({**rule.model_dump(mode="json"), "arbitrary_code": "x"})
    with pytest.raises(ValidationError):
        MetricLeaf(factor_id="x", operator=">=", value=0.1)
    with pytest.raises(ValidationError):
        MetricLeaf(factor_id="x", operator="gte", value="1")


def test_selector_and_metric_bounds_reject_empty_or_deep_shapes() -> None:
    with pytest.raises(ValidationError):
        CategorySelector.model_validate({"kind": "category", "expression": {"operator": "all", "terms": []}})
    with pytest.raises(ValidationError):
        MetricCondition.model_validate({"kind": "all", "conditions": []})
    too_deep = {"kind": "all", "conditions": [{"kind": "any", "conditions": [{"kind": "all", "conditions": [{"kind": "any", "conditions": [{"kind": "all", "conditions": [{"kind": "factor", "factor_id": "x", "operator": ">", "value": "1"}]}]}]}]}]}
    with pytest.raises(ValidationError):
        MetricCondition.model_validate(too_deep)
    bad = {"kind": "category", "expression": {"operator": "all", "terms": [
        {"operator": "all", "terms": [{"operator": "any", "terms": [{"operator": "all", "terms": [
            {"kind": "product", "members": ["synthetic"], "complete": True, "source_ref": {"kind": "evidence", "id": "11111111-1111-4111-8111-111111111111", "revision": 1}}
        ]}]}]}
    ]}}
    with pytest.raises(ValidationError):
        CategorySelector.model_validate(bad)


def test_canonical_hashes_keep_decimal_and_binary_float_distinct() -> None:
    assert canonical_hash({"value": Decimal("0.10")}) != canonical_hash({"value": 0.1})
    assert canonical_hash({"value": 0.1}) != canonical_hash({"value": 0.10000000000000002})
    assert stable_series_key("local_owner", 7, "11111111-1111-4111-8111-111111111111", "company_research") == stable_series_key(
        "local_owner", "7", UUID("11111111-1111-4111-8111-111111111111"), "company_research"
    )


def test_series_identity_excludes_rule_revision_issue_and_purpose() -> None:
    first = _rule(revision=1)
    second = _rule(revision=2, name="Renamed")
    assert first.reference.id == second.reference.id
    assert stable_series_key("local_owner", 7, first.id, "company_research") == stable_series_key(
        "local_owner", 7, second.id, "company_research"
    )
    assert rule_content_hash(first) != rule_content_hash(second)
    assert rule_semantic_hash(first) == rule_semantic_hash(second)


def test_mutation_digest_binds_command_target_and_body() -> None:
    body = {"idempotency_key": "11111111-1111-4111-8111-111111111111", "value": "1"}
    digest = mutation_request_hash("rule_revision.create.v1", UUID(body["idempotency_key"]), body)
    assert digest == mutation_request_hash("rule_revision.create.v1", body["idempotency_key"], body)
    assert digest != mutation_request_hash("rule_revision.create.v1", "22222222-2222-4222-8222-222222222222", body)
    assert digest != mutation_request_hash("rule_revision.update.v1", body["idempotency_key"], body)
    assert digest != mutation_request_hash("rule_revision.create.v1", body["idempotency_key"], {**body, "value": "2"})


def test_g0_fixture_covers_cases_and_records_synthetic_serialization_size() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(expected_cases()) == 24
    assert document["capacity_profile"]["declared_pairs"] == 2000
    payload_size = len(json.dumps(synthetic_manifest_stress_shape(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    assert payload_size == 4_183_886


def test_internal_manifest_accepts_zero_targets_but_public_request_does_not() -> None:
    manifest = FrozenManifest(
        owner_id="local_owner", listing_refs=(), contexts={"company_research"},
        as_of=datetime(2026, 10, 16, tzinfo=UTC), evaluated_at=datetime(2026, 10, 16, tzinfo=UTC),
        rules_generation=0, events_generation=0, matcher_version="m1", evaluator_version="e1",
        pairs=(), unbound_events=(), scope_semantic_hash="a" * 64,
        input_hash="b" * 64, manifest_hash="c" * 64,
    )
    assert manifest.listing_refs == () and manifest.pairs == ()
    with pytest.raises(ValidationError):
        EvaluationRequest(
            idempotency_key=UUID("11111111-1111-4111-8111-111111111111"), listing_refs=(),
            contexts={"company_research"}, mode="live",
        )
    with pytest.raises(ValidationError):
        FrozenManifest.model_validate({**manifest.model_dump(mode="python"), "listing_refs": (), "pairs": ({"bad": True},)})
