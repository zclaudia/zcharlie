"""IT-001 contracts over frozen EV fixtures, with the BASE-03 guard active."""

import copy
import json
import socket
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from investment_agent.investor.common import Contract, DecimalValue, ObjectRef
from investment_agent.research_events import canonical
from investment_agent.research_events import contracts as events
from investment_agent.research_inbox.contracts import (
    AdapterSpecification,
    IgnoredContribution,
    InboxContribution,
    InboxProjectionHead,
    InboxRebuildState,
    InboxRevisions,
    Subject,
    validate_adapter_output,
)

ROOT = Path(__file__).resolve().parents[1] / "fixtures/next_iterations/contracts"
FIXTURE = json.loads((ROOT / "inbox.json").read_text())
EVENTS = json.loads((ROOT / "events.valid.json").read_text())["events"]


def contribution(**changes):
    value = copy.deepcopy(FIXTURE["contribution"])
    value.update(changes)
    return value


def stored_for(raw):
    event = next(item for item in EVENTS if item["event_id"] == raw["event_ref"]["event_id"])
    return events.StoredEvent.model_validate({**event, "sequence": raw["event_ref"]["sequence"]})


@pytest.mark.parametrize("key", ["contribution", "account_contribution"])
def test_frozen_contributions_and_same_session_adapter_binding(key):
    raw = FIXTURE[key]
    parsed = InboxContribution.model_validate(raw)
    assert validate_adapter_output(stored_for(raw), parsed) is parsed
    assert parsed.schema_version == 1
    assert Subject is events.Subject
    assert InboxRebuildState is events.InboxRebuildState
    if key == "account_contribution":
        assert parsed.case_ref is None and parsed.episode is None
        assert set(parsed.allowed_actions) == {"read", "snooze", "view_decision", "view_check"}


@pytest.mark.parametrize("bad", [True, False, 0, -1, 18.0, "18", str(UUID(int=18))])
def test_listing_identity_is_a_strict_positive_integer(bad):
    with pytest.raises(ValidationError):
        InboxContribution.model_validate(
            contribution(subject={"kind": "listing", "listing_id": bad})
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": {"kind": "listing", "listing_id": 18, "account_id": str(UUID(int=200))}},
        {"subject": {"kind": "account", "account_id": 200}},
        {"subject": {"kind": "account", "account_id": True}},
        {"subject": {"kind": "global", "listing_id": 18}},
        {"event_ref": {"event_id": str(UUID(int=1)), "sequence": True}},
        {"event_ref": {"event_id": str(UUID(int=1)), "sequence": 0}},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"owner_id": "other_owner"},
        {"grant": {}},
        {"source_text": "raw report"},
        {"detail_url": "https://arbitrary.invalid"},
        {"exact_source_refs": []},
        {"evidence_status": "verified"},
        {"materiality": "trade_signal"},
    ],
)
def test_contribution_refuses_ambiguous_subject_authority_and_open_payload(changes):
    with pytest.raises(ValidationError):
        InboxContribution.model_validate(contribution(**changes))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  ",
        "x" * 301,
        "<img src=x>",
        "原标题\n原文正文",
        "标题\x00",
        "https://arbitrary.invalid",
        "查看https://arbitrary.invalid",
        "www.arbitrary.invalid",
        "[查看](/app/agent)",
        "//arbitrary.invalid",
        " /app/agent",
        "javascript:alert(1)",
    ],
)
def test_display_text_is_short_plain_server_summary_without_arbitrary_links(text):
    with pytest.raises(ValidationError):
        InboxContribution.model_validate(contribution(display_text=text))


def test_case_episode_and_account_actions_cannot_invoke_company_control():
    case = {"kind": "case", "id": str(UUID(int=100)), "revision": 4}
    assert InboxContribution.model_validate(contribution(case_ref=case, episode=2))
    for changes in (
        {"case_ref": case},
        {"episode": 1},
        {"case_ref": case, "episode": True},
        {"case_ref": {**case, "field_path": "status"}, "episode": 1},
    ):
        with pytest.raises(ValidationError):
            InboxContribution.model_validate(contribution(**changes))
    raw = copy.deepcopy(FIXTURE["account_contribution"])
    for changes in (
        {"case_ref": case, "episode": 1},
        {"episode": 1},
        {"allowed_actions": ["read", "request_review"]},
        {"allowed_actions": ["acknowledge"]},
        {"allowed_actions": ["read", "read"]},
    ):
        with pytest.raises(ValidationError):
            InboxContribution.model_validate({**raw, **changes})


def test_optional_relations_require_exact_parent_versions_and_closed_namespaces():
    parent = {
        "kind": "recommendation",
        "id": str(UUID(int=100)),
        "revision": 1,
        "field_path": "claims.0",
    }
    assert InboxContribution.model_validate(contribution(claim_ref=parent))
    topic = {"kind": "topic_revision", "id": str(UUID(int=101)), "revision": 1}
    condition = {"kind": "topic_condition", "id": str(UUID(int=102)), "revision": 3}
    assert InboxContribution.model_validate(
        contribution(topic_ref=topic, monitor_condition_ref=condition)
    )
    for changes in (
        {"claim_ref": {**parent, "field_path": None}},
        {"topic_ref": {**topic, "revision": 2}},
        {"monitor_condition_ref": topic},
        {"topic_ref": {**topic, "field_path": "data"}},
        {"decision_ref": {"kind": "case", "id": str(UUID(int=100)), "revision": 1}},
        {
            "exact_source_refs": [
                {"kind": "quality_result", "id": str(UUID(int=100)), "revision": 1}
            ]
        },
        {"relation": "opposition"},
    ):
        with pytest.raises(ValidationError):
            InboxContribution.model_validate(contribution(**changes))
    # A relation ref is an address only; a producer cannot assert opposition in this DTO.
    parsed = InboxContribution.model_validate(
        contribution(
            reviewed_relation_ref={"kind": "review", "id": str(UUID(int=103)), "revision": 1}
        )
    )
    assert not hasattr(parsed, "relation")


