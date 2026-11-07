"""Static bank DTO boundary checks against the frozen synthetic wire example."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.bank_topics.contracts import (
    AnnualPeriod,
    CreateTopicReceipt,
    CreateTopicRequest,
    Decomposition,
    FactBinding,
    SourceTiming,
    TopicExplanation,
    TopicFactRevision,
    TopicFactRowRef,
    TopicRef,
    TopicRevision,
)

FROZEN = json.loads((Path(__file__).parents[1] / "fixtures/next_iterations/contracts/bank.json").read_text())


def fact_input() -> dict[str, Any]:
    return {
        "id": str(uuid4()), "owner_id": "local_owner", "case_id": str(uuid4()),
        "stable_fact_id": "nii_2025", "revision": 1, "metric": "nii", "value": "0",
        "unit": "CNY", "raw_value": "0", "raw_unit": "CNY_million",
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "comparison_period": None, "source_refs": [], "proof": None,
        "verification": "candidate", "unknown_reason": None,
        "timing": {"published_at": None, "published_date": "2026-03-28",
                   "published_precision": "date", "acquired_at": "2026-10-11T15:10:33Z",
                   "checked_at": None, "review_actor": None},
    }


def test_frozen_create_request_and_receipt_parse() -> None:
    request = CreateTopicRequest.model_validate(FROZEN["create"]["request"])
    assert request.period.end.year == 2025
    assert request.model_dump(mode="json") == FROZEN["create"]["request"]
    receipt = CreateTopicReceipt.model_validate(FROZEN["create"]["response"])
    assert receipt.head_ref.kind == "topic_revision"
    assert receipt.coverage == "partial"


@pytest.mark.parametrize(("field", "value"), [
    ("ticker", "601398.SH"), ("template_key", "arbitrary_template"),
    ("accounting_standard", "IFRS"), ("reporting_scope", "standalone"),
    ("currency", "USD"), ("owner_id", "local_owner"), ("schema_version", True),
])
def test_create_rejects_scope_expansion_or_client_owner(field: str, value: Any) -> None:
    data = deepcopy(FROZEN["create"]["request"])
    data[field] = value
    with pytest.raises(ValidationError):
        CreateTopicRequest.model_validate(data)


@pytest.mark.parametrize("period", [
    {"start": "2025-01-01", "end": "2025-06-30"},
    {"start": "2025-04-01", "end": "2025-12-31"},
    {"start": "2025-01-01", "end": "2024-12-31"},
    {"start": "2025-01-01", "end": "2025-12-31", "basis": "point_in_time"},
])
def test_only_full_calendar_annual_periods(period: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        AnnualPeriod.model_validate(period)


def test_fixed_template_rejects_future_initial_case() -> None:
    data = deepcopy(FROZEN["create"]["request"])
    data["period"] = {"start": "2026-01-01", "end": "2026-12-31"}
    with pytest.raises(ValidationError):
        CreateTopicRequest.model_validate(data)


@pytest.mark.parametrize("bad", [1.5, True, "NaN", "Infinity", "-Infinity"])
def test_financial_values_have_no_float_or_nonfinite_route(bad: Any) -> None:
    data = fact_input()
    data["value"] = bad
    with pytest.raises(ValidationError):
        TopicFactRevision.model_validate(data)


def test_decimal_zero_null_and_source_precision_are_distinct() -> None:
    data = fact_input()
    fact = TopicFactRevision.model_validate(data)
    assert fact.model_dump(mode="json")["value"] == "0"
    assert fact.raw_value == "0" and fact.raw_unit == "CNY_million"
    with pytest.raises(ValidationError):
        fact.value = None
    data["value"] = None
    with pytest.raises(ValidationError):
        TopicFactRevision.model_validate(data)
    data["unknown_reason"] = "no_disclosure"
    assert TopicFactRevision.model_validate(data).value is None
    data["value"] = "10"
    with pytest.raises(ValidationError):
        TopicFactRevision.model_validate(data)


@pytest.mark.parametrize(("field", "value"), [
    ("stable_fact_id", "facts.nii"), ("unit", "fraction"), ("raw_unit", "percent"),
    ("verification", "verified"), ("revision", True), ("unexpected", "x"),
])
def test_fact_metadata_is_closed_and_candidate_is_not_verified(field: str, value: Any) -> None:
    data = fact_input()
    data[field] = value
    with pytest.raises(ValidationError):
        TopicFactRevision.model_validate(data)


def test_nim_fraction_preserves_raw_percent_without_second_conversion() -> None:
    data = fact_input()
    data.update(metric="nim", value="0.0187", unit="fraction", raw_value="1.87", raw_unit="percent")
    fact = TopicFactRevision.model_validate(data)
    assert fact.model_dump(mode="json")["value"] == "0.0187"
    assert fact.raw_value == "1.87"


@pytest.mark.parametrize("change", [
    {"published_precision": "timestamp"},
    {"published_at": "2026-03-28T00:00:00Z"},
    {"published_precision": "unknown"},
    {"acquired_at": "2026-10-11T15:10:33"},
    {"checked_at": "2026-11-07T00:00:00Z"},
])
def test_source_timing_does_not_invent_precision_or_check_actor(change: dict[str, Any]) -> None:
    data = fact_input()["timing"]
    data.update(change)
    with pytest.raises(ValidationError):
        SourceTiming.model_validate(data)


def test_source_timestamp_normalizes_to_utc() -> None:
    data = fact_input()["timing"]
    data["acquired_at"] = "2026-10-11T23:10:33+08:00"
    assert SourceTiming.model_validate(data).acquired_at == datetime(2026, 10, 11, 15, 10, 33, tzinfo=UTC)


@pytest.mark.parametrize("change", [
    {"id": 1}, {"id": True}, {"revision": True}, {"revision": 0},
    {"kind": "topic_check", "revision": 2},
    {"kind": "topic_revision", "revision": 2},
    {"kind": "topic_case", "field_path": "facts.nii_2025"},
    {"kind": "topic_revision", "field_path": "__dict__.anything"},
])
def test_topic_reference_namespace_is_closed(change: dict[str, Any]) -> None:
    data = {"kind": "topic_case", "id": str(uuid4()), "revision": 1, **change}
    with pytest.raises(ValidationError):
        TopicRef.model_validate(data)


def test_explanation_has_stable_revision_layer_and_explicit_counter_gap() -> None:
    data: dict[str, Any] = {
        "stable_id": "volume_hypothesis", "revision": 2, "level": "L3", "statement": "待验证的机制假设",
        "support_refs": [], "counter_refs": [], "missing_evidence": ["下一年度规模贡献"],
        "review_state": "unreviewed", "reviewer": None, "reviewed_at": None,
    }
    assert TopicExplanation.model_validate(data).level == "L3"
    data["missing_evidence"] = []
    with pytest.raises(ValidationError):
        TopicExplanation.model_validate(data)
    data["missing_evidence"] = ["缺反证"]
    data["review_state"] = "reviewed"
    with pytest.raises(ValidationError):
        TopicExplanation.model_validate(data)


def test_revision_disallows_duplicate_stable_fact_binding() -> None:
    binding = {"stable_fact_id": "nii_2025", "metric": "nii",
               "source": {"binding_kind": "existing_fact", "source_ref": {"kind": "evidence", "id": str(uuid4()), "revision": 1}}}
    data = {"id": str(uuid4()), "case_id": str(uuid4()), "revision": 2, "question": "净息差下降时为何增长？",
            "period": {"start": "2025-01-01", "end": "2025-12-31"},
            "baseline_period": {"start": "2024-01-01", "end": "2024-12-31"},
            "as_of": "2026-11-07T00:00:00Z", "fact_bindings": [binding, binding],
            "decomposition": None, "explanations": [], "condition_refs": [], "content_hash": "a" * 64,
            "supersedes_ref": None}
    with pytest.raises(ValidationError):
        TopicRevision.model_validate(data)


def test_internal_fact_binding_freezes_row_uuid_and_business_revision() -> None:
    identity = str(uuid4())
    binding = FactBinding.model_validate({"stable_fact_id": "nii_2025", "metric": "nii", "source": {
        "binding_kind": "topic_fact", "fact_ref": {"id": identity, "revision": 3}}})
    assert binding.model_dump(mode="json")["source"]["fact_ref"] == {"id": identity, "revision": 3}
    with pytest.raises(ValidationError):
        TopicFactRowRef.model_validate({"id": identity, "revision": True})
    with pytest.raises(ValidationError):
        TopicFactRowRef.model_validate({"id": identity, "revision": 3, "kind": "topic_fact"})


@pytest.mark.parametrize(("kind", "path", "revision"), [
    ("topic_revision", "facts.nii_2025", 1), ("calculation", None, 1),
    ("evidence", "text", 1), ("peer_annual_snapshot", None, 1),
    ("peer_annual_snapshot", "fields.nim.value", 1), ("peer_annual_snapshot", "fields.nim", 2),
])
def test_existing_fact_binding_cannot_use_explanations_calculations_or_a_topic_cycle(
    kind: str, path: str | None, revision: int,
) -> None:
    with pytest.raises(ValidationError):
        FactBinding.model_validate({"stable_fact_id": "nim", "metric": "nim", "source": {
            "binding_kind": "existing_fact", "source_ref": {"kind": kind, "id": str(uuid4()),
                                                              "revision": revision, "field_path": path}}})


@pytest.mark.parametrize(("kind", "path"), [("evidence", "value"), ("peer_annual_snapshot", "fields.nim")])
def test_existing_fact_binding_is_exact_but_not_a_verified_fact(kind: str, path: str) -> None:
    binding = FactBinding.model_validate({"stable_fact_id": "nim", "metric": "nim", "source": {
        "binding_kind": "existing_fact", "source_ref": {"kind": kind, "id": str(uuid4()),
                                                          "revision": 1, "field_path": path}}})
    assert "verified" not in binding.model_dump_json()


def decomposition_input() -> dict[str, Any]:
    def quantity(value: str | None, source: str, unit: str = "CNY") -> dict[str, Any]:
        return {"value": value, "unit": unit, "unknown_reason": None if value is not None else "missing_disclosure",
                "source_fact_ids": [source]}
    return {"coverage": "complete", "nii_current": quantity("215593000000", "nii_2025"),
            "nii_baseline": quantity("211277000000", "nii_2024"),
            "nim_current": quantity("0.0187", "nim_2025", "fraction"),
            "nim_baseline": quantity("0.0198", "nim_2024", "fraction"),
            "volume_effect": quantity("16798000000", "volume"),
            "rate_effect": quantity("-12482000000", "rate"),
            "disclosed_change": quantity("4316000000", "change"),
            "delta_nii": quantity("4316000000", "nii_2025"),
            "nii_yoy": quantity("0.02042815829456116851337343866", "nii_2025", "fraction"),
            "delta_nim_bp": quantity("-11", "nim_2025", "bp"),
            "residual": quantity("0", "change"), "tolerance": quantity("2000000", "change"),
            "rounding_consistent": True}


def test_decomposition_has_typed_decimal_quantities_and_explicit_unknowns() -> None:
    data = decomposition_input()
    result = Decomposition.model_validate(data)
    assert result.model_dump(mode="json")["rate_effect"]["value"] == "-12482000000"
    change: dict[str, Any]
    for change in ({"value": 1.5}, {"unit": "fraction"}, {"source_fact_ids": []}, {"value": None}):
        changed = deepcopy(data)
        changed["volume_effect"].update(change)
        with pytest.raises(ValidationError):
            Decomposition.model_validate(changed)
    data["volume_effect"].update(value=None, unknown_reason="missing_disclosure")
    with pytest.raises(ValidationError):
        Decomposition.model_validate(data)
    data.update(coverage="partial", rounding_consistent=None)
    assert Decomposition.model_validate(data).volume_effect.value is None
    data["rate_effect"].update(value=None, unknown_reason="missing_disclosure")
    with pytest.raises(ValidationError):
        Decomposition.model_validate(data)
    data["coverage"] = "unavailable"
    assert Decomposition.model_validate(data).coverage == "unavailable"


def test_parent_decomposition_can_only_reference_its_frozen_fact_bindings() -> None:
    data = {"id": str(uuid4()), "case_id": str(uuid4()), "revision": 1, "question": "为何增长？",
            "period": {"start": "2025-01-01", "end": "2025-12-31"},
            "baseline_period": {"start": "2024-01-01", "end": "2024-12-31"},
            "as_of": "2026-11-07T00:00:00Z", "fact_bindings": [], "decomposition": decomposition_input(),
            "explanations": [], "condition_refs": [], "content_hash": "a" * 64, "supersedes_ref": None}
    with pytest.raises(ValidationError, match="frozen parent"):
        TopicRevision.model_validate(data)
