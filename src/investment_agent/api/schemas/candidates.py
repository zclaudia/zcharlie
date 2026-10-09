"""Schemas of the candidate, rule and screen-run endpoints (P1-D3).

No 0-100 score appears here (funnel design 3/11): candidates carry their
structured screen reasons; ``rank`` is the only ordering metadata.
``calibration_status`` is passed through so the UI can mark uncalibrated
templates ("未校准").
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from investment_agent.rules.schema import TEMPLATE_ID

CandidateStatus = Literal["open", "promoted", "dismissed", "expired"]
DraftState = Literal["none", "pending", "ready", "failed"]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
TICKER_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"


def _required_text(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace")
        return stripped
    return value


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


class CandidateSourceOut(BaseModel):
    kind: Literal["screen", "manual"]
    screen_run_id: int | None = None
    template_id: str | None = None
    template_version: int | None = None
    screen_result_id: int | None = None
    as_of: date | None = None


class ScreenReasonOut(BaseModel):
    """One condition of a screen rule: threshold, actual value and the
    snapshot it came from (``snapshot_ref``)."""

    rule_path: str | None = None
    factor: str | None = None
    op: str | None = None
    threshold: Any = None
    param: Any = None
    actual: float | bool | None = None
    quality: str | None = None
    flags: list[str] = Field(default_factory=list)
    outcome: str | None = None
    snapshot_ref: dict[str, Any] = Field(default_factory=dict)
    detail: str | None = None
    unit: str | None = None
    """The factor's unit from the factor registry (display only)."""


class ScreenResultOut(BaseModel):
    screen_result_id: int
    screen_run_id: int
    as_of: date
    template_id: str
    template_version: int
    template_ref: str
    calibration_status: str | None
    outcome: str
    rank: int | None
    reasons: list[ScreenReasonOut]
    context: dict[str, Any]
    """Member-level provenance (provider, as_of, industry match)."""


class ParamRangeOut(BaseModel):
    min: float
    max: float


class RuleSummaryOut(BaseModel):
    rule_instance_id: int
    listing_id: int
    ticker: str
    scope: str
    template_id: str
    template_version: int
    template_ref: str
    calibration_status: str | None
    version: int
    status: str
    params: dict[str, float]
    """The instance's parameter overrides."""
    drafted_by: str


class RuleDetailOut(RuleSummaryOut):
    definition: dict[str, Any]
    draft_rationale: str
    approved_by: str | None
    approved_at: datetime | None
    retired_at: datetime | None
    created_at: datetime
    template_params: dict[str, float]
    param_ranges: dict[str, ParamRangeOut]
    effective_params: dict[str, float]
    zones: dict[str, Any] = Field(default_factory=dict)
    """The zones in force for the rule's scope: the instance's own zones, or
    the template's when the instance only overrides parameters. Condition
    values may still be ``"$param"`` references (resolve with
    ``effective_params``)."""
    factor_units: dict[str, str] = Field(default_factory=dict)
    """Unit of every factor the zones reference (display only)."""


class DecisionOut(BaseModel):
    id: int
    kind: str
    actor: str
    ticker: str
    reason_text: str | None
    rule_instance_id: int | None
    rule_ref: str | None
    candidate_id: int | None
    at: datetime
    context: dict[str, Any]


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #


class CandidateSummaryOut(BaseModel):
    id: int
    listing_id: int
    ticker: str
    name: str | None
    status: CandidateStatus
    opened_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    sources: list[CandidateSourceOut]
    template_refs: list[str]
    calibration_status: str | None
    """Of the newest source's template; ``uncalibrated`` until P3."""
    latest_screen: ScreenResultOut | None
    """Newest screen result: its ``reasons`` are the candidate's latest reasons."""
    draft_state: DraftState
    draft_rule: RuleSummaryOut | None
    note: str | None


class CandidatePageOut(BaseModel):
    items: list[CandidateSummaryOut]
    total: int
    limit: int
    offset: int


class SegmentOut(BaseModel):
    """One 主营构成 line (mrd-intake C3): a fact shown on the detail page, never ranked."""

    report_period: date
    kind: str
    name: str
    revenue: float | None
    """CNY."""
    share: float | None
    """Fraction of main-business revenue."""
    margin: float | None
    """Gross margin, fraction."""
    profit: float | None = None


class CandidateDetailOut(CandidateSummaryOut):
    screen_results: list[ScreenResultOut]
    """Newest screen result per template among the sources."""
    draft_rule_detail: RuleDetailOut | None
    decisions: list[DecisionOut]
    segments: list[SegmentOut] = Field(default_factory=list)
    """Latest-period 主营构成 (revenue top 5); empty when not collected."""


class CandidateNominateRequest(BaseModel):
    ticker: str = Field(pattern=TICKER_PATTERN)
    template_id: str | None = Field(default=None, pattern=TEMPLATE_ID)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("ticker", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class CandidateNominateOut(BaseModel):
    candidate: CandidateDetailOut
    created: bool
    draft_run_id: int | None


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return _required_text(value)


class PromoteRequest(BaseModel):
    rule_instance_id: int | None = Field(default=None, ge=1)
    params_override: dict[str, FiniteFloat] | None = None
    reason: str | None = Field(default=None, max_length=2000)


class WatchlistEntryOut(BaseModel):
    listing_id: int
    ticker: str
    industry: str | None
    industry_source: str | None
    report_year: int | None
    report_year_source: str
    enabled: bool


class PromoteOut(BaseModel):
    candidate: CandidateSummaryOut
    rule: RuleSummaryOut
    retired_rule_instance_id: int | None
    watchlist: WatchlistEntryOut
    decisions: list[DecisionOut]
    """The decisions this promotion wrote, in order (edit_rule?, approve_rule?, promote)."""
    verification_requested: bool
    research_run_id: int | None
    warnings: list[str]


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


class RulePatchRequest(BaseModel):
    params: dict[str, FiniteFloat] = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=2000)


class RuleDecisionOut(BaseModel):
    rule: RuleDetailOut
    decision: DecisionOut


# --------------------------------------------------------------------------- #
# Screen runs
# --------------------------------------------------------------------------- #


class ScreenHitOut(BaseModel):
    screen_result_id: int
    listing_id: int
    ticker: str
    rank: int | None
    reasons: list[ScreenReasonOut]
    candidate_id: int | None
    """The open candidate of the listing, if any."""


class ScreenTemplateOut(BaseModel):
    template_ref: str
    template_id: str
    template_version: int
    calibration_status: str | None
    counts: dict[str, int]
    hits: list[ScreenHitOut]


class ScreenRunOut(BaseModel):
    run_id: int
    status: str
    stage: str
    universe_code: str | None
    as_of: date | None
    started_at: datetime
    finished_at: datetime | None
    error_summary: str | None
    member_count: int | None
    templates: list[ScreenTemplateOut]
    candidates: dict[str, Any] | None
    """Candidate bookkeeping of the run (created / merged / skipped / expired)."""


class RedraftRequest(BaseModel):
    template_id: str | None = Field(default=None, min_length=1, max_length=64)


class RedraftOut(BaseModel):
    candidate: CandidateDetailOut
    draft_run_id: int
