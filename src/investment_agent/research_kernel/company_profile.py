from __future__ import annotations

from typing import Any

from investment_agent.research_kernel.industry import IndustryBasis, resolve_industry_basis
from investment_agent.research_kernel.models import SECTOR_LABELS

_PROFILES: dict[str, dict[str, list[str]]] = {
    "banking": {
        "tags": ["financial", "banking", "dividend_income"],
        "preferred_models": ["banking", "dividend_blue_chip", "steady_quality_growth"],
        "non_applicable_fields": ["gross_margin", "free_cash_flow"],
        "peer_group_hints": ["招商银行", "工商银行", "建设银行", "农业银行"],
    },
    "insurance": {
        "tags": ["financial", "insurance", "dividend_income"],
        "preferred_models": ["insurance", "dividend_blue_chip", "steady_quality_growth"],
        "non_applicable_fields": ["gross_margin", "free_cash_flow"],
        "peer_group_hints": ["中国太保", "新华保险", "中国人寿"],
    },
    "manufacturing_growth": {
        "tags": ["manufacturing", "quality_growth"],
        "preferred_models": ["manufacturing_growth", "quality_growth", "steady_quality_growth"],
        "non_applicable_fields": [],
        "peer_group_hints": ["三花智控", "拓普集团", "银轮股份", "汇川技术"],
    },
    "public_utility": {
        "tags": ["public_utility", "dividend_income", "asset_heavy"],
        "preferred_models": ["public_utility", "dividend_blue_chip", "steady_quality_growth"],
        "non_applicable_fields": [],
        "peer_group_hints": ["长江电力", "华能水电", "国投电力", "川投能源"],
    },
    "general": {
        "tags": ["general"],
        "preferred_models": ["generic"],
        "non_applicable_fields": [],
        "peer_group_hints": [],
    },
}


def classify_company_profile(
    stock: dict[str, Any],
    market: dict[str, Any],
    financial: dict[str, Any],
    *,
    industry: IndustryBasis | None = None,
) -> dict[str, Any]:
    """Company profile from the industry basis (industry group, or the legacy
    keyword tables for user-entered industry text; see :mod:`.industry`)."""

    sector_type = (industry or resolve_industry_basis(stock)).sector
    profile = _PROFILES[sector_type]
    return {
        "sector_type": sector_type,
        "sector_label": SECTOR_LABELS[sector_type],
        "business_model_tags": list(profile["tags"]),
        "non_applicable_fields": list(profile["non_applicable_fields"]),
        "preferred_models": list(profile["preferred_models"]),
        "peer_group_hints": list(profile["peer_group_hints"]),
    }
