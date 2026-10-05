from __future__ import annotations

from typing import Any

from investment_agent.research_kernel.models import SECTOR_LABELS

INSURANCE_KEYWORDS = ("保险", "平安", "太保", "人寿", "新华")
BANKING_KEYWORDS = ("银行",)
PUBLIC_UTILITY_KEYWORDS = ("公用事业", "电力", "水电", "核电", "高速", "运营商")
MANUFACTURING_GROWTH_KEYWORDS = (
    "汽车零部件",
    "电力设备",
    "新能源",
    "自动化",
    "高端制造",
    "智能制造",
)


def _text_for(stock: dict[str, Any]) -> str:
    return " ".join(str(stock.get(key) or "") for key in ("symbol", "name", "industry", "market"))


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def classify_company_profile(
    stock: dict[str, Any],
    market: dict[str, Any],
    financial: dict[str, Any],
) -> dict[str, Any]:
    text = _text_for(stock)

    if _has_any(text, BANKING_KEYWORDS):
        sector_type = "banking"
        tags = ["financial", "banking", "dividend_income"]
        preferred_models = ["banking", "dividend_blue_chip", "steady_quality_growth"]
        non_applicable_fields = ["gross_margin", "free_cash_flow"]
        peer_group_hints = ["招商银行", "工商银行", "建设银行", "农业银行"]
    elif _has_any(text, INSURANCE_KEYWORDS):
        sector_type = "insurance"
        tags = ["financial", "insurance", "dividend_income"]
        preferred_models = ["insurance", "dividend_blue_chip", "steady_quality_growth"]
        non_applicable_fields = ["gross_margin", "free_cash_flow"]
        peer_group_hints = ["中国太保", "新华保险", "中国人寿"]
    elif _has_any(text, MANUFACTURING_GROWTH_KEYWORDS):
        sector_type = "manufacturing_growth"
        tags = ["manufacturing", "quality_growth"]
        preferred_models = ["manufacturing_growth", "quality_growth", "steady_quality_growth"]
        non_applicable_fields = []
        peer_group_hints = ["三花智控", "拓普集团", "银轮股份", "汇川技术"]
    elif _has_any(text, PUBLIC_UTILITY_KEYWORDS):
        sector_type = "public_utility"
        tags = ["public_utility", "dividend_income", "asset_heavy"]
        preferred_models = ["public_utility", "dividend_blue_chip", "steady_quality_growth"]
        non_applicable_fields = []
        peer_group_hints = ["长江电力", "华能水电", "国投电力", "川投能源"]
    else:
        sector_type = "general"
        tags = ["general"]
        preferred_models = ["generic"]
        non_applicable_fields = []
        peer_group_hints = []

    return {
        "sector_type": sector_type,
        "sector_label": SECTOR_LABELS[sector_type],
        "business_model_tags": tags,
        "non_applicable_fields": non_applicable_fields,
        "preferred_models": preferred_models,
        "peer_group_hints": peer_group_hints,
    }
