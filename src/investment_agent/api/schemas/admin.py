"""/watchlist + /admin endpoint schemas."""

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


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


class WatchlistUpdate(BaseModel):
    enabled: bool


class WatchlistAdd(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    market_code: str | None = Field(None, min_length=1, max_length=16)
    name: str | None = Field(None, min_length=1, max_length=256)
    industry: str | None = Field(None, min_length=1, max_length=128)
    provider: str | None = Field(None, min_length=1, max_length=32)
    report_year: int | None = Field(None, ge=1990, le=2100)
    enabled: bool | None = None
    note: str | None = None

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
