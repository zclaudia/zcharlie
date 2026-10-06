"""Auditable provider-to-canonical field mapping registry (the only one).

Every canonical field declares, per provider, where it comes from
(``endpoint`` + ``source_fields``) and its unit or formula. Collectors derive
both their request plan and their field extraction from these entries, so a
canonical field has exactly one definition per provider:

* ``IFIND_FIELD_MAPPINGS`` - iFinD via ifind-proxy (watchlist / research depth);
* ``BAOSTOCK_FIELD_MAPPINGS`` - baostock (target-pool breadth, decision D6).

A unit text containing ``percent`` means "provider percent ÷ 100"; every other
numeric value is stored as the provider returns it. Ratios are fractions
(0.3446 = 34.46%); baostock already answers fractions (verified 2026-10-06:
600519 FY2025 ``roeAvg`` 0.344620, ``liabilityToAsset`` 0.164154).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PayloadType = Literal["stock_pool", "market", "financial"]
MappingStatus = Literal["direct", "derived", "manual", "missing"]
SourceProvider = Literal["ifind", "baostock"]


@dataclass(frozen=True)
class FieldMapping:
    payload_type: PayloadType
    canonical_field: str
    endpoint: str
    source_fields: tuple[str, ...]
    status: MappingStatus
    unit_or_formula: str
    provider: SourceProvider = "ifind"


# Historical name: entries were iFinD-only before baostock joined (P1-A7).
IfindFieldMapping = FieldMapping


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


def _baostock(
    payload_type: PayloadType,
    canonical_field: str,
    endpoint: str,
    source_field: str,
    unit: str,
    status: MappingStatus = "direct",
) -> FieldMapping:
    return FieldMapping(
        payload_type,
        canonical_field,
        endpoint,
        (source_field,) if source_field else (),
        status,
        unit,
        provider="baostock",
    )


_PROFIT = "query_profit_data"
_BALANCE = "query_balance_data"
_CASH_FLOW = "query_cash_flow_data"
_GROWTH = "query_growth_data"
_KDATA = "query_history_k_data_plus"

BAOSTOCK_FIELD_MAPPINGS: tuple[FieldMapping, ...] = (
    # -- quarterly financials (financial_snapshot.fields) ------------------- #
    _baostock(
        "financial", "roe", _PROFIT, "roeAvg",
        "fraction; on average equity; period-to-date (Q1/H1/Q3 are not annualized)",
    ),
    _baostock("financial", "net_margin", _PROFIT, "npMargin", "fraction; period-to-date"),
    _baostock(
        "financial", "gross_margin", _PROFIT, "gpMargin",
        "fraction; period-to-date; null for banks",
    ),
    _baostock(
        "financial", "net_profit", _PROFIT, "netProfit",
        "CNY; period-to-date 净利润 including minority interests "
        "(iFinD net_profit is attributable to parent)",
    ),
    _baostock("financial", "eps_ttm", _PROFIT, "epsTTM", "CNY/share; trailing twelve months"),
    _baostock(
        "financial", "revenue", _PROFIT, "MBRevenue",
        "CNY; period-to-date 主营营业收入 (main-business revenue), narrower than iFinD "
        "total operating revenue; often null for banks and Q1/Q3 reports",
    ),
    _baostock(
        "financial", "total_shares", _PROFIT, "totalShare",
        "shares; effective from available_from (pubDate); the factor-time source "
        "of total shares for market cap",
    ),
    _baostock("financial", "float_shares", _PROFIT, "liqaShare", "shares; tradable A shares"),
    _baostock("financial", "debt_ratio", _BALANCE, "liabilityToAsset", "fraction"),
    _baostock("financial", "current_ratio", _BALANCE, "currentRatio", "times"),
    _baostock("financial", "quick_ratio", _BALANCE, "quickRatio", "times"),
    _baostock("financial", "cash_ratio", _BALANCE, "cashRatio", "times"),
    _baostock("financial", "equity_multiplier", _BALANCE, "assetToEquity", "times"),
    _baostock("financial", "yoy_liability", _BALANCE, "YOYLiability", "fraction; year over year"),
    _baostock(
        "financial", "cfo_to_np", _CASH_FLOW, "CFOToNP",
        "times; operating cash flow ÷ net profit, period-to-date",
    ),
    _baostock(
        "financial", "cfo_to_revenue", _CASH_FLOW, "CFOToOR",
        "times; operating cash flow ÷ operating revenue, period-to-date",
    ),
    _baostock("financial", "yoy_net_profit", _GROWTH, "YOYNI", "fraction; year over year"),
    _baostock(
        "financial", "yoy_net_profit_parent", _GROWTH, "YOYPNI",
        "fraction; year over year, attributable to parent",
    ),
    _baostock("financial", "yoy_equity", _GROWTH, "YOYEquity", "fraction; year over year"),
    _baostock("financial", "yoy_asset", _GROWTH, "YOYAsset", "fraction; year over year"),
    _baostock("financial", "yoy_eps_basic", _GROWTH, "YOYEPSBasic", "fraction; year over year"),
    # -- daily bars (market_snapshot columns) ------------------------------- #
    _baostock("market", "close", _KDATA, "close", "CNY/share; adjustflag 3 raw, 1 backward"),
    _baostock("market", "volume", _KDATA, "volume", "shares; 0 on a suspended day"),
    _baostock("market", "amount", _KDATA, "amount", "CNY; null on a suspended day"),
    _baostock(
        "market", "pe_ttm", _KDATA, "peTTM",
        "times; close ÷ TTM EPS; negative when loss-making",
    ),
    _baostock("market", "pb_mrq", _KDATA, "pbMRQ", "times; close ÷ latest-quarter BVPS"),
    _baostock("market", "ps_ttm", _KDATA, "psTTM", "times; close ÷ TTM revenue/share"),
    _baostock(
        "market", "turnover", _KDATA, "turn",
        "provider percent ÷ 100 (fraction of float shares traded)",
    ),
    _baostock("market", "trade_status", _KDATA, "tradestatus", "1 trading, 0 suspended"),
    _baostock("market", "is_st", _KDATA, "isST", "bool; 1 = ST / *ST"),
    _baostock(
        "market", "market_cap", _KDATA, "", "close (adjust=none) × total_shares of the latest "
        "baostock financial_snapshot with available_from <= trade_date; CNY",
        status="derived",
    ),
)

FIELD_MAPPINGS: tuple[FieldMapping, ...] = IFIND_FIELD_MAPPINGS + BAOSTOCK_FIELD_MAPPINGS


def mappings_for_provider(provider: SourceProvider) -> tuple[FieldMapping, ...]:
    return tuple(mapping for mapping in FIELD_MAPPINGS if mapping.provider == provider)


def mapping_for(
    payload_type: PayloadType, canonical_field: str, provider: SourceProvider = "ifind"
) -> FieldMapping:
    matches = [
        mapping
        for mapping in FIELD_MAPPINGS
        if mapping.payload_type == payload_type
        and mapping.canonical_field == canonical_field
        and mapping.provider == provider
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one {provider} mapping for {payload_type}.{canonical_field}, "
            f"found {len(matches)}"
        )
    return matches[0]
