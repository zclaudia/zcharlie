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

from investment_agent.factors.field_sources import default_field_sources
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
from investment_agent.factors.snapshot_model import (
    Bar,
    Dividend,
    FinancialPeriod,
    SnapshotView,
)

FLAG_DIVIDENDS_NOT_COLLECTED = "dividends_not_collected"
FLAG_DIVIDENDS_STALE = "dividends_stale"
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
FLAG_PROVIDER_FALLBACK = "provider_fallback"
FLAG_NO_SHARES = "no_total_shares"
FLAG_NON_POSITIVE_DENOMINATOR = "non_positive_denominator"
FLAG_NON_POSITIVE_NET_INCOME = "non_positive_net_income"
FLAG_NON_POSITIVE_FINANCE_EXPENSE = "non_positive_finance_expense"

NOT_FINANCIAL = Applicability(excluded=FINANCIAL_GROUPS)
# Banks and insurers: cash-profit and interest-burden ratios are not the same
# reading. Brokers stay in (they are not deposit- or float-funded in the same way).
NOT_BANK_OR_INSURER = Applicability(excluded=frozenset({"banking", "insurance"}))
# A latest bar older than this (calendar days) before as_of is flagged stale;
# long enough to span the Spring Festival / National Day closures.
STALE_PRICE_DAYS = 14
TURNOVER_WINDOW_SESSIONS = 20
# Trailing dividend window: ex-dates in (as_of - 365 days, as_of].
DIVIDEND_WINDOW_DAYS = 365
# The latest collection of the as_of year older than this (calendar days) is
# flagged stale (approx): the weekly job has missed two runs.
DIVIDEND_STALE_DAYS = 15
# baostock ``liabilityToAsset`` occasionally arrives scaled by 1/100 (seen on
# FY2024/FY2025 annual rows, e.g. 600519 FY2024 0.0019 vs ~0.17 in adjacent
# quarters). The rule lives in the field source registry (P1-A12,
# ``debt_ratio.sanity.baostock.min``); the view rejects such values and falls
# through to the fallback source (Eastmoney ``stock_zcfz_em``).
_DEBT_RATIO_SANITY = default_field_sources().require("debt_ratio").sanity["baostock"]
assert _DEBT_RATIO_SANITY.minimum is not None
MIN_PLAUSIBLE_DEBT_RATIO: float = _DEBT_RATIO_SANITY.minimum

_FIN = "financial_snapshot.fields"


def _both(
    field: str, *, baostock: str | None = None, ifind: str | None = None
) -> tuple[FactorInput, ...]:
    return (
        FactorInput("baostock", baostock or field, _FIN),
        FactorInput("ifind", ifind or field, _FIN),
    )


def _ref(
    *periods: FinancialPeriod | None, source_fields: tuple[str, ...] = (), **extra: Any
) -> dict[str, Any]:
    """Financial periods used (with each field's provider / verification, P1-A12)."""

    names = [
        *(value for key in ("field", "basis") if isinstance(value := extra.get(key), str)),
        *source_fields,
    ]
    ref: dict[str, Any] = {
        "financial": [period.describe(names) for period in periods if period]
    }
    ref.update(extra)
    return ref


def _bar_ref(bar: Bar) -> dict[str, Any]:
    ref: dict[str, Any] = {"trade_date": bar.trade_date.isoformat(), "ref": bar.ref}
    if bar.provider is not None:
        ref["provider"] = bar.provider
    if bar.conflicts:
        # D2: another source's same-day close beyond the registry tolerance.
        ref["verification"] = "conflict"
        ref["conflicts"] = [dict(item) for item in bar.conflicts]
    return ref


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
    # The view applied the registry sanity rule (baostock < MIN_PLAUSIBLE_DEBT_RATIO
    # is a provider defect) and fell through to the fallback source.
    resolution = period.source("debt_ratio")
    rejected = resolution.rejected if resolution is not None else ()
    if rejected:
        ref = {**ref, "provider_value": rejected[0].value}
        if value is None:
            return Partial(None, flags=(FLAG_SUSPECT_PROVIDER_VALUE,), inputs_ref=ref)
        return Partial(value, flags=(FLAG_PROVIDER_FALLBACK,), inputs_ref=ref)
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
        used: tuple[str, ...] = ("operating_cash_flow", "capital_expenditure")
    else:
        value = period.value("free_cash_flow")
        used = ("free_cash_flow",)
    formula = "operating_cash_flow - capital_expenditure"
    ref = _ref(
        period,
        formula=formula,
        source_fields=("operating_cash_flow", "capital_expenditure", "free_cash_flow"),
    )
    approx = any(
        (resolution := period.source(name)) is not None and resolution.approx for name in used
    )
    if value is not None and approx:
        return Partial(value, quality="approx", inputs_ref=ref)
    return Partial(value, inputs_ref=ref)


