"""Versioned contracts for the bounded AF-P4-08 campaign sidecar.

V2 grants are intentionally distinct from the legacy V1 wire so an older
reader fails closed instead of silently dropping campaign authority.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from investment_agent.investor.common import Contract, ObjectRef, OwnerId
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
)

CAMPAIGN_FEED = "cninfo_szse_campaign_v2"
CAMPAIGN_CONTRACT_VERSION = "2"
CAMPAIGN_MAX_SECONDS = 7 * 24 * 60 * 60
CAMPAIGN_HTTP_CAP = 32
CAMPAIGN_RESPONSE_BYTES_CAP = 192 * 1024 * 1024
CAMPAIGN_STORAGE_BYTES_CAP = 512 * 1024 * 1024
S0_HTTP_CAP = 8
S0_RESPONSE_BYTES_CAP = 40 * 1024 * 1024
S0_STORAGE_BYTES_CAP = 128 * 1024 * 1024
SAMPLE_HTTP_CAP = 24
SAMPLE_RESPONSE_BYTES_CAP = 152 * 1024 * 1024


class BackgroundDisclosureAuthorizationBodyV2(BackgroundDisclosureAuthorizationBody):
    schema_version: Literal[2] = 2
    campaign_id: UUID
    sample_env_id: str = Field(min_length=1, max_length=128)
    manual_only: Literal[True] = True
    valid_from: AwareDatetime

    @model_validator(mode="after")
    def bounded_validity(self) -> BackgroundDisclosureAuthorizationBodyV2:
        if self.expires_at <= self.valid_from:
            raise ValueError("V2 grant validity must be positive")
        if (self.expires_at - self.valid_from).total_seconds() > 72 * 60 * 60:
            raise ValueError("V2 sample grant validity exceeds 72 hours")
        return self


class BackgroundDisclosureAuthorizationV2(BackgroundDisclosureAuthorizationBodyV2):
    authorization_id: UUID
    revision: int = Field(strict=True, ge=1)
    owner_id: OwnerId = "local_owner"
    created_at: AwareDatetime
    actor: str = Field(min_length=1, max_length=128)
    status: Literal["active", "revoked"] = "active"
    revoked_at: AwareDatetime | None = None
    revocation_reason: str | None = Field(default=None, max_length=256)
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackgroundDisclosureAuthorizationCommandV2(BackgroundDisclosureAuthorizationCommand):
    schema_version: Literal[2] = 2
    body: BackgroundDisclosureAuthorizationBodyV2 | None = None


class AuthorizationCommandResultV2(Contract):
    authorization: BackgroundDisclosureAuthorizationV2
    receipt_id: UUID
    receipt_sequence: int = Field(strict=True, ge=1)
    replayed: bool = False


class CampaignAction(Contract):
    action_id: str = Field(min_length=1, max_length=128)
    kind: Literal["terms", "raw_probe", "sample_poll"]
    status: Literal["approved", "reserved", "disabled"]
    listing_ref: ObjectRef | None = None
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    native_id: str | None = None
    url_allowlist: tuple[str, ...] = ()
    method: Literal["GET", "POST"] = "GET"
    request_byte_cap: int = Field(default=2 * 1024 * 1024, strict=True, ge=0, le=2 * 1024 * 1024)
    http_cap: int = Field(default=8, strict=True, ge=1, le=32)
    response_byte_cap: int = Field(default=64 * 1024 * 1024, strict=True, ge=1, le=192 * 1024 * 1024)
    storage_byte_cap: int = Field(default=128 * 1024 * 1024, strict=True, ge=1, le=512 * 1024 * 1024)
    profile_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    contract_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    body_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    poll_ordinal: int | None = Field(default=None, strict=True, ge=1, le=3)

    @model_validator(mode="after")
    def exact_action(self) -> CampaignAction:
        if self.status == "approved" and not self.url_allowlist:
            raise ValueError("approved campaign action requires exact URL allowlist")
        if self.kind == "sample_poll":
            if self.poll_ordinal is None:
                raise ValueError("sample poll requires fixed ordinal")
            if self.status == "approved" and (
                self.listing_ref is None or self.window_start is None or self.window_end is None
                or not self.url_allowlist or self.profile_hash is None or self.contract_hash is None
            ):
                raise ValueError("approved sample poll action requires exact scope and profile")
            if self.listing_ref is not None and self.listing_ref.kind != "listing":
                raise ValueError("sample poll requires listing reference")
            if self.window_start is not None and self.window_end is not None and self.window_end <= self.window_start:
                raise ValueError("sample poll window must be positive")
            if self.poll_ordinal == 3 and self.status not in ("reserved", "disabled", "approved"):
                raise ValueError("third poll action status invalid")
        return self


class DisclosureCampaignV2(Contract):
    schema_version: Literal[2] = 2
    campaign_id: UUID
    revision: int = Field(strict=True, ge=1)
    approval_ref: str = Field(min_length=1, max_length=256)
    sample_env_id: str = Field(min_length=1, max_length=128)
    database_path: str = Field(min_length=1, max_length=1024)
    database_device: int | None = Field(default=None, strict=True, ge=0)
    database_inode: int | None = Field(default=None, strict=True, ge=0)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    http_cap: int = Field(strict=True, ge=1, le=CAMPAIGN_HTTP_CAP)
    response_bytes_cap: int = Field(strict=True, ge=1, le=CAMPAIGN_RESPONSE_BYTES_CAP)
    storage_bytes_cap: int = Field(strict=True, ge=1, le=CAMPAIGN_STORAGE_BYTES_CAP)
    s0_http_cap: int = Field(strict=True, ge=0, le=S0_HTTP_CAP)
    s0_response_bytes_cap: int = Field(strict=True, ge=0, le=S0_RESPONSE_BYTES_CAP)
    s0_storage_bytes_cap: int = Field(strict=True, ge=0, le=S0_STORAGE_BYTES_CAP)
    sample_http_cap: int = Field(strict=True, ge=0, le=SAMPLE_HTTP_CAP)
    sample_response_bytes_cap: int = Field(strict=True, ge=0, le=SAMPLE_RESPONSE_BYTES_CAP)
    actions: tuple[CampaignAction, ...] = Field(min_length=1, max_length=16)
    status: Literal["active", "paused", "terminated", "unknown"] = "active"
    bound_grants: tuple[dict[str, object], ...] = ()
    http_reserved: int = Field(default=0, strict=True, ge=0)
    http_consumed: int = Field(default=0, strict=True, ge=0)
    response_bytes_reserved: int = Field(default=0, strict=True, ge=0)
    response_bytes_consumed: int = Field(default=0, strict=True, ge=0)
    storage_bytes_reserved: int = Field(default=0, strict=True, ge=0)
    storage_bytes_consumed: int = Field(default=0, strict=True, ge=0)
    s0_http_consumed: int = Field(default=0, strict=True, ge=0)
    s0_response_bytes_consumed: int = Field(default=0, strict=True, ge=0)
    s0_storage_bytes_consumed: int = Field(default=0, strict=True, ge=0)
    s0_http_reserved: int = Field(default=0, strict=True, ge=0)
    s0_response_bytes_reserved: int = Field(default=0, strict=True, ge=0)
    s0_storage_bytes_reserved: int = Field(default=0, strict=True, ge=0)
    sample_http_reserved: int = Field(default=0, strict=True, ge=0)
    sample_http_consumed: int = Field(default=0, strict=True, ge=0)
    sample_response_bytes_reserved: int = Field(default=0, strict=True, ge=0)
    sample_response_bytes_consumed: int = Field(default=0, strict=True, ge=0)
    poll_usage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    action_usage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    active_poll: dict[str, object] | None = None

    @model_validator(mode="after")
    def bounded_campaign(self) -> DisclosureCampaignV2:
        if self.expires_at <= self.valid_from:
            raise ValueError("campaign validity must be positive")
        if (self.expires_at - self.valid_from).total_seconds() > CAMPAIGN_MAX_SECONDS:
            raise ValueError("campaign validity exceeds seven days")
        if self.s0_http_cap > self.http_cap or self.sample_http_cap > self.http_cap:
            raise ValueError("phase cap cannot exceed campaign cap")
        if self.s0_response_bytes_cap > self.response_bytes_cap or self.sample_response_bytes_cap > self.response_bytes_cap:
            raise ValueError("phase byte cap cannot exceed campaign cap")
        if len({item.action_id for item in self.actions}) != len(self.actions):
            raise ValueError("campaign action ids must be unique")
        sample_actions = [action for action in self.actions if action.kind == "sample_poll"]
        ordinals = [action.poll_ordinal for action in sample_actions]
        if len(sample_actions) > 3 or len(set(ordinals)) != len(ordinals):
            raise ValueError("campaign has duplicate or excess sample poll slots")
        if sample_actions and set(ordinals) != {1, 2, 3}:
            raise ValueError("campaign sample manifest must reserve exactly poll slots one through three")
        scoped_sample = [action for action in sample_actions if action.listing_ref is not None]
        if scoped_sample and any(action.listing_ref != scoped_sample[0].listing_ref for action in scoped_sample):
            raise ValueError("campaign sample actions must share one exact listing")
        return self


class DisclosureCampaignCommand(Contract):
    action: Literal["create", "amend", "bind_grant", "pause", "terminate"]
    campaign_id: UUID
    operation_id: UUID
    expected_revision: int = Field(strict=True, ge=0)
    approval_ref: str = Field(min_length=1, max_length=256)
    patch: dict[str, object] = {}

