"""Versioned canonical contracts for evidence-first A-share research.

Provider adapters must produce these models before data can enter the deterministic
research kernel.  Unknown numeric values are represented by ``None``; provenance and
quality information travel with the value instead of being reconstructed later.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CANONICAL_SCHEMA_VERSION: Literal["ifind-canonical.v1"] = "ifind-canonical.v1"


class CanonicalModel(BaseModel):
    """Strict base model used at the provider/kernel boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FieldSource(CanonicalModel):
    field: str
    provider: str
    source: str
    quality: Literal["direct", "derived", "proxy", "missing"]
    updated_at: datetime
    note: str = ""


class QualityFlag(CanonicalModel):
    field: str
    flag: str
    message: str
    provider: str
    severity: Literal["info", "review", "warning", "error"]


class CanonicalMetadata(CanonicalModel):
    source: str
    updated_at: datetime
    provider: str
    payload_type: Literal["stock_pool", "market", "financial"]
    schema_version: Literal["ifind-canonical.v1"] = CANONICAL_SCHEMA_VERSION
    report_year: int | None = None


class AuditedItem(CanonicalModel):
    """Fields shared by every canonical item."""

    provenance_fields: ClassVar[tuple[str, ...]] = ()

    symbol: str = Field(pattern=r"^\d{6}$")
    source: str
    updated_at: datetime
    field_sources: dict[str, FieldSource]
    missing_fields: list[str]
    quality_flags: list[QualityFlag]
    provider_conflicts: dict[str, object]
    warnings: list[str]
    needs_agent_review: bool

    @model_validator(mode="after")
    def validate_audit_fields(self) -> AuditedItem:
        known_fields = type(self).model_fields
        unknown_missing = sorted(set(self.missing_fields) - set(known_fields))
        if unknown_missing:
            raise ValueError(
                "missing_fields references unknown fields: " + ", ".join(unknown_missing)
            )

        incorrectly_missing = sorted(
            field for field in self.missing_fields if getattr(self, field) is not None
        )
        if incorrectly_missing:
            raise ValueError(
                "missing_fields contains non-null fields: " + ", ".join(incorrectly_missing)
            )
        if self.missing_fields and not self.needs_agent_review:
            raise ValueError("missing fields require needs_agent_review=true")

        missing_provenance = sorted(
            field for field in self.provenance_fields if field not in self.field_sources
        )
        if missing_provenance:
            raise ValueError(
                "field_sources is missing provenance for: " + ", ".join(missing_provenance)
            )
        return self


class StockPoolItem(AuditedItem):
    provenance_fields = ("name", "industry", "market_cap")

    name: str
    market: Literal["SH", "SZ", "BJ"]
    industry: str
    market_cap: float | None


class MarketDataItem(AuditedItem):
    provenance_fields = (
        "latest_price",
        "pe",
        "pb",
        "ps",
        "dividend_yield",
        "return_1y",
        "volatility_1y",
        "max_drawdown_1y",
        "market_cap",
    )

    latest_price: float | None
    pe: float | None
    pb: float | None
    ps: float | None
    dividend_yield: float | None
    return_1y: float | None
    volatility_1y: float | None = Field(ge=0)
    max_drawdown_1y: float | None = Field(ge=0, le=1)
    market_cap: float | None


class FinancialDataItem(AuditedItem):
    provenance_fields = (
        "revenue",
        "net_profit",
        "gross_margin",
        "net_margin",
        "roe",
        "roe_stability",
        "debt_ratio",
        "operating_cash_flow",
        "free_cash_flow",
        "revenue_growth_3y",
        "net_profit_growth_3y",
    )

    revenue: float | None
    net_profit: float | None
    gross_margin: float | None
    net_margin: float | None
    roe: float | None
    roe_stability: float | None = Field(ge=0, le=1)
    debt_ratio: float | None
    operating_cash_flow: float | None
    free_cash_flow: float | None
    revenue_growth_3y: float | None
    net_profit_growth_3y: float | None


class StockPoolPayload(CanonicalModel):
    metadata: CanonicalMetadata
    items: list[StockPoolItem] = Field(min_length=1)


class MarketDataPayload(CanonicalModel):
    metadata: CanonicalMetadata
    items: list[MarketDataItem] = Field(min_length=1)


class FinancialDataPayload(CanonicalModel):
    metadata: CanonicalMetadata
    items: list[FinancialDataItem] = Field(min_length=1)


class CanonicalResearchBundle(CanonicalModel):
    """One atomic provider payload accepted by the research kernel."""

    pool: StockPoolPayload
    market: MarketDataPayload
    financial: FinancialDataPayload

    @model_validator(mode="after")
    def validate_identity(self) -> CanonicalResearchBundle:
        symbol_sets = (
            {item.symbol for item in self.pool.items},
            {item.symbol for item in self.market.items},
            {item.symbol for item in self.financial.items},
        )
        if symbol_sets[0] != symbol_sets[1] or symbol_sets[0] != symbol_sets[2]:
            raise ValueError("pool, market, and financial symbols must match")
        return self
