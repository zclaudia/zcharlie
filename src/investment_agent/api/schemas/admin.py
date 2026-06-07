"""/watchlist + /admin endpoint schemas."""

from datetime import datetime

from pydantic import BaseModel


class WatchlistItemOut(BaseModel):
    listing_id: int
    ticker: str | None = None
    market_code: str | None = None
    added_at: datetime
    note: str | None


class WatchlistAdd(BaseModel):
    ticker: str
    market_code: str = "NASDAQ"
    note: str | None = None


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