def test_source_sets_and_optional_defaults_have_stable_ev_hashes():
    one = InboxContribution.model_validate(contribution())
    raw = contribution()
    raw["exact_source_refs"] = [*reversed(raw["exact_source_refs"]), raw["exact_source_refs"][0]]
    for optional in (
        "case_ref",
        "episode",
        "claim_ref",
        "monitor_condition_ref",
        "topic_ref",
        "decision_ref",
        "reviewed_relation_ref",
        "replaces_refs",
    ):
        raw.pop(optional)
    two = InboxContribution.model_validate(raw)
    assert canonical.payload_hash(one) == canonical.payload_hash(two)
    for ref in one.exact_source_refs:
        assert not isinstance(ref, ObjectRef)  # these two frozen sources use EV integer references


def test_adapter_binding_rejects_cross_subject_wrong_event_cursor_and_missing_source():
    raw = contribution()
    event = stored_for(raw)
    for changes in (
        {"subject": {"kind": "listing", "listing_id": 19}},
        {"event_ref": {"event_id": str(UUID(int=1)), "sequence": event.sequence}},
        {"event_ref": {"event_id": str(event.event_id), "sequence": event.sequence + 1}},
        {"exact_source_refs": [raw["exact_source_refs"][1]]},
    ):
        with pytest.raises(ValueError):
            validate_adapter_output(
                event, InboxContribution.model_validate(contribution(**changes))
            )


def test_row_content_and_read_revisions_remain_distinct():
    initial = InboxRevisions(revision=1, content_revision=1, read_content_revision=0)
    read = InboxRevisions(revision=2, content_revision=1, read_content_revision=1)
    changed = InboxRevisions(revision=3, content_revision=2, read_content_revision=1)
    assert initial.unread and not read.unread and changed.unread
    for changes in (
        {"revision": True},
        {"content_revision": 0},
        {"read_content_revision": -1},
        {"content_revision": 2},
        {"read_content_revision": 2},
        {"read_content_revision": "0"},
        {"unread": False},
    ):
        with pytest.raises(ValidationError):
            InboxRevisions.model_validate({**initial.model_dump(), **changes})


def test_rebuild_reuses_ev_contract_and_existing_cursor_head():
    fixture = json.loads((ROOT / "projection-cursor.json").read_text())
    first = InboxProjectionHead.model_validate(fixture["initial"])
    rebuilding = InboxProjectionHead.model_validate(fixture["rebuilding"])
    assert first.rebuild_state is None
    assert rebuilding.rebuild_state.last_replayed_sequence == 75
    for changes in (
        {"candidate_generation": rebuilding.active_generation},
        {"last_replayed_sequence": 81},
        {"state": "ready"},
        {"journal_watermark": True},
        {"unknown": 1},
    ):
        data = {
            **fixture["rebuilding"],
            "rebuild_state": {**fixture["rebuilding"]["rebuild_state"], **changes},
        }
        with pytest.raises(ValidationError):
            InboxProjectionHead.model_validate(data)
    with pytest.raises(ValidationError):
        InboxProjectionHead.model_validate(fixture["other_consumer"])
    switched = InboxProjectionHead.model_validate(fixture["switch"]["next"])
    assert switched.active_generation == rebuilding.rebuild_state.candidate_generation


def test_adapter_protocol_is_bounded_and_does_not_register_callbacks_or_models():
    spec = {
        "name": "review_panel.v1",
        "projector_version": "inbox/1",
        "event_types": ["review_panel.finalized"],
    }
    assert AdapterSpecification.model_validate(spec).schema_versions == (1,)
    for changes in (
        {"schema_versions": [True]},
        {"schema_versions": [2]},
        {"event_types": ["unknown.callback"]},
        {"event_types": ["review_panel.finalized"] * 2},
        {"callback_url": "https://arbitrary.invalid"},
    ):
        with pytest.raises(ValidationError):
            AdapterSpecification.model_validate({**spec, **changes})
    assert IgnoredContribution(reason="unsupported_optional_adapter").state == "ignored"
    with pytest.raises(ValidationError):
        IgnoredContribution(reason="success")


class TypedLocalValues(Contract):
    amount: DecimalValue
    at: datetime


def test_utc_decimal_use_shared_contract_and_canonical_rules():
    first = TypedLocalValues(amount="1.00", at="2026-11-07T17:30:00+08:00")
    second = TypedLocalValues(amount=1, at="2026-11-07T09:30:00Z")
    assert canonical.payload_hash(first) == canonical.payload_hash(second)
    for bad in (True, 1.0, "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            TypedLocalValues(amount=bad, at="2026-11-07T09:30:00Z")
    with pytest.raises(ValidationError):
        TypedLocalValues(amount=1, at="2026-11-07T09:30:00")


def test_shared_early_guard_blocks_socket_and_http_baits():
    from next_iterations_offline import OfflineViolationError

    with pytest.raises(OfflineViolationError):
        socket.create_connection(("external.invalid", 443))
    with pytest.raises(OfflineViolationError):
        httpx.get("https://external.invalid")
