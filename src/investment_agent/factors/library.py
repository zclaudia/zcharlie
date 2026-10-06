"""First-batch factor definitions (P1-B1); the registry is :mod:`.catalog`.

Conventions (see :mod:`investment_agent.factors.registry` for quality):

* fundamentals come from the **latest annual (Q4) report public on as_of**
  unless stated otherwise - baostock quarterly ratios are period-to-date
  (Q1/H1/Q3 ROE is not annualized), and iFinD research uses fiscal years;
* "3y" growth compares the latest annual report with the annual report three
  fiscal years earlier (v1.3 rules: missing for a negative latest value or a
  non-positive base);
* ``1y`` market statistics use the trailing 365 calendar days ending on
  ``as_of`` of the return series (baostock backward-adjusted; iFinD research
  history), flagged ``short_history`` below 200 sessions;
* daily valuation (baostock ``pe_ttm`` / ``pb_mrq`` / ``ps_ttm``) and fiscal-
  year multiples (iFinD ``pe_fy`` / ``pb_fy`` / ``ps_fy`` = latest close ÷ FY
  per-share value) are different definitions and therefore different factors.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from investment_agent.factors.industry_groups import FINANCIAL_GROUPS
from investment_agent.factors.metrics import (
    MIN_ONE_YEAR_SESSIONS,
    ONE_YEAR_WINDOW_DAYS,
    annualized_growth,
    positive_multiple,
    return_stats,
    roe_stability,
    trim_to_window,
)
from investment_agent.factors.registry import (
    FLAG_NEGATIVE_BOOK_VALUE,
    FLAG_NEGATIVE_EARNINGS,
    FLAG_NON_POSITIVE_REVENUE,
    FLAG_REPORT_OVERDUE,
    FLAG_SHORT_HISTORY,
    Applicability,
    Factor,
    FactorInput,
    Partial,
    Quality,
)
from investment_agent.factors.view import Bar, FinancialPeriod, SnapshotView

FLAG_NO_ANNUAL_REPORT = "no_annual_report"
FLAG_NO_REPORT = "no_report"
FLAG_NO_PRICE = "no_price"
FLAG_STALE_PRICE = "stale_price"
FLAG_INCOMPLETE_HISTORY = "incomplete_history"
FLAG_NON_POSITIVE_MEAN = "non_positive_mean"
FLAG_NEGATIVE_LATEST = "negative_latest"
FLAG_NON_POSITIVE_BASE = "non_positive_base"
FLAG_MAIN_BUSINESS_REVENUE = "main_business_revenue"
FLAG_MAIN_BUSINESS_REVENUE_UNAVAILABLE = "main_business_revenue_unavailable"
FLAG_SUSPECT_PROVIDER_VALUE = "suspect_provider_value"
FLAG_NO_SHARES = "no_total_shares"

NOT_FINANCIAL = Applicability(excluded=FINANCIAL_GROUPS)
# A latest bar older than this (calendar days) before as_of is flagged stale;
# long enough to span the Spring Festival / National Day closures.
STALE_PRICE_DAYS = 14
TURNOVER_WINDOW_SESSIONS = 20
# baostock ``liabilityToAsset`` occasionally arrives scaled by 1/100 (seen on
# FY2024/FY2025 annual rows, e.g. 600519 FY2024 0.0019 vs ~0.17 in adjacent
# quarters). A debt ratio below this is treated as a provider defect.
MIN_PLAUSIBLE_DEBT_RATIO = 0.02

_FIN = "financial_snapshot.fields"


def _both(
    field: str, *, baostock: str | None = None, ifind: str | None = None
) -> tuple[FactorInput, ...]:
    return (
        FactorInput("baostock", baostock or field, _FIN),
        FactorInput("ifind", ifind or field, _FIN),
    )


def _ref(*periods: FinancialPeriod | None, **extra: Any) -> dict[str, Any]:
    ref: dict[str, Any] = {"financial": [period.describe() for period in periods if period]}
    ref.update(extra)
    return ref


def _bar_ref(bar: Bar) -> dict[str, Any]:
    return {"trade_date": bar.trade_date.isoformat(), "ref": bar.ref}


# --------------------------------------------------------------------------- #
# Fundamentals (latest annual report)
# --------------------------------------------------------------------------- #


def _annual_value(field: str) -> Callable[[SnapshotView], Partial]:
    def body(view: SnapshotView) -> Partial:
        period = view.latest_annual()
        if period is None:
            return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
        return Partial(period.value(field), inputs_ref=_ref(period, field=field))

    return body


def _debt_ratio(view: SnapshotView) -> Partial:
    period = view.latest_annual()
    if period is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    value = period.value("debt_ratio")
    ref = _ref(period, field="debt_ratio")
    if view.provider == "baostock" and value is not None and value < MIN_PLAUSIBLE_DEBT_RATIO:
        return Partial(
            None, flags=(FLAG_SUSPECT_PROVIDER_VALUE,), inputs_ref={**ref, "provider_value": value}
        )
    return Partial(value, inputs_ref=ref)


def _roe_stability(view: SnapshotView) -> Partial:
    latest = view.latest_annual()
    if latest is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    year = latest.report_period.year
    periods = [view.annual(y) for y in range(year - 3, year + 1)]
    values = [None if period is None else period.value("roe") for period in periods]
    value = roe_stability(values)
    flags: tuple[str, ...] = ()
    if value is None:
        flags = (
            (FLAG_INCOMPLETE_HISTORY,)
            if any(v is None for v in values)
            else (FLAG_NON_POSITIVE_MEAN,)
        )
    return Partial(
        value,
        flags=flags,
        inputs_ref=_ref(*periods, field="roe", years=[year - 3, year]),
    )


def _growth_3y(
    basis_by_provider: dict[str, str],
) -> Callable[[SnapshotView], tuple[Partial, float | None, float | None]]:
    def body(view: SnapshotView) -> tuple[Partial, float | None, float | None]:
        basis = basis_by_provider[view.provider]
        latest = view.latest_annual()
        if latest is None:
            return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,)), None, None
        base = view.annual(latest.report_period.year - 3)
        latest_value = latest.value(basis)
        base_value = None if base is None else base.value(basis)
        value = annualized_growth(latest_value, base_value, 3)
        flags: list[str] = []
        if value is None:
            if latest_value is None or base_value is None:
                flags.append(FLAG_INCOMPLETE_HISTORY)
            if latest_value is not None and latest_value < 0:
                flags.append(FLAG_NEGATIVE_LATEST)
            if base_value is not None and base_value <= 0:
                flags.append(FLAG_NON_POSITIVE_BASE)
        ref = _ref(latest, base, basis=basis)
        return Partial(value, flags=tuple(flags), inputs_ref=ref), latest_value, base_value

    return body


_NET_PROFIT_GROWTH = _growth_3y({"baostock": "net_profit_total", "ifind": "net_profit_parent"})
_REVENUE_GROWTH = _growth_3y({"baostock": "revenue_main", "ifind": "revenue_total"})


def _net_profit_growth_3y(view: SnapshotView) -> Partial:
    return _NET_PROFIT_GROWTH(view)[0]


def _revenue_growth_3y(view: SnapshotView) -> Partial:
    partial, latest_value, base_value = _REVENUE_GROWTH(view)
    if view.provider != "baostock":
        return partial
    if view.industry_group in FINANCIAL_GROUPS and (latest_value is None or base_value is None):
        # Financials often have no 主营收入 (MBRevenue); not a data gap.
        return Partial(
            None,
            quality="not_applicable",
            flags=(FLAG_MAIN_BUSINESS_REVENUE_UNAVAILABLE,),
            inputs_ref=partial.inputs_ref,
        )
    if partial.value is None:
        return partial
    # Main-business revenue approximates total operating revenue.
    return Partial(
        partial.value,
        quality="approx",
        flags=(*partial.flags, FLAG_MAIN_BUSINESS_REVENUE),
        inputs_ref=partial.inputs_ref,
    )


def _eps_ttm(view: SnapshotView) -> Partial:
    report = view.latest_report()
    if report is None:
        return Partial(None, flags=(FLAG_NO_REPORT,))
    return Partial(report.value("eps_ttm"), inputs_ref=_ref(report, field="eps_ttm"))


def _free_cash_flow(view: SnapshotView) -> Partial:
    period = view.latest_annual()
    if period is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    operating = period.value("operating_cash_flow")
    capex = period.value("capital_expenditure")
    if operating is not None and capex is not None:
        value: float | None = operating - capex
    else:
        value = period.value("free_cash_flow")
    formula = "operating_cash_flow - capital_expenditure"
    return Partial(value, inputs_ref=_ref(period, formula=formula))


# --------------------------------------------------------------------------- #
# Prices and valuation
# --------------------------------------------------------------------------- #


def latest_bar(view: SnapshotView) -> tuple[Bar | None, tuple[str, ...]]:
    """Latest raw bar on or before ``as_of`` plus ``stale_price`` when it is old."""

    bar = view.latest_raw_bar()
    if bar is None:
        return None, (FLAG_NO_PRICE,)
    if bar.trade_date < view.as_of - timedelta(days=STALE_PRICE_DAYS):
        return bar, (FLAG_STALE_PRICE,)
    return bar, ()


def _daily_valuation(column: str, non_positive_flag: str) -> Callable[[SnapshotView], Partial]:
    def body(view: SnapshotView) -> Partial:
        bar, flags = latest_bar(view)
        if bar is None:
            return Partial(None, flags=flags)
        raw = getattr(bar, column)
        ref = {"market": _bar_ref(bar), "column": column}
        if raw is None:
            return Partial(None, flags=flags, inputs_ref=ref)
        if raw <= 0:
            return Partial(
                None, flags=(non_positive_flag, *flags), inputs_ref={**ref, "provider_value": raw}
            )
        return Partial(raw, quality="approx" if flags else None, flags=flags, inputs_ref=ref)

    return body


def _fy_multiple(per_share_field: str, non_positive_flag: str) -> Callable[[SnapshotView], Partial]:
    def body(view: SnapshotView) -> Partial:
        bar, flags = latest_bar(view)
        period = view.latest_annual()
        if bar is None or period is None:
            return Partial(None, flags=flags or (FLAG_NO_ANNUAL_REPORT,))
        per_share = period.value(per_share_field)
        ref = _ref(period, market=_bar_ref(bar), field=per_share_field)
        multiple = positive_multiple(bar.close, per_share)
        if per_share is not None and per_share <= 0:
            return Partial(None, flags=(non_positive_flag, *flags), inputs_ref=ref)
        value = None if multiple is None else round(multiple, 4)
        quality: Quality | None = "approx" if flags and value is not None else None
        return Partial(value, quality=quality, flags=flags, inputs_ref=ref)

    return body


def _market_cap(view: SnapshotView) -> Partial:
    bar, flags = latest_bar(view)
    if bar is None:
        return Partial(None, flags=flags)
    shares = view.total_shares()
    if shares is None:
        return Partial(None, flags=(FLAG_NO_SHARES, *flags), inputs_ref={"market": _bar_ref(bar)})
    total_shares, shares_ref = shares
    return Partial(
        bar.close * total_shares,
        quality="approx" if flags else None,
        flags=flags,
        inputs_ref={"market": _bar_ref(bar), "total_shares": shares_ref},
    )


def _one_year_stats(view: SnapshotView) -> tuple[tuple[float | None, ...], int, dict[str, Any]]:
    window = trim_to_window(((bar.trade_date, bar) for bar in view.adjusted_bars), view.as_of)
    stats = return_stats((bar.trade_date, bar.close) for bar in window)
    sessions = sum(1 for bar in window if bar.close > 0)
    ref = {
        "series": view.adjusted_kind,
        "sessions": sessions,
        "window_days": ONE_YEAR_WINDOW_DAYS,
        "first": window[0].trade_date.isoformat() if window else None,
        "last": window[-1].trade_date.isoformat() if window else None,
    }
    return stats, sessions, ref


def _one_year_stat(index: int) -> Callable[[SnapshotView], Partial]:
    def body(view: SnapshotView) -> Partial:
        stats, sessions, ref = _one_year_stats(view)
        value = stats[index]
        if value is None:
            flag = FLAG_SHORT_HISTORY if sessions else FLAG_NO_PRICE
            return Partial(None, flags=(flag,), inputs_ref=ref)
        if sessions < MIN_ONE_YEAR_SESSIONS:
            return Partial(value, quality="approx", flags=(FLAG_SHORT_HISTORY,), inputs_ref=ref)
        return Partial(value, inputs_ref=ref)

    return body


def _turnover_20d(view: SnapshotView) -> Partial:
    sessions = [bar for bar in view.raw_bars if bar.turnover is not None and bar.trade_status != 0][
        -TURNOVER_WINDOW_SESSIONS:
    ]
    if not sessions:
        return Partial(None, flags=(FLAG_NO_PRICE,))
    value = sum(bar.turnover or 0.0 for bar in sessions) / len(sessions)
    ref = {
        "sessions": len(sessions),
        "first": sessions[0].trade_date.isoformat(),
        "last": sessions[-1].trade_date.isoformat(),
    }
    if len(sessions) < TURNOVER_WINDOW_SESSIONS:
        return Partial(value, quality="approx", flags=(FLAG_SHORT_HISTORY,), inputs_ref=ref)
    return Partial(value, inputs_ref=ref)


def _bar_status(column: str) -> Callable[[SnapshotView], Partial]:
    def body(view: SnapshotView) -> Partial:
        bar, flags = latest_bar(view)
        if bar is None:
            return Partial(None, flags=flags)
        raw = getattr(bar, column)
        ref = {"market": _bar_ref(bar), "column": column}
        if raw is None:
            return Partial(None, flags=flags, inputs_ref=ref)
        return Partial(float(raw), quality="approx" if flags else None, flags=flags, inputs_ref=ref)

    return body


def _report_overdue(view: SnapshotView) -> Partial:
    overdue = bool(view.overdue_periods)
    return Partial(
        1.0 if overdue else 0.0,
        flags=(FLAG_REPORT_OVERDUE,) if overdue else (),
        inputs_ref={"source": "report_overdue.open_as_of"},
    )


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #

_KDATA = "market_snapshot"
_BACKWARD = "market_snapshot(adjust=backward).close"

CORE_FACTORS: tuple[Factor, ...] = (
    Factor(
        "roe",
        "1",
        "fraction",
        "Return on equity of the latest annual report (baostock roeAvg on average equity; "
        "iFinD FY ROE).",
        _both("roe"),
        _annual_value("roe"),
        carries_overdue_flag=True,
    ),
    Factor(
        "roe_stability",
        "1",
        "ratio [0,1]",
        "clamp(1 - pstdev/mean) of the four latest annual ROEs; all four required and "
        "mean > 0 (v1.3).",
        _both("roe"),
        _roe_stability,
        carries_overdue_flag=True,
    ),
    Factor(
        "gross_margin",
        "1",
        "fraction",
        "Gross margin of the latest annual report.",
        _both("gross_margin"),
        _annual_value("gross_margin"),
        applicability=NOT_FINANCIAL,
        carries_overdue_flag=True,
    ),
    Factor(
        "net_margin",
        "1",
        "fraction",
        "Net margin of the latest annual report.",
        _both("net_margin"),
        _annual_value("net_margin"),
        carries_overdue_flag=True,
    ),
    Factor(
        "debt_ratio",
        "1",
        "fraction",
        "Liabilities / assets of the latest annual report; baostock values below "
        f"{MIN_PLAUSIBLE_DEBT_RATIO} are treated as provider defects (missing).",
        _both("debt_ratio"),
        _debt_ratio,
        applicability=NOT_FINANCIAL,
        carries_overdue_flag=True,
    ),
    Factor(
        "net_profit_growth_3y",
        "1",
        "fraction / year",
        "3-year CAGR of annual net profit: baostock net_profit_total (incl. minority "
        "interests), iFinD net_profit_parent; missing for a negative latest value or a "
        "non-positive base (v1.3).",
        _both("net_profit", baostock="net_profit_total", ifind="net_profit_parent"),
        _net_profit_growth_3y,
        carries_overdue_flag=True,
    ),
    Factor(
        "revenue_growth_3y",
        "1",
        "fraction / year",
        "3-year CAGR of annual revenue: iFinD revenue_total; baostock revenue_main "
        "(main-business revenue, approx; not_applicable for financials without it).",
        _both("revenue", baostock="revenue_main", ifind="revenue_total"),
        _revenue_growth_3y,
        carries_overdue_flag=True,
    ),
    Factor(
        "free_cash_flow",
        "1",
        "CNY",
        "Operating cash flow - cash paid for long-term assets, latest annual report "
        "(iFinD only; baostock has no absolute cash flows).",
        (
            FactorInput("ifind", "operating_cash_flow", _FIN),
            FactorInput("ifind", "capital_expenditure", _FIN),
        ),
        _free_cash_flow,
        carries_overdue_flag=True,
    ),
    Factor(
        "eps_ttm",
        "1",
        "CNY/share",
        "Trailing-twelve-month EPS of the latest public report.",
        (FactorInput("baostock", "eps_ttm", _FIN),),
        _eps_ttm,
        carries_overdue_flag=True,
    ),
    Factor(
        "pe_ttm",
        "1",
        "times",
        "baostock daily peTTM on the latest session <= as_of; missing (negative_earnings) "
        "when <= 0.",
        (FactorInput("baostock", "pe_ttm", f"{_KDATA}.pe_ttm"),),
        _daily_valuation("pe_ttm", FLAG_NEGATIVE_EARNINGS),
        carries_overdue_flag=True,
    ),
    Factor(
        "pb_mrq",
        "1",
        "times",
        "baostock daily pbMRQ on the latest session <= as_of; missing when <= 0.",
        (FactorInput("baostock", "pb_mrq", f"{_KDATA}.pb_mrq"),),
        _daily_valuation("pb_mrq", FLAG_NEGATIVE_BOOK_VALUE),
        carries_overdue_flag=True,
    ),
    Factor(
        "ps_ttm",
        "1",
        "times",
        "baostock daily psTTM on the latest session <= as_of; missing when <= 0.",
        (FactorInput("baostock", "ps_ttm", f"{_KDATA}.ps_ttm"),),
        _daily_valuation("ps_ttm", FLAG_NON_POSITIVE_REVENUE),
        carries_overdue_flag=True,
    ),
    Factor(
        "pe_fy",
        "1",
        "times",
        "Latest close / basic EPS of the latest fiscal year (iFinD research PE); "
        "missing when EPS <= 0.",
        (FactorInput("ifind", "eps_basic_fy", _FIN),),
        _fy_multiple("eps_basic_fy", FLAG_NEGATIVE_EARNINGS),
        carries_overdue_flag=True,
    ),
    Factor(
        "pb_fy",
        "1",
        "times",
        "Latest close / net assets per share of the latest fiscal year (iFinD research PB).",
        (FactorInput("ifind", "bvps_fy", _FIN),),
        _fy_multiple("bvps_fy", FLAG_NEGATIVE_BOOK_VALUE),
        carries_overdue_flag=True,
    ),
    Factor(
        "ps_fy",
        "1",
        "times",
        "Latest close / revenue per share of the latest fiscal year (iFinD research PS).",
        (FactorInput("ifind", "revenue_ps_fy", _FIN),),
        _fy_multiple("revenue_ps_fy", FLAG_NON_POSITIVE_REVENUE),
        carries_overdue_flag=True,
    ),
    Factor(
        "market_cap",
        "1",
        "CNY",
        "Raw close of the latest session × total shares usable on as_of (baostock: "
        "latest public report's totalShare; iFinD: stock-info total shares).",
        (
            FactorInput("baostock", "close", f"{_KDATA}(adjust=none).close"),
            FactorInput("baostock", "total_shares", _FIN),
            FactorInput("ifind", "close", f"{_KDATA}(adjust=none).close"),
            FactorInput("ifind", "total_shares", f"{_KDATA}.total_shares"),
        ),
        _market_cap,
        carries_overdue_flag=True,
    ),
    Factor(
        "return_1y",
        "1",
        "fraction",
        "Last / first close - 1 over the trailing 365 days (adjusted series).",
        (FactorInput("baostock", "close", _BACKWARD), FactorInput("ifind", "close", _BACKWARD)),
        _one_year_stat(0),
    ),
    Factor(
        "volatility_1y",
        "1",
        "fraction (annualized)",
        "Population stdev of daily returns × sqrt(252) over the trailing 365 days.",
        (FactorInput("baostock", "close", _BACKWARD), FactorInput("ifind", "close", _BACKWARD)),
        _one_year_stat(1),
    ),
    Factor(
        "max_drawdown_1y",
        "1",
        "fraction",
        "Largest peak-to-trough decline over the trailing 365 days (positive).",
        (FactorInput("baostock", "close", _BACKWARD), FactorInput("ifind", "close", _BACKWARD)),
        _one_year_stat(2),
    ),
    Factor(
        "turnover_20d",
        "1",
        "fraction of float / day",
        "Mean daily turnover over the last 20 trading (non-suspended) sessions.",
        (FactorInput("baostock", "turnover", f"{_KDATA}.turnover"),),
        _turnover_20d,
    ),
    Factor(
        "is_st",
        "1",
        "bool (1/0)",
        "ST / *ST status on the latest session.",
        (FactorInput("baostock", "is_st", f"{_KDATA}.is_st"),),
        _bar_status("is_st"),
    ),
    Factor(
        "trade_status",
        "1",
        "code (1 trading, 0 suspended)",
        "Trading status on the latest session.",
        (FactorInput("baostock", "trade_status", f"{_KDATA}.trade_status"),),
        _bar_status("trade_status"),
    ),
    Factor(
        "report_overdue",
        "1",
        "bool (1/0)",
        "1 when a periodic report is past its statutory deadline and still unpublished on as_of.",
        (
            FactorInput("baostock", "report_overdue", "report_overdue"),
            FactorInput("ifind", "report_overdue", "report_overdue"),
        ),
        _report_overdue,
    ),
)
