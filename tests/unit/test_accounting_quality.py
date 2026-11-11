"""Accounting-quality pre-screen: three signals, exemptions, missing data, defaults."""

from __future__ import annotations

from datetime import date

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.library import (
    FLAG_NON_POSITIVE_FINANCE_EXPENSE,
    FLAG_NON_POSITIVE_NET_INCOME,
)
from investment_agent.factors.registry import FactorValue
from investment_agent.factors.snapshot_model import OBSERVATION, SnapshotView
from investment_agent.rules.accounting_quality import (
    DEFAULT_ACCOUNTING_QUALITY,
    UNCALIBRATED_NOTE,
    AccountingQualityPolicy,
    assess_accounting_quality,
)
from investment_agent.rules.screen import ScreenMember, run_screen
from investment_agent.rules.templates import load_templates
from tests.unit._factor_support import annual

AS_OF = date(2026, 9, 30)
PASSING = {
    "ocf_to_net_income": 1.2,
    "share_dilution": 0.01,
    "interest_coverage": 8.0,
}


def _fv(name: str, value: float | None, quality: str = "ok", *flags: str) -> FactorValue:
    return FactorValue(name, "1", value, quality, tuple(flags))  # type: ignore[arg-type]


def _templates() -> dict:
    return {template.id: template for _, template in load_templates()}


def _member(ticker: str, group: str, factors: dict[str, FactorValue]) -> ScreenMember:
    return ScreenMember(listing_id=1, ticker=ticker, industry_group=group, factors=factors)


def _quality_factors(**overrides: FactorValue | float | None) -> dict[str, FactorValue]:
    raw: dict[str, FactorValue | float | None] = {**PASSING, **overrides}
    factors: dict[str, FactorValue] = {}
    for name, value in raw.items():
        if isinstance(value, FactorValue):
            factors[name] = value
        elif value is not None:
            factors[name] = _fv(name, float(value))
    return factors


def _screen_ready(**quality: FactorValue | float | None) -> dict[str, FactorValue]:
    """Thesis inputs that clear quality_growth, plus the quality signals."""

    base = {
        "roe": 0.20,
        "roe_stability": 0.80,
        "revenue_growth_3y": 0.20,
        "net_profit_growth_3y": 0.20,
        "gross_margin": 0.40,
        "pe_percentile_5y": 0.30,
        "is_st": 0.0,
        "report_overdue": 0.0,
    }
    factors = {name: _fv(name, value) for name, value in base.items()}
    factors.update(_quality_factors(**quality))
    return factors


def _dividend_ready(**quality: FactorValue | float | None) -> dict[str, FactorValue]:
    base = {
        "market_cap": 2.0e11,
        "roe": 0.12,
        "roe_stability": 0.90,
        "net_profit_growth_3y": 0.02,
        "volatility_1y": 0.20,
        "dividend_yield_ttm": 0.04,
        "pb_percentile_5y": 0.20,
        "is_st": 0.0,
        "report_overdue": 0.0,
    }
    factors = {name: _fv(name, value) for name, value in base.items()}
    factors.update(_quality_factors(**quality))
    return factors


def _outcome(template_id: str, member: ScreenMember) -> str:
    template = _templates()[template_id]
    if member.industry_group not in template.applicable_groups:
        template = template.model_copy(
            update={"applicable_groups": [*template.applicable_groups, member.industry_group]}
        )
    run = run_screen(as_of=AS_OF, members=[member], templates=[template])
    result = run.templates[0]
    found = next(item for item in result.members if item.ticker == member.ticker)
    return found.outcome


def _view(**fields_by_year: dict[str, float | None]) -> SnapshotView:
    periods = [annual(int(year), **fields) for year, fields in fields_by_year.items()]
    return SnapshotView.build(
        ticker="600519.SH",
        as_of=AS_OF,
        policy=OBSERVATION,
        industry_code="C15",
        industry_name="酒、饮料和精制茶制造业",
        periods=periods,
    )


def test_each_signal_excludes_and_a_clean_name_still_hits() -> None:
    template = _templates()["quality_growth"]
    clean = _member("600519.SH", "consumer", _screen_ready())
    weak_cash = _member("600001.SH", "consumer", _screen_ready(ocf_to_net_income=0.2))
    diluted = _member("600002.SH", "consumer", _screen_ready(share_dilution=0.20))
    thin_cover = _member("600003.SH", "consumer", _screen_ready(interest_coverage=1.5))
    run = run_screen(
        as_of=AS_OF,
        members=[clean, weak_cash, diluted, thin_cover],
        templates=[template],
    )
    result = run.templates[0]
    outcomes = {member.ticker: member for member in result.members}
    assert outcomes["600519.SH"].outcome == "hit"
    assert outcomes["600001.SH"].outcome == "excluded"
    assert outcomes["600002.SH"].outcome == "excluded"
    assert outcomes["600003.SH"].outcome == "excluded"
    assert outcomes["600001.SH"].detail == "pre_screen:ocf_to_net_income"
    assert outcomes["600002.SH"].detail == "pre_screen:share_dilution"
    assert outcomes["600003.SH"].detail == "pre_screen:interest_coverage"
    assert outcomes["600001.SH"].reasons[0]["outcome"] == "not_met"
    assert outcomes["600001.SH"].reasons[0]["actual"] == 0.2
    assert set(result.pre_screen["excluded"]) == {"600001.SH", "600002.SH", "600003.SH"}
    assert "600519.SH" not in result.pre_screen["excluded"]


