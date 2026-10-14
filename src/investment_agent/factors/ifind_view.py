"""iFinD research rows -> observation view (golden corpus parity, decision D3).

:func:`ifind_research_view` builds the ``OBSERVATION`` view from decoded
iFinD research rows (``plan_ifind_requests`` keys): no database, no field
resolution. Request family and source field decide the factor-layer name and
whether the value is a percentage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from investment_agent.factors.industry_groups import IndustryGroupMap
from investment_agent.factors.metrics import parse_number, percent_to_fraction
from investment_agent.factors.snapshot_model import (
    OBSERVATION,
    Bar,
    FinancialPeriod,
    SnapshotView,
)

# (request family, source field) -> (factor-layer field, percent?)
_IFIND_PROFITABILITY_FIELDS: dict[str, tuple[str, bool]] = {
    "ths_roe_stock": ("roe", True),
    "ths_gross_selling_rate_stock": ("gross_margin", True),
    "ths_net_sales_rate_stock": ("net_margin", True),
    "ths_eps_basic_stock": ("eps_basic_fy", False),
    "ths_nav_ps_stock": ("bvps_fy", False),
    "ths_revenue_ps_stock": ("revenue_ps_fy", False),
}
_IFIND_INCOME_FIELDS: dict[str, str] = {
    "ths_operating_total_revenue_stock": "revenue_total",
    "ths_np_atoopc_stock": "net_profit_parent",
}
_IFIND_CASH_FIELDS: dict[str, str] = {
    "ths_ncf_from_oa_stock": "operating_cash_flow",
    "ths_cash_paid_for_assets_stock": "capital_expenditure",
}
_IFIND_CAPITAL_FIELDS: dict[str, tuple[str, bool]] = {
    "ths_asset_liab_ratio_stock": ("debt_ratio", True),
}


def _first(rows: Sequence[Mapping[str, str]] | None) -> Mapping[str, str] | None:
    return rows[0] if rows else None


def ifind_research_view(
    *,
    ticker: str,
    as_of: date,
    report_year: int,
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    industry_code: str | None = None,
    industry_name: str | None = None,
    industry_groups: IndustryGroupMap | None = None,
) -> SnapshotView:
    """Observation view from decoded iFinD research rows (``plan_ifind_requests`` keys).

    ``rows`` maps request names (``info``, ``history``, ``income_latest``,
    ``income_base``, ``cash_latest``, ``capital_latest``,
    ``profitability_<year>``) to decoded rows. The fiscal-year periods are
    treated as public on ``as_of`` (``availability="approx"``): the research
    request names the report year explicitly. History rows are the provider's
    (forward-adjusted) series; the last close equals the raw close, so it
    serves both as the raw and the return series.
    """

    info = _first(rows.get("info")) or {}
    history = sorted(rows.get("history") or [], key=lambda row: str(row.get("time", "")))
    total_shares = parse_number(info.get("ths_total_shares_stock"))
    bars: list[Bar] = []
    for row in history:
        time_text = str(row.get("time", ""))
        try:
            trade_date = datetime.strptime(time_text, "%Y%m%d").date()
        except ValueError:
            continue
        close = parse_number(row.get("close"))
        if close is None:
            continue
        bars.append(
            Bar(
                trade_date=trade_date,
                close=close,
                ref=f"ifind:/stock/history:{time_text}",
            )
        )
    if bars and total_shares is not None:
        bars[-1] = replace(bars[-1], total_shares=total_shares)

    def convert(row: Mapping[str, str], spec: Mapping[str, tuple[str, bool]]) -> dict[str, Any]:
        return {
            target: (percent_to_fraction if percent else parse_number)(row.get(source))
            for source, (target, percent) in spec.items()
        }

    periods: list[FinancialPeriod] = []
    for year in range(report_year - 3, report_year + 1):
        fields: dict[str, float | None] = {}
        refs: list[str] = []
        profitability = _first(rows.get(f"profitability_{year}"))
        if profitability is not None:
            fields.update(convert(profitability, _IFIND_PROFITABILITY_FIELDS))
            refs.append(f"profitability_{year}")
        income_key = (
            "income_latest"
            if year == report_year
            else "income_base"
            if year == report_year - 3
            else None
        )
        income = _first(rows.get(income_key)) if income_key else None
        if income is not None:
            fields.update(
                {
                    target: parse_number(income.get(source))
                    for source, target in _IFIND_INCOME_FIELDS.items()
                }
            )
            refs.append(str(income_key))
        if year == report_year:
            cash = _first(rows.get("cash_latest"))
            if cash is not None:
                fields.update(
                    {
                        target: parse_number(cash.get(source))
                        for source, target in _IFIND_CASH_FIELDS.items()
                    }
                )
                refs.append("cash_latest")
            capital = _first(rows.get("capital_latest"))
            if capital is not None:
                fields.update(convert(capital, _IFIND_CAPITAL_FIELDS))
                refs.append("capital_latest")
        if not refs:
            continue
        periods.append(
            FinancialPeriod(
                report_period=date(year, 12, 31),
                period_kind="annual",
                available_from=as_of,
                availability="approx",
                fields=fields,
                ref="ifind:" + "+".join(refs),
            )
        )
    return SnapshotView.build(
        ticker=ticker,
        as_of=as_of,
        policy=OBSERVATION,
        industry_code=industry_code,
        industry_name=industry_name,
        raw_bars=bars,
        adjusted_bars=bars,
        periods=periods,
        adjusted_kind="forward",
        industry_groups=industry_groups,
    )
