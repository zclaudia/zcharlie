"""Template v2 (F2 user review, 2026-10-07): the review decisions are encoded in
the template files, and the ROE cap screens out an equity-base artefact."""

from __future__ import annotations

from datetime import date

import pytest

from investment_agent.factors.registry import FactorValue
from investment_agent.rules.schema import Condition, RuleTemplate
from investment_agent.rules.screen import ScreenMember, run_screen
from investment_agent.rules.templates import DEFAULT_TEMPLATES_DIR, load_templates


def _templates() -> dict[str, RuleTemplate]:
    return {template.id: template for _, template in load_templates(DEFAULT_TEMPLATES_DIR)}


def _screen_conditions(template: RuleTemplate, factor: str) -> list[Condition]:
    return [
        child
        for child in template.screen.children
        if isinstance(child, Condition) and child.factor == factor
    ]


def test_dividend_blue_chip_uses_the_dividend_yield():
    template = _templates()["dividend_blue_chip"]
    assert set(template.applicable_groups) == {"utility", "banking"}  # Q1: unchanged
    (condition,) = _screen_conditions(template, "dividend_yield_ttm")
    assert (condition.op, condition.value) == (">=", "$dividend_yield_min")
    assert template.params["dividend_yield_min"] == 0.03
    drop = [
        child
        for child in template.candidate_zones["drop"].children
        if isinstance(child, Condition) and child.factor == "dividend_yield_ttm"
    ]
    assert [(c.op, c.value) for c in drop] == [("<", "$drop_dividend_yield_max")]
    assert template.params["drop_dividend_yield_max"] < template.params["dividend_yield_min"]


def test_steady_quality_growth_screens_debt_ratio():
    template = _templates()["steady_quality_growth"]
    (condition,) = _screen_conditions(template, "debt_ratio")
    assert (condition.op, condition.value) == ("<=", "$debt_ratio_max")
    assert template.params["debt_ratio_max"] == 0.55
    bounds = template.param_ranges["debt_ratio_max"]
    assert (bounds.min, bounds.max) == (0.40, 0.70)


@pytest.mark.parametrize(
    ("template_id", "cap"),
    [
        ("cycle_reversal", 0.40),
        ("dividend_blue_chip", 0.40),
        ("steady_quality_growth", 0.40),
        # high stable ROE is the thesis; roe_stability guards one-off spikes
        ("quality_growth", 0.60),
    ],
)
def test_roe_screen_is_bounded_above(template_id: str, cap: float):
    template = _templates()[template_id]
    (condition,) = _screen_conditions(template, "roe")
    assert (condition.op, condition.value) == ("between", ["$roe_min", "$roe_max"])
    assert template.params["roe_max"] == cap
    assert template.param_ranges["roe_max"].min > template.param_ranges["roe_min"].max


def test_templates_without_an_roe_screen_are_unchanged():
    template = _templates()["high_growth_venture"]
    assert _screen_conditions(template, "roe") == []
    assert "roe_max" not in template.params


def _member(ticker: str, roe: float) -> ScreenMember:
    values = {
        "max_drawdown_1y": 0.36,
        "pb_percentile_5y": 0.09,
        "roe": roe,
        "market_cap": 5.2e10,
        "is_st": 0.0,
        "report_overdue": 0.0,
    }
    return ScreenMember(
        listing_id=len(ticker),
        ticker=ticker,
        industry_group="cyclical",
        factors={name: FactorValue(name, "1", value, "ok") for name, value in values.items()},
    )


def test_roe_cap_screens_out_an_equity_base_artefact():
    template = _templates()["cycle_reversal"]
    run = run_screen(
        as_of=date(2026, 9, 30),
        members=[_member("600221.SH", 0.5253), _member("600547.SH", 0.1146)],
        templates=[template],
    )
    (result,) = run.templates
    outcomes = {member.ticker: member for member in result.members}
    assert outcomes["600547.SH"].outcome == "hit"
    artefact = outcomes["600221.SH"]
    assert artefact.outcome == "miss"
    (reason,) = artefact.reasons
    assert reason["factor"] == "roe" and reason["outcome"] == "not_met"
    assert reason["threshold"] == [0.05, 0.40]
