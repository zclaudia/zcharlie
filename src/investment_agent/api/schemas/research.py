"""Pydantic models for evidence-first research endpoints."""

from datetime import date

from pydantic import BaseModel, Field, field_validator


class SingleStockResearchRequest(BaseModel):
    ticker: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    industry: str = Field(min_length=1, max_length=128)
    report_year: int = Field(ge=1990, le=2100)
    as_of: date | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class SingleStockResearchResponse(BaseModel):
    run_id: int
    status: str
    ticker: str
    company_name: str
    business_score: float
    research_confidence_score: float
    workflow_label: str
    needs_agent_review: bool
    artifact_dir: str
    report_path: str


class ResearchSnapshotResponse(BaseModel):
    run_id: int
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
    artifacts: dict[str, object]
