"""/attribute endpoint schemas."""

from datetime import date, datetime

from pydantic import BaseModel


class AttributionRunRequest(BaseModel):
    as_of: date | None = None
    benchmark_ticker: str = "SPY"
    market_code: str = "NASDAQ"


class AttributionRunCreated(BaseModel):
    run_id: int
    status: str
    started_at: datetime


class AttributionReportOut(BaseModel):
    run_id: int
    listing_id: int
    ticker: str | None = None
    analysis_date: date
    price_change_pct: float
    narrative: str
    valuation_delta: str | None
    triggering_events: list[dict[str, object]]
    beta_attribution: dict[str, object]
    markdown_path: str | None
