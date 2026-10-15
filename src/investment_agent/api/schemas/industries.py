"""Wire contracts for the saved, read-only industry overview."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class IndustrySummaryOut(BaseModel):
    cohort_key: str
    metric_pack: Literal["baijiu_annual_v1", "bank_annual_v1"]
    revision: int
    observed_on: date
    acquired_at: datetime
    verification: str
    source: dict[str, Any] = Field(default_factory=dict)
    candidate_count: int
    periods: list[date] = Field(default_factory=list)


class IndustryListOut(BaseModel):
    enabled: bool
    industries: list[IndustrySummaryOut] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class IndustryOverviewOut(BaseModel):
    enabled: bool = True
    cohort_key: str
    metric_pack: Literal["baijiu_annual_v1", "bank_annual_v1"] | None = None
    cohort_ref: dict[str, Any] = Field(default_factory=dict)
    cohort_content_hash: str | None = None
    observed_on: date | None = None
    acquired_at: datetime | None = None
    verification: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    candidate_count: int = 0
    available_periods: list[date] = Field(default_factory=list)
    report_period: date | None = None
    batch_run_id: str | None = None
    batch_status: str | None = None
    generation: int | None = None
    submitted_at: datetime | None = None
    comparison_target: dict[str, str] | None = None
    context_status: str | None = None
    stale_reasons: list[str] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    companies: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
