"""Regression tests for the scoring v1.4 safety fixes.

See docs/product/scoring-v1.4-changes.md. Offline and synthetic:

1. quality_growth scores each input once (no duplicate revenue/ROE weight);
2. fixed team/leverage/capital-structure constants are unevaluated, not
   weighted, and do not inflate factor_coverage;
3. valuation needs both positive PE and PB; one unknown leg is ``unknown``;
4. factor_coverage below 0.75 suppresses ``investment_score``;
5. research-pool order puts unscored items last and breaks ties by
   ``needs_agent_review`` / confidence;
6. restated / unverified / provider-conflict signals already present in the
   input reach needs_review, confidence and therefore the order;
7. cycle_position is no longer scored from a one-year drawdown alone.
"""

from __future__ import annotations

import pytest

from investment_agent.research_kernel.scoring import (
    MIN_FACTOR_COVERAGE,
    SCHEMA_VERSION,
    SCORING_VERSION,
    factor_scores_for,
    research_rank_key,
    score_stock,
)
from investment_agent.research_kernel.scoring_models import (
    factor_scores_for_model,
    proxy_factors,
    unevaluated_factors,
    valuation_factor_score,
    valuation_level,
)

MARKET = {
    "symbol": "600000",
    "pe": 20.0,
    "pb": 3.0,
    "dividend_yield": 0.02,
    "max_drawdown_1y": 0.25,
    "volatility_1y": 0.25,
    "missing_fields": [],
    "quality_flags": [],
    "needs_agent_review": False,
}
FINANCIAL = {
    "symbol": "600000",
    "roe": 0.16,
    "roe_stability": 0.9,
    "debt_ratio": 0.4,
    "free_cash_flow": 1.0,
    "operating_cash_flow": 2.0,
    "revenue_growth_3y": 0.12,
    "net_profit_growth_3y": 0.12,
    "missing_fields": [],
    "quality_flags": [],
    "needs_agent_review": False,
}


def _rows(industry: str = "电力设备") -> tuple[dict, dict, dict]:
    stock = {
        "symbol": "600000",
        "name": "测试",
        "market": "SH",
        "industry": industry,
        "market_cap": 150_000_000_000,
        "missing_fields": [],
        "quality_flags": [],
        "needs_agent_review": False,
    }
    return stock, dict(MARKET), dict(FINANCIAL)


# --------------------------------------------------------------------------
# 1. Duplicate factor weight removed
# --------------------------------------------------------------------------


def test_quality_growth_scores_each_input_once():
    factors, weights = factor_scores_for("quality_growth", MARKET, FINANCIAL)

    assert weights == {
        "growth_proxy": 45,
        "profitability_proxy": 40,
        "cash_flow_quality": 10,
        "valuation_level": 5,
    }
    assert sum(weights.values()) == 100
    assert set(weights) == set(factors)
    # The old duplicate names are gone: revenue_growth_3y and ROE each appear
    # in exactly one factor.
    assert not {"industry_space", "competitive_advantage", "revenue_growth", "roe_level"} & set(
        factors
    )
    assert factors["growth_proxy"] == pytest.approx(30.0)
    assert factors["profitability_proxy"] == pytest.approx(55.0)


# --------------------------------------------------------------------------
# 2. Fixed constants are unevaluated, not weighted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("builder", "factor", "reason", "total_weight"),
    [
        (
            lambda: factor_scores_for("high_growth_venture", MARKET, FINANCIAL),
            "team_quality",
            "no_evidence_source",
            80,
        ),
        (
            lambda: factor_scores_for("cycle_reversal", MARKET, FINANCIAL),
            "cycle_position",
            "no_calibrated_cycle_position_input",
            70,
        ),
        (
            lambda: factor_scores_for_model("banking", MARKET, FINANCIAL),
            "capital_structure_review",
            "no_evidence_source",
            90,
        ),
        (
            lambda: factor_scores_for_model("insurance", MARKET, FINANCIAL),
            "financial_leverage_review",
            "no_evidence_source",
            90,
        ),
    ],
)
def test_fixed_or_unvalidated_factors_are_unevaluated(builder, factor, reason, total_weight):
    factors, weights = builder()

    assert factor not in weights, "unevaluated factors must not carry weight"
    assert factors[factor] is None, "no fixed 50/60 prior is passed off as data"
    assert sum(weights.values()) == total_weight
    assert unevaluated_factors(factors, weights)[factor] == reason
    score_values = [value for key, value in factors.items() if key in weights]
    assert all(value is not None for value in score_values), "fixed constants must not pad coverage"


