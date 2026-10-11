"""Offline wire-contract tests, not an executor/publication acceptance suite."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from investment_agent.agent.contracts import MessageAccepted, TaskEvent
from investment_agent.evidence.contracts import EvidenceRecord, ToolResult
from investment_agent.investor.common import Quantity
from investment_agent.investor.contracts import CompanyRecommendation, NumericNode
from investment_agent.investor.enums import LABELS, LabelledEnum

FIXTURES = Path(__file__).parent / "fixtures" / "agent"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    "model,name",
    [
        (EvidenceRecord, "evidence.json"),
        (CompanyRecommendation, "company_view.json"),
        (MessageAccepted, "message_accepted.json"),
    ],
)
def test_fixed_records_round_trip(model, name):
    original = model.model_validate(load(name))
    assert model.model_validate_json(original.model_dump_json()) == original


def test_all_event_payloads_round_trip_and_reject_provider_content():
    adapter = TypeAdapter(list[TaskEvent])
    events = adapter.validate_python(load("events.json"))
    assert adapter.validate_json(adapter.dump_json(events)) == events
    assert len({event.type for event in events}) == 9
    invalid = load("events.json")
    invalid[0]["payload"]["reasoning_content"] = "hidden"
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)


@pytest.mark.parametrize("value", [1.1, True, "NaN", "Infinity"])
def test_decimal_rejects_lossy_or_non_finite_values(value):
    with pytest.raises(ValidationError):
        Quantity(value=value, unit="ratio")


def test_decimal_transport_and_unknown_are_explicit():
    assert json.loads(Quantity(value="1.200", unit="ratio").model_dump_json())["value"] == "1.200"
    with pytest.raises(ValidationError):
        Quantity(value=None, unit="yuan")
    assert Quantity(value=None, unit="yuan", unknown_reason="missing").value is None


@pytest.mark.parametrize(
    "change",
    [
        dict(created_at="2026-08-01T00:00:00"),
        dict(revision=0),
        dict(schema_version=2),
        dict(revision=True),
    ],
)
def test_record_rejects_naive_time_and_invalid_versions(change):
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate({**load("evidence.json"), **change})


def test_timestamp_normalizes_utc_and_user_report_is_not_fact():
    data = load("evidence.json")
    data["created_at"] = "2026-08-01T08:00:00+08:00"
    assert EvidenceRecord.model_validate(data).created_at.isoformat() == "2026-08-01T00:00:00+00:00"
    data["source_kind"] = "user_report"
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "portfolio_action"),
        ("case_ids", []),
        ("position_proposal", {"quantity": "100"}),
        ("business_view", "hold"),
        ("assessed_dimensions", []),
    ],
)
def test_company_scope_cannot_imply_holdings_or_omit_dimensions(field, value):
    data = load("company_view.json")
    data[field] = value
    with pytest.raises(ValidationError):
        CompanyRecommendation.model_validate(data)


def test_numeric_node_does_not_accept_model_supplied_value():
    with pytest.raises(ValidationError):
        NumericNode.model_validate(
            dict(
                type="numeric_claim",
                ref=load("evidence.json")["subject_ref"],
                role="historical_fact",
                display_format="decimal",
                value="1",
            )
        )


def test_all_domain_enums_have_exact_ui_mapping():
    for enum in LabelledEnum.__subclasses__():
        assert set(LABELS[enum.__name__]) == {member.value for member in enum}
        assert all(member.label for member in enum)


def test_context_is_frozen_and_refs_keep_revision():
    accepted = MessageAccepted.model_validate(load("message_accepted.json"))
    with pytest.raises(ValidationError):
        accepted.message.context_refs = ()
    assert accepted.message.context_refs[0].revision == 1


def test_pending_tool_requires_dependency():
    with pytest.raises(ValidationError):
        ToolResult[EvidenceRecord](
            status="pending", data=None, refs=(), as_of="2026-08-01T00:00:00Z", coverage="missing"
        )


def test_fixed_scenarios_cover_scope_and_do_not_load_future_inputs():
    rows = load("scenarios.json")
    assert {row["id"] for row in rows} == {
        *(f"S{i:02}" for i in range(1, 11)),
        "S14_preference",
        "S16",
        "S17",
    }
    for row in rows:
        assert row["expected_invariants"]
        assert [event["sequence"] for event in row["events"]] == list(
            range(1, len(row["events"]) + 1)
        )
        assert all((FIXTURES / name).is_file() for name in row["input_refs"])
    evidence = EvidenceRecord.model_validate(load("evidence.json"))
    accepted = MessageAccepted.model_validate(load("message_accepted.json"))
    assert evidence.available_from <= accepted.task.binding.as_of
    assert evidence.acquired_at <= accepted.task.binding.as_of


@pytest.mark.parametrize(
    "kind,identifier",
    [
        ("listing", "00000000-0000-0000-0000-000000000002"),
        ("case", 2),
        ("financial_snapshot", True),
    ],
)
def test_existing_and_new_identity_namespaces_cannot_be_confused(kind, identifier):
    from investment_agent.investor.common import ObjectRef

    with pytest.raises(ValidationError):
        ObjectRef.model_validate(dict(kind=kind, id=identifier, revision=1))


def test_existing_listing_and_snapshot_keep_integer_ids():
    evidence = EvidenceRecord.model_validate(load("evidence.json"))
    assert evidence.subject_ref.id == 2
    assert evidence.source_ref.id == 101


@pytest.mark.parametrize("status", ["waiting_data", "succeeded"])
def test_task_shape_requires_wait_or_stop_metadata(status):
    from investment_agent.agent.contracts import Task

    data = load("message_accepted.json")["task"]
    data["status"] = status
    with pytest.raises(ValidationError):
        Task.model_validate(data)
