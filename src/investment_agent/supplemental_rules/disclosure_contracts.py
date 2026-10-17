"""Strict contracts for the bounded CNInfo background disclosure adapter.

Parsing these models grants no authority.  Authorization is loaded and checked
from the P4 journal/source-state projections by ``disclosure_authorization``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from investment_agent.investor.common import Contract, ObjectRef, OwnerId

SOURCE_ID: Literal["cninfo.v1"] = "cninfo.v1"
FEED_ID: Literal["cninfo_szse_v1"] = "cninfo_szse_v1"
AUTH_FEED = "cninfo_szse_auth_v1"
QUOTA_FEED = "cninfo_szse_quota_v1"
DOCUMENT_FEED = "cninfo_szse_document_v1"
SOURCE_POLICY_VERSION = "cninfo_szse_quota_policy_v1"
SOURCE_CONTRACT_VERSION = "1"

# Immutable owner/source ceilings.  A grant may use lower values; issuing a new
# grant or revision cannot increase these aggregate limits.
OWNER_DAILY_HTTP_CAP = 64
OWNER_DAILY_RESPONSE_BYTES_CAP = 256 * 1024 * 1024
OWNER_STORAGE_BYTES_CAP = 512 * 1024 * 1024
OWNER_MIN_REQUEST_INTERVAL_SECONDS = 5
MAX_LISTINGS_PER_GRANT = 3
MAX_ACTIVE_GRANTS_PER_OWNER_SOURCE = 8
MAX_GRANT_SECONDS = 31 * 24 * 60 * 60
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_POLL_HTTP_CAP = 32
MAX_POLL_RESPONSE_BYTES = 64 * 1024 * 1024

DisclosureDocumentType = Literal[
    "annual_report_full",
    "annual_report_correction",
    "annual_report_supplement",
]


class BackgroundDisclosureAuthorizationBody(Contract):
    """User-approved bounds; the server supplies actor and timestamps."""

    source: Literal["cninfo.v1"] = SOURCE_ID
    feed_id: Literal["cninfo_szse_v1"] = FEED_ID
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    listing_refs: tuple[ObjectRef, ...] = Field(min_length=1, max_length=MAX_LISTINGS_PER_GRANT)
    doc_type_allowlist: tuple[DisclosureDocumentType, ...] = Field(min_length=1, max_length=3)
    history_start: AwareDatetime
    history_end: AwareDatetime
    allow_overlap: bool = True
    allow_deep_sweep: bool = False
    min_poll_interval_seconds: int = Field(strict=True, ge=21_600, le=MAX_GRANT_SECONDS)
    min_http_interval_seconds: int = Field(strict=True, ge=OWNER_MIN_REQUEST_INTERVAL_SECONDS, le=86_400)
    expires_at: AwareDatetime
    max_staleness_seconds: int = Field(strict=True, ge=1, le=MAX_GRANT_SECONDS)
    per_poll_query_http: int = Field(strict=True, ge=1, le=10)
    per_poll_fetch: int = Field(strict=True, ge=0, le=20)
    per_poll_http: int = Field(strict=True, ge=1, le=MAX_POLL_HTTP_CAP)
    per_poll_response_bytes: int = Field(strict=True, ge=1, le=MAX_POLL_RESPONSE_BYTES)
    grant_daily_http: int = Field(strict=True, ge=1, le=OWNER_DAILY_HTTP_CAP)
    grant_daily_response_bytes: int = Field(
        strict=True, ge=1, le=OWNER_DAILY_RESPONSE_BYTES_CAP
    )
    storage_bytes_cap: int = Field(strict=True, ge=1, le=OWNER_STORAGE_BYTES_CAP)

    @model_validator(mode="after")
    def exact_scope(self) -> BackgroundDisclosureAuthorizationBody:
        if self.history_end <= self.history_start:
            raise ValueError("history window must be positive")
        if self.expires_at <= self.history_start:
            raise ValueError("authorization expires before its history window")
        if len(set(self.listing_refs)) != len(self.listing_refs):
            raise ValueError("listing refs must be unique")
        for ref in self.listing_refs:
            if ref.kind != "listing" or ref.revision != 1 or ref.field_path is not None:
                raise ValueError("authorization requires exact listing revision 1 refs")
        if self.per_poll_query_http > self.per_poll_http:
            raise ValueError("query HTTP cap cannot exceed total poll HTTP cap")
        return self


class BackgroundDisclosureAuthorizationV1(BackgroundDisclosureAuthorizationBody):
    """Immutable server-side grant snapshot used by every request attempt."""

    authorization_id: UUID
    revision: int = Field(strict=True, ge=1)
    owner_id: OwnerId = "local_owner"
    created_at: AwareDatetime
    actor: str = Field(min_length=1, max_length=128)
    status: Literal["active", "revoked"] = "active"
    revoked_at: AwareDatetime | None = None
    revocation_reason: str | None = Field(default=None, max_length=256)
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackgroundDisclosureAuthorizationCommand(Contract):
    """CAS mutation envelope for one explicit grant operation."""

    action: Literal["create", "amend", "revoke"]
    operation_id: UUID
    authorization_id: UUID
    expected_revision: int = Field(strict=True, ge=0)
    # The owner/source daily policy is independent from the grant and survives
    # revocation/new grant identities.  A value change is an explicit audited
    # policy change guarded by its own CAS revision.
    owner_daily_http: int = Field(strict=True, ge=1, le=OWNER_DAILY_HTTP_CAP)
    owner_daily_response_bytes: int = Field(
        strict=True, ge=1, le=OWNER_DAILY_RESPONSE_BYTES_CAP
    )
    owner_min_request_interval_seconds: int = Field(
        strict=True, ge=OWNER_MIN_REQUEST_INTERVAL_SECONDS, le=86_400
    )
    expected_owner_policy_revision: int = Field(strict=True, ge=0)
    update_owner_policy: bool = False
    body: BackgroundDisclosureAuthorizationBody | None = None
    reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def action_shape(self) -> BackgroundDisclosureAuthorizationCommand:
        if self.action in {"create", "amend"} and self.body is None:
            raise ValueError("create/amend requires an authorization body")
        if self.action == "revoke" and self.body is not None:
            raise ValueError("revoke cannot replace the authorization body")
        if self.action == "create" and self.expected_revision != 0:
            raise ValueError("create requires expected_revision=0")
        if self.action == "revoke" and self.update_owner_policy:
            raise ValueError("revoke cannot change the owner/source quota policy")
        return self


class DisclosureAttemptIdentity(Contract):
    """Frozen identity and scope for one logical request attempt/hop."""

    poll_id: str = Field(min_length=1, max_length=128)
    logical_request_key: str = Field(min_length=1, max_length=200)
    attempt_id: UUID
    attempt_ordinal: int = Field(strict=True, ge=1)
    hop_ordinal: int = Field(strict=True, ge=0, le=2)
    hop_id: str = Field(min_length=1, max_length=128)
    request_kind: Literal["directory", "query", "fetch"]
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: UUID
    authorization_revision: int = Field(strict=True, ge=1)
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    listing_ref: ObjectRef
    window_start: AwareDatetime
    window_end: AwareDatetime
    poll_http_cap: int = Field(strict=True, ge=1, le=MAX_POLL_HTTP_CAP)
    poll_query_http_cap: int = Field(strict=True, ge=1, le=10)
    poll_fetch_cap: int = Field(strict=True, ge=0, le=20)
    poll_response_bytes_cap: int = Field(
        strict=True, ge=1, le=MAX_POLL_RESPONSE_BYTES
    )
    url: str = Field(min_length=8, max_length=2048)
    response_bytes_limit: int = Field(strict=True, ge=1, le=MAX_RESPONSE_BYTES)

    @model_validator(mode="after")
    def exact_request_scope(self) -> DisclosureAttemptIdentity:
        if self.listing_ref.kind != "listing" or self.listing_ref.revision != 1:
            raise ValueError("attempt requires exact listing ref")
        if self.window_end <= self.window_start:
            raise ValueError("attempt window must be positive")
        if self.poll_query_http_cap > self.poll_http_cap:
            raise ValueError("poll query cap cannot exceed total poll HTTP cap")
        if self.request_kind == "fetch" and self.poll_fetch_cap < 1:
            raise ValueError("fetch attempt is forbidden by the frozen poll")
        return self


def authorization_scope_hash(
    authorization_id: UUID,
    authorization_revision: int,
    authorization_hash: str,
    contract_hash: str,
    listing_ref: ObjectRef,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Canonical scope fingerprint shared by poll, page, and HTTP permits."""
    from investment_agent.supplemental_rules.canonical import canonical_hash

    if listing_ref.kind != "listing" or listing_ref.revision != 1:
        raise ValueError("exact listing ref required")
    if window_end <= window_start:
        raise ValueError("window must be positive")
    return canonical_hash({
        "source": SOURCE_ID,
        "feed_id": FEED_ID,
        "authorization_id": str(authorization_id),
        "authorization_revision": authorization_revision,
        "authorization_hash": authorization_hash,
        "contract_hash": contract_hash,
        "listing_ref": listing_ref,
        "window_start": window_start,
        "window_end": window_end,
    })


class HttpHopPermit(Contract):
    permit_id: UUID
    owner_id: OwnerId
    identity: DisclosureAttemptIdentity
    shanghai_day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reserved_response_bytes: int = Field(strict=True, ge=0)
    storage_reserved_bytes: int = Field(strict=True, ge=0)
    state: Literal["reserved", "sent", "result", "canceled", "unknown"]
    created_at: AwareDatetime


class HttpHopSettlement(Contract):
    permit_id: UUID
    state: Literal["result", "unknown"]
    shanghai_day: str
    request_consumed: Literal[0, 1]
    response_bytes_consumed: int = Field(strict=True, ge=0)
    storage_bytes_consumed: int = Field(strict=True, ge=0)
    result_code: str = Field(min_length=1, max_length=64)


class AuthorizationCommandResult(Contract):
    authorization: BackgroundDisclosureAuthorizationV1
    receipt_id: UUID
    receipt_sequence: int = Field(strict=True, ge=1)
    replayed: bool = False

