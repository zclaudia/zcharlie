"""Kernel industry inputs: industry groups first-class, legacy keywords as fallback (P1-B4)."""

import json
from pathlib import Path

import pytest
import yaml

from investment_agent.factors.industry_groups import default_industry_groups
from investment_agent.research_kernel.company_profile import classify_company_profile
from investment_agent.research_kernel.industry import (
    GROUP_TRAITS,
    legacy_keyword_basis,
    resolve_industry_basis,
)
from investment_agent.research_kernel.scoring import classify_stock

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"


def _manual(industry: str, **extra: object) -> dict[str, object]:
    return {
        "symbol": "600000",
        "name": "测试",
        "market": "SH",
        "industry": industry,
        "field_sources": {"industry": {"provider": "manual"}},
        **extra,
    }


def test_every_configured_group_has_kernel_traits():
    assert set(default_industry_groups().groups) == set(GROUP_TRAITS)


@pytest.mark.parametrize(
    ("text", "traits", "sector"),
    [
        ("电力设备", {"dividend", "growth"}, "manufacturing_growth"),  # "电力" substring, as v1.3
        ("银行", {"dividend"}, "banking"),
        ("半导体材料", {"venture"}, "general"),
        ("石油化工", {"cycle"}, "general"),
        ("白酒", set(), "general"),
        ("航空运输", set(), "general"),
    ],
)
def test_user_text_uses_the_legacy_keyword_tables(text, traits, sector):
    basis = resolve_industry_basis(_manual(text))
    assert basis.matched_by == "legacy_keywords"
    assert basis.traits == frozenset(traits)
    assert basis.sector == sector


def test_user_text_equal_to_a_group_id_selects_that_group():
    basis = resolve_industry_basis(_manual("utility", industry_group="consumer"))
    assert (basis.matched_by, basis.group) == ("group_id", "utility")
    assert basis.traits == frozenset({"dividend"}) and basis.sector == "public_utility"


def test_user_text_overrides_the_data_industry_group():
    basis = resolve_industry_basis(_manual("银行", industry_group="consumer"))
    assert basis.matched_by == "legacy_keywords" and basis.sector == "banking"


def test_data_industry_group_decides_without_user_text():
    stock = {
        "symbol": "600900",
        "name": "长江电力",
        "industry": "电力",
        "industry_group": "utility",
        "field_sources": {"industry": {"provider": "sw2021"}},
    }
    basis = resolve_industry_basis(stock)
    assert (basis.matched_by, basis.group) == ("industry_group", "utility")
    profile = classify_company_profile(stock, {}, {})
    assert profile["sector_type"] == "public_utility"
    fit = classify_stock(stock, {}, {})["classification_fit_scores"]
    assert fit["dividend_blue_chip"] == 35


def test_group_traits_replace_name_keywords():
    # Legacy profile matching also scanned the company name (中国平安 -> insurance);
    # a data-derived group does not.
    stock = {
        "symbol": "000001",
        "name": "平安银行",
        "industry": "股份制银行Ⅲ",
        "industry_group": "banking",
        "field_sources": {"industry": {"provider": "sw2021"}},
    }
    assert classify_company_profile(stock, {}, {})["sector_type"] == "banking"


def test_no_industry_at_all_is_general():
    basis = resolve_industry_basis({"symbol": "600000"})
    assert basis.matched_by == "none" and not basis.traits and basis.sector == "general"


def test_golden_pool_items_resolve_through_the_legacy_tables():
    manifest = yaml.safe_load((FIXTURE_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    for sample in manifest["samples"]:
        pool = json.loads(
            (FIXTURE_ROOT / sample["ticker"].split(".")[0] / "pool.json").read_text("utf-8")
        )
        (stock,) = pool["items"]
        assert resolve_industry_basis(stock) == legacy_keyword_basis(stock), sample["ticker"]