def _ocf_to_net_income(view: SnapshotView) -> Partial:
    """Latest annual operating cash flow / net profit.

    Prefers ``net_profit_parent``. ``net_profit_total`` is an explicit approx
    fallback (it includes minority interests), not a silent substitute for a
    missing parent figure when the parent figure is the one we have. A
    non-positive profit does not produce a ratio.
    """

    period = view.latest_annual()
    if period is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    operating = period.value("operating_cash_flow")
    parent = period.value("net_profit_parent")
    total = period.value("net_profit_total")
    if parent is not None:
        profit, basis, approx = parent, "net_profit_parent", False
    elif total is not None:
        profit, basis, approx = total, "net_profit_total", True
    else:
        profit, basis, approx = None, "net_profit_parent", False
    ref = _ref(
        period,
        formula="operating_cash_flow / net_profit",
        basis=basis,
        source_fields=("operating_cash_flow", "net_profit_parent", "net_profit_total"),
    )
    if operating is None or profit is None:
        return Partial(None, flags=(FLAG_INCOMPLETE_HISTORY,), inputs_ref=ref)
    if profit <= 0:
        return Partial(
            None,
            flags=(FLAG_NON_POSITIVE_NET_INCOME,),
            inputs_ref={**ref, "net_profit": profit, "operating_cash_flow": operating},
        )
    ocf_source = period.source("operating_cash_flow")
    if ocf_source is not None and ocf_source.approx:
        approx = True
    return Partial(
        operating / profit,
        quality="approx" if approx else None,
        inputs_ref=ref,
    )


def _share_dilution(view: SnapshotView) -> Partial:
    """Total-share change from the prior annual report to the latest one.

    ``shares_t / shares_t-1 - 1``. This is the change in the reported share
    count, including bonus and capitalisation issues, not a cash-issuance
    measure. One missing count means the change cannot be computed.
    """

    latest = view.latest_annual()
    if latest is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    prior = view.annual(latest.report_period.year - 1)
    latest_shares = latest.value("total_shares")
    prior_shares = None if prior is None else prior.value("total_shares")
    ref = _ref(
        latest,
        prior,
        formula="total_shares_t / total_shares_t-1 - 1",
        field="total_shares",
    )
    if prior is None or latest_shares is None or prior_shares is None:
        return Partial(None, flags=(FLAG_INCOMPLETE_HISTORY,), inputs_ref=ref)
    if latest_shares <= 0 or prior_shares <= 0:
        return Partial(None, flags=(FLAG_NON_POSITIVE_BASE,), inputs_ref=ref)
    return Partial(latest_shares / prior_shares - 1, inputs_ref=ref)


def _interest_coverage(view: SnapshotView) -> Partial:
    """Approximate coverage: (operating profit + finance expense) / finance expense.

    营业利润 is already after 财务费用, so the expense is added back. 财务费用
    nets interest income, interest expense and FX; it is not interest expense
    alone, so a computed value is ``approx``. A non-positive finance expense
    means there is no net finance cost to cover: ``not_applicable``, not a
    made-up large ratio. A missing input stays missing.
    """

    period = view.latest_annual()
    if period is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    operating = period.value("operating_profit")
    finance = period.value("finance_expense")
    ref = _ref(
        period,
        formula="(operating_profit + finance_expense) / finance_expense",
        source_fields=("operating_profit", "finance_expense"),
    )
    ref = {
        **ref,
        "definition": (
            "营业利润加回财务费用，近似息税前利润；分母是财务费用，不是单独的利息支出。"
        ),
    }
    if operating is None or finance is None:
        return Partial(None, flags=(FLAG_INCOMPLETE_HISTORY,), inputs_ref=ref)
    if finance <= 0:
        return Partial(
            None,
            quality="not_applicable",
            flags=(FLAG_NON_POSITIVE_FINANCE_EXPENSE,),
            inputs_ref={**ref, "finance_expense": finance, "operating_profit": operating},
        )
    return Partial((operating + finance) / finance, quality="approx", inputs_ref=ref)


