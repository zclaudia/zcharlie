"""Server-side, read-only source profile registry for P4 disclosure adapters.

The registry is intentionally empty at process start. A request can name a
profile hash, but cannot install or validate one. Installation is an explicit
server bootstrap operation over an independently reviewed approval record.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from investment_agent.evidence.disclosure_http import DisclosureError, validate_url
from investment_agent.evidence.disclosure_pages import CNInfoPageProtocolV1
from investment_agent.investor.common import Contract

SOURCE = "cninfo.v1"
FEED = "cninfo_szse_v1"
URL_POLICY = "cninfo-https-single-hop-v1"


def current_adapter_build() -> str:
    base = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for filename in ("disclosure_profiles.py", "disclosure_pages.py", "disclosure_http.py"):
        digest.update(filename.encode() + b"\0")
        digest.update((base / filename).read_bytes())
    return digest.hexdigest()


ADAPTER_BUILD = current_adapter_build()


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def protocol_material(protocol: CNInfoPageProtocolV1) -> dict[str, Any]:
    """Hash only protocol semantics; omit the self-hash and derived validation bit."""
    return protocol.model_dump(mode="json", exclude={"protocol_hash", "validated"})


def expected_protocol_hash(protocol: CNInfoPageProtocolV1) -> str:
    return canonical_hash(protocol_material(protocol))


class DisclosureProfileV1(Contract):
    schema_version: Literal[1] = 1
    source_id: Literal["cninfo.v1"] = "cninfo.v1"
    feed_id: Literal["cninfo_szse_v1"] = "cninfo_szse_v1"
    protocol: CNInfoPageProtocolV1
    adapter_build: str = ADAPTER_BUILD
    url_policy: Literal["cninfo-https-single-hop-v1"] = "cninfo-https-single-hop-v1"
    exact_terms_urls: tuple[str, ...] = Field(default=(), max_length=2)
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def profile_hashes_are_consistent(self) -> DisclosureProfileV1:
        if self.adapter_build != current_adapter_build():
            raise ValueError("adapter_build is stale or not server-derived")
        if self.protocol.protocol_hash != expected_protocol_hash(self.protocol):
            raise ValueError("protocol_hash does not match immutable protocol material")
        if self.protocol.validated:
            raise ValueError("validated is a derived registry state, not profile input")
        if len(set(self.exact_terms_urls)) != len(self.exact_terms_urls):
            raise ValueError("terms URLs must be unique")
        for url in self.exact_terms_urls:
            try:
                validate_url(url, "terms", exact_urls=self.exact_terms_urls)
            except DisclosureError as exc:
                raise ValueError("terms URL must be an exact allowed official URL") from exc
        if self.contract_hash != expected_contract_hash(self):
            raise ValueError("contract_hash does not match profile contract material")
        return self


def contract_material(profile: DisclosureProfileV1 | dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = profile.model_dump(mode="json") if isinstance(profile, DisclosureProfileV1) else profile
    protocol = values["protocol"]
    return {
        "schema_version": values["schema_version"],
        "source_id": values["source_id"],
        "feed_id": values["feed_id"],
        "protocol_hash": protocol["protocol_hash"],
        "schema": "CNInfoPageProtocolV1/1",
        "adapter_build": values["adapter_build"],
        "url_policy": values["url_policy"],
        "exact_terms_urls": list(values["exact_terms_urls"]),
        "document_types": {
            "annual_full_text": list(protocol["annual_full_text_type_values"]),
            "correction": list(protocol["correction_type_values"]),
            "supplement": list(protocol["supplement_type_values"]),
            "excluded": list(protocol["excluded_type_values"]),
        },
        "time": {"field": protocol["published_millis_field"], "precision": "millisecond", "window": "closed"},
        "order": {
            "primary": [protocol["query_sort_name"], protocol["query_sort_type"]],
            "tie_break": [protocol["stable_tie_break_field"], protocol["stable_tie_break_order"]],
        },
        "coverage": "explicit-window-pages-v1",
    }


def expected_contract_hash(profile: DisclosureProfileV1 | dict[str, Any]) -> str:
    return canonical_hash(contract_material(profile))


class DisclosureApprovalV1(Contract):
    schema_version: Literal[1] = 1
    profile_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hashes: tuple[str, ...] = Field(min_length=1, max_length=16)
    approval_ref: str = Field(min_length=1, max_length=256)
    executor_name: str = Field(min_length=1, max_length=200)
    reviewer_name: str = Field(min_length=1, max_length=200)
    purpose_scopes: tuple[Literal["terms", "directory", "query", "original"], ...] = Field(min_length=1)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def approval_is_immutable_and_scoped(self) -> DisclosureApprovalV1:
        if self.executor_name.strip().casefold() == self.reviewer_name.strip().casefold():
            raise ValueError("independent source reviewer must differ from executor")
        if self.valid_from >= self.expires_at:
            raise ValueError("approval validity window must be positive")
        if len(set(self.evidence_hashes)) != len(self.evidence_hashes) or any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in self.evidence_hashes
        ):
            raise ValueError("evidence hashes must be unique lowercase SHA-256 values")
        if len(set(self.purpose_scopes)) != len(self.purpose_scopes):
            raise ValueError("purpose scopes must be unique")
        if self.approval_hash != expected_approval_hash(self):
            raise ValueError("approval_hash does not match approval record")
        return self


def approval_material(approval: DisclosureApprovalV1 | dict[str, Any]) -> dict[str, Any]:
    values = approval.model_dump(mode="json", exclude={"approval_hash"}) if isinstance(
        approval, DisclosureApprovalV1
    ) else {k: v for k, v in approval.items() if k != "approval_hash"}
    return values


def expected_approval_hash(approval: DisclosureApprovalV1 | dict[str, Any]) -> str:
    return canonical_hash(approval_material(approval))


class ApprovedDisclosureProfile(Contract):
    profile: DisclosureProfileV1
    approval: DisclosureApprovalV1
    revoked_at: AwareDatetime | None = None
    revocation_ref: str | None = None

    @model_validator(mode="after")
    def binds_profile_and_approval(self) -> ApprovedDisclosureProfile:
        if self.approval.profile_contract_hash != self.profile.contract_hash:
            raise ValueError("approval is for a different source contract")
        if self.approval.protocol_hash != self.profile.protocol.protocol_hash:
            raise ValueError("approval is for a different protocol")
        if (self.revoked_at is None) != (self.revocation_ref is None):
            raise ValueError("revocation timestamp and reference must be paired")
        return self


class DisclosureProfileRegistry:
    """Mutable only through trusted server bootstrap/revocation code; requests are read-only."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._profiles: dict[str, ApprovedDisclosureProfile] = {}

    @classmethod
    def for_tests(cls) -> DisclosureProfileRegistry:
        """An isolated test registry; this does not turn a fixture into live approval."""
        return cls()

    def install_approved(
        self, profile: DisclosureProfileV1, approval: DisclosureApprovalV1
    ) -> ApprovedDisclosureProfile:
        """Load a server-reviewed record. Never expose this through an API or request model."""
        entry = ApprovedDisclosureProfile(profile=profile, approval=approval)
        with self._lock:
            existing = self._profiles.get(profile.contract_hash)
            if existing is not None:
                if existing.approval.approval_hash != approval.approval_hash:
                    raise DisclosureError("source_profile_install_conflict")
                return existing
            self._profiles[profile.contract_hash] = entry
        return entry

    def revoke(self, approval_hash: str, *, at: datetime, reference: str) -> None:
        with self._lock:
            matches = [
                (contract, entry)
                for contract, entry in self._profiles.items()
                if entry.approval.approval_hash == approval_hash
            ]
            if len(matches) != 1:
                raise DisclosureError("source_profile_approval_unknown")
            contract_hash, entry = matches[0]
            if entry.revoked_at is not None:
                if entry.revoked_at == at.astimezone(UTC) and entry.revocation_ref == reference:
                    return
                raise DisclosureError("source_profile_revocation_conflict")
            self._profiles[contract_hash] = entry.model_copy(
                update={"revoked_at": at.astimezone(UTC), "revocation_ref": reference}
            )

    def approved_contract(
        self,
        contract_hash: str,
        *,
        now: datetime,
        protocol_hash: str | None = None,
        source_id: str = SOURCE,
    ) -> bool:
        try:
            self.require_approved_contract(
                contract_hash, now=now, protocol_hash=protocol_hash, source_id=source_id
            )
            return True
        except DisclosureError:
            return False

    def require_approved_contract(
        self,
        contract_hash: str,
        *,
        now: datetime,
        protocol_hash: str | None = None,
        source_id: str = SOURCE,
    ) -> ApprovedDisclosureProfile:
        with self._lock:
            entry = self._profiles.get(contract_hash)
        if entry is None or entry.profile.source_id != source_id:
            raise DisclosureError("source_profile_unapproved")
        if entry.profile.adapter_build != current_adapter_build():
            raise DisclosureError("source_profile_adapter_build_stale")
        if entry.profile.contract_hash != expected_contract_hash(entry.profile):
            raise DisclosureError("source_profile_contract_hash_stale")
        if protocol_hash is not None and entry.profile.protocol.protocol_hash != protocol_hash:
            raise DisclosureError("source_profile_protocol_mismatch")
        if entry.revoked_at is not None and now >= entry.revoked_at:
            raise DisclosureError("source_profile_revoked")
        if not entry.approval.valid_from <= now < entry.approval.expires_at:
            raise DisclosureError("source_profile_approval_inactive")
        return entry


