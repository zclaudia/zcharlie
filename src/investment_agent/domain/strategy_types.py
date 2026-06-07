"""Strategy-layer DTOs: ListingSnapshot, StockScore, DeepAnalysis, ScoringContext, DeepAnalysisContext."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _DTOBase(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ListingSnapshot(_DTOBase):
    """One stock's current state — used as Tier 1 screening input."""

    listing_id: int
    company_id: int
    ticker: str
    market_code: str
    company_name: str
    sector_code: str | None = None

    # FundamentalSnapshot fields (latest as of as_of date)
    as_of: date
    pe: Decimal | None = None
    pb: Decimal | None = None
    peg: Decimal | None = None
    ps: Decimal | None = None
    roe: Decimal | None = None
    roa: Decimal | None = None
    fcf_yield: Decimal | None = None
    debt_to_equity: Decimal | None = None
    market_cap: Decimal | None = None

    # Latest financial report extracts (used by some ScreeningRule conditions)
    revenue_ttm: Decimal | None = None
    net_income_ttm: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None
    gross_margin: Decimal | None = None
    beta: Decimal | None = None
    volatility_30d: Decimal | None = None


class ScoringContext(_DTOBase):
    """Tier 2 LLM scoring input — listing + recent peers + sector context."""

    listing: ListingSnapshot
    peer_summary: list[dict[str, object]] = Field(default_factory=list)
    recent_news_titles: list[str] = Field(default_factory=list)
    recent_rating_changes: list[dict[str, object]] = Field(default_factory=list)


class DeepAnalysisContext(_DTOBase):
    """Tier 3 deep analysis input — much richer."""

    listing: ListingSnapshot
    financials_history: list[dict[str, object]] = Field(default_factory=list)
    peer_comparables: list[dict[str, object]] = Field(default_factory=list)
    known_relationships: list[dict[str, object]] = Field(default_factory=list)
    auxiliary_context: list[dict[str, object]] = Field(default_factory=list)
    recent_news_full: list[dict[str, object]] = Field(default_factory=list)


class StockScore(_DTOBase):
    """Tier 2 output — structured score."""

    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    bull_thesis: str
    bear_thesis: str
    key_metrics_explained: dict[str, str] = Field(default_factory=dict)


class DeepAnalysis(_DTOBase):
    """Tier 3 output — full deep dive."""

    business_model: str
    moat_assessment: str
    risk_factors: list[str]
    upstream_dependencies: list[str]
    downstream_customers: list[str]
    competitor_overview: list[str]
    valuation_summary: str
    extracted_relationships: list["ExtractedRelationship"] = Field(default_factory=list)


class ExtractedRelationship(_DTOBase):
    """LLM-extracted company relationship — written to company_relationship table."""

    counterparty_name: str
    counterparty_ticker: str | None = None
    relationship_type: str  # 'supplier_of' | 'competes_with' | 'subsidiary_of' | 'partner_of'
    strength: float = Field(ge=0.0, le=1.0)
    revenue_share_pct: float | None = None
    description: str
    evidence: str


# Re-export for typing
DeepAnalysis.model_rebuild()
