"""Factor registry, first-batch factors and the in-memory snapshot view (P1-B1)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.field_resolution import AMBIGUOUS_STORED_FIELDS, factor_fields
from investment_agent.factors.library import MIN_PLAUSIBLE_DEBT_RATIO
from investment_agent.factors.metrics import annualized_growth, return_stats, trim_to_window
from investment_agent.factors.registry import (
    Applicability,
    Factor,
    FactorRegistry,
    FactorValue,
    Partial,
)
from investment_agent.factors.snapshot_model import (
    OBSERVATION,
    TARGET_POOL,
    FinancialPeriod,
    SnapshotView,
)
from tests.unit._factor_support import (
    annual,
    baostock_view,
    fixture_backward_bars,
    synthetic_bars,
)

AS_OF = date(2026, 9, 30)

FIRST_BATCH = {
    "roe",
    "roe_stability",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "net_profit_growth_3y",
    "revenue_growth_3y",
    "eps_ttm",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "market_cap",
    "return_1y",
    "volatility_1y",
    "max_drawdown_1y",
    "turnover_20d",
    "is_st",
    "trade_status",
    "report_overdue",
    "pe_fy",
    "pb_fy",
    "ps_fy",
    "free_cash_flow",
}


def _compute(view: SnapshotView, name: str) -> FactorValue:
    return DEFAULT_REGISTRY.get(name).compute(view)


# --------------------------------------------------------------------------- #
# Registry and metadata
# --------------------------------------------------------------------------- #


def test_registry_lists_the_first_batch_with_metadata():
    assert set(DEFAULT_REGISTRY.names()) >= FIRST_BATCH
    for factor in DEFAULT_REGISTRY:
        assert factor.version and factor.unit and factor.description
        assert factor.inputs
        assert factor.providers <= {"baostock", "ifind"}
    catalogue = {entry["name"]: entry for entry in DEFAULT_REGISTRY.catalogue()}
    assert catalogue["gross_margin"]["applicability"] == "all except banking, broker, insurance"
    assert catalogue["roe"]["applicability"] == "all"
    assert {item["provider"] for item in catalogue["pe_ttm"]["inputs"]} == {"baostock"}
    assert {item["provider"] for item in catalogue["pe_fy"]["inputs"]} == {"ifind"}


def test_registry_rejects_duplicates_unknown_names_and_inputless_factors():
    factor = DEFAULT_REGISTRY.get("roe")
    registry = FactorRegistry([factor])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(factor)
    with pytest.raises(KeyError, match="unknown factor"):
        registry.get("nope")
    with pytest.raises(ValueError, match="no inputs"):
        registry.register(Factor("x", "1", "u", "d", (), lambda view: Partial(1.0)))
    assert "roe" in registry and len(registry) == 1


def test_factor_value_invariants():
    with pytest.raises(ValueError, match="carries no value"):
        FactorValue("x", "1", 1.0, "missing")
    with pytest.raises(ValueError, match="needs a value"):
        FactorValue("x", "1", None, "ok")
    with pytest.raises(ValueError, match="unknown quality"):
        FactorValue("x", "1", None, "bad")  # type: ignore[arg-type]


def test_applicability_rules():
    assert Applicability().applies_to("banking")
    assert not Applicability(excluded=frozenset({"banking"})).applies_to("banking")
    only = Applicability(groups=frozenset({"utility"}))
    assert only.applies_to("utility") and not only.applies_to("consumer")
    assert only.describe() == "utility"


def test_no_factor_input_uses_an_ambiguous_field_name():
    for factor in DEFAULT_REGISTRY:
        for item in factor.inputs:
            assert item.field not in AMBIGUOUS_STORED_FIELDS, factor.name


# --------------------------------------------------------------------------- #
# Field semantics
# --------------------------------------------------------------------------- #


def test_provider_fields_with_different_meanings_get_different_names():
    stored = {"net_profit": 1.0, "revenue": 2.0, "roe": 0.1, "missing_fields": ["x"]}
    assert factor_fields("baostock", stored) == {
        "net_profit_total": 1.0,
        "revenue_main": 2.0,
        "roe": 0.1,
    }
    assert factor_fields("ifind", stored) == {
        "net_profit_parent": 1.0,
        "revenue_total": 2.0,
        "roe": 0.1,
    }
    with pytest.raises(ValueError, match="unknown provider"):
        factor_fields("tushare", stored)


def test_ifind_source_values_become_per_share_fields():
    stored = {
        "roe": 0.1,
        "source_values": {"ths_eps_basic_stock": 2.5, "ths_nav_ps_stock": "10"},
    }
    fields = factor_fields("ifind", stored)
    assert fields["eps_basic_fy"] == 2.5
    assert fields["bvps_fy"] == 10.0
    assert "eps_basic_fy" not in factor_fields("baostock", stored)


def test_ambiguous_names_cannot_be_read_from_a_period():
    period = annual(2025, net_profit_total=1.0)
    with pytest.raises(KeyError, match="ambiguous"):
        period.value("net_profit")
    with pytest.raises(KeyError, match="ambiguous"):
        period.value("revenue")


def test_growth_basis_is_provider_specific():
    periods = [
        annual(2022, net_profit_total=100.0, net_profit_parent=80.0),
        annual(2025, net_profit_total=133.1, net_profit_parent=80.0),
    ]
    target = SnapshotView.build(ticker="1.SH", as_of=AS_OF, periods=periods)
    observation = SnapshotView.build(
        ticker="1.SH", as_of=AS_OF, policy=OBSERVATION, periods=periods
    )
    pool_value = _compute(target, "net_profit_growth_3y")
    watch_value = _compute(observation, "net_profit_growth_3y")
    assert pool_value.value == 0.1 and pool_value.inputs_ref["basis"] == "net_profit_total"
    assert watch_value.value == 0.0 and watch_value.inputs_ref["basis"] == "net_profit_parent"


# --------------------------------------------------------------------------- #
# baostock fixture (600519, 601398)
# --------------------------------------------------------------------------- #


def test_moutai_fundamentals_from_latest_annual_report():
    view = baostock_view("600519", AS_OF)
    assert view.industry_group == "consumer"
    assert view.latest_annual().report_period == date(2025, 12, 31)
    assert view.latest_report().report_period == date(2026, 6, 30)

    roe = _compute(view, "roe")
    assert (roe.value, roe.quality) == (0.34462, "ok")
    assert roe.inputs_ref["financial"][0]["available_from"] == "2026-04-17"
    assert _compute(view, "gross_margin").value == 0.911796
    assert _compute(view, "debt_ratio").value == 0.164154
    assert _compute(view, "eps_ttm").value == 65.142936  # semi-annual report, TTM

    stability = _compute(view, "roe_stability")
    assert stability.quality == "ok"
    assert stability.inputs_ref["years"] == [2022, 2025]

    growth = _compute(view, "net_profit_growth_3y")
    assert growth.value == annualized_growth(85310324833.67, 65375136766.75, 3)
    assert growth.quality == "ok"

    revenue = _compute(view, "revenue_growth_3y")
    assert revenue.value == annualized_growth(172054171890.91, 123772332348.71, 3)
    assert revenue.quality == "approx"
    assert "main_business_revenue" in revenue.flags


def test_moutai_market_factors():
    view = baostock_view("600519", AS_OF)
    assert _compute(view, "pe_ttm").value == 19.320898
    assert _compute(view, "pb_mrq").value == 6.26211
    assert _compute(view, "ps_ttm").value == 9.082149
    market_cap = _compute(view, "market_cap")
    assert market_cap.value == 1258.62 * 1250081601.0
    assert market_cap.inputs_ref["total_shares"]["report_period"] == "2026-06-30"
    assert _compute(view, "is_st").value == 0.0
    assert _compute(view, "trade_status").value == 1.0
    turnover = _compute(view, "turnover_20d")
    assert turnover.quality == "ok" and turnover.inputs_ref["sessions"] == 20
    assert _compute(view, "report_overdue").value == 0.0

    window = trim_to_window(
        ((bar.trade_date, bar) for bar in fixture_backward_bars("600519")), AS_OF
    )
    expected = return_stats((bar.trade_date, bar.close) for bar in window)
    assert (
        tuple(
            _compute(view, name).value for name in ("return_1y", "volatility_1y", "max_drawdown_1y")
        )
        == expected
    )
    assert _compute(view, "return_1y").inputs_ref["series"] == "backward"
    # iFinD-only factors are missing on the target-pool path.
    assert _compute(view, "pe_fy").flags == ("unsupported_provider", "industry_fallback")


def test_point_in_time_pubdate_gating():
    before = baostock_view("600519", date(2026, 4, 16))
    after = baostock_view("600519", date(2026, 4, 17))
    assert before.latest_annual().report_period == date(2024, 12, 31)
    assert after.latest_annual().report_period == date(2025, 12, 31)
    assert _compute(before, "roe").value == 0.384283
    assert _compute(after, "roe").value == 0.34462
    assert all(bar.trade_date <= date(2026, 4, 16) for bar in before.raw_bars)
    assert all(bar.trade_date <= date(2026, 4, 16) for bar in before.adjusted_bars)
    assert all(p.available_from <= date(2026, 4, 16) for p in before.periods)


def test_baostock_debt_ratio_scaled_by_hundredth_is_rejected():
    # 600519 FY2024 liabilityToAsset arrives as 0.001904 (vs ~0.17 elsewhere).
    view = baostock_view("600519", date(2026, 4, 16))
    value = _compute(view, "debt_ratio")
    assert value.value is None and value.quality == "missing"
    assert "suspect_provider_value" in value.flags
    assert value.inputs_ref["provider_value"] < MIN_PLAUSIBLE_DEBT_RATIO


def test_bank_applicability():
    view = baostock_view("601398", AS_OF)
    assert view.industry_group == "banking"
    for name in ("gross_margin", "debt_ratio"):
        value = _compute(view, name)
        assert (value.value, value.quality) == (None, "not_applicable")
    assert _compute(view, "roe").value == 0.089739
    assert _compute(view, "net_margin").quality == "ok"
    # Annual 主营收入 is present for ICBC: computed, flagged as an approximation.
    assert _compute(view, "revenue_growth_3y").quality == "approx"
    # Applicability can be bypassed for parity checks.
    raw = DEFAULT_REGISTRY.get("debt_ratio").compute(view, enforce_applicability=False)
    assert raw.quality == "missing"  # FY2025 ICBC liabilityToAsset 0.0092 is a defect


def test_bank_without_main_business_revenue_is_not_applicable():
    periods = [annual(2022, revenue_main=None), annual(2025, revenue_main=None)]
    bank = SnapshotView.build(ticker="1.SH", as_of=AS_OF, industry_code="J66", periods=periods)
    value = _compute(bank, "revenue_growth_3y")
    assert value.quality == "not_applicable"
    assert "main_business_revenue_unavailable" in value.flags
    other = SnapshotView.build(ticker="1.SH", as_of=AS_OF, industry_code="C15", periods=periods)
    assert _compute(other, "revenue_growth_3y").quality == "missing"


# --------------------------------------------------------------------------- #
# Synthetic edge cases
# --------------------------------------------------------------------------- #


def test_empty_view_is_missing_everywhere_with_reasons():
    view = SnapshotView.build(ticker="1.SH", as_of=AS_OF, industry_code="C38")
    values = DEFAULT_REGISTRY.compute_all(view)
    for name, value in values.items():
        if name == "report_overdue":
            assert value.value == 0.0
            continue
        assert value.quality == "missing", name
        assert value.flags, name
    # CSRC-only classification: the group is the CSRC fallback (P1-B3)
    assert values["roe"].flags == ("no_annual_report", "industry_fallback")
    assert values["pe_ttm"].flags == ("no_price", "industry_fallback")


def test_negative_valuation_is_missing_with_flag():
    view = SnapshotView.build(
        ticker="1.SH", as_of=AS_OF, raw_bars=synthetic_bars(AS_OF, 30, pe_ttm=-5.0, pb_mrq=-1.0)
    )
    pe = _compute(view, "pe_ttm")
    assert (pe.value, pe.quality, pe.flags) == (None, "missing", ("negative_earnings",))
    assert _compute(view, "pb_mrq").flags == ("negative_book_value",)


def test_stale_price_is_approx():
    bars = synthetic_bars(AS_OF - timedelta(days=30), 30)
    view = SnapshotView.build(ticker="1.SH", as_of=AS_OF, raw_bars=bars)
    value = _compute(view, "pe_ttm")
    assert value.quality == "approx" and "stale_price" in value.flags


def test_short_history_flags_one_year_statistics():
    bars = [
        bar.__class__(trade_date=bar.trade_date, close=10.0 + index * 0.1)
        for index, bar in enumerate(synthetic_bars(AS_OF, 120))
    ]
    view = SnapshotView.build(ticker="1.SH", as_of=AS_OF, adjusted_bars=bars)
    for name in ("return_1y", "volatility_1y", "max_drawdown_1y"):
        value = _compute(view, name)
        assert value.quality == "approx" and value.flags == ("short_history",), name
    assert _compute(view, "return_1y").inputs_ref["sessions"] == 120


def test_turnover_skips_suspended_sessions_and_marks_short_windows():
    bars = synthetic_bars(AS_OF, 10)
    suspended = bars[-1].__class__(
        trade_date=AS_OF + timedelta(days=0), close=10.0, turnover=0.0, trade_status=0
    )
    view = SnapshotView.build(ticker="1.SH", as_of=AS_OF, raw_bars=[*bars[:-1], suspended])
    value = _compute(view, "turnover_20d")
    assert value.quality == "approx" and value.inputs_ref["sessions"] == 9
    assert value.value == pytest.approx(0.01)


def test_report_overdue_flag_is_carried():
    view = baostock_view("600519", AS_OF, overdue=(date(2026, 6, 30),))
    assert _compute(view, "report_overdue").value == 1.0
    roe = _compute(view, "roe")
    assert "report_overdue" in roe.flags
    assert roe.inputs_ref["overdue_periods"] == ["2026-06-30"]
    assert "report_overdue" in _compute(view, "pe_ttm").flags
    assert "report_overdue" not in _compute(view, "return_1y").flags


def test_roe_stability_and_growth_missing_reasons():
    partial = SnapshotView.build(
        ticker="1.SH",
        as_of=AS_OF,
        periods=[annual(2023, roe=0.1), annual(2024, roe=0.1), annual(2025, roe=0.1)],
    )
    assert _compute(partial, "roe_stability").flags == ("incomplete_history",)
    losing = SnapshotView.build(
        ticker="1.SH",
        as_of=AS_OF,
        periods=[annual(y, roe=-0.1, net_profit_total=-1.0) for y in range(2022, 2026)],
    )
    assert _compute(losing, "roe_stability").flags == ("non_positive_mean",)
    growth = _compute(losing, "net_profit_growth_3y")
    assert growth.quality == "missing"
    assert set(growth.flags) == {"negative_latest", "non_positive_base"}


def test_build_drops_unpublished_and_unknown_periods_and_future_bars():
    periods = [
        annual(2024, available=date(2025, 4, 1), roe=0.1),
        annual(2025, available=date(2026, 10, 1), roe=0.2),
        FinancialPeriod(date(2023, 12, 31), "annual", None, "unknown", {"roe": 0.3}),
    ]
    view = SnapshotView.build(
        ticker="1.SH",
        as_of=AS_OF,
        periods=periods,
        raw_bars=synthetic_bars(AS_OF + timedelta(days=5), 10),
    )
    assert [p.report_period.year for p in view.periods] == [2024]
    assert view.latest_raw_bar().trade_date == AS_OF
    assert view.policy == TARGET_POOL