def test_banks_and_insurers_are_exempt_even_when_the_signals_fail() -> None:
    bad = dict(ocf_to_net_income=0.1, share_dilution=0.5, interest_coverage=0.4)
    bank = _member("601398.SH", "banking", _dividend_ready(**bad))
    insurer = _member("601318.SH", "insurance", _screen_ready(**bad))
    broker = _member("600030.SH", "broker", _screen_ready(**bad))
    assert _outcome("dividend_blue_chip", bank) == "hit"
    assert _outcome("quality_growth", insurer) == "hit"
    assert _outcome("quality_growth", broker) == "excluded"

    bank_run = run_screen(
        as_of=AS_OF,
        members=[bank],
        templates=[_templates()["dividend_blue_chip"]],
    )
    assert bank_run.templates[0].pre_screen["exempted"] == ["601398.SH"]
    assert bank_run.templates[0].pre_screen["excluded"] == []
    assert "banking" in bank_run.templates[0].pre_screen["exempt_groups"]
    assert "insurance" in bank_run.templates[0].pre_screen["exempt_groups"]
    assert "broker" not in bank_run.templates[0].pre_screen["exempt_groups"]


def test_missing_or_uncomputable_signals_do_not_pass() -> None:
    absent = _member(
        "600010.SH",
        "consumer",
        _screen_ready(ocf_to_net_income=None, share_dilution=None, interest_coverage=None),
    )
    missing = _member(
        "600011.SH",
        "consumer",
        _screen_ready(
            ocf_to_net_income=_fv(
                "ocf_to_net_income", None, "missing", FLAG_NON_POSITIVE_NET_INCOME
            ),
        ),
    )
    # not_applicable without the finance-expense skip is not a free pass.
    disguised = _member(
        "600012.SH",
        "consumer",
        _screen_ready(interest_coverage=_fv("interest_coverage", None, "not_applicable")),
    )
    run = run_screen(
        as_of=AS_OF,
        members=[absent, missing, disguised],
        templates=[_templates()["quality_growth"]],
    )
    by_ticker = {member.ticker: member for member in run.templates[0].members}
    assert by_ticker["600010.SH"].outcome == "excluded"
    assert {reason["outcome"] for reason in by_ticker["600010.SH"].reasons} == {"missing"}
    assert by_ticker["600010.SH"].reasons[0]["flags"] == ["factor_absent"]
    assert by_ticker["600011.SH"].outcome == "excluded"
    cash = next(
        reason for reason in by_ticker["600011.SH"].reasons if reason["factor"] == "ocf_to_net_income"
    )
    assert cash["outcome"] == "missing"
    assert cash["quality"] == "missing"
    assert FLAG_NON_POSITIVE_NET_INCOME in cash["flags"]
    assert cash["actual"] is None
    assert by_ticker["600012.SH"].outcome == "excluded"
    coverage = next(
        reason
        for reason in by_ticker["600012.SH"].reasons
        if reason["factor"] == "interest_coverage"
    )
    assert coverage["outcome"] == "missing"
    assert coverage["quality"] == "not_applicable"


def test_non_positive_finance_expense_skips_only_that_signal() -> None:
    factors = _quality_factors(
        interest_coverage=_fv(
            "interest_coverage",
            None,
            "not_applicable",
            FLAG_NON_POSITIVE_FINANCE_EXPENSE,
        )
    )
    decision = assess_accounting_quality(
        industry_group="consumer",
        factors=factors,
        thresholds=DEFAULT_ACCOUNTING_QUALITY.params(),
    )
    assert decision.status == "pass"
    assert decision.signals == ()
    (reason,) = decision.reasons
    assert reason["factor"] == "interest_coverage"
    assert reason["outcome"] == "not_applicable"
    assert FLAG_NON_POSITIVE_FINANCE_EXPENSE in reason["flags"]

    member = _member("600519.SH", "consumer", _screen_ready(
        interest_coverage=_fv(
            "interest_coverage",
            None,
            "not_applicable",
            FLAG_NON_POSITIVE_FINANCE_EXPENSE,
        )
    ))
    assert _outcome("quality_growth", member) == "hit"


def test_template_threshold_overrides_the_shared_default() -> None:
    template = _templates()["quality_growth"]
    looser = template.model_copy(
        update={"params": {**template.params, "ocf_to_net_income_min": 0.5}}
    )
    member = _member("600519.SH", "consumer", _screen_ready(ocf_to_net_income=0.6))
    strict = run_screen(as_of=AS_OF, members=[member], templates=[template])
    relaxed = run_screen(as_of=AS_OF, members=[member], templates=[looser])
    assert strict.templates[0].members[0].outcome == "excluded"
    assert relaxed.templates[0].members[0].outcome == "hit"
    assert strict.templates[0].pre_screen["thresholds"]["ocf_to_net_income_min"] == 0.8
    assert relaxed.templates[0].pre_screen["thresholds"]["ocf_to_net_income_min"] == 0.5


