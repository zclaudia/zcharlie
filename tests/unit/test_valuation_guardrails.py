"""Reverse-DCF quality guardrails. The solver's growth path stays unchanged."""

from decimal import Decimal
from uuid import uuid4

import pytest

from investment_agent.valuation.guardrails import (
    DEFAULT_GUARDRAILS,
    FLIP_CHANGES,
    FLIP_NOT_COMPARABLE,
    LOW_INFORMATION,
    LOW_INFORMATION_MARK,
    RATE_OUTSIDE_ANCHOR,
    SPREAD_BELOW_FLOOR,
    UNCALIBRATED_NOTE,
    GuardrailPolicy,
    assess_reverse_dcf_guardrails,
    flip_discount_rate,
    implied_growth_reading,
    rate_anchor_outcome,
    spread_outcome,
    terminal_share_outcome,
)
from investment_agent.valuation.service import ValuationService
from investment_agent.valuation.solver import present_value, solve
from investment_agent.valuation.tools import tool_summary
from tests.support.valuation import D, FixtureQualification, assumption, seed


def test_default_policy_is_an_uncalibrated_risk_free_plus_premium_band():
    policy = DEFAULT_GUARDRAILS
    assert policy.calibration == "uncalibrated"
    assert "未校准" in UNCALIBRATED_NOTE
    assert "不是已研究的 A 股参数" in UNCALIBRATED_NOTE
    assert policy.rate_floor == policy.risk_free_rate + policy.equity_risk_premium_low
    assert policy.rate_ceiling == policy.risk_free_rate + policy.equity_risk_premium_high
    assert policy.rate_floor == D("0.05")
    assert policy.rate_ceiling == D("0.14")
    assert policy.min_discount_spread == D("0.05")
    assert policy.terminal_share_low_information == D("0.70")
    assert policy.rate_flip_shift == D("0.02")


def test_policy_rejects_an_empty_premium_band():
    with pytest.raises(ValueError, match="empty"):
        GuardrailPolicy(equity_risk_premium_low=D("0.04"), equity_risk_premium_high=D("0.03"))


def test_rate_anchor_is_inclusive_at_the_risk_free_plus_premium_edges():
    policy = DEFAULT_GUARDRAILS
    assert rate_anchor_outcome(policy.rate_floor, policy) == "pass"
    assert rate_anchor_outcome(policy.rate_ceiling, policy) == "pass"
    assert rate_anchor_outcome(D("0.09"), policy) == "pass"
    assert rate_anchor_outcome(policy.rate_floor - D("1e-10"), policy) == "fail"
    assert rate_anchor_outcome(policy.rate_ceiling + D("1e-10"), policy) == "fail"


def test_custom_band_is_risk_free_plus_premium_not_a_hardcoded_interval():
    policy = GuardrailPolicy(
        risk_free_rate=D("0.04"),
        equity_risk_premium_low=D("0.02"),
        equity_risk_premium_high=D("0.03"),
    )
    assert policy.rate_floor == D("0.06")
    assert policy.rate_ceiling == D("0.07")
    assert rate_anchor_outcome(D("0.06"), policy) == "pass"
    assert rate_anchor_outcome(D("0.09"), policy) == "fail"


def test_discount_spread_floor_passes_at_exactly_five_points():
    policy = DEFAULT_GUARDRAILS
    rate = D("0.10")
    assert spread_outcome(rate, rate - policy.min_discount_spread, policy) == "pass"
    assert spread_outcome(rate, rate - policy.min_discount_spread + D("1e-10"), policy) == "fail"
    assert spread_outcome(D("0.09"), D("0.02"), policy) == "pass"


def test_terminal_share_marks_only_strictly_above_the_threshold():
    policy = DEFAULT_GUARDRAILS
    threshold = policy.terminal_share_low_information
    assert terminal_share_outcome(threshold, policy) == "within_threshold"
    assert terminal_share_outcome(threshold + D("1e-10"), policy) == "low_information"
    assert terminal_share_outcome(D("0"), policy) == "within_threshold"
    assert terminal_share_outcome(D("0.767"), policy) == "low_information"


