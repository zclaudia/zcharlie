"""Finite predicates and immutable check input contracts; no evaluator or queue."""

import json
from copy import deepcopy
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.bank_topics.contracts import (
    CheckResult,
    CheckRevision,
    CheckRunView,
    ConditionHead,
    ConditionPredicate,
    CreateConditionRequest,
    ExecutionManifest,
    LeafResult,
    SemanticInputs,
    TopicObservationPayload,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/next_iterations/contracts"
FROZEN = json.loads((FIXTURE / "bank.json").read_text())
EVENTS = json.loads((FIXTURE / "events.valid.json").read_text())["events"]
TOPIC_EVENT = next(event for event in EVENTS if event["event_type"] == "topic.observation_changed")


def semantic_input() -> dict[str, Any]:
    payload = TOPIC_EVENT["payload"]
    return {
        "subject": payload["subject"], "topic_revision_ref": payload["topic_ref"],
        "condition_ref": payload["condition_ref"], "evaluator_version": "bank-condition/1",
        "target_period": payload["target_period"], "baseline_period": payload["baseline_period"],
        "scope": {"reporting_scope": "consolidated", "accounting_standard": "CAS", "currency": "CNY"},
        "exact_source_refs": [], "values": FROZEN["check"]["values"],
        "availability": "awaiting_source", "due_phase": "due",
    }


def test_frozen_condition_request_retains_decimal_and_strict_boolean() -> None:
    condition = CreateConditionRequest.model_validate(FROZEN["condition"]["request"])
    wire = condition.model_dump(mode="json")
    assert wire["predicate"]["conditions"][0]["threshold"] == "0"
    assert wire["predicate"]["conditions"][1]["threshold"] is True
    assert condition.review_due_at.tzinfo is UTC
    assert condition.topic_revision_ref.revision == 1


@pytest.mark.parametrize(("field", "value"), [
    ("activate", 1), ("activate", "true"), ("expected_revision", True),
    ("dsl_version", "bank-condition/99"), ("target_selector", "latest_report"),
    ("review_due_at", "2027-04-30T15:59:59"), ("expires_at", "2027-01-01T00:00:00Z"),
    ("timezone", "UTC"), ("owner_id", "local_owner"), ("control_revision", 1),
])
def test_condition_request_rejects_unbounded_or_ambiguous_inputs(field: str, value: Any) -> None:
    data = deepcopy(FROZEN["condition"]["request"])
    data[field] = value
    with pytest.raises(ValidationError):
        CreateConditionRequest.model_validate(data)


@pytest.mark.parametrize("leaf", [
    {"metric": "nii_yoy", "operator": "eq", "threshold": "0", "unit": "fraction"},
    {"metric": "nii_yoy", "operator": "gt", "threshold": True, "unit": "fraction"},
    {"metric": "nii_yoy", "operator": "gt", "threshold": 0.1, "unit": "fraction"},
    {"metric": "nii_yoy", "operator": "gt", "threshold": "NaN", "unit": "fraction"},
    {"metric": "nim_delta_bp", "operator": "gte", "threshold": "0", "unit": "fraction"},
    {"metric": "nii_rate_effect", "operator": "lte", "threshold": "0", "unit": "percent"},
    {"metric": "nii_scale_covers_rate", "operator": "gt", "threshold": True, "unit": "boolean"},
    {"metric": "nii_scale_covers_rate", "operator": "eq", "threshold": 1, "unit": "boolean"},
    {"metric": "nii_scale_covers_rate", "operator": "eq", "threshold": "true", "unit": "boolean"},
    {"metric": "roe", "operator": "gt", "threshold": "0", "unit": "fraction"},
    {"metric": "nii_yoy", "operator": "gt", "threshold": "0", "unit": "fraction", "script": "x"},
    {"kind": "any", "conditions": []},
    {"metric": "nii_yoy; DROP TABLE topic_case", "operator": "gt", "threshold": "0", "unit": "fraction"},
])
def test_predicates_reject_unknown_metrics_nested_roots_scripts_and_wrong_types(leaf: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ConditionPredicate.model_validate({"kind": "all", "conditions": [leaf]})


@pytest.mark.parametrize("count", [0, 6])
def test_predicate_has_one_root_and_one_to_five_leaves(count: int) -> None:
    leaf = FROZEN["condition"]["request"]["predicate"]["conditions"][0]
    with pytest.raises(ValidationError):
        ConditionPredicate.model_validate({"kind": "all", "conditions": [leaf] * count})


@pytest.mark.parametrize(("metric", "unit"), [
    ("nii_yoy", "fraction"), ("nim_delta_bp", "bp"),
    ("nii_volume_effect", "CNY"), ("nii_rate_effect", "CNY"),
])
def test_numeric_metric_units_are_finite(metric: str, unit: str) -> None:
    parsed = ConditionPredicate.model_validate({"kind": "any", "conditions": [
        {"metric": metric, "operator": "gte", "threshold": "-1.50", "unit": unit},
    ]})
    assert parsed.model_dump(mode="json")["conditions"][0]["threshold"] == "-1.50"


def test_check_outcome_is_separate_from_run_and_leaf_states() -> None:
    result = CheckResult.model_validate(FROZEN["check"])
    assert result.outcome == "insufficient_evidence"
    for outcome in ("queued", "running", "failed", "cancelled", "not_applicable"):
        data = {**FROZEN["check"], "outcome": outcome}
        with pytest.raises(ValidationError):
            CheckResult.model_validate(data)
    head = ConditionHead.model_validate({
        "condition_id": str(uuid4()), "head_revision": 2, "control_revision": 7,
        "state": "armed", "next_due_at": None, "active_run_ref": None, "last_check_ref": None,
    })
    assert head.head_revision != head.control_revision


def test_cancelled_run_is_a_nonretryable_view_without_a_financial_result() -> None:
    data: dict[str, Any] = {
        "run_ref": {"kind": "pipeline_run", "id": 19, "revision": 1},
        "state": "cancelled", "check_ref": None, "reason": "cancelled", "retryable": False,
    }
    assert CheckRunView.model_validate(data).state == "cancelled"
    for change in ({"state": "met"}, {"retryable": True}, {"reason": None},
                   {"check_ref": FROZEN["check"]["check_ref"]}):
        with pytest.raises(ValidationError):
            CheckRunView.model_validate({**data, **change})


@pytest.mark.parametrize("change", [
    {"execution_manifest_hash": "bad"}, {"semantic_input_hash": "A" * 64},
    {"subject": {"kind": "account", "account_id": str(uuid4())}},
    {"as_of": "2027-04-30T16:00:00"},
    {"values": {"nii_yoy": False}}, {"values": {"nii_scale_covers_rate": "true"}},
])
def test_check_result_has_closed_values_hashes_and_aware_time(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CheckResult.model_validate({**FROZEN["check"], **change})


def test_no_rate_drag_is_only_a_leaf_explanation() -> None:
    leaf = LeafResult.model_validate({"metric": "nii_scale_covers_rate", "status": "not_applicable",
                                      "value": None, "unknown_reason": "no_rate_drag"})
    assert leaf.value is None
    for change in ({"value": False}, {"unknown_reason": None}, {"metric": "nii_yoy"}):
        with pytest.raises(ValidationError):
            LeafResult.model_validate({**leaf.model_dump(), **change})


def test_semantic_manifest_excludes_scan_clock_but_execution_retains_it() -> None:
    inputs = semantic_input()
    semantic = SemanticInputs.model_validate(inputs)
    assert semantic.due_phase == "due"
    assert "as_of" not in semantic.model_dump()
    with pytest.raises(ValidationError):
        SemanticInputs.model_validate({**inputs, "as_of": "2027-04-30T16:00:00Z"})
    execution = ExecutionManifest.model_validate({"semantic_inputs": inputs, "as_of": "2027-05-01T00:00:00+08:00"})
    assert execution.as_of.isoformat() == "2027-04-30T16:00:00+00:00"
    assert execution.semantic_inputs == semantic


def test_check_revision_rejects_subject_and_condition_drift() -> None:
    inputs = semantic_input()
    # The independent event example is condition v2; use the check's v1 here.
    inputs["condition_ref"] = FROZEN["check"]["condition_ref"]
    data = {"owner_id": "local_owner", "subject": inputs["subject"], "result": FROZEN["check"],
            "execution_manifest": {"semantic_inputs": inputs, "as_of": FROZEN["check"]["as_of"]},
            "prior_check_ref": None, "supersedes_ref": None, "trigger": "due",
            "created_at": "2027-04-30T16:00:01Z"}
    assert CheckRevision.model_validate(data).subject.listing_id == 18
    for change in ({"subject": {"kind": "listing", "listing_id": 19}},
                   {"execution_manifest": {"semantic_inputs": semantic_input(), "as_of": FROZEN["check"]["as_of"]}}):
        with pytest.raises(ValidationError):
            CheckRevision.model_validate({**data, **change})


def test_frozen_observation_payload_matches_event_schema() -> None:
    parsed = TopicObservationPayload.model_validate(TOPIC_EVENT["payload"])
    assert parsed.model_dump(mode="json") == TOPIC_EVENT["payload"]
    assert parsed.subject.listing_id == 18
    for subject in ({"kind": "listing", "listing_id": True},
                    {"kind": "account", "account_id": str(uuid4())},
                    {"kind": "listing", "listing_id": 18, "account_id": str(uuid4())}):
        with pytest.raises(ValidationError):
            TopicObservationPayload.model_validate({**TOPIC_EVENT["payload"], "subject": subject})


def test_condition_dependency_manifest_freezes_internal_row_versions() -> None:
    from investment_agent.bank_topics.contracts import ConditionRevision

    request = FROZEN["condition"]["request"]
    condition = ConditionRevision.model_validate({
        "condition_ref": FROZEN["check"]["condition_ref"], "owner_id": "local_owner", "case_id": str(uuid4()),
        "topic_revision_ref": request["topic_revision_ref"], "dsl_version": request["dsl_version"],
        "predicate": request["predicate"], "dependency_manifest": [{"stable_fact_id": "nii_2025", "metric": "nii",
            "source": {"binding_kind": "topic_fact", "fact_ref": {"id": str(uuid4()), "revision": 4}}}],
        "target_selector": request["target_selector"], "review_due_at": request["review_due_at"],
        "expires_at": request["expires_at"], "timezone": request["timezone"],
        "activation_basis": None, "content_hash": "a" * 64,
    })
    assert condition.dependency_manifest[0].source.binding_kind == "topic_fact"
    assert "control_revision" not in condition.model_dump()
