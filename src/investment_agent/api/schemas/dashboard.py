"""Pydantic models for the browser dashboard read models."""

from datetime import date, datetime

from pydantic import BaseModel


class DashboardEntry(BaseModel):
    run_id: int
    ticker: str | None = None
    name: str | None = None
    status: str | None = None
    stage: str | None = None
    attempt_count: int | None = None
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    changed_at: datetime | None = None
    summary: str | None = None


class DashboardBucket(BaseModel):
    count: int
    entries: list[DashboardEntry]


class DashboardToday(BaseModel):
    running: DashboardBucket
    failed: DashboardBucket
    partial: DashboardBucket
    needs_review: DashboardBucket
    meaningful_changes: DashboardBucket
    generated_at: datetime


class StockSnapshotSummary(BaseModel):
    run_id: int
    as_of: date
    report_year: int
    strategy_label: str | None = None
    investment_score: float | None = None
    business_score: float | None = None
    research_confidence_score: float | None = None
    needs_agent_review: bool | None = None
    canonical_schema_version: str
    scoring_version: str
    score_schema_version: str
    has_meaningful_changes: bool | None = None
    meaningful_change_count: int | None = None
    created_at: datetime