def test_concept_proxies_are_named_as_proxies():
    factors, _ = factor_scores_for("high_growth_venture", MARKET, FINANCIAL)
    assert proxy_factors(factors) == [
        "debt_ratio_proxy",
        "profit_growth_proxy",
        "revenue_growth_proxy",
        "roe_proxy",
    ]

    factors, _ = factor_scores_for("cycle_reversal", MARKET, FINANCIAL)
    assert proxy_factors(factors) == ["profit_growth_proxy", "roe_proxy"]


# --------------------------------------------------------------------------
# 3. Valuation needs both legs
# --------------------------------------------------------------------------


def test_single_unknown_valuation_leg_is_unknown_and_unscored():
    assert valuation_level(10.0, None) == "unknown"
    assert valuation_level(None, 1.5) == "unknown"
    assert valuation_factor_score(10.0, None) is None
    assert valuation_factor_score(None, 1.5) is None
    assert valuation_level(10.0, 1.5) == "cheap"


def test_score_item_does_not_call_a_missing_valuation_expensive():
    stock, market, financial = _rows()
    market["pb"] = None

    item = score_stock(stock, market, financial)

    assert item["valuation_level"] == "unknown"
    assert item["factor_scores"]["valuation_level"] is None
    assert item["suggestion"] != "defer", "unknown valuation must not be labelled expensive"


# --------------------------------------------------------------------------
# 4. Coverage floor suppresses the score
# --------------------------------------------------------------------------


def test_coverage_below_floor_suppresses_score():
    stock, market, financial = _rows(industry="银行")
    market["dividend_yield"] = None

    item = score_stock(stock, market, financial)

    assert item["factor_coverage"] < MIN_FACTOR_COVERAGE
    assert item["investment_score"] is None
    assert item["business_score"] is None
    assert item["score_suppression_reason"] == "factor_coverage_below_minimum"
    assert item["suggestion"] == "needs_review"


def test_full_coverage_keeps_the_score():
    stock, market, financial = _rows(industry="银行")

    item = score_stock(stock, market, financial)

    assert item["factor_coverage"] == 1.0
    assert item["investment_score"] is not None
    assert item["score_suppression_reason"] is None


# --------------------------------------------------------------------------
# 5. Deterministic research-pool ordering
# --------------------------------------------------------------------------


def test_research_order_places_unscored_last_and_uses_quality_keys_only_for_ties():
    items = [
        {
            "symbol": "A",
            "investment_score": None,
            "needs_agent_review": False,
            "research_confidence_score": 99.0,
        },
        {
            "symbol": "B",
            "investment_score": 60.0,
            "needs_agent_review": True,
            "research_confidence_score": 90.0,
        },
        {
            "symbol": "C",
            "investment_score": 60.0,
            "needs_agent_review": False,
            "research_confidence_score": 80.0,
        },
        {
            "symbol": "D",
            "investment_score": 60.0,
            "needs_agent_review": False,
            "research_confidence_score": 90.0,
        },
    ]

    ordered = sorted(items, key=research_rank_key, reverse=True)

    # All three scored items share 60, so review/confidence decide: clean and
    # more confident first, review-flagged last; the None score is last.
    assert [item["symbol"] for item in ordered] == ["D", "C", "B", "A"]


