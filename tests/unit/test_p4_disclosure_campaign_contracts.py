from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.investor.common import ObjectRef
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationBodyV2,
    CampaignAction,
    DisclosureCampaignV2,
)


def test_v2_grant_is_strict_and_absolute_validity_bounded():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    fields = {
        "contract_hash": "a" * 64,
        "listing_refs": (ObjectRef(kind="listing", id=1, revision=1),),
        "doc_type_allowlist": ("annual_report_full",),
        "history_start": now,
        "history_end": now + timedelta(days=1),
        "min_poll_interval_seconds": 21600,
        "min_http_interval_seconds": 5,
        "expires_at": now + timedelta(hours=24),
        "max_staleness_seconds": 86400,
        "per_poll_query_http": 2,
        "per_poll_fetch": 1,
        "per_poll_http": 8,
        "per_poll_response_bytes": 64 * 1024 * 1024,
        "grant_daily_http": 24,
        "grant_daily_response_bytes": 128 * 1024 * 1024,
        "storage_bytes_cap": 128 * 1024 * 1024,
        "campaign_id": uuid4(),
        "sample_env_id": "sample-env-1",
        "valid_from": now,
    }
    grant = BackgroundDisclosureAuthorizationBodyV2.model_validate(fields)
    assert grant.schema_version == 2
    assert grant.manual_only is True
    with pytest.raises(ValidationError):
        BackgroundDisclosureAuthorizationBodyV2.model_validate({**fields, "manual_only": False})
    with pytest.raises(ValidationError):
        BackgroundDisclosureAuthorizationBodyV2.model_validate({
            **fields, "expires_at": now + timedelta(hours=73),
        })


def test_third_poll_starts_reserved_and_can_only_have_complete_approval_scope_to_enable():
    reserved = CampaignAction.model_validate({
        "action_id": "sample-p3", "kind": "sample_poll", "status": "reserved", "poll_ordinal": 3,
    })
    assert reserved.status == "reserved"
    with pytest.raises(ValidationError):
        CampaignAction.model_validate({
            "action_id": "sample-p3", "kind": "sample_poll", "status": "approved", "poll_ordinal": 3,
        })


def test_campaign_caps_are_hard_bounded_and_action_ids_unique():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    action = CampaignAction(action_id="terms", kind="terms", status="approved", url_allowlist=("https://example.test/terms",))
    with pytest.raises(ValidationError):
        DisclosureCampaignV2(
            campaign_id=uuid4(), revision=1, approval_ref="approval", sample_env_id="env",
            database_path="/tmp/sample.db", valid_from=now, expires_at=now + timedelta(days=1),
            http_cap=33, response_bytes_cap=192 * 1024 * 1024, storage_bytes_cap=512 * 1024 * 1024,
            s0_http_cap=8, s0_response_bytes_cap=40 * 1024 * 1024, s0_storage_bytes_cap=128 * 1024 * 1024,
            sample_http_cap=24, sample_response_bytes_cap=152 * 1024 * 1024,
            actions=(action,),
        )
