from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.investor.common import ObjectRef
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
    DisclosureAttemptIdentity,
    authorization_scope_hash,
)


def body(**changes):
    values = {
        "contract_hash": "a" * 64,
        "listing_refs": (ObjectRef(kind="listing", id=1, revision=1),),
        "doc_type_allowlist": ("annual_report_full",),
        "history_start": datetime(2025, 1, 1, tzinfo=UTC),
        "history_end": datetime(2025, 12, 31, tzinfo=UTC),
        "min_poll_interval_seconds": 21600,
        "min_http_interval_seconds": 5,
        "expires_at": datetime(2026, 1, 1, tzinfo=UTC),
        "max_staleness_seconds": 86400,
        "per_poll_query_http": 2,
        "per_poll_fetch": 1,
        "per_poll_http": 4,
        "per_poll_response_bytes": 1024 * 1024,
        "grant_daily_http": 8,
        "grant_daily_response_bytes": 8 * 1024 * 1024,
        "storage_bytes_cap": 64 * 1024 * 1024,
    }
    values.update(changes)
    return BackgroundDisclosureAuthorizationBody.model_validate(values)


def test_authorization_body_rejects_out_of_scope_or_unbounded_caps():
    with pytest.raises(ValidationError):
        body(listing_refs=(ObjectRef(kind="listing", id=1, revision=2),))
    with pytest.raises(ValidationError):
        body(per_poll_query_http=5)
    with pytest.raises(ValidationError):
        body(grant_daily_http=65)
    with pytest.raises(ValidationError):
        body(min_poll_interval_seconds=21599)


def test_command_requires_explicit_owner_policy_cas_and_rejects_extras():
    base = {
        "action": "create", "operation_id": uuid4(), "authorization_id": uuid4(),
        "expected_revision": 0, "owner_daily_http": 24,
        "owner_daily_response_bytes": 128 * 1024 * 1024,
        "owner_min_request_interval_seconds": 5,
        "expected_owner_policy_revision": 0, "body": body().model_dump(mode="python"),
    }
    cmd = BackgroundDisclosureAuthorizationCommand.model_validate(base)
    assert cmd.expected_owner_policy_revision == 0
    with pytest.raises(ValidationError):
        BackgroundDisclosureAuthorizationCommand.model_validate({**base, "unexpected": True})
    with pytest.raises(ValidationError):
        BackgroundDisclosureAuthorizationCommand.model_validate({**base, "owner_daily_http": 65})


def test_scope_hash_is_deterministic_and_scope_bound():
    listing = ObjectRef(kind="listing", id=1, revision=1)
    auth_id = uuid4()
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    first = authorization_scope_hash(auth_id, 1, "b" * 64, "a" * 64, listing, start, end)
    assert first == authorization_scope_hash(auth_id, 1, "b" * 64, "a" * 64, listing, start, end)
    assert first != authorization_scope_hash(auth_id, 2, "c" * 64, "a" * 64, listing, start, end)


def test_attempt_contract_requires_exact_listing_and_positive_window():
    with pytest.raises(ValidationError):
        DisclosureAttemptIdentity.model_validate({
            "poll_id": "poll", "logical_request_key": "logical", "attempt_id": uuid4(),
            "attempt_ordinal": 1, "hop_ordinal": 0, "hop_id": "hop",
            "request_kind": "directory", "request_hash": "a" * 64,
            "authorization_id": uuid4(), "authorization_revision": 1,
            "authorization_hash": "b" * 64, "contract_hash": "c" * 64,
            "scope_hash": "d" * 64,
            "listing_ref": ObjectRef(kind="listing", id=1, revision=2),
            "window_start": datetime(2025, 1, 2, tzinfo=UTC),
            "window_end": datetime(2025, 1, 1, tzinfo=UTC),
            "poll_http_cap": 2, "poll_query_http_cap": 1, "poll_fetch_cap": 0,
            "poll_response_bytes_cap": 100, "url": "https://www.cninfo.com.cn/a",
            "response_bytes_limit": 100,
        })
