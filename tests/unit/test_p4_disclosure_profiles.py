from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from investment_agent.evidence.disclosure_http import DisclosureError, validate_url
from investment_agent.evidence.disclosure_pages import CNInfoPageProtocolV1
from investment_agent.evidence.disclosure_profiles import (
    ADAPTER_BUILD,
    DisclosureApprovalV1,
    DisclosureProfileRegistry,
    DisclosureProfileV1,
    expected_approval_hash,
    expected_contract_hash,
    expected_protocol_hash,
)
from tests.support.p4_disclosures import fixture_protocol


def _profile() -> DisclosureProfileV1:
    proto_raw = fixture_protocol().model_dump(mode="json")
    proto_raw["protocol_hash"] = "0" * 64
    protocol_hash = expected_protocol_hash(CNInfoPageProtocolV1.model_validate(proto_raw))
    proto_raw["protocol_hash"] = protocol_hash
    protocol = CNInfoPageProtocolV1.model_validate(proto_raw)
    profile_raw = {
        "schema_version": 1,
        "source_id": "cninfo.v1",
        "feed_id": "cninfo_szse_v1",
        "protocol": protocol.model_dump(mode="json"),
        "adapter_build": ADAPTER_BUILD,
        "url_policy": "cninfo-https-single-hop-v1",
        "exact_terms_urls": (),
        "contract_hash": "0" * 64,
    }
    profile_raw["contract_hash"] = expected_contract_hash(profile_raw)
    return DisclosureProfileV1.model_validate(profile_raw)


def _approval(profile: DisclosureProfileV1) -> DisclosureApprovalV1:
    raw = {
        "schema_version": 1,
        "profile_contract_hash": profile.contract_hash,
        "protocol_hash": profile.protocol.protocol_hash,
        "evidence_hashes": ("a" * 64,),
        "approval_ref": "synthetic-source-review-1",
        "executor_name": "synthetic executor",
        "reviewer_name": "synthetic independent reviewer",
        "purpose_scopes": ("query",),
        "valid_from": datetime.now(UTC) - timedelta(minutes=1),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "approval_hash": "0" * 64,
    }
    raw["approval_hash"] = expected_approval_hash(raw)
    return DisclosureApprovalV1.model_validate(raw)


def test_empty_registry_and_synthetic_profile_contract_are_fail_closed() -> None:
    registry = DisclosureProfileRegistry()
    profile = _profile()
    approval = _approval(profile)
    now = datetime.now(UTC)

    assert not registry.approved_contract(profile.contract_hash, now=now)
    loaded = registry.install_approved(profile, approval)
    assert registry.approved_contract(
        profile.contract_hash, now=now, protocol_hash=profile.protocol.protocol_hash
    )
    assert loaded.profile == profile


def test_profile_install_cannot_reactivate_revocation_or_repurpose_approval() -> None:
    registry = DisclosureProfileRegistry()
    profile = _profile()
    approval = _approval(profile)
    revoked_at = datetime.now(UTC)
    registry.install_approved(profile, approval)
    registry.revoke(approval.approval_hash, at=revoked_at, reference="synthetic revoke")

    assert registry.install_approved(profile, approval).revoked_at == revoked_at
    assert not registry.approved_contract(profile.contract_hash, now=revoked_at + timedelta(seconds=1))
    with pytest.raises(DisclosureError, match="revocation_conflict"):
        registry.revoke(
            approval.approval_hash,
            at=revoked_at + timedelta(seconds=1),
            reference="different revocation",
        )


def test_profile_protocol_hash_excludes_self_and_validated_flag() -> None:
    profile = _profile()
    altered = profile.protocol.model_copy(update={"validated": True})
    assert expected_protocol_hash(profile.protocol) == expected_protocol_hash(altered)
    assert profile.protocol.protocol_hash == expected_protocol_hash(profile.protocol)


def test_terms_urls_are_exact_and_limited_to_official_hosts() -> None:
    approved = "https://webapi.cninfo.com.cn/"
    assert validate_url(approved, "terms", exact_urls=(approved,))[0] == "webapi.cninfo.com.cn"
    with pytest.raises(DisclosureError, match="unsupported_source_url"):
        validate_url("https://webapi.cninfo.com.cn/other", "terms", exact_urls=(approved,))
    with pytest.raises(DisclosureError, match="unsupported_source_url"):
        validate_url("https://example.test/terms", "terms", exact_urls=("https://example.test/terms",))


def test_current_registry_rejects_adapter_build_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_agent.evidence.disclosure_profiles as profile_module

    registry = DisclosureProfileRegistry()
    profile = _profile()
    approval = _approval(profile)
    registry.install_approved(profile, approval)
    monkeypatch.setattr(profile_module, "current_adapter_build", lambda: "f" * 64)
    assert not registry.approved_contract(profile.contract_hash, now=datetime.now(UTC))
    with pytest.raises(DisclosureError, match="adapter_build_stale"):
        registry.require_approved_contract(profile.contract_hash, now=datetime.now(UTC))

