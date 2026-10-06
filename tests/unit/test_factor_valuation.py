"""Valuation percentile factors (P1-B2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.factors.valuation import (
    MIN_VALID_SESSIONS,
    inclusive_percentile,
    years_before,
)
from investment_agent.factors.view import Bar, SnapshotView
from tests.unit._factor_support import baostock_view, fixture_raw_bars

AS_OF = date(2026, 9, 30)


def _series(values: list[float | None], *, end: date = AS_OF, pb: bool = False) -> list[Bar]:
    """One bar per calendar day ending on ``end`` with the given PE (or PB) values."""

    count = len(values)
    return [
        Bar(
            trade_date=end - timedelta(days=count - 1 - index),
            close=10.0,
            pe_ttm=None if pb else value,
            pb_mrq=value if pb else None,
        )
        for index, value in enumerate(values)
    ]


def _view(bars: list[Bar], as_of: date = AS_OF, **kwargs) -> SnapshotView:
    return SnapshotView.build(ticker="1.SH", as_of=as_of, raw_bars=bars, **kwargs)


def _pe(view: SnapshotView) -> FactorValue:
    return DEFAULT_REGISTRY.get("pe_percentile_5y").compute(view)


def test_inclusive_percentile_definition():
    assert inclusive_percentile([1.0, 2.0, 3.0, 4.0], 4.0) == 1.0
    assert inclusive_percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 0.25
    assert inclusive_percentile([1.0, 2.0, 2.0, 4.0], 2.0) == 0.75  # ties count
    assert inclusive_percentile([1.0, 2.0, 3.0], 2.5) == pytest.approx(0.666667)
    with pytest.raises(ValueError):
        inclusive_percentile([], 1.0)


def test_years_before_handles_leap_day():
    assert years_before(date(2026, 9, 30), 5) == date(2021, 9, 30)
    assert years_before(date(2024, 2, 29), 5) == date(2019, 2, 28)


def test_full_five_year_window_is_ok():
    days = (AS_OF - years_before(AS_OF, 5)).days  # sessions in (start, as_of]
    values = [float(index % 100 + 1) for index in range(days + 30)]  # 30 days before window
    values[-1] = 50.0
    value = _pe(_view(_series(values)))
    window = values[-days:]
    assert value.quality == "ok" and value.flags == ()
    assert value.value == round(sum(1 for v in window if v <= 50.0) / len(window), 6)
    assert value.inputs_ref["valid_sessions"] == days
    assert value.inputs_ref["window"] == ["2021-09-30", "2026-09-30"]


def test_loss_days_are_excluded_from_the_distribution():
    values: list[float | None] = [float(v) for v in range(1, 401)]
    values[:100] = [-5.0] * 100  # loss-making stretch
    values[100:110] = [None] * 10
    value = _pe(_view(_series(values)))
    assert value.inputs_ref["valid_sessions"] == 290
    assert value.inputs_ref["excluded_non_positive"] == 100
    assert value.value == 1.0  # the latest PE (400) is the highest positive value


def test_negative_current_pe_has_no_value():
    values: list[float | None] = [10.0] * 400
    values[-1] = -3.0
    value = _pe(_view(_series(values)))
    assert (value.value, value.quality) == (None, "missing")
    assert value.flags == ("negative_earnings",)


def test_negative_current_pb_flags_book_value():
    values: list[float | None] = [1.0] * 400
    values[-1] = 0.0
    view = _view(_series(values, pb=True))
    value = DEFAULT_REGISTRY.get("pb_percentile_5y").compute(view)
    assert value.flags == ("negative_book_value",) and value.value is None


def test_under_one_year_is_missing():
    value = _pe(_view(_series([10.0] * (MIN_VALID_SESSIONS - 1))))
    assert (value.value, value.quality) == (None, "missing")
    assert value.flags == ("short_history",)


def test_short_history_is_approx_with_years_covered():
    value = _pe(_view(_series([float(v) for v in range(1, 731)])))  # two years
    assert value.quality == "approx"
    assert value.flags == ("short_history",)
    assert value.value == 1.0
    assert value.inputs_ref["years_covered"] == pytest.approx(2.0, abs=0.01)


def test_point_in_time_ignores_later_sessions():
    values = [float(v) for v in range(1, 801)]
    full = _series(values)
    as_of = AS_OF - timedelta(days=100)
    later_spike = [*full, Bar(trade_date=AS_OF + timedelta(days=1), close=1.0, pe_ttm=0.01)]
    value = _pe(_view(later_spike, as_of=as_of))
    truncated = _pe(_view([bar for bar in full if bar.trade_date <= as_of], as_of=as_of))
    assert value == truncated
    assert value.value == 1.0
    assert value.inputs_ref["current_date"] == as_of.isoformat()


def test_report_overdue_is_carried():
    view = _view(_series([10.0] * 400), overdue_periods=(date(2026, 6, 30),))
    value = _pe(view)
    assert value.quality == "approx"  # short history (400 days) ...
    assert "report_overdue" in value.flags  # ... and the overdue flag


def test_missing_price_and_current_value():
    assert _pe(_view([])).flags == ("no_price",)
    bars = _series([10.0] * 400)
    bars[-1] = replace(bars[-1], pe_ttm=None)
    assert _pe(_view(bars)).quality == "missing"


def test_moutai_fixture_percentiles():
    view = baostock_view("600519", AS_OF)
    start = years_before(AS_OF, 5)
    for name, column in (("pe_percentile_5y", "pe_ttm"), ("pb_percentile_5y", "pb_mrq")):
        window = [
            getattr(bar, column)
            for bar in fixture_raw_bars("600519")
            if start < bar.trade_date <= AS_OF and (getattr(bar, column) or 0) > 0
        ]
        current = getattr(view.latest_raw_bar(), column)
        value = DEFAULT_REGISTRY.get(name).compute(view)
        assert value.quality == "ok", (name, value.flags)
        assert value.value == round(sum(1 for v in window if v <= current) / len(window), 6)
        assert value.inputs_ref["valid_sessions"] == len(window) > 1100
    # PE TTM ~19.3 sits near the bottom of Moutai's 2021-2026 range.
    assert DEFAULT_REGISTRY.get("pe_percentile_5y").compute(view).value < 0.1


def test_bank_percentiles_apply():
    value = DEFAULT_REGISTRY.get("pb_percentile_5y").compute(baostock_view("601398", AS_OF))
    assert value.quality == "ok" and value.value is not None
