"""Snapshot source mode: canonical bundles from target-pool views (P1-B5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from investment_agent.factors.view import (
    TARGET_POOL,
    FinancialPeriod,
    ProviderFields,
    SnapshotView,
    resolve_period_fields,
)
from investment_agent.research.contracts import CanonicalResearchBundle
from investment_agent.research.snapshot_source import (
    SnapshotDataMissingError,
    build_snapshot_bundle,
)
from investment_agent.research_kernel.scoring import SCORING_VERSION, score_pool
from tests.unit._factor_support import baostock_view, synthetic_bars

AS_OF = date(2026, 9, 30)
RETRIEVED = "2026-10-01T00:00:00+00:00"


def _item(payload: dict[str, Any]) -> dict[str, Any]:
    (item,) = payload["items"]
    return item


def _score(bundle) -> dict[str, Any]:
    scores = score_pool(bundle.pool, bundle.market, bundle.financial, as_of="2026-09-30T15:59:59+00:00")
    (item,) = scores["items"]
    return item


@pytest.mark.parametrize(
    ("symbol", "group"), [("600519", "consumer"), ("601398", "banking")]
)
def test_factor_fixtures_build_a_valid_bundle_that_scores(symbol: str, group: str):
    view = baostock_view(symbol, AS_OF)
    bundle = build_snapshot_bundle(
        view, company_name=symbol, industry=None, retrieved_at=RETRIEVED, requested_report_year=2025
    )
    CanonicalResearchBundle.model_validate(
        {"pool": bundle.pool, "market": bundle.market, "financial": bundle.financial}
    )
    stock, market, financial = _item(bundle.pool), _item(bundle.market), _item(bundle.financial)
    assert bundle.pool["metadata"]["source_mode"] == "snapshot"
    assert bundle.financial["metadata"]["report_year"] == 2025
    assert stock["industry_group"] == group
    assert stock["field_sources"]["industry"]["provider"] == "csrc"  # fixtures carry CSRC only
    bar = view.latest_raw_bar()
    assert market["latest_price"] == bar.close
    assert market["pe"] == bar.pe_ttm and market["pb"] == bar.pb_mrq
    assert market["field_sources"]["pe"]["source"] == "snapshot_view:factor:pe_ttm"
    assert market["field_sources"]["pe"]["verification"] == "unverified"
    assert financial["roe"] == view.latest_annual().value("roe")
    assert financial["field_sources"]["roe"]["provider"] == "baostock"
    # dividend history not loaded for this view -> provider gap, non-blocking
    assert market["dividend_yield"] is None
    assert market["field_sources"]["dividend_yield"]["source"] == "unavailable"

    score = _score(bundle)
    assert score["scoring_version"] == SCORING_VERSION
    assert score["business_score"] is not None
    assert "dividend_yield" in score["provider_unavailable_fields"]
    expected_sector = "banking" if group == "banking" else "general"
    assert score["company_profile"]["sector_type"] == expected_sector


def test_user_industry_text_overrides_the_data_group():
    view = baostock_view("600519", AS_OF)
    bundle = build_snapshot_bundle(
        view, company_name="贵州茅台", industry="银行", retrieved_at=RETRIEVED
    )
    stock = _item(bundle.pool)
    assert stock["industry"] == "银行" and stock["industry_group"] == "consumer"
    assert stock["field_sources"]["industry"]["provider"] == "manual"
    assert _score(bundle)["company_profile"]["sector_type"] == "banking"


def test_requested_report_year_substitution_is_flagged():
    view = baostock_view("600519", AS_OF)
    bundle = build_snapshot_bundle(
        view, company_name="x", industry=None, retrieved_at=RETRIEVED, requested_report_year=2026
    )
    flags = {flag["flag"] for flag in _item(bundle.financial)["quality_flags"]}
    assert "report_year_substituted" in flags


def _resolved_view() -> SnapshotView:
    """FY2022-2025 resolved over baostock + AkShare + an iFinD verification row."""

    periods = []
    for year in range(2022, 2026):
        baostock = ProviderFields(
            "baostock",
            {
                "roe": 0.2 + (year - 2022) / 100,
                "gross_margin": 0.5,
                "net_margin": 0.2,
                "debt_ratio": 0.4,
                "net_profit_total": 100.0 + year - 2022,
                "revenue_main": 500.0 * 1.1 ** (year - 2022),
                "total_shares": 10.0,
            },
            ref=f"financial_snapshot:b{year}",
        )
        akshare = ProviderFields(
            "akshare",
            {
                "net_profit_parent": 90.0 + year - 2022,
                "revenue_total": 520.0 * 1.1 ** (year - 2022),
                "operating_cash_flow": 120.0,
                "capital_expenditure": 20.0,
                "debt_ratio": 0.401,
            },
            ref=f"financial_snapshot:a{year}",
        )
        candidates = [baostock, akshare]
        if year == 2025:
            candidates.append(
                ProviderFields(
                    "ifind",
                    {"roe": 0.25, "debt_ratio": 0.6},
                    ref="financial_snapshot:i2025",
                    verification={"roe": "verified", "debt_ratio": "conflict"},
                )
            )
        fields, sources = resolve_period_fields(candidates, anchor="baostock")
        periods.append(
            FinancialPeriod(
                report_period=date(year, 12, 31),
                period_kind="annual",
                available_from=date(year + 1, 4, 20),
                availability="actual",
                fields=fields,
                ref=f"financial_snapshot:b{year}",
                sources=sources,
            )
        )
    bars = synthetic_bars(AS_OF, 260, close=50.0, pe_ttm=12.0, pb_mrq=2.0)
    return SnapshotView.build(
        ticker="600900.SH",
        as_of=AS_OF,
        policy=TARGET_POOL,
        industry_code="D44",
        industry_name="电力、热力生产和供应业",
        raw_bars=bars,
        adjusted_bars=bars,
        periods=periods,
    )


def test_field_sources_carry_provider_and_verification_state():
    bundle = build_snapshot_bundle(
        _resolved_view(), company_name="长江电力", industry=None, retrieved_at=RETRIEVED
    )
    stock, market, financial = _item(bundle.pool), _item(bundle.market), _item(bundle.financial)
    sources = financial["field_sources"]
    # verified iFinD value wins (A12 precedence)
    assert financial["roe"] == 0.25
    assert (sources["roe"]["provider"], sources["roe"]["verification"]) == ("ifind", "verified")
    # iFinD conflict: iFinD value, flagged for review
    assert financial["debt_ratio"] == 0.6
    assert sources["debt_ratio"]["verification"] == "conflict"
    assert "debt_ratio" in financial["provider_conflicts"]
    assert financial["needs_agent_review"] is True
    # Eastmoney (AkShare) primary fields
    assert financial["revenue"] == pytest.approx(520.0 * 1.1**3)
    assert financial["net_profit"] == 93.0
    assert sources["revenue"]["provider"] == "akshare"
    assert financial["free_cash_flow"] == 100.0
    assert sources["free_cash_flow"]["provider"] == "akshare"
    # target-pool growth basis: baostock main-business revenue (approx -> proxy)
    assert sources["revenue_growth_3y"]["quality"] == "proxy"
    assert financial["revenue_growth_3y"] == pytest.approx(0.1, abs=1e-6)
    # unverified free-source value
    assert sources["gross_margin"]["verification"] == "unverified"
    assert stock["industry_group"] == "utility"
    assert market["market_cap"] == 500.0
    score = _score(bundle)
    assert score["company_profile"]["sector_type"] == "public_utility"
    assert "financial_data" in score["provider_conflicts"]
    assert score["needs_agent_review"] is True


def test_negative_pe_ttm_is_not_meaningful_rather_than_missing():
    view = _resolved_view()
    bars = tuple(replace(bar, pe_ttm=-5.0) for bar in view.raw_bars)
    view = replace(view, raw_bars=bars)
    market = _item(
        build_snapshot_bundle(view, company_name="x", industry=None, retrieved_at=RETRIEVED).market
    )
    assert market["pe"] is None and "pe" not in market["missing_fields"]
    assert any(
        flag["field"] == "pe" and flag["flag"] == "negative_earnings"
        for flag in market["quality_flags"]
    )


def test_listing_without_price_or_annual_report_is_missing_data():
    view = _resolved_view()
    with pytest.raises(SnapshotDataMissingError, match="no free-source price"):
        build_snapshot_bundle(
            replace(view, raw_bars=()), company_name="x", industry=None, retrieved_at=RETRIEVED
        )
    with pytest.raises(SnapshotDataMissingError, match="no free-source annual report"):
        build_snapshot_bundle(
            replace(view, periods=()), company_name="x", industry=None, retrieved_at=RETRIEVED
        )


def test_snapshot_bundle_records_raw_view_audit():
    bundle = build_snapshot_bundle(
        _resolved_view(), company_name="x", industry=None, retrieved_at=RETRIEVED
    )
    assert bundle.raw["schema_version"] == "snapshot-raw.v1"
    assert bundle.raw["policy"] == "target_pool"
    assert bundle.raw["industry"]["group"] == "utility"
    assert bundle.raw["factors"]["roe"]["value"] == 0.25