def test_implied_growth_sign_uses_the_solver_width_as_a_flat_deadband():
    assert implied_growth_reading(D("1e-8")) == "flat_implied_growth"
    assert implied_growth_reading(D("-1e-8")) == "flat_implied_growth"
    assert implied_growth_reading(D("1e-8") + D("1e-12")) == "positive_implied_growth"
    assert implied_growth_reading(D("-1e-8") - D("1e-12")) == "negative_implied_growth"


def test_flip_keeps_a_clearly_positive_implied_growth():
    solved = solve(D("212.4"), D("8.2"), D("0.09"), D("0.02"))
    assert solved.growth is not None
    report = flip_discount_rate(
        market_value=D("212.4"),
        base=D("8.2"),
        rate=D("0.09"),
        terminal_growth=D("0.02"),
        published_growth=solved.growth,
    )
    assert report["conclusion_changes"] is False
    assert report["lower"]["rate"] == "0.07"
    assert report["upper"]["rate"] == "0.11"
    assert {
        report["main"]["conclusion"],
        report["lower"]["conclusion"],
        report["upper"]["conclusion"],
    } == {"positive_implied_growth"}


def test_flip_changes_when_implied_growth_crosses_zero():
    solved = solve(D(1000), D(100), D("0.10"), D(0))
    assert solved.growth is not None and abs(solved.growth) < D("1e-8")
    report = flip_discount_rate(
        market_value=D(1000),
        base=D(100),
        rate=D("0.10"),
        terminal_growth=D(0),
        published_growth=solved.growth,
    )
    assert report["main"]["conclusion"] == "flat_implied_growth"
    assert report["lower"]["conclusion"] == "negative_implied_growth"
    assert report["upper"]["conclusion"] == "positive_implied_growth"
    assert report["conclusion_changes"] is True
    assert "安全边际" in report["reading_note"]
    assert "不计算高估" in report["reading_note"]


def test_flip_is_not_comparable_when_a_perturbed_rate_leaves_the_search_range():
    base, rate, terminal = D("8.2"), D("0.09"), D("0.02")
    lower_rate = rate - DEFAULT_GUARDRAILS.rate_flip_shift
    main_floor = present_value(base, D("-0.95"), rate, terminal)[0]
    lower_floor = present_value(base, D("-0.95"), lower_rate, terminal)[0]
    assert lower_floor > main_floor
    market = (main_floor + lower_floor) / 2
    solved = solve(market, base, rate, terminal)
    assert solved.status.value == "ok" and solved.growth is not None
    report = flip_discount_rate(
        market_value=market,
        base=base,
        rate=rate,
        terminal_growth=terminal,
        published_growth=solved.growth,
    )
    assert report["lower"]["conclusion"] == "unsolved"
    assert report["lower"]["reason"] == "below_search_range"
    assert report["conclusion_changes"] is None


def test_failed_checks_are_ordered_and_a_missing_rate_is_not_a_pass():
    failed = assess_reverse_dcf_guardrails(
        main_rate=D("0.20"),
        terminal_growth=D("0.16"),
        sensitivity_rates=(D("0.18"), D("0.22")),
        scenarios=[
            {
                "basis": "median",
                "status": "ok",
                "rate": "0.20",
                "growth": "0.01",
                "terminal_pv_fraction": "0.80",
            }
        ],
        market_value=None,
        median_base=None,
    )
    assert failed.quality == (RATE_OUTSIDE_ANCHOR, SPREAD_BELOW_FLOOR, LOW_INFORMATION)
    assert failed.details["terminal_share"]["mark"] == LOW_INFORMATION_MARK
    assert failed.details["terminal_share"]["applied"] is True
    assert any("不是已研究的 A 股参数" in warning for warning in failed.warnings)
    assert any(LOW_INFORMATION_MARK in warning for warning in failed.warnings)
    assert failed.details["rate_flip"]["attempted"] is False
    missing = assess_reverse_dcf_guardrails(
        main_rate=None,
        terminal_growth=D("0.02"),
        sensitivity_rates=(None,),
        scenarios=[],
        market_value=None,
        median_base=None,
    )
    assert missing.quality == ()
    assert missing.details["rate_anchor"]["rates"][0]["outcome"] == "not_checked"
    assert missing.details["discount_spread"]["pairs"][1]["outcome"] == "not_checked"