# Process-local authoritative registry. It starts empty and has no environment,
# request, or database auto-loader.
SERVER_PROFILE_REGISTRY = DisclosureProfileRegistry()


def approved_contract(
    contract_hash: str, *, now: datetime, protocol_hash: str | None = None
) -> bool:
    """Boolean guard for read/coverage/publication paths; current revocation is checked."""
    return SERVER_PROFILE_REGISTRY.approved_contract(
        contract_hash, now=now, protocol_hash=protocol_hash
    )


def require_approved_contract(
    contract_hash: str, *, now: datetime, protocol_hash: str | None = None
) -> ApprovedDisclosureProfile:
    return SERVER_PROFILE_REGISTRY.require_approved_contract(
        contract_hash, now=now, protocol_hash=protocol_hash
    )


def assert_approved_binding(
    registry: DisclosureProfileRegistry,
    grant: object,
    action: object,
    *,
    now: datetime,
    sample_env_id: str,
    expected_protocol_hash: str | None = None,
) -> ApprovedDisclosureProfile:
    """Synchronously re-read current approval for an exact V2 manual action."""
    if getattr(grant, "schema_version", None) != 2 or getattr(grant, "manual_only", None) is not True:
        raise DisclosureError("manual_v2_authorization_required")
    if getattr(grant, "status", "active") != "active":
        raise DisclosureError("authorization_inactive")
    if getattr(grant, "sample_env_id", None) != sample_env_id:
        raise DisclosureError("sample_environment_mismatch")
    valid_from = getattr(grant, "valid_from", None)
    expires_at = getattr(grant, "expires_at", None)
    if valid_from is None or expires_at is None or not valid_from <= now < expires_at:
        raise DisclosureError("authorization_inactive")
    contract_hash = getattr(action, "contract_hash", None)
    profile_hash = getattr(action, "profile_hash", None)
    if not isinstance(contract_hash, str) or not isinstance(profile_hash, str):
        raise DisclosureError("source_profile_binding_mismatch")
    entry = registry.require_approved_contract(
        contract_hash,
        now=now,
        protocol_hash=expected_protocol_hash,
    )
    if profile_hash != entry.profile.protocol.protocol_hash:
        raise DisclosureError("source_profile_binding_mismatch")
    allowlist = tuple(getattr(action, "url_allowlist", ()))
    if getattr(action, "status", "approved") != "approved":
        raise DisclosureError("source_action_not_approved")
    if getattr(action, "contract_hash", None) != entry.profile.contract_hash:
        raise DisclosureError("source_action_contract_mismatch")
    if len(allowlist) > 4 or len(set(allowlist)) != len(allowlist):
        raise DisclosureError("source_action_url_scope_invalid")
    for url in allowlist:
        if url in entry.profile.exact_terms_urls:
            purpose = "terms"
        elif url == "https://www.cninfo.com.cn/new/data/szse_stock.json":
            purpose = "directory"
        elif url == "https://www.cninfo.com.cn/new/hisAnnouncement/query":
            purpose = "query"
        else:
            purpose = "original"
        validate_url(url, purpose, exact_urls=entry.profile.exact_terms_urls)
    return entry

