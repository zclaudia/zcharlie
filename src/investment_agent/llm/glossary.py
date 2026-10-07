"""Chinese display names for identifiers LLM agents see in their input.

Agents read raw JSON (``key_metrics.roe = 0.12``) but write prose for a human
reader. The prompts embed :func:`readability_rules` so the prose names things
the way the web UI does (``ROE 12.0%``) while structured fields
(``supporting_fields`` / ``cited_factors``) keep the identifiers.
Keep in sync with ``web/static/format.js``.
"""

from __future__ import annotations

FIELD_LABELS: dict[str, str] = {
    # factors / key metrics
    "roe": "ROE",
    "roe_stability": "ROE 稳定性",
    "gross_margin": "毛利率",
    "net_margin": "净利率",
    "debt_ratio": "资产负债率",
    "net_profit_growth_3y": "净利润 3 年复合增速",
    "revenue_growth_3y": "营收 3 年复合增速",
    "free_cash_flow": "自由现金流",
    "operating_cash_flow": "经营现金流",
    "pe": "PE",
    "pb": "PB",
    "pe_ttm": "PE(TTM)",
    "pb_mrq": "PB(MRQ)",
    "market_cap": "总市值",
    "dividend_yield": "股息率",
    "dividend_yield_ttm": "股息率(TTM)",
    "return_1y": "近 1 年涨跌幅",
    "volatility_1y": "近 1 年波动率",
    "max_drawdown_1y": "近 1 年最大回撤",
    "pe_percentile_5y": "PE 5 年分位",
    "pb_percentile_5y": "PB 5 年分位",
    # score items
    "investment_score": "投资分",
    "business_score": "业务分",
    "research_confidence_score": "研究置信度",
    "data_quality_score": "数据质量分",
    "factor_coverage": "因子覆盖率",
    "valuation_level": "估值水平",
    "risk_level": "风险等级",
    "shareholder_return": "股东回报",
    "roe_quality": "ROE 质量",
    "profit_growth": "利润成长",
    "drawdown_control": "回撤控制",
    "capital_structure_review": "资本结构复核",
    "growth_durability": "成长持续性",
    "missing_fields": "缺失字段",
    "quality_flags": "质量标记",
    "freshness": "数据新鲜度",
    # rule zones
    "buy_zone": "可研究区间",
    "drop": "移出候选",
    "add": "持仓 add 区间",
    "trim": "持仓 trim 区间",
    "exit_review": "持仓复核区间",
}


def readability_rules() -> str:
    """Prompt paragraph: human-readable prose, identifiers only in structured fields."""

    names = "、".join(f"{key}={label}" for key, label in FIELD_LABELS.items())
    return (
        "可读性要求（面向人阅读的文字字段）：\n"
        "- 文字中用中文名称指代字段、因子、参数与区间，不写英文标识符或点路径"
        "（如写“ROE”而不是 key_metrics.roe，写“PB 5 年分位”而不是 pb_percentile_5y）；"
        "标识符只放在 supporting_fields / cited_factors 等结构化字段中。\n"
        "- 数值写成人类可读形式：比例写百分比并保留 1 位小数（0.167 → 16.7%），"
        "估值倍数写“倍”（10.32 → 10.3 倍），金额写“亿元”，分数保留 1 位小数；"
        "不要写 0.120198 这类原始小数。\n"
        f"- 常用中文名：{names}。"
    )


__all__ = ["FIELD_LABELS", "readability_rules"]
