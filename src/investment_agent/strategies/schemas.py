"""Per-strategy output schemas. Subclass StockScore / DeepAnalysis with extra fields if needed."""

from pydantic import Field

from investment_agent.domain.strategy_types import DeepAnalysis, StockScore


class ValueStockScore(StockScore):
    intrinsic_value_estimate: float | None = None
    margin_of_safety_pct: float | None = None


class ValueDeepAnalysis(DeepAnalysis):
    dcf_summary: str | None = None
    owner_earnings_yield: float | None = None


class GrowthStockScore(StockScore):
    tam_estimate: str | None = None
    revenue_growth_3y_cagr: float | None = None


class GrowthDeepAnalysis(DeepAnalysis):
    growth_drivers: list[str] = Field(default_factory=list)
    expansion_runway: str | None = None


class RiskAggressiveStockScore(StockScore):
    catalyst_summary: str
    short_interest_pct: float | None = None
    iv_rank: float | None = None


class RiskAggressiveDeepAnalysis(DeepAnalysis):
    technical_setup: str | None = None
    sentiment_score: float | None = None


SCHEMAS_BY_CODE: dict[str, tuple[type[StockScore], type[DeepAnalysis]]] = {
    "value": (ValueStockScore, ValueDeepAnalysis),
    "growth": (GrowthStockScore, GrowthDeepAnalysis),
    "risk_aggressive": (RiskAggressiveStockScore, RiskAggressiveDeepAnalysis),
}
