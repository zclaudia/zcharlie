"""EV-01 routing contracts, independent of DB/resolver/domain implementation."""

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_agent.investor.common import Contract, DecimalValue, ObjectRef
from investment_agent.research_events import canonical
from investment_agent.research_events.contracts import (
    EVENT_CONTRACTS,
    SOURCE_REF_ADAPTER,
    EventEnvelope,
    InboxRebuildState,
    ResearchChangePayload,
    ReviewPanelPayload,
    StoredEvent,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/next_iterations/contracts"
EVENTS: list[dict[str, Any]] = json.loads((FIXTURES / "events.valid.json").read_text())["events"]
BY_TYPE = {event["event_type"]: event for event in EVENTS}


def envelope(base_type: str = "review_panel.finalized", **changes: Any) -> dict[str, Any]:
    value = copy.deepcopy(BY_TYPE[base_type])
    value.update(changes)
    return value


@pytest.mark.parametrize("wire", EVENTS, ids=lambda e: e["event_type"])
def test_frozen_events_roundtrip_without_domain_modules(wire: dict[str, Any]) -> None:
    event = EventEnvelope.model_validate(wire)
    assert json.loads(canonical.canonical_json(event)) == wire
    assert event.occurred_at.tzinfo == UTC
    assert canonical.payload_hash(event.payload) == wire["payload_hash"]
    assert str(canonical.event_id(event.owner_id, event.event_type, event.source_ref,
                                  event.source_revision)) == wire["event_id"]
    assert json.loads(canonical.source_identity(event.source_ref)) == {
        "kind": wire["source_ref"]["kind"], "id": wire["source_ref"]["id"]}
    assert event.event_type in EVENT_CONTRACTS
    with pytest.raises(ValidationError):
        ObjectRef.model_validate({"kind": "quality_result", "id": str(UUID(int=1)), "revision": 1})


@pytest.mark.parametrize("case", [case for case in json.loads((FIXTURES / "events.invalid.json").read_text())["cases"]
                                 if case["rejected_by"] in {"schema", "schema/UTC validator", "strict DTO/schema", "cross-field validator", "persistent resolver source equality"}],
                         ids=lambda case: case["name"])
def test_contract_layer_invalid_fixtures(case: dict[str, Any]) -> None:
    value = envelope(case["base_event"])
    value.update(case.get("replace", {}))
    value["payload"].update(case.get("replace_payload", {}))
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(value)


@pytest.mark.parametrize("bad_id", [True, False, 0, -1, 381.0, "381", str(UUID(int=381))])
def test_legacy_ids_are_strict_positive_int(bad_id: object) -> None:
    with pytest.raises(ValidationError):
        SOURCE_REF_ADAPTER.validate_python({"kind": "review_round", "id": bad_id, "revision": 1})


@pytest.mark.parametrize("kind,bad_id", [("unknown", 3), ("evidence", 3), ("review_round", UUID(int=3))])
def test_unknown_kind_and_namespace_mismatch(kind: str, bad_id: object) -> None:
    with pytest.raises(ValidationError):
        SOURCE_REF_ADAPTER.validate_python({"kind": kind, "id": bad_id, "revision": 1})


@pytest.mark.parametrize("bad_revision", [True, 1.0, "1", 0, 2])
def test_immutable_source_revision_is_strict_and_one(bad_revision: object) -> None:
    with pytest.raises(ValidationError):
        SOURCE_REF_ADAPTER.validate_python({"kind": "evidence", "id": UUID(int=3), "revision": bad_revision})


def test_identity_excludes_revision_and_preserves_kind() -> None:
    first = SOURCE_REF_ADAPTER.validate_python({"kind": "case", "id": UUID(int=3), "revision": 1})
    next_ref = first.model_copy(update={"revision": 2})
    assert canonical.source_identity(first) == canonical.source_identity(next_ref)
    assert canonical.event_id("local_owner", "case.control_changed", first, 1) != canonical.event_id("local_owner", "case.control_changed", next_ref, 2)
    with pytest.raises(ValueError):
        canonical.event_id("local_owner", "case.control_changed", first, 2)


class TypedValues(Contract):
    amount: DecimalValue
    ratio: DecimalValue
    at: datetime


@pytest.mark.parametrize("vector", json.loads((FIXTURES / "canonical.vectors.json").read_text())["vectors"], ids=lambda v: v["name"])
def test_canonical_vectors(vector: dict[str, Any]) -> None:
    values = [TypedValues.model_validate(item) for item in vector["typed_inputs"]] if "typed_inputs" in vector else vector["inputs"]
    for value in values:
        assert canonical.canonical_json(value) == vector["canonical"]
        assert canonical.payload_hash(value) == vector["sha256"]


@pytest.mark.parametrize("bad", [1.1, True, False, "NaN", "Infinity", "-Infinity"])
def test_typed_decimal_rejects_bad_inputs(bad: object) -> None:
    with pytest.raises(ValidationError):
        TypedValues.model_validate({"amount": bad, "ratio": "0", "at": "2026-11-07T09:30:00Z"})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.0, Decimal("NaN"), Decimal("Infinity"), Decimal("1e1000000"), {1: "non-string key"}])