def _main_growth(output: dict) -> Decimal:
    return D(output["result"]["median_0_growth"]["value"])


@pytest.mark.asyncio
async def test_saved_valuation_keeps_solved_growth_and_marks_low_information(db_session):
    request, qualified, _ = await seed(db_session)
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf(
        "local_owner", request, uuid4()
    )
    direct = solve(D("212.4"), D("8.2"), D("0.09"), D("0.02"))
    assert output["status"] == "ok"
    assert direct.growth is not None and _main_growth(output) == direct.growth
    record = output["calculation"]
    assert LOW_INFORMATION in record["quality"]
    assert RATE_OUTSIDE_ANCHOR not in record["quality"]
    assert SPREAD_BELOW_FLOOR not in record["quality"]
    assert FLIP_CHANGES not in record["quality"]
    guardrails = record["details"]["guardrails"]
    assert guardrails["calibration"] == "uncalibrated"
    assert guardrails["rate_anchor"]["band_low"] == "0.05"
    assert guardrails["rate_anchor"]["band_high"] == "0.14"
    assert guardrails["rate_anchor"]["source"] == "placeholder_no_risk_free_series"
    assert "未校准" in guardrails["note"]
    assert guardrails["terminal_share"]["applied"] is True
    assert guardrails["terminal_share"]["outcome"] == "low_information"
    assert guardrails["rate_flip"]["conclusion_changes"] is False
    assert D(output["result"][LOW_INFORMATION]["value"]) == 1
    assert D(output["result"][FLIP_CHANGES]["value"]) == 0
    main = next(
        cell
        for cell in record["details"]["scenarios"]
        if cell["basis"] == "median" and cell["rate"] == "0.09"
    )
    assert main["information"] == LOW_INFORMATION_MARK
    assert any(
        LOW_INFORMATION_MARK in warning and "70" in warning
        for warning in record["details"]["warnings"]
    )
    summary = tool_summary(output)
    assert summary["guardrails"]["terminal_share"]["mark"] == LOW_INFORMATION_MARK
    assert LOW_INFORMATION in summary["input_quality"]


@pytest.mark.asyncio
async def test_terminal_threshold_is_the_configured_policy(db_session):
    request, qualified, _ = await seed(db_session)
    loose = GuardrailPolicy(terminal_share_low_information=D("0.90"))
    output = await ValuationService(
        db_session,
        FixtureQualification(qualified),
        guardrails=loose,
    ).reverse_dcf("local_owner", request, uuid4())
    direct = solve(D("212.4"), D("8.2"), D("0.09"), D("0.02"))
    assert output["status"] == "ok"
    assert direct.growth is not None and _main_growth(output) == direct.growth
    assert LOW_INFORMATION not in output["calculation"]["quality"]
    assert output["calculation"]["details"]["guardrails"]["terminal_share"]["applied"] is False
    assert D(output["result"][LOW_INFORMATION]["value"]) == 0
    assert (
        output["calculation"]["details"]["policy"]["guardrails"]["terminal_share_low_information"]
        == "0.90"
    )


@pytest.mark.asyncio
async def test_rate_outside_the_placeholder_band_is_a_visible_check(db_session):
    request, qualified, _ = await seed(db_session)
    request = request.model_copy(
        update={
            "main_rate": assumption("discount_rate", "0.20"),
            "sensitivity_rates": (
                assumption("discount_rate", "0.18"),
                assumption("discount_rate", "0.22"),
            ),
        }
    )
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf(
        "local_owner", request, uuid4()
    )
    direct = solve(D("212.4"), D("8.2"), D("0.20"), D("0.02"))
    assert output["status"] == "ok" and output["result"] is not None
    assert direct.growth is not None and _main_growth(output) == direct.growth
    record = output["calculation"]
    assert RATE_OUTSIDE_ANCHOR in record["quality"]
    assert SPREAD_BELOW_FLOOR not in record["quality"]
    assert LOW_INFORMATION not in record["quality"]
    rates = record["details"]["guardrails"]["rate_anchor"]["rates"]
    assert rates[0] == {"role": "main", "rate": "0.20", "outcome": "fail"}
    assert any("占位锚" in warning for warning in record["details"]["warnings"])


