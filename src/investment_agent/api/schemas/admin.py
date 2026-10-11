"""/watchlist + /admin endpoint schemas."""

import math
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Ticker shape: A-share codes (600519.SH) plus plain alphanumeric symbols;
# validated after upper-casing.
TICKER_PATTERN = r"^[A-Za-z0-9]{1,10}(?:[.\-][A-Za-z0-9]{1,4})?$"


class WatchlistLatestResearch(BaseModel):
    """Summary of the newest research snapshot for one watchlist entry."""

    run_id: int
    as_of: date
    strategy_label: str | None = None
    investment_score: float | None = None
    research_confidence_score: float | None = None
    freshness_ok: bool | None = None
    needs_agent_review: bool | None = None
    scoring_version: str | None = None
    changed_at: datetime | None = None
    meaningful_change_count: int | None = None


class WatchlistRuleRequestOut(BaseModel):
    """The entry's current drafting request (plan 1.5)."""

    id: str
    status: str
    template_ref: str
    error: str | None = None
    reason: str | None = None


class WatchlistRuleInstanceOut(BaseModel):
    rule_instance_id: int
    version: int
    template_id: str
    template_version: int
    template_ref: str
    status: str
    scope: str | None = None
    params: dict[str, float]
    """Effective values (template defaults merged with the instance overrides)."""
    overrides: dict[str, float] = Field(default_factory=dict)
    calibration_status: str | None = None
    drafted_by: str | None = None
    definition: dict[str, Any] | None = None
    zones: dict[str, Any] | None = None
    param_ranges: dict[str, dict[str, float]] = Field(default_factory=dict)
    factor_units: dict[str, str] = Field(default_factory=dict)


class ZoneEventOut(BaseModel):
    """One zone event, always carrying the rule version that triggered it (WP8)."""

    id: int
    as_of: date
    listing_id: int
    ticker: str
    scope: str
    zone: str
    transition: str
    level: str
    rule_instance_id: int
    rule_version: int
    template_id: str
    template_version: int
    template_ref: str
    rule_ref: str
    calibration_status: str
    conditions_met: list[dict[str, Any]] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    input_digest: str
    valuation_ids: list[str] = Field(default_factory=list)
    ledger_revision: int | None = None
    notified_at: datetime | None = None
    created_at: datetime | None = None


class ZoneIntervalOut(BaseModel):
    """Whether a listing is currently inside a zone, and since when."""

    inside: bool = False
    zone: str | None = None
    entered_as_of: date | None = None
    rule_instance_id: int | None = None
    notified_at: datetime | None = None


class ZoneEventsOut(BaseModel):
    """The interval state plus the newest entering event of one rule scope."""

    interval: ZoneIntervalOut = Field(default_factory=ZoneIntervalOut)
    zones: dict[str, ZoneIntervalOut] = Field(default_factory=dict)
    latest: ZoneEventOut | None = None


class WatchlistRuleOut(BaseModel):
    """Rule state of one observation entry: draft, active rule, request."""

    state: str
    request: WatchlistRuleRequestOut | None = None
    draft: WatchlistRuleInstanceOut | None = None
    active: WatchlistRuleInstanceOut | None = None
    zone_events: ZoneEventsOut | None = None


class WatchlistItemOut(BaseModel):
    listing_id: int
    ticker: str
    market_code: str
    name: str
    industry: str | None
    provider: str
    report_year: int | None
    enabled: bool
    added_at: datetime
    updated_at: datetime
    note: str | None
    last_refresh_status: str
    last_refreshed_at: datetime | None
    last_refresh_error: str | None
    last_research_run_id: int | None
    latest_research: WatchlistLatestResearch | None = None
    rule: WatchlistRuleOut | None = None


class WatchlistUpdate(BaseModel):
    enabled: bool


class WatchlistAdd(BaseModel):
    ticker: str = Field(min_length=1, max_length=32, pattern=TICKER_PATTERN)
    market_code: str | None = Field(None, min_length=1, max_length=16)
    name: str | None = Field(None, min_length=1, max_length=256)
    industry: str | None = Field(None, min_length=1, max_length=128)
    provider: str | None = Field(None, min_length=1, max_length=32)
    report_year: int | None = Field(None, ge=1990, le=2100)
    enabled: bool | None = None
    note: str | None = Field(None, max_length=500)
    template_id: str | None = Field(None, min_length=1, max_length=64)
    """Chosen drafting template (plan 1.5); omitted = needs_template."""

    @field_validator("ticker", "market_code", mode="before")
    @classmethod
    def normalize_code(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("name", "industry", "note", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class WatchlistRemove(BaseModel):
    """Removal requires a reason; it is journaled as a ``remove_watch`` decision."""

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class WatchlistRuleDraftRequest(BaseModel):
    template_id: str | None = Field(None, min_length=1, max_length=64)


class WatchlistRuleApproveRequest(BaseModel):
    rule_instance_id: int = Field(ge=1)
    reason: str | None = Field(None, max_length=1000)
    expected_active_rule_instance_id: int | None = Field(None, ge=1)


class WatchlistRuleEditRequest(BaseModel):
    rule_instance_id: int = Field(ge=1)
    params: dict[str, float] = Field(min_length=1)
    reason: str | None = Field(None, max_length=1000)

    @field_validator("params")
    @classmethod
    def finite_params(cls, value: dict[str, float]) -> dict[str, float]:
        for name, number in value.items():
            if not math.isfinite(number):
                raise ValueError(f"param {name!r} must be finite")
        return value


class WatchlistRuleRejectRequest(BaseModel):
    rule_instance_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TemplateSuggestionOut(BaseModel):
    """Best-effort template suggestion; null fields when nothing could be picked."""

    template_id: str | None = None
    template_version: int | None = None
    name: str | None = None
    reason: str | None = None
    model: str | None = None


class WatchlistRefreshRequest(BaseModel):
    as_of: date | None = None


class WatchlistRefreshItemOut(BaseModel):
    listing_id: int
    ticker: str | None
    run_id: int | None
    status: str
    created: bool
    error: str | None


class WatchlistRefreshOut(BaseModel):
    as_of: date
    total: int
    created_runs: int
    reused_runs: int
    failed: int
    items: list[WatchlistRefreshItemOut]


class LLMCallOut(BaseModel):
    id: int
    pipeline_run_id: int | None
    purpose: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    started_at: datetime
    success: bool


class LLMCostSummary(BaseModel):
    month_start: str
    total_calls: int
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    by_purpose: list[dict[str, object]]