def test_canonical_rejects_float_and_unbounded_expansion(bad: object) -> None:
    with pytest.raises(ValueError):
        canonical.canonical_json({"nested": [bad]})


def test_decimal_hash_ignores_context_precision_but_not_numeric_text() -> None:
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 2
        assert canonical.canonical_json(Decimal("123456789.123456789000")) == '"123456789.123456789"'
    assert canonical.canonical_json("1.00") != canonical.canonical_json("1")
    assert canonical.canonical_json(Decimal("-0.000")) == '"0"'
    assert canonical.canonical_json(datetime(2026, 11, 7, 9, 30, 0, 120000, UTC)) == '"2026-11-07T09:30:00.12Z"'
    with pytest.raises(ValueError):
        canonical.canonical_json(datetime(2026, 11, 7))


def test_defaults_and_set_semantics_have_stable_hash() -> None:
    payload = copy.deepcopy(BY_TYPE["review_panel.finalized"]["payload"])
    payload.pop("backfill")
    payload.pop("manifest_hash")
    one = ReviewPanelPayload.model_validate(payload)
    assert canonical.payload_hash(one) == BY_TYPE["review_panel.finalized"]["payload_hash"]
    payload["notify_reasons"] = ["stance_flip", "high_divergence"]
    a = ReviewPanelPayload.model_validate(payload)
    payload["notify_reasons"].reverse()
    b = ReviewPanelPayload.model_validate(payload)
    assert canonical.payload_hash(a) == canonical.payload_hash(b)
    # De-duplicate sets before hashing; input bounds remain enforced first.
    payload["notify_reasons"] = ["stance_flip", "stance_flip"]
    duplicate = ReviewPanelPayload.model_validate(payload)
    payload["notify_reasons"] = ["stance_flip"]
    assert canonical.payload_hash(duplicate) == canonical.payload_hash(ReviewPanelPayload.model_validate(payload))
    research = copy.deepcopy(BY_TYPE["research_change.recorded"]["payload"])
    research["run_ref"].pop("field_path")
    assert canonical.payload_hash(ResearchChangePayload.model_validate(research)) == BY_TYPE["research_change.recorded"]["payload_hash"]


