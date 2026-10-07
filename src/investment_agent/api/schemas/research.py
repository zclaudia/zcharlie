"""Pydantic models for evidence-first research endpoints."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SingleStockResearchRequest(BaseModel):
    """``provider``: ``snapshot`` (default, free-source snapshot store, no iFinD
    call) or ``ifind`` (manual deep research through ifind-proxy, counted in
    the iFinD budget ledger; needs ``industry`` and ``report_year``)."""

    ticker: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    industry: str | None = Field(None, min_length=1, max_length=128)
    report_year: int | None = Field(None, ge=1990, le=2100)
    as_of: date | None = None
    provider: Literal["snapshot", "ifind"] = "snapshot"

    @model_validator(mode="after")
    def _ifind_inputs(self) -> "SingleStockResearchRequest":
        if self.provider == "ifind" and (self.industry is None or self.report_year is None):
            raise ValueError("provider=ifind needs industry and report_year")
        return self

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class ResearchRunAccepted(BaseModel):
    run_id: int
    status: str
    stage: str
    created: bool
    status_url: str
    result_url: str


class ResearchArtifactOut(BaseModel):
    name: str
    download_url: str


class ResearchRunSummary(BaseModel):
    run_id: int
    status: str
    stage: str
    ticker: str | None
    company_name: str | None
    industry: str | None
    as_of: date | None
    report_year: int | None
    business_score: float | None
    workflow_label: str | None
    needs_agent_review: bool | None
    has_meaningful_changes: bool | None
    meaningful_change_count: int | None
    attempt_count: int
    max_attempts: int
    worker_id: str | None
    started_at: datetime
    finished_at: datetime | None
    error_summary: str | None


class ResearchRunResult(BaseModel):
    ticker: str
    company_name: str
    industry: str
    as_of: date
    report_year: int
    canonical_schema_version: str
    scoring_version: str
    score_schema_version: str
    score: dict[str, object]
    explanation: dict[str, object] | None
    artifacts: list[ResearchArtifactOut]


class ResearchChangeSetOut(BaseModel):
    schema_version: str
    baseline: bool
    previous_run_id: int | None
    current_run_id: int
    has_meaningful_changes: bool
    change_count: int
    meaningful_change_count: int
    category_counts: dict[str, int]
    changes: list[dict[str, object]]
    notification_url: str | None = None


class ResearchRunDetail(ResearchRunSummary):
    request: dict[str, object]
    result: ResearchRunResult | None
    change: ResearchChangeSetOut | None