@pytest.mark.asyncio
async def test_discount_spread_floor_boundaries_keep_the_solved_growth(db_session):
    request, qualified, _ = await seed(db_session)
    service = ValuationService(db_session, FixtureQualification(qualified))
    exact = request.model_copy(update={"terminal_growth": assumption("terminal_growth", "0.03")})
    output = await service.reverse_dcf("local_owner", exact, uuid4())
    direct = solve(D("212.4"), D("8.2"), D("0.09"), D("0.03"))
    assert output["status"] == "ok"
    assert direct.growth is not None and _main_growth(output) == direct.growth
    pairs = output["calculation"]["details"]["guardrails"]["discount_spread"]["pairs"]
    assert [pair["outcome"] for pair in pairs] == ["pass", "pass", "pass"]
    assert pairs[1]["spread"] == "0.05"
    assert SPREAD_BELOW_FLOOR not in output["calculation"]["quality"]

    under = request.model_copy(update={"terminal_growth": assumption("terminal_growth", "0.0301")})
    output = await service.reverse_dcf("local_owner", under, uuid4())
    direct = solve(D("212.4"), D("8.2"), D("0.09"), D("0.0301"))
    assert output["status"] == "ok"
    assert direct.growth is not None and _main_growth(output) == direct.growth
    pairs = output["calculation"]["details"]["guardrails"]["discount_spread"]["pairs"]
    assert pairs[0]["outcome"] == "pass"
    assert pairs[1]["outcome"] == "fail"
    assert SPREAD_BELOW_FLOOR in output["calculation"]["quality"]
    assert any("5 个百分点" in warning for warning in output["calculation"]["details"]["warnings"])


@pytest.mark.asyncio
async def test_spread_floor_does_not_replace_the_solver_rejection_when_g_meets_r(db_session):
    request, qualified, _ = await seed(db_session)
    equal = request.model_copy(update={"terminal_growth": assumption("terminal_growth", "0.09")})
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf(
        "local_owner", equal, uuid4()
    )
    assert output["status"] == "calculation_error" and output["result"] is None
    assert output["calculation"]["errors"] == ["invalid_parameters"]
    assert SPREAD_BELOW_FLOOR in output["calculation"]["quality"]
    main = next(
        cell
        for cell in output["calculation"]["details"]["scenarios"]
        if cell["basis"] == "median" and cell["rate"] == "0.09"
    )
    assert main["growth"] is None and main["status"] == "calculation_error"


@pytest.mark.asyncio
async def test_saved_flip_reports_when_the_perturbed_solve_is_not_comparable(db_session):
    base, rate, terminal = D("8.2"), D("0.09"), D("0.02")
    lower_rate = rate - DEFAULT_GUARDRAILS.rate_flip_shift
    market = (
        present_value(base, D("-0.95"), rate, terminal)[0]
        + present_value(base, D("-0.95"), lower_rate, terminal)[0]
    ) / 2
    request, qualified, _ = await seed(db_session, market=format(market, "f"))
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf(
        "local_owner", request, uuid4()
    )
    direct = solve(market, base, rate, terminal)
    assert output["status"] == "ok"
    assert direct.growth is not None and _main_growth(output) == direct.growth
    assert FLIP_NOT_COMPARABLE in output["calculation"]["quality"]
    assert FLIP_CHANGES not in output["calculation"]["quality"]
    flip = output["calculation"]["details"]["guardrails"]["rate_flip"]
    assert flip["conclusion_changes"] is None
    assert flip["lower"]["conclusion"] == "unsolved"
    assert output["result"][FLIP_CHANGES]["value"] is None
    assert output["result"][FLIP_CHANGES]["unknown_reason"] == "rate_flip_not_comparable"