def test_research_order_keeps_score_primary_over_review_and_confidence():
    high_score_needs_review = {
        "symbol": "HIGH",
        "investment_score": 80.0,
        "needs_agent_review": True,
        "research_confidence_score": 60.0,
    }
    lower_score_clean = {
        "symbol": "LOW",
        "investment_score": 70.0,
        "needs_agent_review": False,
        "research_confidence_score": 95.0,
    }

    ordered = sorted(
        [lower_score_clean, high_score_needs_review],
        key=research_rank_key,
        reverse=True,
    )

    assert [item["symbol"] for item in ordered] == ["HIGH", "LOW"]


# --------------------------------------------------------------------------
# 6. Provenance signals already in the input
# --------------------------------------------------------------------------


def _score_with(field_sources: dict | None = None, **market_overrides):
    stock, market, financial = _rows()
    market.update(market_overrides)
    if field_sources is not None:
        financial["field_sources"] = field_sources
    return score_stock(stock, market, financial)


def test_restated_and_unverified_signals_lower_confidence_and_require_review():
    clean = _score_with()
    assert clean["needs_agent_review"] is False
    assert clean["quality_signals"] == []

    unverified = _score_with({"roe": {"provider": "akshare", "verification": "unverified"}})
    assert unverified["needs_agent_review"] is True
    assert {"payload": "financial_data", "field": "roe", "flag": "unverified"} in unverified[
        "quality_signals"
    ]
    assert unverified["research_confidence_score"] < clean["research_confidence_score"]
    assert unverified["investment_score"] == clean["investment_score"], (
        "provenance issues change confidence/review, not the score itself"
    )

    restated = _score_with({"roe": {"provider": "akshare", "restated": True}})
    assert restated["needs_agent_review"] is True
    assert {"payload": "financial_data", "field": "roe", "flag": "restated"} in restated[
        "quality_signals"
    ]
    assert restated["research_confidence_score"] < clean["research_confidence_score"]


def test_provider_conflict_already_in_input_requires_review_and_lowers_confidence():
    clean = _score_with()
    conflicted = _score_with(provider_conflicts={"pe": {"baostock": 18.0, "ifind": 20.0}})

    assert conflicted["needs_agent_review"] is True
    assert any(signal["flag"] == "provider_conflict" for signal in conflicted["quality_signals"])
    assert conflicted["research_confidence_score"] < clean["research_confidence_score"]


def test_quality_signals_reach_the_research_order():
    clean = _score_with()
    conflicted = _score_with(provider_conflicts={"pe": {"baostock": 18.0, "ifind": 20.0}})

    # Same score, but the conflicted item needs review, so it sorts after the
    # clean item even though confidence is also lower.
    ordered = sorted([conflicted, clean], key=research_rank_key, reverse=True)
    assert ordered[0]["symbol"] == clean["symbol"]
    assert ordered[0]["needs_agent_review"] is False


# --------------------------------------------------------------------------
# 7. cycle_position removed from the weighted model
# --------------------------------------------------------------------------


def test_cycle_position_is_not_scored_from_drawdown_alone():
    market = dict(MARKET, max_drawdown_1y=0.49)
    factors, weights = factor_scores_for("cycle_reversal", market, FINANCIAL)

    assert "cycle_position" not in weights
    assert factors["cycle_position"] is None
    assert unevaluated_factors(factors, weights)["cycle_position"] == (
        "no_calibrated_cycle_position_input"
    )
    assert set(factors) == set(weights) | {"cycle_position"}


# --------------------------------------------------------------------------
# Versions and output fields
# --------------------------------------------------------------------------


def test_scoring_output_versions_and_new_fields():
    stock, market, financial = _rows(industry="银行")

    item = score_stock(stock, market, financial)

    assert SCORING_VERSION == "v1.4"
    assert SCHEMA_VERSION == "v2.3"
    assert item["scoring_version"] == SCORING_VERSION
    assert item["schema_version"] == SCHEMA_VERSION
    assert item["unevaluated_factors"] == {"capital_structure_review": "no_evidence_source"}
    assert item["proxy_factors"] == []
    assert item["quality_signals"] == []
    assert item["score_suppression_reason"] is None
