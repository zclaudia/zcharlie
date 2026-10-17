"""Persistent journal contracts for P4 supplemental updates."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from investment_agent.investor.common import OwnerId

SupplementalChangeKind = Literal[
    "rule_revision",
    "rule_lifecycle",
    "research_event",
    "evidence_revision",
    "material_revision",
    "source_page",
    "source_failure",
    "due_boundary",
    "bootstrap",
    "scope_member",
    "source_authorization",
    "source_attempt",
    "evaluation_admission",
    "scope_outcome",
    "scope_handoff",
]
SupplementalCoverageState = Literal["complete", "partial", "unknown", "failed"]


class SupplementalChangeReceipt(BaseModel):
    """Immutable, owner-scoped record of a committed change or journal member."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=1)
    id: UUID
    owner_id: OwnerId
    change_key: str = Field(min_length=1, max_length=200)
    kind: SupplementalChangeKind
    operation_id: UUID | None = None
    request_hash: str | None = None
    parent_sequence: int | None = Field(default=None, ge=1)
    listing_id: int | None = Field(default=None, ge=1)
    context: str | None = Field(default=None, min_length=1, max_length=32)
    origin_run_id: int | None = Field(default=None, ge=1)
    source_manifest_hash: str | None = None
    acquired_at: datetime
    available_from: datetime
    committed_at: datetime
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any]

    @field_validator("request_hash", "source_manifest_hash")
    @classmethod
    def _optional_hash(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("hash must be lowercase SHA-256 hex")
        return value


class SupplementalSourceState(BaseModel):
    """Mutable CAS snapshot for one owner/feed/scope."""

    model_config = ConfigDict(extra="forbid")

    owner_id: OwnerId
    feed_id: str
    scope_key: str
    revision: int = Field(ge=0)
    listing_id: int | None = Field(default=None, ge=1)
    context: str | None = None
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_version: str = "v1"
    scope_hash: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    window_start: datetime | None = None
    window_end: datetime | None = None
    complete_watermark: datetime | None = None
    coverage_state: SupplementalCoverageState = "unknown"
    success_sequence: int = Field(default=0, ge=0)
    success_receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_code: str | None = None
    retry_at: datetime | None = None
    health_limit_seconds: int = Field(default=0, ge=0)
    payload: dict[str, Any]


class SupplementalRefreshState(BaseModel):
    """Mutable per-subject refresh/consumption projection."""

    model_config = ConfigDict(extra="forbid")

    owner_id: OwnerId
    listing_id: int
    context: str
    revision: int = Field(ge=0)
    confirmed_sequence: int = Field(default=0, ge=0)
    requested_sequence: int = Field(default=0, ge=0)
    active_run_id: int | None = Field(default=None, ge=1)
    next_due_at: datetime | None = None
    last_success: datetime | None = None
    blocked_reason: str | None = None
    payload: dict[str, Any]
