"""/listings endpoint schemas."""

from datetime import date

from pydantic import BaseModel

# Legacy ticker shape: US symbols (AAPL, BRK.B, BRK-B) and A-share codes
# (600519.SH). Also used by the watchlist add body after upper-casing.
TICKER_PATTERN = r"^[A-Za-z0-9]{1,10}(?:[.\-][A-Za-z0-9]{1,4})?$"


class ListingOut(BaseModel):
    listing_id: int
    company_id: int
    ticker: str
    market_code: str
    company_name: str
    sector_code: str | None


class ListingScoreOut(BaseModel):
    pipeline_run_id: int
    strategy_code: str
    tier: int
    score: int
    confidence: float
    bull_thesis: str | None
    bear_thesis: str | None


class RelationshipOut(BaseModel):
    id: int
    from_company_id: int
    to_company_id: int
    counterparty_name: str | None
    counterparty_ticker: str | None
    relationship_type: str
    strength: float
    confidence: float
    source: str
    valid_from: date
    valid_to: date | None


class RelationshipCreate(BaseModel):
    counterparty_ticker: str | None = None
    counterparty_name: str
    relationship_type: str
    strength: float = 0.5
    description: str | None = None