def test_defaults_are_marked_uncalibrated_and_match_every_template() -> None:
    policy = DEFAULT_ACCOUNTING_QUALITY
    assert policy.calibration == "uncalibrated"
    assert "未校准的起始默认值，不是研究过的 A 股参数" in UNCALIBRATED_NOTE
    assert "没有用 A 股样本校准" in UNCALIBRATED_NOTE
    with pytest.raises(ValueError, match="uncalibrated"):
        AccountingQualityPolicy(calibration="validated")  # type: ignore[arg-type]
    loaded = load_templates()
    assert {template.id for _, template in loaded} == {
        "cycle_reversal",
        "dividend_blue_chip",
        "high_growth_venture",
        "quality_growth",
        "steady_quality_growth",
    }
    for _, template in loaded:
        for name, default in policy.params().items():
            assert template.params[name] == default
            help_text = (template.param_meta or {})[name].help or ""
            assert "未校准的起始默认值，不是研究过的 A 股参数" in help_text
            factor = name.removesuffix("_min").removesuffix("_max")
            assert factor in help_text
        # Sell discipline shipped in the previous change stays put.
        assert "exit_sell" in template.holding_zones
        assert "exit_review" in template.holding_zones
        assert template.params["streak_periods"] == 3
        assert "未校准" in ((template.param_meta or {})["streak_periods"].help or "")
    described = policy.describe()
    assert described["calibration"] == "uncalibrated"
    assert described["note"] == UNCALIBRATED_NOTE
    assert described["thresholds"] == policy.params()


def test_factors_compute_the_three_ratios_and_refuse_silent_fallbacks() -> None:
    healthy = _view(
        **{
            "2024": {"total_shares": 100.0},
            "2025": {
                "net_profit_parent": 100.0,
                "net_profit_total": 140.0,
                "operating_cash_flow": 90.0,
                "total_shares": 103.0,
                "operating_profit": 70.0,
                "finance_expense": 10.0,
            },
        }
    )
    ocf = DEFAULT_REGISTRY.get("ocf_to_net_income").compute(healthy)
    dilution = DEFAULT_REGISTRY.get("share_dilution").compute(healthy)
    coverage = DEFAULT_REGISTRY.get("interest_coverage").compute(healthy)
    assert ocf.quality == "ok"
    assert ocf.value == pytest.approx(0.9)  # parent, not the larger total
    assert dilution.value == pytest.approx(0.03)
    assert coverage.quality == "approx"
    assert coverage.value == pytest.approx(8.0)

    total_only = _view(
        **{"2025": {"net_profit_total": 100.0, "operating_cash_flow": 80.0}}
    )
    approx = DEFAULT_REGISTRY.get("ocf_to_net_income").compute(total_only)
    assert approx.quality == "approx"
    assert approx.value == pytest.approx(0.8)

    loss = _view(
        **{"2025": {"net_profit_parent": -5.0, "operating_cash_flow": 10.0, "total_shares": 10.0}}
    )
    missing_ratio = DEFAULT_REGISTRY.get("ocf_to_net_income").compute(loss)
    assert missing_ratio.value is None
    assert missing_ratio.quality == "missing"
    assert FLAG_NON_POSITIVE_NET_INCOME in missing_ratio.flags

    no_prior = DEFAULT_REGISTRY.get("share_dilution").compute(loss)
    assert no_prior.value is None
    assert no_prior.quality == "missing"

    net_interest = _view(
        **{"2025": {"operating_profit": 40.0, "finance_expense": -3.0}}
    )
    skipped = DEFAULT_REGISTRY.get("interest_coverage").compute(net_interest)
    assert skipped.value is None
    assert skipped.quality == "not_applicable"
    assert FLAG_NON_POSITIVE_FINANCE_EXPENSE in skipped.flags

    no_expense = _view(**{"2025": {"operating_profit": 40.0}})
    absent = DEFAULT_REGISTRY.get("interest_coverage").compute(no_expense)
    assert absent.value is None
    assert absent.quality == "missing"

    bank = SnapshotView.build(
        ticker="601398.SH",
        as_of=AS_OF,
        policy=OBSERVATION,
        industry_code="J66",
        industry_name="货币金融服务",
        periods=[annual(2025, net_profit_parent=10.0, operating_cash_flow=1.0, total_shares=10.0)],
    )
    for name in ("ocf_to_net_income", "share_dilution", "interest_coverage"):
        value = DEFAULT_REGISTRY.get(name).compute(bank)
        assert value.quality == "not_applicable"
        assert value.value is None

    catalogue = {entry["name"]: entry for entry in DEFAULT_REGISTRY.catalogue()}
    assert catalogue["interest_coverage"]["applicability"] == "all except banking, insurance"