def test_unordered_exact_refs_deduplicate_but_version_refs_stay_ordered() -> None:
    raw = copy.deepcopy(BY_TYPE["review_cycle.changed"]["payload"])
    raw["exact_source_refs"] *= 2
    selected = EVENT_CONTRACTS["review_cycle.changed"][1]
    assert canonical.payload_hash(selected.model_validate(raw)) == BY_TYPE["review_cycle.changed"]["payload_hash"]
    raw_account = copy.deepcopy(BY_TYPE["portfolio_stress.completed"]["payload"])
    raw_account["version_refs"].append({"kind": "portfolio_valuation", "id": str(UUID(int=999)), "revision": 1})
    selected = EVENT_CONTRACTS["portfolio_stress.completed"][1]
    first = selected.model_validate(raw_account)
    raw_account["version_refs"].reverse()
    assert canonical.payload_hash(first) != canonical.payload_hash(selected.model_validate(raw_account))


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 1.0), ("schema_version", 2), ("owner_id", "other_owner"), ("source_revision", True), ("event_type", "arbitrary.callback"), ("event_id", str(UUID(int=100)))])
def test_envelope_guards(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        raw = envelope()
        raw[field] = value
        EventEnvelope.model_validate(raw)


def test_changed_hash_is_not_silently_overwritten_and_source_is_closed() -> None:
    raw = envelope()
    raw["payload"]["notify_reasons"] = ["stance_flip"]
    with pytest.raises(ValidationError, match="payload hash mismatch"):
        EventEnvelope.model_validate(raw)
    raw = envelope()
    raw["source_ref"]["field_path"] = None
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(raw)


def test_parser_support_is_not_publication_permission() -> None:
    # Optional domain DTOs are usable before resolvers exist; no registry is
    # installed, no source lookup or publication side effect is performed here.
    EventEnvelope.model_validate(BY_TYPE["topic.observation_changed"])
    with pytest.raises(ValidationError):
        raw = envelope("source.invalidated")
        raw["payload"]["replacement_refs"][0]["kind"] = "quality_result"
        EventEnvelope.model_validate(raw)


def test_stored_event_sequence_and_rebuild_bounds() -> None:
    assert StoredEvent.model_validate({**envelope(), "sequence": 1}).sequence == 1
    with pytest.raises(ValidationError):
        StoredEvent.model_validate({**envelope(), "sequence": True})
    rebuild = json.loads((FIXTURES / "projection-cursor.json").read_text())["rebuilding"]["rebuild_state"]
    assert InboxRebuildState.model_validate(rebuild).last_replayed_sequence == 75
    with pytest.raises(ValidationError):
        InboxRebuildState.model_validate({**rebuild, "last_replayed_sequence": 81})


@pytest.mark.parametrize("event_type,field", [
    ("source.invalidated", "replacement_refs"),
    ("case.control_changed", "case_ref"),
    ("topic.observation_changed", "check_ref"),
    ("portfolio_stress.completed", "calculation_ref"),
    ("decision_observation.due", "observation_ref"),
    ("decision_fact_check.partial", "calculation_ref"),
])
def test_source_mirror_rejected_even_with_correct_payload_hash(event_type: str, field: str) -> None:
    raw = envelope(event_type)
    target = raw["payload"][field]
    if isinstance(target, list):
        target = target[0]
    target["id"] = str(UUID(int=9999))
    raw["payload_hash"] = canonical.payload_hash(raw["payload"])
    with pytest.raises(ValidationError, match="reference disagrees|replacement must equal"):
        EventEnvelope.model_validate(raw)


@pytest.mark.parametrize("url", ["https://example.invalid/a", "//example.invalid/app/a", "/app/../secrets", "/app/%2e%2e/secrets", "/app/\\evil", "/app/a?secret=x", "/app/a#secret"])
def test_detail_links_cannot_escape_product_path(url: str) -> None:
    raw = envelope("portfolio_stress.completed")
    raw["payload"]["detail_url"] = url
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(raw)


def test_html_and_missing_schema_are_rejected() -> None:
    raw = envelope("portfolio_stress.completed")
    raw["payload"]["safe_summary"] = "<script>alert(1)</script>"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(raw)
    raw = envelope()
    raw.pop("schema_version")
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(raw)
