"""Regression tests for the scoring v1.3 correctness fixes.

See docs/product/scoring-v1.3-changes.md. Covers, offline and with synthetic
provider data:

1. non-positive EPS / book value / revenue per share never yields a negative
   multiple, and non-positive multiples never count as cheap;
2. ROE stability needs the full history and a positive mean;
3. financial freshness follows the reporting period;
4. market freshness is measured in trading sessions (calendar aware, with a
   weekday fallback for unknown years);
5. provider-unavailable fields (iFinD dividend_yield) do not force review;
6. missing factors are excluded and renormalised instead of scoring 50;
7. newly listed companies without FY-3 history do not crash;
8. explanation citations must carry values and the label match is exact;
9. "1y" market statistics only use the trailing year and flag short history.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from investment_agent.research.ifind_chain import (
    EvidenceClaim,
    IfindProxyResult,
    ResearchExplanation,
    canonicalize_ifind_results,
    parse_proxy_rows,
    validate_explanation_policy,
)
from investment_agent.research.kernel import InProcessResearchKernel
from investment_agent.research_kernel.peer_comparison import compare_peer_metrics
from investment_agent.research_kernel.scoring import (
    SCORING_VERSION,
    assess_freshness,
    count_trading_days_after,
    latest_expected_annual_report_year,
    row_requires_review,
    score_stock,
)
from investment_agent.research_kernel.scoring_models import (
    factor_coverage,
    score_ratio,
    valuation_level,
    weighted_score,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar

REPO_ROOT = Path(__file__).parents[2]
CALENDAR = load_trading_calendar(REPO_ROOT / "config" / "calendars" / "a_share.yaml")
AS_OF = date(2026, 10, 5)
RETRIEVED_AT = "2026-10-05T08:00:00+00:00"

# --------------------------------------------------------------------------
# Synthetic provider bodies
# --------------------------------------------------------------------------

PROFITABILITY_FIELDS = (
    "ths_eps_basic_stock",
    "ths_nav_ps_stock",
    "ths_revenue_ps_stock",
    "ths_roe_stock",
    "ths_gross_selling_rate_stock",
    "ths_net_sales_rate_stock",
)
STATEMENT_FIELDS = (
    "ths_operating_total_revenue_stock",
    "ths_np_atoopc_stock",
    "ths_ncf_from_oa_stock",
    "ths_cash_paid_for_assets_stock",
)


def _csv_body(fields: tuple[str, ...], values: dict[str, str] | None) -> dict[str, Any]:
    header = ",".join(fields)
    if values is None:
        return {"format": "json", "data": {"data_preview": f"{header}\n"}}
    row = ",".join(values.get(field, "") for field in fields)
    return {"format": "json", "data": {"data_preview": f"{header}\n{row}\n"}}


def _history_body(sessions: int, *, end: date = date(2026, 9, 30), start_price: float = 10.0) -> dict[str, Any]:
    lines = ["time,close"]
    dates: list[date] = []
    current = end
    while len(dates) < sessions:
        if current.weekday() < 5:
            dates.append(current)
        current -= timedelta(days=1)
    for index, day in enumerate(sorted(dates)):
        price = start_price * (1 + 0.001 * index) * (1 + (0.02 if index % 2 else -0.02))
        lines.append(f"{day:%Y%m%d},{price:.4f}")
    return {"format": "json", "data": {"data_preview": "\n".join(lines) + "\n"}}


def _healthy_profitability(**overrides: str) -> dict[str, str]:
    values = {
        "ths_eps_basic_stock": "1.0",
        "ths_nav_ps_stock": "8.0",
        "ths_revenue_ps_stock": "10.0",
        "ths_roe_stock": "12",
        "ths_gross_selling_rate_stock": "30",
        "ths_net_sales_rate_stock": "10",
    }
    values.update(overrides)
    return values


def _results(**overrides: dict[str, Any]) -> dict[str, IfindProxyResult]:
    bodies: dict[str, dict[str, Any]] = {
        "info": _csv_body(
            ("ths_stock_short_name_stock", "ths_total_shares_stock"),
            {"ths_stock_short_name_stock": "测试股份", "ths_total_shares_stock": "1000000000"},
        ),
        "history": _history_body(250),
        "income_latest": _csv_body(
            STATEMENT_FIELDS,
            {
                "ths_operating_total_revenue_stock": "10000000000",
                "ths_np_atoopc_stock": "1000000000",
                "ths_ncf_from_oa_stock": "1500000000",
                "ths_cash_paid_for_assets_stock": "500000000",
            },
        ),
        "income_base": _csv_body(
            STATEMENT_FIELDS,
            {
                "ths_operating_total_revenue_stock": "7000000000",
                "ths_np_atoopc_stock": "700000000",
            },
        ),
        "cash_latest": _csv_body(
            STATEMENT_FIELDS,
            {"ths_ncf_from_oa_stock": "1500000000", "ths_cash_paid_for_assets_stock": "500000000"},
        ),
        "capital_latest": _csv_body(("ths_asset_liab_ratio_stock",), {"ths_asset_liab_ratio_stock": "40"}),
        **{
            f"profitability_{year}": _csv_body(PROFITABILITY_FIELDS, _healthy_profitability())
            for year in range(2022, 2026)
        },
    }
    bodies.update(overrides)
    return {
        name: IfindProxyResult(path="/synthetic", body=body, rows=parse_proxy_rows(body))
        for name, body in bodies.items()
    }


def _canonicalize(results: dict[str, IfindProxyResult]) -> Any:
    return canonicalize_ifind_results(
        ticker="600406.SH",
        industry="电力设备",
        report_year=2025,
        results=results,
        retrieved_at=RETRIEVED_AT,
        as_of=AS_OF,
    )


def _score_bundle(bundle: Any, tmp_path: Path) -> dict[str, Any]:
    for name in ("pool", "market", "financial"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(getattr(bundle, name), ensure_ascii=False), encoding="utf-8"
        )
    artifacts = InProcessResearchKernel().score_and_render(
        pool=tmp_path / "pool.json",
        market=tmp_path / "market.json",
        financial=tmp_path / "financial.json",
        artifact_dir=tmp_path,
        as_of=RETRIEVED_AT,
    )
    item = json.loads(artifacts.scores_json.read_text(encoding="utf-8"))["items"][0]
    assert isinstance(item, dict)
    return item


def _flags(item: dict[str, Any]) -> set[tuple[str, str]]:
    return {(flag["field"], flag["flag"]) for flag in item["quality_flags"]}


# --------------------------------------------------------------------------
# 1. Non-positive valuation denominators
# --------------------------------------------------------------------------


def test_loss_making_company_has_no_negative_pe_and_flags_negative_earnings():
    loss_year = _csv_body(
        PROFITABILITY_FIELDS,
        _healthy_profitability(ths_eps_basic_stock="-0.5", ths_roe_stock="-6"),
    )
    bundle = _canonicalize(_results(profitability_2025=loss_year))
    market = bundle.market["items"][0]

    assert market["pe"] is None
    assert "pe" not in market["missing_fields"], "a loss is not a provider data gap"
    assert ("pe", "negative_earnings") in _flags(market)
    assert market["needs_agent_review"] is True
    assert market["pb"] is not None and market["pb"] > 0


def test_negative_book_value_and_revenue_yield_no_multiple():
    distressed = _csv_body(
        PROFITABILITY_FIELDS,
        _healthy_profitability(ths_nav_ps_stock="-2", ths_revenue_ps_stock="0"),
    )
    market = _canonicalize(_results(profitability_2025=distressed)).market["items"][0]

    assert market["pb"] is None and market["ps"] is None
    assert {("pb", "negative_book_value"), ("ps", "non_positive_revenue")} <= _flags(market)


@pytest.mark.parametrize(
    ("pe", "pb", "expected"),
    [
        (-5.0, 1.0, "expensive"),  # loss-making: PE ignored, never cheap
        (10.0, -1.0, "expensive"),  # negative equity: PB ignored
        (-5.0, -1.0, "unknown"),
        (0.0, 0.0, "unknown"),
        (10.0, 1.5, "cheap"),
        (25.0, 3.0, "fair"),
    ],
)
def test_valuation_level_never_treats_non_positive_multiples_as_cheap(pe, pb, expected):
    assert valuation_level(pe, pb) == expected


def test_peer_comparison_ignores_non_positive_pe():
    target = {"key_metrics": {"pe": 20.0}}
    peers = [{"key_metrics": {"pe": -3.0}}, {"key_metrics": {"pe": 30.0}}]

    comparison = compare_peer_metrics(target, peers)["pe"]

    assert comparison["peer_count"] == 1
    assert comparison["peer_median"] == 30.0


def test_loss_making_company_end_to_end_is_not_cheap_and_needs_review(tmp_path: Path):
    loss = {
        f"profitability_{year}": _csv_body(
            PROFITABILITY_FIELDS,
            _healthy_profitability(ths_eps_basic_stock="-0.4", ths_roe_stock="-5"),
        )
        for year in range(2022, 2026)
    }
    item = _score_bundle(_canonicalize(_results(**loss)), tmp_path)

    assert item["scoring_version"] == SCORING_VERSION == "v1.3"
    assert item["key_metrics"]["pe"] is None
    assert item["valuation_level"] != "cheap"
    assert item["needs_agent_review"] is True
    assert item["suggestion"] == "needs_review"
    assert ("pe", "negative_earnings") in _flags(item)


# --------------------------------------------------------------------------
# 2. ROE stability
# --------------------------------------------------------------------------


def test_roe_stability_requires_every_year():
    gap = _csv_body(PROFITABILITY_FIELDS, _healthy_profitability(ths_roe_stock=""))
    financial = _canonicalize(_results(profitability_2023=gap)).financial["items"][0]

    assert financial["roe_stability"] is None
    assert "roe_stability" in financial["missing_fields"]


def test_roe_stability_is_missing_for_non_positive_mean_roe():
    negative = {
        f"profitability_{year}": _csv_body(
            PROFITABILITY_FIELDS, _healthy_profitability(ths_roe_stock=roe)
        )
        for year, roe in zip(range(2022, 2026), ("-4", "-3", "-5", "-4"), strict=True)
    }
    financial = _canonicalize(_results(**negative)).financial["items"][0]

    # v1.2 clamped 1 - std/negative_mean to a "perfectly stable" 1.0.
    assert financial["roe_stability"] is None


def test_roe_stability_for_full_positive_history():
    financial = _canonicalize(_results()).financial["items"][0]
    assert financial["roe_stability"] == 1.0


# --------------------------------------------------------------------------
# 3. Financial freshness follows the reporting period
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (date(2026, 4, 30), 2024),
        (date(2026, 5, 1), 2025),
        (date(2026, 10, 5), 2025),
        (date(2027, 1, 15), 2025),
    ],
)
def test_latest_expected_annual_report_year(as_of, expected):
    assert latest_expected_annual_report_year(as_of) == expected


def _freshness(**kwargs: Any) -> dict[str, dict[str, Any]]:
    defaults: dict[str, Any] = {
        "max_market_age_trading_days": 1,
        "max_financial_age_days": 180,
        "max_stock_age_days": 30,
    }
    defaults.update(kwargs)
    stock = defaults.pop("stock", {"updated_at": "2026-10-05T00:00:00+00:00"})
    market = defaults.pop("market", {"updated_at": "2026-09-30T07:00:00+00:00"})
    financial = defaults.pop("financial", {"updated_at": "2026-10-05T00:00:00+00:00"})
    return assess_freshness(stock, market, financial, **defaults)


def test_financial_freshness_flags_outdated_report_year_despite_fresh_fetch():
    result = _freshness(as_of="2026-10-05T12:00:00+00:00", report_year=2024)["financial_data"]

    assert result["basis"] == "report_period"
    assert result["expected_report_year"] == 2025
    assert result["is_stale"] is True


def test_financial_freshness_accepts_latest_due_annual_report():
    result = _freshness(as_of="2026-10-05T12:00:00+00:00", report_year=2025)["financial_data"]
    assert result["is_stale"] is False
    assert result["report_period_end"] == "2025-12-31"


def test_financial_freshness_falls_back_to_updated_at_without_report_year():
    result = _freshness(as_of="2026-10-05T12:00:00+00:00")["financial_data"]
    assert result["basis"] == "updated_at"
    assert result["is_stale"] is False


# --------------------------------------------------------------------------
# 4. Market freshness in trading sessions
# --------------------------------------------------------------------------


def test_weekend_run_on_friday_close_is_fresh():
    # Friday 2026-09-11 close, Monday 2026-09-14 evening run: one session.
    result = _freshness(
        as_of="2026-09-14T12:00:00+00:00",
        market={"updated_at": "2026-09-11T07:00:00+00:00"},
    )["market_data"]
    assert result["age_trading_days"] == 1
    assert result["is_stale"] is False

    saturday = _freshness(
        as_of="2026-09-12T12:00:00+00:00",
        market={"updated_at": "2026-09-11T07:00:00+00:00"},
    )["market_data"]
    assert saturday["age_trading_days"] == 0 and saturday["is_stale"] is False


def test_holiday_run_uses_exchange_calendar():
    holiday_end = {"as_of": "2026-10-08T12:00:00+00:00"}
    with_calendar = _freshness(**holiday_end, is_trading_day=CALENDAR.is_trading_day)
    weekday_only = _freshness(**holiday_end)

    assert with_calendar["market_data"]["calendar_basis"] == "exchange_calendar"
    assert with_calendar["market_data"]["age_trading_days"] == 1  # only 10-08 is open
    assert with_calendar["market_data"]["is_stale"] is False
    assert weekday_only["market_data"]["calendar_basis"] == "weekday"
    assert weekday_only["market_data"]["is_stale"] is True


def test_unknown_calendar_year_falls_back_to_weekdays_without_crashing():
    result = _freshness(
        as_of="2031-01-06T12:00:00+00:00",  # Monday; 2031 is not in the calendar
        market={"updated_at": "2031-01-03T07:00:00+00:00"},  # Friday
        is_trading_day=CALENDAR.is_trading_day,
    )["market_data"]

    assert result["calendar_basis"] == "weekday_fallback"
    assert result["age_trading_days"] == 1
    assert result["is_stale"] is False


def test_count_trading_days_after_spanning_known_and_unknown_years():
    count, basis = count_trading_days_after(
        date(2026, 12, 30), date(2027, 1, 4), is_trading_day=CALENDAR.is_trading_day
    )
    # 2026-12-31 (Thu) from the calendar; 2027-01-01 (Fri), 01-04 (Mon) by weekday.
    assert count == 3
    assert basis == "weekday_fallback"


def test_kernel_golden_holiday_window_is_fresh(tmp_path: Path):
    fixture = REPO_ROOT / "tests" / "fixtures" / "research_kernel" / "688019"
    artifacts = InProcessResearchKernel().score_and_render(
        pool=fixture / "pool.json",
        market=fixture / "market.json",
        financial=fixture / "financial.json",
        artifact_dir=tmp_path,
        as_of=datetime.fromisoformat("2026-10-05T15:22:50+00:00"),
    )
    item = json.loads(artifacts.scores_json.read_text(encoding="utf-8"))["items"][0]

    market = item["freshness"]["market_data"]
    assert market["age_hours"] > 24 * 5, "five calendar days old during the National Day break"
    assert market["age_trading_days"] == 0
    assert market["is_stale"] is False


# --------------------------------------------------------------------------
# 5. Provider-unavailable fields do not force review
# --------------------------------------------------------------------------


def _rows(dividend_source: str = "unavailable") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    stock = {
        "symbol": "600000",
        "name": "测试",
        "market": "SH",
        "industry": "电力设备",
        "market_cap": 150_000_000_000,
        "missing_fields": [],
        "quality_flags": [],
        "needs_agent_review": False,
    }
    market = {
        "symbol": "600000",
        "pe": 20.0,
        "pb": 3.0,
        "dividend_yield": None,
        "max_drawdown_1y": 0.2,
        "volatility_1y": 0.25,
        "missing_fields": ["dividend_yield"],
        "quality_flags": [
            {"field": "dividend_yield", "flag": "missing_field", "severity": "review"}
        ],
        "field_sources": {"dividend_yield": {"source": dividend_source, "quality": "missing"}},
        "needs_agent_review": True,
    }
    financial = {
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
    return stock, market, financial


def test_provider_unavailable_field_is_non_blocking():
    stock, market, financial = _rows()
    assert row_requires_review(market, {"dividend_yield"}) is False

    item = score_stock(stock, market, financial)
    assert item["provider_unavailable_fields"] == ["dividend_yield"]
    assert "dividend_yield" in item["missing_fields"], "still reported as missing data"
    assert item["needs_agent_review"] is False


def test_unexpected_missing_field_still_blocks():
    stock, market, financial = _rows(dividend_source="/stock/realtime")
    item = score_stock(stock, market, financial)
    assert item["needs_agent_review"] is True


def test_review_flag_on_other_field_still_blocks():
    _, market, _ = _rows()
    market["quality_flags"].append({"field": "pe", "flag": "negative_earnings", "severity": "review"})
    assert row_requires_review(market, {"dividend_yield"}) is True


# --------------------------------------------------------------------------
# 6. Missing factors are excluded, not imputed as 50
# --------------------------------------------------------------------------


def test_score_ratio_returns_none_for_missing_value():
    assert score_ratio(None, 0.0, 1.0) is None


def test_weighted_score_renormalises_over_present_factors():
    factors = {"a": 80.0, "b": None, "c": 40.0}
    weights = {"a": 50, "b": 30, "c": 20}

    assert weighted_score(factors, weights) == pytest.approx((80 * 50 + 40 * 20) / 70, abs=0.01)
    assert factor_coverage(factors, weights) == (0.7, ["b"])


def test_weighted_score_is_none_when_every_factor_is_missing():
    assert weighted_score({"a": None, "b": None}, {"a": 1, "b": 1}) is None
    assert factor_coverage({"a": None, "b": None}, {"a": 1, "b": 1}) == (0.0, ["a", "b"])


def test_missing_factor_is_recorded_and_lowers_confidence():
    stock, market, financial = _rows()
    stock["industry"] = "银行"
    item = score_stock(stock, market, financial)

    full_stock, full_market, full_financial = _rows()
    full_stock["industry"] = "银行"
    full_market.update(
        dividend_yield=0.05, missing_fields=[], quality_flags=[], needs_agent_review=False
    )
    full = score_stock(full_stock, full_market, full_financial)

    assert item["selected_model"] == full["selected_model"] == "banking"
    assert item["missing_factors"] == ["shareholder_return"]
    assert item["factor_scores"]["shareholder_return"] is None
    assert item["factor_coverage"] == 0.75
    assert full["factor_coverage"] == 1.0 and full["missing_factors"] == []
    assert item["research_confidence_score"] < full["research_confidence_score"]
    # The missing factor is excluded rather than scored as a neutral 50.
    present = {k: v for k, v in item["factor_scores"].items() if v is not None}
    assert "shareholder_return" not in present


def test_all_factors_missing_yields_no_score_and_review():
    stock = {"symbol": "600000", "name": "空", "market": "SH", "industry": "", "market_cap": None}
    item = score_stock(stock, {"symbol": "600000"}, {"symbol": "600000"})

    assert item["investment_score"] is None
    assert item["factor_coverage"] == 0.0
    assert item["needs_agent_review"] is True
    assert item["suggestion"] == "needs_review"


# --------------------------------------------------------------------------
# 7. Newly listed company
# --------------------------------------------------------------------------


def test_newly_listed_company_without_base_period_does_not_crash(tmp_path: Path):
    empty_statement = _csv_body(STATEMENT_FIELDS, None)
    empty_profitability = _csv_body(PROFITABILITY_FIELDS, None)
    results = _results(
        history=_history_body(120),
        income_base=empty_statement,
        profitability_2022=empty_profitability,
        profitability_2023=empty_profitability,
    )
    bundle = _canonicalize(results)
    financial = bundle.financial["items"][0]
    market = bundle.market["items"][0]

    assert financial["revenue_growth_3y"] is None
    assert financial["net_profit_growth_3y"] is None
    assert financial["roe_stability"] is None
    assert {"revenue_growth_3y", "net_profit_growth_3y", "roe_stability"} <= set(
        financial["missing_fields"]
    )
    assert ("return_1y", "short_price_history") in _flags(market)

    item = _score_bundle(bundle, tmp_path)
    assert item["investment_score"] is not None
    assert item["missing_factors"], "growth/stability factors must be excluded, not imputed"
    assert item["needs_agent_review"] is True


def test_latest_period_endpoint_still_fails_hard():
    with pytest.raises(ValueError, match="no rows for latest income statement"):
        _canonicalize(_results(income_latest=_csv_body(STATEMENT_FIELDS, None)))


# --------------------------------------------------------------------------
# 8. Explanation citations
# --------------------------------------------------------------------------


def _explanation(*, evidence: list[str], conclusion: str, dq: list[str] | None = None) -> ResearchExplanation:
    return ResearchExplanation(
        summary="s",
        classification_rationale=[EvidenceClaim(text="c", supporting_fields=evidence)],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=(
            [EvidenceClaim(text="dq", supporting_fields=dq)] if dq is not None else []
        ),
        open_questions=[],
        workflow_conclusion=conclusion,
    )


SCORE_ITEM = {
    "needs_agent_review": True,
    "business_score": 60.0,
    "key_metrics": {"roe": 0.16, "dividend_yield": None, "pe": None},
    "factor_scores": {"shareholder_return": None, "roe_quality": 70.0},
    "missing_fields": ["dividend_yield"],
    "field_sources": {"market_data": {"dividend_yield": {"source": "unavailable"}}},
}


@pytest.mark.parametrize(
    "path",
    ["key_metrics.dividend_yield", "key_metrics.pe", "factor_scores.shareholder_return"],
)
def test_evidence_cannot_cite_null_or_missing_values(path: str):
    with pytest.raises(ValueError, match="missing or null fields"):
        validate_explanation_policy(
            _explanation(evidence=[path], conclusion="needs_review"), SCORE_ITEM
        )


def test_data_quality_notes_may_cite_missing_values_and_provenance():
    validate_explanation_policy(
        _explanation(
            evidence=["key_metrics.roe", "field_sources.market_data.dividend_yield"],
            conclusion="needs_review：股息率缺失",
            dq=["key_metrics.dividend_yield", "missing_fields"],
        ),
        SCORE_ITEM,
    )


@pytest.mark.parametrize("conclusion", ["no_needs_review", "needs_reviewed", "needsreview", "复核"])
def test_needs_review_label_requires_exact_token(conclusion: str):
    with pytest.raises(ValueError, match="needs_review workflow label"):
        validate_explanation_policy(
            _explanation(evidence=["key_metrics.roe"], conclusion=conclusion), SCORE_ITEM
        )


# --------------------------------------------------------------------------
# 9. Trailing one-year market window
# --------------------------------------------------------------------------


def test_one_year_statistics_ignore_sessions_older_than_365_days():
    lines = ["time,close"]
    # A crash 380 days before as_of must not leak into the 1y drawdown/return.
    lines.append(f"{AS_OF - timedelta(days=390):%Y%m%d},100")
    lines.append(f"{AS_OF - timedelta(days=380):%Y%m%d},20")
    trailing = _history_body(250, end=date(2026, 9, 30))
    trailing_rows = trailing["data"]["data_preview"].strip().splitlines()[1:]
    history = {"format": "json", "data": {"data_preview": "\n".join(lines + trailing_rows) + "\n"}}

    market = _canonicalize(_results(history=history)).market["items"][0]

    assert market["max_drawdown_1y"] < 0.1
    assert market["return_1y"] is not None and abs(market["return_1y"]) < 1.0
    assert not any(flag == "short_price_history" for _, flag in _flags(market))


def test_short_history_is_flagged_for_review():
    market = _canonicalize(_results(history=_history_body(60))).market["items"][0]

    assert market["return_1y"] is not None
    assert ("volatility_1y", "short_price_history") in _flags(market)
    assert market["needs_agent_review"] is True
