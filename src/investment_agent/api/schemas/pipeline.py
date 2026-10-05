"""Pydantic models for /pipelines endpoints."""

from datetime import date, datetime

from pydantic import BaseModel


class DiscoveryRunRequest(BaseModel):
    universe_code: str = "NASDAQ_100"
    as_of: date | None = None  # defaults to today server-side


class PipelineRunCreated(BaseModel):
    run_id: int
    status: str
    started_at: datetime


class PipelineRunOut(BaseModel):
    run_id: int
    kind: str
    universe_code: str | None
    listing_id: int | None
    status: str
    stage: str
    attempt_count: int
    heartbeat_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    error_summary: str | None
    metadata: dict[str, object]


class StockScoreOut(BaseModel):
    listing_id: int
    strategy_code: str
    tier: int
    score: int
    confidence: float
    bull_thesis: str | None
    bear_thesis: str | None