def _roe_dupont(view: SnapshotView) -> Partial:
    """Latest-annual ROE identity decomposition; components live in ``inputs_ref``.

    The value is the product (net profit / equity); margin, turnover, multiplier
    and the residual vs the stored ``roe`` are recorded, never asserted as
    independent facts. Non-positive denominators make the identity meaningless.
    """
    period = view.latest_annual()
    if period is None:
        return Partial(None, flags=(FLAG_NO_ANNUAL_REPORT,))
    profit = period.value("net_profit_parent")
    revenue = period.value("revenue_total")
    assets = period.value("total_assets")
    equity = period.value("total_equity")
    stored_roe = period.value("roe")
    ref = _ref(
        period,
        formula="(net_profit_parent / revenue_total) * (revenue_total / total_assets)"
        " * (total_assets / total_equity)",
        source_fields=("net_profit_parent", "revenue_total", "total_assets", "total_equity", "roe"),
    )
    if profit is None or revenue is None or assets is None or equity is None:
        return Partial(None, inputs_ref=ref)
    if revenue <= 0 or assets <= 0 or equity <= 0:
        return Partial(None, flags=(FLAG_NON_POSITIVE_DENOMINATOR,), inputs_ref=ref)
    margin = profit / revenue
    turnover = revenue / assets
    multiplier = assets / equity
    product = margin * turnover * multiplier
    ref["components"] = {
        "report_period": period.report_period.isoformat(),
        "net_margin": margin,
        "asset_turnover": turnover,
        "equity_multiplier": multiplier,
        "roe_product": product,
        "stored_roe": stored_roe,
        "identity_residual": None if stored_roe is None else product - stored_roe,
    }
    approx = any(
        (resolution := period.source(name)) is not None and resolution.approx
        for name in ("net_profit_parent", "revenue_total", "total_assets", "total_equity")
    )
    if approx:
        return Partial(product, quality="approx", inputs_ref=ref)
    return Partial(product, inputs_ref=ref)


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


def split_adjusted_cash(event: Dividend, later: list[Dividend]) -> float:
    """Cash per share of ``event`` restated to the current share base.

    A later bonus / capitalisation issue (送股 / 转增) multiplies the share
    count, so cash paid per share before it is divided by the product of the
    later events' share multipliers.
    """

    cash = event.cash_per_share or 0.0
    for item in later:
        if item.ex_date > event.ex_date and item.share_multiplier > 0:
            cash /= item.share_multiplier
    return cash


