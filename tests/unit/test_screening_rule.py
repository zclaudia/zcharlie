from decimal import Decimal

import pytest

from investment_agent.domain.strategy_types import ListingSnapshot
from investment_agent.strategies.screening_rule import (
    Condition,
    ScreeningRule,
    evaluate_rule,
)


def _snap(**kw) -> ListingSnapshot:
    base = dict(
        listing_id=1, company_id=1, ticker="AAPL", market_code="NASDAQ",
        company_name="Apple", as_of="2025-06-03",
    )
    base.update(kw)
    return ListingSnapshot.model_validate(base)


def test_required_lt_passes():
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    assert evaluate_rule(rule, _snap(pe=Decimal("25"))).passed is True


def test_required_lt_fails():
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    result = evaluate_rule(rule, _snap(pe=Decimal("35")))
    assert result.passed is False
    assert "pe" in result.failed_conditions[0]


def test_between():
    rule = ScreeningRule(required=[Condition(metric="pe", op="between", value=(0, 25))])
    assert evaluate_rule(rule, _snap(pe=Decimal("18"))).passed is True
    assert evaluate_rule(rule, _snap(pe=Decimal("26"))).passed is False


def test_excluded_blocks():
    rule = ScreeningRule(
        required=[],
        excluded=[Condition(metric="net_income_ttm", op="<", value=0)],
    )
    assert evaluate_rule(rule, _snap(net_income_ttm=Decimal("-1000000"))).passed is False
    assert evaluate_rule(rule, _snap(net_income_ttm=Decimal("1000000"))).passed is True


def test_missing_value_treated_as_failure_for_required():
    """如果某指标是 None，required 视为失败。"""
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    assert evaluate_rule(rule, _snap(pe=None)).passed is False


def test_preferred_does_not_block():
    """preferred 不通过不影响 passed，但记录 preferred_misses。"""
    rule = ScreeningRule(
        required=[],
        preferred=[Condition(metric="fcf_yield", op=">", value=0.05)],
    )
    result = evaluate_rule(rule, _snap(fcf_yield=Decimal("0.02")))
    assert result.passed is True
    assert len(result.preferred_misses) == 1
    assert result.preferred_score == 0.0


def test_preferred_score_calculation():
    rule = ScreeningRule(
        required=[],
        preferred=[
            Condition(metric="fcf_yield", op=">", value=0.05),
            Condition(metric="roe", op=">", value=0.15),
        ],
    )
    result = evaluate_rule(
        rule,
        _snap(fcf_yield=Decimal("0.08"), roe=Decimal("0.20")),
    )
    assert result.preferred_score == 1.0  # 全满足


def test_unknown_op_raises():
    with pytest.raises(ValueError, match="Unknown op"):
        rule = ScreeningRule(required=[Condition(metric="pe", op="approx", value=10)])
        evaluate_rule(rule, _snap(pe=Decimal("10")))
