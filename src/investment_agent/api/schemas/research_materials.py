"""/research listing material schemas: the read model and the refresh entry (plan 09).

The wire shapes are declared explicitly (frozen, no unknown fields) instead of
serialising the read model's dataclasses, so a field added for an internal
caller cannot leak into the product response unnoticed. Every amount is a
decimal string (never a JSON float): the source's own figure, its unit and its
currency travel separately and a missing amount travels as null with its reason.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FiscalYearOut(BaseModel):
    """One real fiscal year of the material (never a calendar year)."""

    model_config = ConfigDict(extra="forbid")

    end: date
    label: str | None = None
    days: int | None = None
    """Day span: the source's stated value, else the real start-end span."""
    basis: str


class FactOut(BaseModel):
    """One material fact with its definition, provenance and verification state."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    label: str
    period_end: date
    basis: str
    amount: str | None = None
    unit: str
    currency: str | None = None
    missing_reason: str | None = None
    """Why the source states no amount (a missing amount is never 0)."""
    definition: str
    verification: str
    """``candidate`` (not yet checked against the original) or ``verified``."""
    conflict: str
    """``none`` / ``unresolved`` / ``resolved``, passed through as stored."""
    source: str
    """Kind of original document (never a supplier endpoint name)."""
    evidence_kind: str = "reported"
    """Whether the value is directly reported or a separately evidenced calculation."""
    source_date: date | None = None
    locator: str | None = None
    """Page number, snippet or table locator in the original document."""


class MetricOut(BaseModel):
    """One computed metric: status, value and the caveats that qualify it."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    status: str
    value: str | None = None
    unit: str
    currency: str | None = None
    basis: str
    periods: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class StatusOut(BaseModel):
    """One business status: the affected functions and the next step."""

    model_config = ConfigDict(extra="forbid")

    code: str
    affected: tuple[str, ...] = ()
    next_step: str


class FreshnessOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_requested_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_imported_at: datetime | None = None
    last_verified_on: date | None = None
    latest_period_end: date | None = None
    latestness: str
    latestness_checked_on: date | None = None
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RefreshLifecycleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int | None = None
    status: str | None = None
    reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class MetricCoverageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicable: bool
    evidence_status: str
    usable_facts: int
    verified_facts: int


class FinancialContextResponse(BaseModel):
    """The read-only financial context of one listing."""

    model_config = ConfigDict(extra="forbid")

    listing_id: int
    market: str
    display_code: str
    company_name: str
    security_type: str
    quote_currency: str
    report_currency: str
    covered: bool
    """Whether the security is in the hand-checked identity list."""
    fiscal_years: tuple[FiscalYearOut, ...] = ()
    facts: tuple[FactOut, ...] = ()
    metrics: tuple[MetricOut, ...] = ()
    capabilities: dict[str, bool]
    """``financials`` / ``price`` / ``annual_pe`` / ``dividends`` / ``pb`` /
    ``online_refresh``; a capability the first delivery does not ship is false."""
    coverage: dict[str, int]
    """``periods_covered`` plus ``facts_total`` / ``facts_candidate`` /
    ``facts_verified`` — material coverage and verification coverage are
    counted separately."""
    statuses: tuple[StatusOut, ...] = ()
    freshness: FreshnessOut = Field(default_factory=lambda: FreshnessOut(latestness="unknown"))
    refresh: RefreshLifecycleOut = Field(default_factory=RefreshLifecycleOut)
    metric_coverage: dict[str, MetricCoverageOut] = Field(default_factory=dict)


class RefreshBody(BaseModel):
    """Optional knobs of one refresh request (all fields optional).

    ``sources``/``periods`` empty or absent mean the licensed default: the
    checked source set and the adapter's own default period window. Explicit
    unsupported HK periods are rejected before enqueue; SEC periods are retained.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[str] | None = None
    periods: list[str] | None = None
    reason: str | None = None


class RefreshResponse(BaseModel):
    """One queued refresh run; a queued run is not a claim that data was fetched."""

    model_config = ConfigDict(extra="forbid")

    run_id: int
    created: bool
    """``False`` when an identical request already had a run (deduplicated)."""
    idempotency_key: str
    adapter_versions: dict[str, str]
    online_refresh_enabled: bool
    """Whether at least one requested source currently passes every fetch gate."""
    source_decisions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-source decision; queue registration never authorizes a worker fetch."""