def _dividend_yield_ttm(view: SnapshotView) -> Partial:
    bar, flags = latest_bar(view)
    if bar is None:
        return Partial(None, flags=flags)
    market = {"market": _bar_ref(bar)}
    history = view.dividends
    if history is None:
        return Partial(None, flags=(FLAG_DIVIDENDS_NOT_COLLECTED,), inputs_ref=market)
    window_start = view.as_of - timedelta(days=DIVIDEND_WINDOW_DAYS)
    years = range((window_start + timedelta(days=1)).year, view.as_of.year + 1)
    coverage = history.describe_coverage()
    uncovered = [year for year in years if year not in history.covered_years]
    if uncovered:
        return Partial(
            None,
            flags=(FLAG_DIVIDENDS_NOT_COLLECTED,),
            inputs_ref={**market, "covered_years": coverage, "uncovered_years": uncovered},
        )
    if bar.close <= 0:
        return Partial(None, flags=(FLAG_NO_PRICE, *flags), inputs_ref=market)
    # Bonus issues up to the price date restate earlier cash to the price's share base.
    later = [event for event in history.events if event.ex_date <= bar.trade_date]
    paid = [
        (event, split_adjusted_cash(event, later))
        for event in history.events
        if window_start < event.ex_date <= view.as_of
        and event.cash_per_share is not None
        and event.cash_per_share > 0
    ]
    total = sum(cash for _, cash in paid)
    result_flags = list(flags)
    last_check = history.covered_years.get(view.as_of.year)
    if last_check is not None and last_check < view.as_of - timedelta(days=DIVIDEND_STALE_DAYS):
        result_flags.append(FLAG_DIVIDENDS_STALE)
    ref = {
        **market,
        "dividend_provider": history.provider,
        "dividend_selection_reason": history.selection_reason,
        "window": f"({window_start.isoformat()}, {view.as_of.isoformat()}]",
        "cash_per_share_ttm": round(total, 8),
        "dividends": [
            {
                "ex_date": event.ex_date.isoformat(),
                "cash_per_share": event.cash_per_share,
                "adjusted_cash": round(cash, 8),
                "ref": event.ref,
                "action_ref": event.action_ref,
            }
            for event, cash in paid
        ],
        "covered_years": coverage,
        "share_actions": [
            {
                "ex_date": item.ex_date.isoformat(),
                "multiplier": item.share_multiplier,
                "ref": item.action_ref or item.ref,
            }
            for item in later
            if item.share_multiplier != 1.0
        ],
    }
    quality: Quality | None = "approx" if result_flags else None
    return Partial(
        round(total / bar.close, 8), quality=quality, flags=tuple(result_flags), inputs_ref=ref
    )


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
        f"{MIN_PLAUSIBLE_DEBT_RATIO} are provider defects: the view falls back to "
        "Eastmoney (field source registry), else missing (suspect_provider_value).",
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
        "(target pool: Eastmoney cash-flow statements via the field source registry; "
        "observation: iFinD).",
        (
            FactorInput("baostock", "operating_cash_flow", _FIN),
            FactorInput("baostock", "capital_expenditure", _FIN),
            FactorInput("ifind", "operating_cash_flow", _FIN),
            FactorInput("ifind", "capital_expenditure", _FIN),
        ),
        _free_cash_flow,
        carries_overdue_flag=True,
    ),
    Factor(
        "ocf_to_net_income",
        "1",
        "ratio",
        "Latest annual operating cash flow / net profit. Prefers net_profit_parent; "
        "net_profit_total is an approx fallback (includes minority interests). Missing "
        "when an input is absent or net profit is not positive (non_positive_net_income). "
        "Not applicable to banks and insurers. The screen threshold is an uncalibrated "
        "template parameter, not part of this factor.",
        (
            FactorInput("baostock", "operating_cash_flow", _FIN),
            FactorInput("baostock", "net_profit_parent", _FIN),
            FactorInput("baostock", "net_profit_total", _FIN),
            FactorInput("ifind", "operating_cash_flow", _FIN),
            FactorInput("ifind", "net_profit_parent", _FIN),
            FactorInput("ifind", "net_profit_total", _FIN),
        ),
        _ocf_to_net_income,
        applicability=NOT_BANK_OR_INSURER,
        carries_overdue_flag=True,
    ),
    Factor(
        "share_dilution",
        "1",
        "fraction",
        "Total-share change from the prior annual report to the latest: "
        "shares_t / shares_t-1 - 1, including bonus and capitalisation issues. "
        "Missing when either share count is absent or not positive. Not applicable "
        "to banks and insurers.",
        (
            FactorInput("baostock", "total_shares", _FIN),
            FactorInput("ifind", "total_shares", _FIN),
        ),
        _share_dilution,
        applicability=NOT_BANK_OR_INSURER,
        carries_overdue_flag=True,
    ),
    Factor(
        "interest_coverage",
        "1",
        "times",
        "Approximate coverage of the latest annual report: "
        "(operating_profit + finance_expense) / finance_expense. Finance expense is "
        "added back because operating profit is already after it. The denominator is "
        "财务费用, not interest expense alone, so a value is approx. "
        "not_applicable when finance expense is not positive "
        "(non_positive_finance_expense). Missing when an input is absent. "
        "Not applicable to banks and insurers.",
        (
            FactorInput("baostock", "operating_profit", _FIN),
            FactorInput("baostock", "finance_expense", _FIN),
            FactorInput("ifind", "operating_profit", _FIN),
            FactorInput("ifind", "finance_expense", _FIN),
        ),
        _interest_coverage,
        applicability=NOT_BANK_OR_INSURER,
        carries_overdue_flag=True,
    ),
    Factor(
        "roe_dupont",
        "1",
        "fraction",
        "ROE identity decomposition of the latest annual report: net margin x asset "
        "turnover x equity multiplier (Eastmoney statements via the field source "
        "registry). The value is the product; components and the residual vs the "
        "stored roe are recorded in inputs_ref, not asserted as independent facts.",
        (
            FactorInput("baostock", "net_profit_parent", _FIN),
            FactorInput("baostock", "revenue_total", _FIN),
            FactorInput("baostock", "total_assets", _FIN),
            FactorInput("baostock", "total_equity", _FIN),
            FactorInput("ifind", "net_profit_parent", _FIN),
            FactorInput("ifind", "revenue_total", _FIN),
            FactorInput("ifind", "total_assets", _FIN),
            FactorInput("ifind", "total_equity", _FIN),
        ),
        _roe_dupont,
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
        "dividend_yield_ttm",
        "1",
        "fraction",
        "Pre-tax cash dividends per share with ex-date in (as_of - 365 d, as_of] "
        "(restated for later bonus / capitalisation issues) / raw close of the latest "
        "session <= as_of. 0 when the collected years hold no cash dividend; missing "
        "(dividends_not_collected) when a year of the window was not collected.",
        (
            FactorInput("baostock", "close", f"{_KDATA}(adjust=none).close"),
            FactorInput(
                "baostock", "cash_dividend_per_share", "dividend_event.cash_per_share_pre_tax"
            ),
        ),
        _dividend_yield_ttm,
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
