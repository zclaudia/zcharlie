"""Auditable iFinD-to-canonical field mapping registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PayloadType = Literal["stock_pool", "market", "financial"]
MappingStatus = Literal["direct", "derived", "manual", "missing"]


@dataclass(frozen=True)
class IfindFieldMapping:
    payload_type: PayloadType
    canonical_field: str
    endpoint: str
    source_fields: tuple[str, ...]
    status: MappingStatus
    unit_or_formula: str


IFIND_FIELD_MAPPINGS: tuple[IfindFieldMapping, ...] = (
    IfindFieldMapping(
        "stock_pool",
        "name",
        "/stock/info",
        ("ths_stock_short_name_stock",),
        "direct",
        "text",
    ),
    IfindFieldMapping(
        "stock_pool",
        "industry",
        "command_input",
        (),
        "manual",
        "canonical industry label",
    ),
    IfindFieldMapping(
        "stock_pool",
        "market_cap",
        "/stock/info + /stock/history",
        ("ths_total_shares_stock", "close"),
        "derived",
        "latest close × total shares; CNY",
    ),
    IfindFieldMapping(
        "market",
        "latest_price",
        "/stock/history",
        ("close",),
        "direct",
        "CNY/share",
    ),
    IfindFieldMapping(
        "market",
        "pe",
        "/stock/history + /financial/index:profitability",
        ("close", "ths_eps_basic_stock"),
        "derived",
        "latest close ÷ latest complete FY basic EPS",
    ),
    IfindFieldMapping(
        "market",
        "pb",
        "/stock/history + /financial/index:profitability",
        ("close", "ths_nav_ps_stock"),
        "derived",
        "latest close ÷ latest complete FY net assets/share",
    ),
    IfindFieldMapping(
        "market",
        "ps",
        "/stock/history + /financial/index:profitability",
        ("close", "ths_revenue_ps_stock"),
        "derived",
        "latest close ÷ latest complete FY revenue/share",
    ),
    IfindFieldMapping(
        "market",
        "dividend_yield",
        "unavailable",
        (),
        "missing",
        "fraction; remains null until the gateway exposes audited cash dividends",
    ),
    IfindFieldMapping(
        "market",
        "return_1y",
        "/stock/history",
        ("close",),
        "derived",
        "last close ÷ first close - 1 over the fetched window",
    ),
    IfindFieldMapping(
        "market",
        "volatility_1y",
        "/stock/history",
        ("close",),
        "derived",
        "population stdev of daily return × sqrt(252)",
    ),
    IfindFieldMapping(
        "market",
        "max_drawdown_1y",
        "/stock/history",
        ("close",),
        "derived",
        "positive peak-to-trough fraction",
    ),
    IfindFieldMapping(
        "market",
        "market_cap",
        "/stock/info + /stock/history",
        ("ths_total_shares_stock", "close"),
        "derived",
        "latest close × total shares; CNY",
    ),
    IfindFieldMapping(
        "financial",
        "revenue",
        "/financial/statements:income_statement",
        ("ths_operating_total_revenue_stock",),
        "direct",
        "CNY",
    ),
    IfindFieldMapping(
        "financial",
        "net_profit",
        "/financial/statements:income_statement",
        ("ths_np_atoopc_stock",),
        "direct",
        "CNY; attributable to parent",
    ),
    IfindFieldMapping(
        "financial",
        "gross_margin",
        "/financial/index:profitability",
        ("ths_gross_selling_rate_stock",),
        "direct",
        "provider percent ÷ 100",
    ),
    IfindFieldMapping(
        "financial",
        "net_margin",
        "/financial/index:profitability",
        ("ths_net_sales_rate_stock",),
        "direct",
        "provider percent ÷ 100",
    ),
    IfindFieldMapping(
        "financial",
        "roe",
        "/financial/index:profitability",
        ("ths_roe_stock",),
        "direct",
        "provider percent ÷ 100",
    ),
    IfindFieldMapping(
        "financial",
        "roe_stability",
        "/financial/index:profitability",
        ("ths_roe_stock",),
        "derived",
        "clamp(1 - population stdev ÷ mean, 0, 1) over four FY values",
    ),
    IfindFieldMapping(
        "financial",
        "debt_ratio",
        "/financial/index:capital_structure",
        ("ths_asset_liab_ratio_stock",),
        "direct",
        "provider percent ÷ 100",
    ),
    IfindFieldMapping(
        "financial",
        "operating_cash_flow",
        "/financial/statements:cash_flow",
        ("ths_ncf_from_oa_stock",),
        "direct",
        "CNY",
    ),
    IfindFieldMapping(
        "financial",
        "free_cash_flow",
        "/financial/statements:cash_flow",
        ("ths_ncf_from_oa_stock", "ths_cash_paid_for_assets_stock"),
        "derived",
        "operating cash flow - cash paid for long-term assets; CNY",
    ),
    IfindFieldMapping(
        "financial",
        "revenue_growth_3y",
        "/financial/statements:income_statement",
        ("ths_operating_total_revenue_stock",),
        "derived",
        "three-year CAGR from complete FY values",
    ),
    IfindFieldMapping(
        "financial",
        "net_profit_growth_3y",
        "/financial/statements:income_statement",
        ("ths_np_atoopc_stock",),
        "derived",
        "three-year CAGR from complete FY values",
    ),
)


def mapping_for(payload_type: PayloadType, canonical_field: str) -> IfindFieldMapping:
    matches = [
        mapping
        for mapping in IFIND_FIELD_MAPPINGS
        if mapping.payload_type == payload_type
        and mapping.canonical_field == canonical_field
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one iFinD mapping for {payload_type}.{canonical_field}, "
            f"found {len(matches)}"
        )
    return matches[0]
