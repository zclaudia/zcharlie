"""Deterministic financial metrics and applicability rules for the HK/US pilots (plan 08).

This module turns frozen :class:`~investment_agent.materials.contracts.FactRecord` values
into the six first-delivery indicators plus a human-readable gap list. It is a pure
computation layer:

* no IO, no database, no network, no HTTP client and no new dependency — facts, the
  company profile and the real fiscal years are all arguments;
* the caller builds :class:`CompanyProfile` from task 01's controlled identity
  (:func:`profile_from_identity`); this module never reads the pilot list file;
* missing is never zero: a null ``raw_value``, an absent metric, a zero denominator, a
  loss year or a missing beginning equity all produce a documented non-OK status instead
  of a misleading number, and a metric is never backed out of a different definition;
* bank and ADS applicability are decided from the profile, not from the numbers.

Metric keys and the window they describe
----------------------------------------

``revenue_yoy`` covers the latest two complete fiscal years; ``revenue_cagr_3y`` the
latest four consecutive complete fiscal years (three intervals). The year-scoped keys
``roe_avg_equity``, ``debt_ratio``, ``cfo_to_profit`` and ``fcf_simple`` are emitted once
per fiscal year in ``fiscal_years``; a year whose inputs are missing or rejected keeps its
own result so the gap stays visible instead of silently disappearing.

Bank and ADS rules
------------------

``is_bank``: ``roe_avg_equity`` and ``cfo_to_profit`` are not applied to banks (no
ordinary-corporate profitability or cash-quality comparison) and ``fcf_simple`` is not
applied either; ``debt_ratio`` is reported with an explicit bank caveat. ``is_ads``:
``roe_avg_equity`` is not applied (the ADS capital structure is not verified). Every
rejection names the reason.

FCF naming and capex equivalence
--------------------------------

``fcf_simple`` is ``operating_cash_flow - capex_ppe`` in the original reporting currency
and is never presented as an equity free-cash-flow measure. It is computed only when the
capex fact is applicable and its ``sign_convention`` is listed in
:data:`FCF_ELIGIBLE_CAPEX`; a non-equivalent definition (net purchases and sales,
prepayments including construction in progress) is rejected by name.

Candidate facts
---------------

Facts not yet checked against the issuer's own report are usable but never silent: when
any input of a metric is a ``candidate`` fact the result carries a note saying so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Final

from investment_agent.domain.security import ControlledIdentity, SecurityType
from investment_agent.investor.common import Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import (
    CURRENCY_UNITS,
    AccountingStandard,
    EquityScope,
    FactPeriod,
    FactRecord,
    Verification,
)
from investment_agent.materials.metric_core import (
    CompanyProfile as CompanyProfile,
)
from investment_agent.materials.metric_core import (
    MetricResult as MetricResult,
)
from investment_agent.materials.metric_core import (
    MetricSet as MetricSet,
)
from investment_agent.materials.metric_core import (
    _amount,
    _amount_quantity,
    _ordered_years,
    _period_label,
    _ratio_quantity,
    _signed,
    _span_days,
    _unique_periods,
    build_result,
)

#: The six first-delivery metric keys, in display order.
METRIC_KEYS: Final[tuple[str, ...]] = (
    "revenue_yoy",
    "revenue_cagr_3y",
    "roe_avg_equity",
    "debt_ratio",
    "cfo_to_profit",
    "fcf_simple",
)

METRIC_LABELS: Final[dict[str, str]] = {
    "revenue_yoy": "收入同比",
    "revenue_cagr_3y": "收入三年复合增长率",
    "roe_avg_equity": "平均权益 ROE",
    "debt_ratio": "资产负债率",
    "cfo_to_profit": "经营现金流/利润",
    "fcf_simple": "简化自由现金流（经营现金流减资本开支）",
}

#: Revenue definitions tried in order; the first one complete over the needed years wins.
#: The chosen metric is always named in ``basis``, so the choice is never silent.
REVENUE_METRIC_PREFERENCE: Final[tuple[str, ...]] = (
    "revenue_operating",
    "revenue_net_credit",
    "revenue_gross",
)

#: Capex ``sign_convention`` values equivalent to cash capital expenditure. The first
#: delivery carries only the CLP-style ``capital expenditure`` convention; the HSBC
#: net-purchase-and-sale and Tencent prepayment conventions are rejected by name.
FCF_ELIGIBLE_CAPEX: Final[frozenset[str]] = frozenset({"capital expenditure"})

#: Profit definitions tried in order for the cash-conversion ratio. A ``profit_parent``
#: fact is only accepted when its ``equity_scope`` is ``ordinary_shareholders``, which is
#: the same definition; a parent figure that includes other equity instruments is not
#: substituted.
_CASH_PROFIT_PREFERENCE: Final[tuple[str, ...]] = (
    "profit_ordinary_shareholders",
    "profit_parent",
)
_ROE_PROFIT_PREFERENCE: Final[tuple[str, ...]] = ("profit_parent", "profit_ordinary_shareholders")
_EQUITY_PREFERENCE: Final[tuple[str, ...]] = ("equity_parent", "equity_ordinary")

#: Capability gaps that the first delivery states explicitly for every company. Gross and
#: net margin are not computed: revenue/cost fields were not verified per company.
FIXED_GAPS: Final[tuple[str, ...]] = (
    "gross_margin: 收入与成本字段本轮未逐家补验，不交付",
    "net_margin: 收入与成本字段本轮未逐家补验，不交付",
)

#: An annual fiscal year outside this span is not a complete year.
_COMPLETE_YEAR_MIN_DAYS: Final[int] = 330
_COMPLETE_YEAR_MAX_DAYS: Final[int] = 400

#: Fiscal-year lengths within this many days are treated as a 52/53-week difference; a
#: wider spread means the fiscal-year length itself changed and no annualised rate is
#: produced. The widest difference is used, not first against last, because a 53-week
#: year can fall in the middle of the window.
_FISCAL_LENGTH_TOLERANCE_DAYS: Final[int] = 7

#: One day of difference is a calendar-year artefact (leap years change the exclusive
#: day span of 1 January to 31 December), so the 52/53-week note needs a wider spread.
_LEAP_ROUNDING_TOLERANCE_DAYS: Final[int] = 1


def profile_from_identity(identity: ControlledIdentity, *, is_bank: bool) -> CompanyProfile:
    """Map a controlled identity to a metric profile; ``is_bank`` must be stated.

    The identity carries the market-checked fields (company key, currencies, ADS status
    and ratio, fiscal-year note). Whether the issuer is a bank is not part of the identity
    list, so the caller passes it explicitly instead of letting it default.
    """

    return CompanyProfile(
        company_key=identity.company_key,
        is_bank=is_bank,
        is_ads=identity.security_type == SecurityType.ADS,
        ads_ratio=identity.ads_ratio,
        report_currency=identity.report_currency,
        quote_currency=identity.quote_currency,
        fiscal_year_end_note=identity.fiscal_year_end_note,
    )


def compute_metrics(
    *,
    facts: Sequence[FactRecord],
    profile: CompanyProfile,
    fiscal_years: Sequence[FactPeriod],
) -> MetricSet:
    """Compute the six deterministic metrics for ``profile`` over ``fiscal_years``.

    ``fiscal_years`` are the real fiscal years of the material layer (ascending, never
    back-computed from natural years); duplicates by period end and out-of-order input are
    normalised by period end. Facts are indexed by ``metric`` and ``period.end``; several
    distinct facts for the same metric/date are reported as a conflict and no value is
    picked.
    """

    years = _ordered_years(fiscal_years)
    index = _FactIndex(facts)
    results: list[MetricResult] = [
        _revenue_yoy(index, profile, years),
        _revenue_cagr_3y(index, profile, years),
    ]
    for period in years:
        results.append(_roe_avg_equity(index, profile, period))
    for period in years:
        results.append(_debt_ratio(index, profile, period))
    for period in years:
        results.append(_cfo_to_profit(index, profile, period))
    for period in years:
        results.append(_fcf_simple(index, profile, period))
    gaps = list(FIXED_GAPS)
    gaps.extend(_gap_lines(results))
    return MetricSet(metrics=tuple(results), gaps=tuple(gaps))


# --- revenue over the latest years ---------------------------------------------


def _revenue_yoy(
    index: _FactIndex, profile: CompanyProfile, years: tuple[FactPeriod, ...]
) -> MetricResult:
    key = "revenue_yoy"
    if len(years) < 2:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=_yoy_basis(profile),
            notes=(
                "two complete adjacent fiscal years are required; "
                f"{len(years)} fiscal year(s) available ({_year_labels(years)})",
            ),
        )
    previous, current = years[-2], years[-1]
    basis = _yoy_basis(profile, current=current, previous=previous)
    problem = _adjacency_problem(previous, current)
    if problem is not None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(previous, current),
            notes=(problem,),
        )
    ends = (previous.end, current.end)
    complete = _complete_series(index, REVENUE_METRIC_PREFERENCE, ends)
    if not complete:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(previous, current),
            notes=(_series_problem(index, REVENUE_METRIC_PREFERENCE, ends),),
        )
    series = complete[0]
    basis = _yoy_basis(profile, current=current, previous=previous, series=series.metric)
    periods = (previous, current)
    inputs = _inputs(series, ends)
    notes = list(_definition_choice_notes(complete, series, "revenue"))
    notes.extend(_candidate_notes(series))
    status, comparability_notes = _comparability_notes(profile, series)
    notes.extend(comparability_notes)
    if status is not None:
        return _result(
            key, status=status, basis=basis, periods=periods, notes=tuple(notes), inputs=inputs
        )
    length_note = _length_comparability_note(previous, current)
    if length_note is not None:
        notes.append(length_note)
    previous_amount = _amount(series.facts[previous.end])
    current_amount = _amount(series.facts[current.end])
    if previous_amount is None or current_amount is None:
        notes.append(f"{series.metric} has no usable amount in one of the two years")
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
            inputs=inputs,
        )
    change, status, change_notes = _yoy_change(
        current=current_amount,
        previous=previous_amount,
        current_label=_period_label(current),
        previous_label=_period_label(previous),
        series=series.metric,
        profit_series=False,
    )
    notes.extend(change_notes)
    value = (
        _ratio_quantity(change) if status is CalculationStatus.OK and change is not None else None
    )
    return _result(
        key,
        status=status,
        value=value,
        basis=basis,
        periods=periods,
        notes=tuple(notes),
        inputs=inputs,
    )


def _revenue_cagr_3y(
    index: _FactIndex, profile: CompanyProfile, years: tuple[FactPeriod, ...]
) -> MetricResult:
    key = "revenue_cagr_3y"
    if len(years) < 4:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=_cagr_basis(profile, None),
            notes=(
                "four consecutive complete fiscal years are required; "
                f"{len(years)} fiscal year(s) available ({_year_labels(years)})",
            ),
        )
    window = years[-4:]
    basis = _cagr_basis(profile, window)
    spans, period_problem = _window_spans(window)
    if spans is None or period_problem is not None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=window,
            notes=(period_problem or "",),
        )
    ends = tuple(period.end for period in window)
    complete = _complete_series(index, REVENUE_METRIC_PREFERENCE, ends)
    if not complete:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=window,
            notes=(_series_problem(index, REVENUE_METRIC_PREFERENCE, ends),),
        )
    series = complete[0]
    basis = _cagr_basis(profile, window, series=series.metric)
    inputs = _inputs(series, ends)
    notes = list(_definition_choice_notes(complete, series, "revenue"))
    notes.extend(_candidate_notes(series))
    status, comparability_notes = _comparability_notes(profile, series)
    notes.extend(comparability_notes)
    if status is not None:
        return _result(
            key, status=status, basis=basis, periods=window, notes=tuple(notes), inputs=inputs
        )
    lengths = list(spans)
    spread = max(lengths) - min(lengths)
    rendered_lengths = "/".join(str(length) for length in lengths)
    if spread > _FISCAL_LENGTH_TOLERANCE_DAYS:
        notes.append(
            f"fiscal year length changed: {rendered_lengths} days over {_year_labels(window)}; "
            "no annualised rate is produced"
        )
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=window,
            notes=tuple(notes),
            inputs=inputs,
        )
    if spread > _LEAP_ROUNDING_TOLERANCE_DAYS:
        notes.append(
            f"52/53-week fiscal year; lengths {rendered_lengths} days over {_year_labels(window)}"
        )
    start_amount = _amount(series.facts[window[0].end])
    end_amount = _amount(series.facts[window[-1].end])
    if start_amount is None or end_amount is None:
        notes.append(f"{series.metric} has no usable amount at a window endpoint")
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=window,
            notes=tuple(notes),
            inputs=inputs,
        )
    if start_amount <= 0 or end_amount <= 0:
        notes.append(
            f"a window endpoint is not positive: {_period_label(window[0])} {series.metric} = "
            f"{start_amount}, {_period_label(window[-1])} {series.metric} = {end_amount}; no "
            "compound rate is produced"
        )
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=window,
            notes=tuple(notes),
            inputs=inputs,
        )
    with localcontext() as context:
        context.prec = 28
        compound = ((end_amount / start_amount).ln() / Decimal(3)).exp() - 1
    return _result(
        key,
        status=CalculationStatus.OK,
        value=_ratio_quantity(compound),
        basis=basis,
        periods=window,
        notes=tuple(notes),
        inputs=inputs,
    )


# --- per-fiscal-year metrics ---------------------------------------------------


def _roe_avg_equity(index: _FactIndex, profile: CompanyProfile, period: FactPeriod) -> MetricResult:
    key = "roe_avg_equity"
    year = _period_label(period)
    basis = (
        f"roe_avg_equity[{year}] = profit[{year}] / ((equity[beginning of {year}] + equity[end of "
        f"{year}]) / 2); equity is the average of two period ends, which is not the weighted-average "
        "equity of other ROE definitions; profit and equity must carry the same equity scope; "
        f"original reporting currency {profile.report_currency}, no display conversion"
    )
    beginning_end = _instant_before(period)
    profit_pick = _roe_profit_pick(index, period.end)
    equity_end_pick = _pick(index, _EQUITY_PREFERENCE, period.end)
    equity_begin_pick = _pick(index, _EQUITY_PREFERENCE, beginning_end)
    inputs = tuple(
        _input_ref(fact)
        for fact in (profit_pick.fact, equity_end_pick.fact, equity_begin_pick.fact)
        if fact is not None
    )
    if profit_pick.fact is not None and equity_end_pick.fact is not None:
        basis = (
            f"{basis}; numerator {profit_pick.fact.metric} "
            f"(equity_scope {profit_pick.fact.semantics.equity_scope.value}), denominator "
            f"{equity_end_pick.fact.metric} at the two period ends"
        )
    notes: list[str] = []
    if profit_pick.fact is not None and profit_pick.fact.metric == "net_profit":
        notes.append(
            "accepted net_profit for ROE because its source explicitly identifies US GAAP and "
            f"equity_scope={profit_pick.fact.semantics.equity_scope.value}; the fact name, source "
            "label, source field and provenance are preserved"
        )
    if profile.is_bank:
        notes.append("bank: the ordinary-corporate profitability comparison is not applied")
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    if profile.is_ads:
        notes.append(
            "ADS: the per-ADS capital structure is not verified, so an ordinary-share return on "
            "equity is not produced"
        )
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    problem = _pick_problem(profit_pick) or _pick_problem(equity_end_pick)
    if problem is not None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(problem,),
            inputs=inputs,
        )
    profit_fact, equity_end_fact = profit_pick.fact, equity_end_pick.fact
    if profit_fact is None or equity_end_fact is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(_missing_input_note(period, _ROE_PROFIT_PREFERENCE, _EQUITY_PREFERENCE),),
            inputs=inputs,
        )
    sample = (profit_fact, equity_end_fact)
    notes.extend(_candidate_notes_for_sample(sample))
    status, currency_notes = _currency_problem(profile, sample)
    notes.extend(currency_notes)
    if status is not None:
        return _result(
            key, status=status, basis=basis, periods=(period,), notes=tuple(notes), inputs=inputs
        )
    scope_problem = _scope_problem(profit_fact, equity_end_fact, equity_begin_pick.fact)
    if scope_problem is not None:
        status = (
            CalculationStatus.MISSING_INPUTS
            if scope_problem.unknown
            else CalculationStatus.NOT_APPLICABLE
        )
        notes.append(scope_problem.note)
        return _result(
            key, status=status, basis=basis, periods=(period,), notes=tuple(notes), inputs=inputs
        )
    equity_begin_fact = equity_begin_pick.fact
    if equity_begin_fact is None:
        where = f" at {beginning_end.isoformat()}" if beginning_end is not None else ""
        notes.append(
            f"beginning equity unavailable; end-only equity is not substituted (no "
            f"{'/'.join(_EQUITY_PREFERENCE)} fact{where})"
        )
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    begin_problem = _pick_problem(equity_begin_pick)
    if begin_problem is not None:
        notes.append(begin_problem)
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    beginning_ref = _input_ref(equity_begin_fact)
    if beginning_ref not in inputs:
        inputs = inputs + (beginning_ref,)
    notes.extend(_currency_problem(profile, (equity_begin_fact,))[1])
    periods = _unique_periods(period, equity_end_fact.period, equity_begin_fact.period)
    profit_amount = _amount(profit_fact)
    equity_end_amount = _amount(equity_end_fact)
    equity_begin_amount = _amount(equity_begin_fact)
    if profit_amount is None or equity_end_amount is None or equity_begin_amount is None:
        notes.append("one of the return inputs has no usable amount")
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
            inputs=inputs,
        )
    average_equity = (equity_begin_amount + equity_end_amount) / Decimal(2)
    if average_equity == 0:
        notes.append("average equity is zero, so no return is produced")
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
            inputs=inputs,
        )
    return _result(
        key,
        status=CalculationStatus.OK,
        value=_ratio_quantity(profit_amount / average_equity),
        basis=basis,
        periods=periods,
        notes=tuple(notes),
        inputs=inputs,
    )


def _debt_ratio(index: _FactIndex, profile: CompanyProfile, period: FactPeriod) -> MetricResult:
    key = "debt_ratio"
    year = _period_label(period)
    basis = (
        f"debt_ratio[{year}] = total_liabilities[end of {year}] / total_assets[end of {year}]; both "
        "figures are the balance sheet at the same instant and the same reporting scope; an "
        "interest-bearing-debt ratio is a different metric and is not substituted; original "
        f"reporting currency {profile.report_currency}, no display conversion"
    )
    assets_pick = _pick(index, ("total_assets",), period.end)
    liabilities_pick = _pick(index, ("total_liabilities",), period.end)
    inputs = tuple(
        _input_ref(fact) for fact in (assets_pick.fact, liabilities_pick.fact) if fact is not None
    )
    problem = _pick_problem(assets_pick) or _pick_problem(liabilities_pick)
    if problem is not None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(problem,),
            inputs=inputs,
        )
    assets_fact, liabilities_fact = assets_pick.fact, liabilities_pick.fact
    if liabilities_fact is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(
                "total_liabilities is missing; it is not derived from total assets minus equity, "
                "and an interest-bearing-debt ratio is not a substitute",
            ),
            inputs=inputs,
        )
    if assets_fact is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=("total_assets is missing; the ratio is not derived from other items",),
            inputs=inputs,
        )
    sample = (assets_fact, liabilities_fact)
    notes: list[str] = []
    notes.extend(_candidate_notes_for_sample(sample))
    status, currency_notes = _currency_problem(profile, sample)
    notes.extend(currency_notes)
    if status is not None:
        return _result(
            key, status=status, basis=basis, periods=(period,), notes=tuple(notes), inputs=inputs
        )
    if assets_fact.semantics.reporting_scope != liabilities_fact.semantics.reporting_scope:
        notes.append(
            "reporting scope differs between the two balance-sheet facts "
            f"({assets_fact.semantics.reporting_scope.value} vs "
            f"{liabilities_fact.semantics.reporting_scope.value}); the same-scope ratio is not "
            "produced"
        )
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    if profile.is_bank:
        notes.append("bank: ordinary-corporate thresholds are not applied")
    assets_amount = _amount(assets_fact)
    liabilities_amount = _amount(liabilities_fact)
    if assets_amount is None or liabilities_amount is None:
        notes.append("one of the balance-sheet inputs has no usable amount")
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    if assets_amount == 0:
        notes.append("total assets is zero at this period end, so no ratio is produced")
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    return _result(
        key,
        status=CalculationStatus.OK,
        value=_ratio_quantity(liabilities_amount / assets_amount),
        basis=basis,
        periods=_unique_periods(assets_fact.period, liabilities_fact.period),
        notes=tuple(notes),
        inputs=inputs,
    )


def _cfo_to_profit(index: _FactIndex, profile: CompanyProfile, period: FactPeriod) -> MetricResult:
    key = "cfo_to_profit"
    year = _period_label(period)
    basis = (
        f"cfo_to_profit[{year}] = operating_cash_flow[{year}] / profit[{year}]; numerator and "
        "denominator cover the same fiscal year, and the profit is the ordinary-shareholders "
        "figure (a parent figure is used only when its equity scope is ordinary shareholders); "
        f"original reporting currency {profile.report_currency}, no display conversion"
    )
    cash_pick = _pick(index, ("operating_cash_flow",), period.end)
    profit_pick = _cash_profit_pick(index, period.end)
    inputs = tuple(
        _input_ref(fact) for fact in (cash_pick.fact, profit_pick.fact) if fact is not None
    )
    notes: list[str] = []
    if profile.is_bank:
        notes.append("bank: the ordinary-corporate cash-quality comparison is not applied")
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    problem = _pick_problem(cash_pick) or _pick_problem(profit_pick)
    if problem is not None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(problem,),
            inputs=inputs,
        )
    cash_fact, profit_fact = cash_pick.fact, profit_pick.fact
    if cash_fact is not None and profit_fact is not None:
        basis = (
            f"{basis}; numerator {cash_fact.metric}, denominator {profit_fact.metric} "
            f"(equity_scope {profit_fact.semantics.equity_scope.value})"
        )
    if cash_fact is None or profit_fact is None:
        missing = [
            name
            for name, fact in (("operating_cash_flow", cash_fact), ("profit", profit_fact))
            if fact is None
        ]
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(f"missing input(s) for {year}: {', '.join(missing)}",),
            inputs=inputs,
        )
    sample = (cash_fact, profit_fact)
    notes.extend(_candidate_notes_for_sample(sample))
    period_problem = _same_window_problem(cash_fact, profit_fact)
    if period_problem is not None:
        notes.append(period_problem)
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    status, currency_notes = _currency_problem(profile, sample)
    notes.extend(currency_notes)
    if status is not None:
        return _result(
            key, status=status, basis=basis, periods=(period,), notes=tuple(notes), inputs=inputs
        )
    if (
        profit_fact.metric == "profit_parent"
        and profit_fact.semantics.equity_scope == EquityScope.ORDINARY
    ):
        notes.append(
            "used the parent figure carrying equity_scope=ordinary_shareholders, which is the same "
            "definition as the ordinary-shareholders profit"
        )
    profit_amount = _amount(profit_fact)
    cash_amount = _amount(cash_fact)
    if profit_amount is None or cash_amount is None:
        notes.append("one of the cash-quality inputs has no usable amount")
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    if profit_amount <= 0:
        previous_period = _previous_year(period)
        previous_profit: Decimal | None = None
        if previous_period is not None:
            previous_pick = _cash_profit_pick(index, previous_period.end)
            previous_profit = (
                _amount(previous_pick.fact) if previous_pick.fact is not None else None
            )
        notes.append(
            _loss_note(
                profit_fact=profit_fact,
                profit_amount=profit_amount,
                previous_profit=previous_profit,
                current_label=year,
                previous_label=_period_label(previous_period),
            )
        )
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    return _result(
        key,
        status=CalculationStatus.OK,
        value=_ratio_quantity(cash_amount / profit_amount),
        basis=basis,
        periods=_unique_periods(cash_fact.period, profit_fact.period),
        notes=tuple(notes),
        inputs=inputs,
    )


def _fcf_simple(index: _FactIndex, profile: CompanyProfile, period: FactPeriod) -> MetricResult:
    key = "fcf_simple"
    year = _period_label(period)
    basis = (
        f"fcf_simple[{year}] = operating_cash_flow[{year}] - capex_ppe[{year}]; both facts cover "
        "the same period and currency, and capex_ppe must be an applicable cash capital "
        "expenditure definition; this is a simple operating-cash measure, not an equity "
        "free-cash-flow measure; original reporting currency "
        f"{profile.report_currency}, no display conversion"
    )
    cash_pick = _pick(index, ("operating_cash_flow",), period.end)
    capex_pick = _pick(index, ("capex_ppe",), period.end)
    inputs = tuple(
        _input_ref(fact) for fact in (cash_pick.fact, capex_pick.fact) if fact is not None
    )
    cash_problem = _pick_problem(cash_pick)
    if cash_problem is not None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(cash_problem,),
            inputs=inputs,
        )
    cash_fact, capex_fact = cash_pick.fact, capex_pick.fact
    if capex_fact is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(_capex_unavailable_note(index, period),),
            inputs=inputs,
        )
    capex_amount = _amount(capex_fact)
    if capex_amount is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(_capex_unavailable_note(index, period),),
            inputs=inputs,
        )
    if cash_fact is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=(f"no operating_cash_flow fact at {period.end.isoformat()}",),
            inputs=inputs,
        )
    notes: list[str] = []
    if profile.is_bank:
        notes.append("bank: the ordinary-corporate free-cash measure is not applied")
    capex_problem = _capex_problem(capex_fact)
    if capex_problem is not None:
        notes.append(capex_problem)
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    sample = (cash_fact, capex_fact)
    notes.extend(_candidate_notes_for_sample(sample))
    same_window = _same_window_problem(cash_fact, capex_fact)
    if same_window is not None:
        notes.append(same_window)
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    status, currency_notes = _currency_problem(profile, sample)
    notes.extend(currency_notes)
    if status is not None:
        return _result(
            key, status=status, basis=basis, periods=(period,), notes=tuple(notes), inputs=inputs
        )
    cash_amount = _amount(cash_fact)
    if cash_amount is None:
        notes.append("operating_cash_flow has no usable amount")
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=(period,),
            notes=tuple(notes),
            inputs=inputs,
        )
    return _result(
        key,
        status=CalculationStatus.OK,
        value=_amount_quantity(cash_amount - capex_amount, profile.report_currency),
        basis=basis,
        periods=_unique_periods(cash_fact.period, capex_fact.period),
        notes=tuple(notes),
        inputs=inputs,
    )


# --- shared rules -------------------------------------------------------------


def _yoy_change(
    *,
    current: Decimal,
    previous: Decimal,
    current_label: str,
    previous_label: str,
    series: str,
    profit_series: bool,
) -> tuple[Decimal | None, CalculationStatus, tuple[str, ...]]:
    """The shared period-over-period rule for an amount series.

    A zero or negative base and a positive-to-negative move never yield a growth rate: the
    amount change is reported instead, and a profit series is stated as turning to a loss.
    """

    change = current - previous
    if previous == 0:
        return (
            None,
            CalculationStatus.NOT_APPLICABLE,
            (
                f"prior period is zero; the amount change {_signed(change)} ({series} "
                f"{current_label} {current} minus {previous_label} {previous}) is given instead of "
                "a ratio",
            ),
        )
    if previous < 0:
        return (
            None,
            CalculationStatus.NOT_APPLICABLE,
            (
                f"prior period is negative ({series} {previous_label} {previous}); the amount "
                f"change {_signed(change)} is given instead of a ratio",
            ),
        )
    if current < 0:
        wording = "turned to loss" if profit_series else "changed sign"
        return (
            None,
            CalculationStatus.NOT_APPLICABLE,
            (
                f"{wording}: {series} {previous_label} {previous} to {current_label} {current}; "
                f"the amount change is {_signed(change)} and no growth ratio is produced",
            ),
        )
    return change / previous, CalculationStatus.OK, ()


def _capex_unavailable_note(index: _FactIndex, period: FactPeriod) -> str:
    """Why no capital-expenditure amount can be used, keeping the reason visible."""

    raw, conflict = index.one("capex_ppe", period.end)
    tail = "a missing capital expenditure is never treated as zero"
    if conflict is not None:
        return f"{conflict}; {tail}"
    if raw is None:
        return f"no capex_ppe fact at {period.end.isoformat()}; {tail}"
    reason = raw.value.missing_reason or "no reason recorded"
    return f"capex_ppe@{period.end.isoformat()} has no value (missing_reason: {reason}); {tail}"


def _capex_problem(fact: FactRecord) -> str | None:
    """Why ``fcf_simple`` cannot use this capex fact, or ``None`` when it can."""

    convention = fact.value.sign_convention
    source_label = fact.semantics.source_label or fact.semantics.source_field or "not recorded"
    detail = f"capex_ppe sign_convention {convention!r} (source label: {source_label})"
    if fact.applicability == "not_applicable":
        return (
            f"{detail} is marked not_applicable for this issuer; an equivalent cash capital "
            "expenditure definition is missing, so no free-cash figure is produced"
        )
    if fact.applicability == "unknown":
        return (
            f"{detail} has unknown applicability; it is not accepted before the definition is "
            "checked"
        )
    if convention not in FCF_ELIGIBLE_CAPEX:
        eligible = ", ".join(sorted(FCF_ELIGIBLE_CAPEX))
        return (
            f"{detail} is not equivalent to cash capital expenditure (accepted: {eligible}); the "
            "definition covers different cash flows, so no free-cash figure is produced"
        )
    return None


def _loss_note(
    *,
    profit_fact: FactRecord,
    profit_amount: Decimal,
    previous_profit: Decimal | None,
    current_label: str,
    previous_label: str,
) -> str:
    if previous_profit is not None and previous_profit > 0:
        return (
            f"turned to loss: {profit_fact.metric} {previous_label} {previous_profit} to "
            f"{current_label} {profit_amount}; {profit_fact.metric} is not positive in "
            f"{current_label}, so no cash-conversion ratio is produced"
        )
    return (
        f"{profit_fact.metric} is not positive in {current_label} ({profit_amount}), so no "
        "cash-conversion ratio is produced"
    )


def _currency_problem(
    profile: CompanyProfile, facts: Sequence[FactRecord]
) -> tuple[CalculationStatus | None, tuple[str, ...]]:
    """Reject amounts that are not stated in the profile's reporting currency."""

    currency_changes: list[str] = []
    unit_problems: list[str] = []
    for fact in facts:
        value = fact.value
        where = f"{fact.metric}@{fact.period.end.isoformat()}"
        if value.unit not in CURRENCY_UNITS:
            unit_problems.append(
                f"{where} has unit {value.unit!r}, which is not an amount in the reporting currency"
            )
        elif value.unit != profile.report_currency or value.currency != profile.report_currency:
            currency_changes.append(
                f"{where} is reported in {value.unit} (currency {value.currency!r}) while the "
                f"profile reporting currency is {profile.report_currency}"
            )
    if currency_changes:
        return (
            CalculationStatus.NOT_APPLICABLE,
            (
                "reporting currency changed: "
                + "; ".join(currency_changes)
                + "; no converted trend is produced",
            ),
        )
    if unit_problems:
        return CalculationStatus.MISSING_INPUTS, tuple(unit_problems)
    return None, ()


def _comparability_notes(
    profile: CompanyProfile, series: _Series
) -> tuple[CalculationStatus | None, tuple[str, ...]]:
    """Currency plus semantics checks for a single metric across years."""

    status, notes = _currency_problem(profile, tuple(series.facts.values()))
    if status is not None:
        return status, notes
    differences: list[str] = []
    for name, element in (
        ("accounting_standard", _accounting_standards(series)),
        ("reporting_scope", _reporting_scopes(series)),
        ("equity_scope", _equity_scopes(series)),
        ("per_share_basis", _per_share_bases(series)),
    ):
        if len(element) > 1:
            rendered = ", ".join(sorted(member for member in element))
            differences.append(f"{name} differs across the series ({rendered})")
    if differences:
        return (
            CalculationStatus.NOT_APPLICABLE,
            (
                "the series is not comparable across years: "
                + "; ".join(differences)
                + "; no combined figure is produced",
            ),
        )
    return None, ()


def _accounting_standards(series: _Series) -> set[str]:
    return {fact.semantics.accounting_standard.value for fact in series.facts.values()}


def _reporting_scopes(series: _Series) -> set[str]:
    return {fact.semantics.reporting_scope.value for fact in series.facts.values()}


def _equity_scopes(series: _Series) -> set[str]:
    return {fact.semantics.equity_scope.value for fact in series.facts.values()}


def _per_share_bases(series: _Series) -> set[str]:
    return {fact.semantics.per_share_basis.value for fact in series.facts.values()}


@dataclass(frozen=True)
class _ScopeProblem:
    """A rejected equity-scope combination and whether it is unknown or inconsistent."""

    note: str
    unknown: bool


def _scope_problem(
    profit_fact: FactRecord, equity_end_fact: FactRecord, equity_begin_fact: FactRecord | None
) -> _ScopeProblem | None:
    scopes = {profit_fact.semantics.equity_scope, equity_end_fact.semantics.equity_scope}
    if equity_begin_fact is not None:
        scopes.add(equity_begin_fact.semantics.equity_scope)
    if EquityScope.UNKNOWN in scopes:
        return _ScopeProblem(
            note=(
                "equity scope is unknown for at least one input, so the required same-scope return "
                "cannot be confirmed"
            ),
            unknown=True,
        )
    if len(scopes) > 1:
        rendered = ", ".join(sorted(scope.value for scope in scopes))
        return _ScopeProblem(
            note=(
                f"equity scope differs between the profit and equity facts ({rendered}); a "
                "mixed-scope return is not produced"
            ),
            unknown=False,
        )
    return None


def _definition_choice_notes(
    complete: tuple[_Series, ...], chosen: _Series, kind: str
) -> tuple[str, ...]:
    if len(complete) < 2:
        return ()
    return (
        f"more than one complete {kind} definition is available "
        f"({', '.join(series.metric for series in complete)}); used {chosen.metric} by the "
        "documented preference order",
    )


def _candidate_notes(series: _Series) -> tuple[str, ...]:
    return _candidate_notes_for_sample(tuple(series.facts.values()))


def _candidate_notes_for_sample(facts: Sequence[FactRecord]) -> tuple[str, ...]:
    if not facts:
        return ()
    candidates = [fact for fact in facts if fact.verification == Verification.CANDIDATE]
    if not candidates:
        return ()
    return (
        f"{len(candidates)} of {len(facts)} input fact(s) are candidates (not verified against the "
        "issuer's report); the figures are shown with that caveat",
    )


def _same_window_problem(left: FactRecord, right: FactRecord) -> str | None:
    if left.period.end != right.period.end:
        return (
            f"{left.metric} ends {left.period.end.isoformat()} and {right.metric} ends "
            f"{right.period.end.isoformat()}; the same period is required"
        )
    if (
        left.period.start is not None
        and right.period.start is not None
        and left.period.start != right.period.start
    ):
        return (
            f"{left.metric} starts {left.period.start.isoformat()} and {right.metric} starts "
            f"{right.period.start.isoformat()}; the same period is required"
        )
    return None


def _adjacency_problem(previous: FactPeriod, current: FactPeriod) -> str | None:
    for period in (previous, current):
        span = _span_days(period)
        if span is None:
            return (
                f"{_period_label(period)} has no usable period span (start or days missing), so "
                "fiscal-year completeness cannot be checked"
            )
        if not _COMPLETE_YEAR_MIN_DAYS <= span <= _COMPLETE_YEAR_MAX_DAYS:
            return f"{_period_label(period)} spans {span} days, which is not a complete fiscal year"
    if previous.end >= current.end:
        return (
            f"{_period_label(previous)} ends on or after {_period_label(current)}; two adjacent "
            "complete fiscal years are required"
        )
    if current.start is None:
        return (
            f"{_period_label(current)} has no period start, so adjacency with "
            f"{_period_label(previous)} cannot be checked"
        )
    if previous.end + timedelta(days=1) != current.start:
        return (
            f"{_period_label(previous)} ends {previous.end.isoformat()} but "
            f"{_period_label(current)} starts {current.start.isoformat()}; the two fiscal years are "
            "not adjacent"
        )
    return None


def _window_spans(window: tuple[FactPeriod, ...]) -> tuple[tuple[int, ...] | None, str | None]:
    """Fiscal-year spans of a window, or the reason the window is not four complete years."""

    spans: list[int] = []
    for position, period in enumerate(window):
        span = _span_days(period)
        if span is None:
            return (
                None,
                f"{_period_label(period)} has no usable period span (start or days missing), so "
                "fiscal-year completeness cannot be checked",
            )
        if not _COMPLETE_YEAR_MIN_DAYS <= span <= _COMPLETE_YEAR_MAX_DAYS:
            return (
                None,
                f"{_period_label(period)} spans {span} days, which is not a complete fiscal year",
            )
        if position > 0:
            previous = window[position - 1]
            if period.start is None or previous.end + timedelta(days=1) != period.start:
                reached = period.start.isoformat() if period.start else period.end.isoformat()
                return (
                    None,
                    f"no complete fiscal year between {previous.end.isoformat()} and {reached}: "
                    f"{_period_label(previous)} to {_period_label(period)} is not consecutive",
                )
        spans.append(span)
    return tuple(spans), None


def _length_comparability_note(previous: FactPeriod, current: FactPeriod) -> str | None:
    previous_span = _span_days(previous)
    current_span = _span_days(current)
    if previous_span is None or current_span is None or previous_span == current_span:
        return None
    return (
        f"period lengths differ ({previous_span} vs {current_span} days; 52/53-week fiscal year); "
        "the rate is shown with that comparability caveat"
    )


# --- selection helpers ---------------------------------------------------------


def _series_problem(index: _FactIndex, candidates: Sequence[str], ends: Sequence[date]) -> str:
    details: list[str] = []
    for metric in candidates:
        missing: list[str] = []
        for end in ends:
            fact, problem = index.one(metric, end)
            if problem is not None:
                details.append(problem)
            elif fact is None:
                missing.append(end.isoformat())
        if missing:
            details.append(f"{metric} is missing at {', '.join(missing)}")
    if not details:
        details.append("no revenue fact is available for the required years")
    return "; ".join(details)


def _complete_series(
    index: _FactIndex, candidates: Sequence[str], ends: Sequence[date]
) -> tuple[_Series, ...]:
    complete: list[_Series] = []
    for metric in candidates:
        facts: dict[date, FactRecord] = {}
        for end in ends:
            fact, problem = index.one(metric, end)
            if problem is not None or fact is None:
                break
            facts[end] = fact
        else:
            complete.append(_Series(metric=metric, facts=facts))
    return tuple(complete)


def _pick(index: _FactIndex, candidates: Sequence[str], end: date | None) -> _Pick:
    """First candidate fact with an amount at ``end``, keeping the reasons visible."""

    problems: list[str] = []
    for metric in candidates:
        fact, problem = index.one(metric, end)
        if problem is not None:
            problems.append(problem)
            continue
        if fact is None:
            continue
        if _amount(fact) is None:
            reason = fact.value.missing_reason or "no reason recorded"
            problems.append(f"{_input_ref(fact)} has no value ({reason})")
            continue
        return _Pick(fact=fact, problems=())
    return _Pick(fact=None, problems=tuple(problems))


def _cash_profit_pick(index: _FactIndex, end: date | None) -> _Pick:
    """The ordinary-shareholders profit for a cash-conversion ratio.

    ``profit_ordinary_shareholders`` wins; ``profit_parent`` is accepted only when it
    carries ``equity_scope=ordinary_shareholders``, i.e. the identical definition. A
    parent figure that includes other equity instruments is never substituted.
    """

    problems: list[str] = []
    for metric in _CASH_PROFIT_PREFERENCE:
        fact, problem = index.one(metric, end)
        if problem is not None:
            problems.append(problem)
            continue
        if fact is None:
            continue
        if metric == "profit_parent" and fact.semantics.equity_scope != EquityScope.ORDINARY:
            problems.append(
                f"{_input_ref(fact)} carries equity_scope="
                f"{fact.semantics.equity_scope.value}, which is not the ordinary-shareholders "
                "profit; it is not substituted"
            )
            continue
        if _amount(fact) is None:
            reason = fact.value.missing_reason or "no reason recorded"
            problems.append(f"{_input_ref(fact)} has no value ({reason})")
            continue
        return _Pick(fact=fact, problems=())
    return _Pick(fact=None, problems=tuple(problems))


def _roe_profit_pick(index: _FactIndex, end: date | None) -> _Pick:
    """Select an ROE numerator without rewriting the source fact's metric name.

    The SEC US GAAP ``NetIncomeLoss`` concept is comparable to a parent-profit
    numerator when the reviewed fact explicitly carries a parent-attributable
    equity scope. That adaptation is limited to ROE; the cash-conversion policy
    continues to require its existing ordinary-shareholder definitions.
    """

    net_profit, net_profit_problem = index.one("net_profit", end)
    if net_profit_problem is not None:
        # A conflict in the newly supported source spelling is not permission to
        # fall back to another numerator.
        return _Pick(fact=None, problems=(net_profit_problem,))

    if net_profit is None:
        # Keep the historical sequential preference semantics exactly when the
        # new source spelling is absent: a usable preferred fact wins even if a
        # lower-preference spelling is independently conflicted.
        return _pick(index, _ROE_PROFIT_PREFERENCE, end)

    net_profit_ineligible: str | None = None
    explicit_parent_basis = (
        net_profit.semantics.accounting_standard == AccountingStandard.US_GAAP
        and net_profit.semantics.equity_scope in {EquityScope.ORDINARY, EquityScope.ALL_PARENT}
    )
    if not explicit_parent_basis:
        net_profit_ineligible = (
            f"{_input_ref(net_profit)} is not an eligible ROE net_profit alias: it requires "
            "explicit us_gaap and equity_scope=ordinary_shareholders or "
            "all_parent_equity_holders"
        )
        # An ineligible net_profit is not an alias; retain the old direct-fact
        # path where possible, and report the eligibility gap if it is all we have.
        direct = _pick(index, _ROE_PROFIT_PREFERENCE, end)
        if direct.fact is not None or direct.problems:
            return direct
        return _Pick(fact=None, problems=(net_profit_ineligible,))

    existing: list[FactRecord] = []
    problems: list[str] = []
    for metric in _ROE_PROFIT_PREFERENCE:
        fact, problem = index.one(metric, end)
        if problem is not None:
            problems.append(problem)
        elif fact is not None:
            existing.append(fact)

    if problems:
        # A conflicted direct source alias must not be bypassed by the new
        # spelling; doing so could make the result depend on a favorable value.
        return _Pick(fact=None, problems=tuple(problems))

    for fact in existing:
        if fact.semantics.equity_scope != net_profit.semantics.equity_scope:
            continue
        basis_mismatches: list[str] = []
        if (fact.value.currency, fact.value.unit) != (
            net_profit.value.currency,
            net_profit.value.unit,
        ):
            basis_mismatches.append("currency/unit")
        if fact.semantics.accounting_standard != net_profit.semantics.accounting_standard:
            basis_mismatches.append("accounting standard")
        if fact.semantics.reporting_scope != net_profit.semantics.reporting_scope:
            basis_mismatches.append("reporting scope")
        if (
            fact.period.start,
            fact.period.end,
            fact.period.basis,
        ) != (
            net_profit.period.start,
            net_profit.period.end,
            net_profit.period.basis,
        ):
            basis_mismatches.append("period start/end/basis")
        if basis_mismatches:
            return _Pick(
                fact=None,
                problems=(
                    f"incomparable same-scope ROE profit aliases at "
                    f"{end.isoformat() if end is not None else 'unknown date'}: "
                    f"{_input_ref(fact)} and {_input_ref(net_profit)} differ in "
                    f"{', '.join(basis_mismatches)}; no numerator is selected",
                ),
            )
        fact_amount, net_profit_amount = _amount(fact), _amount(net_profit)
        if (
            fact_amount is not None
            and net_profit_amount is not None
            and fact_amount != net_profit_amount
        ):
            return _Pick(
                fact=None,
                problems=(
                    f"conflicting same-scope ROE profit aliases at "
                    f"{end.isoformat() if end is not None else 'unknown date'}: "
                    f"{_input_ref(fact)} and {_input_ref(net_profit)} differ; no numerator "
                    "is selected",
                ),
            )

    # Preserve legacy preference if a comparable direct fact exists; otherwise
    # use the reviewed alias with its original metric and provenance intact.
    for fact in existing:
        if _amount(fact) is not None:
            return _Pick(fact=fact, problems=())
    if _amount(net_profit) is None:
        reason = net_profit.value.missing_reason or "no reason recorded"
        return _Pick(
            fact=None,
            problems=(f"{_input_ref(net_profit)} has no value ({reason})",),
        )
    return _Pick(fact=net_profit, problems=())


def _pick_problem(pick: _Pick) -> str | None:
    return "; ".join(pick.problems) if pick.problems else None


def _missing_input_note(
    period: FactPeriod, profit_metrics: Sequence[str], equity_metrics: Sequence[str]
) -> str:
    return (
        f"missing input(s) for {_period_label(period)}: no completed "
        f"{'/'.join(profit_metrics)} or {'/'.join(equity_metrics)} fact at "
        f"{period.end.isoformat()}"
    )


def _gap_lines(results: Sequence[MetricResult]) -> list[str]:
    grouped: dict[tuple[str, CalculationStatus, str], list[str]] = {}
    for result in results:
        if result.status == CalculationStatus.OK:
            continue
        reason = result.notes[0] if result.notes else result.basis
        label = result.period_end.isoformat() if result.period_end is not None else "no period"
        grouped.setdefault((result.key, result.status, reason), []).append(label)
    return [
        f"{key} ({', '.join(periods)}): {status.value} — {reason}"
        for (key, status, reason), periods in grouped.items()
    ]


# --- small helpers ------------------------------------------------------------


def _yoy_basis(
    profile: CompanyProfile,
    *,
    current: FactPeriod | None = None,
    previous: FactPeriod | None = None,
    series: str | None = None,
) -> str:
    tail = (
        "same revenue definition, scope and accounting standard in both years; original reporting "
        f"currency {profile.report_currency}, no display conversion"
    )
    if current is None or previous is None or series is None:
        return (
            "revenue_yoy = (latest fiscal-year revenue minus prior fiscal-year revenue) / prior "
            f"fiscal-year revenue; {tail}"
        )
    return (
        f"revenue_yoy[{_period_label(current)}] = ({series}[{_period_label(current)}] minus "
        f"{series}[{_period_label(previous)}]) / {series}[{_period_label(previous)}]; {tail}"
    )


def _cagr_basis(
    profile: CompanyProfile, window: tuple[FactPeriod, ...] | None, *, series: str | None = None
) -> str:
    tail = (
        "three annual intervals; same revenue definition, scope and accounting standard across the "
        f"four years; original reporting currency {profile.report_currency}, no display conversion"
    )
    if window is None:
        return (
            "revenue_cagr_3y = (revenue[last fiscal year] / revenue[first fiscal year]) ** (1/3) - 1; "
            f"{tail}"
        )
    name = series or "revenue"
    return (
        f"revenue_cagr_3y[{_period_label(window[0])}..{_period_label(window[-1])}] = "
        f"({name}[{_period_label(window[-1])}] / {name}[{_period_label(window[0])}]) ** (1/3) - 1; "
        f"{tail}"
    )


@dataclass(frozen=True)
class _Series:
    """One metric's facts over the dates a rule needs."""

    metric: str
    facts: dict[date, FactRecord]


@dataclass(frozen=True)
class _Pick:
    """A chosen fact plus the reasons no fact could be chosen."""

    fact: FactRecord | None
    problems: tuple[str, ...]


class _FactIndex:
    """``(metric, period end)`` index that never picks between conflicting facts."""

    def __init__(self, facts: Sequence[FactRecord]) -> None:
        grouped: dict[tuple[str, date], list[FactRecord]] = {}
        for fact in facts:
            grouped.setdefault((fact.metric, fact.period.end), []).append(fact)
        self._grouped = {key: tuple(rows) for key, rows in grouped.items()}

    def one(self, metric: str, end: date | None) -> tuple[FactRecord | None, str | None]:
        if end is None:
            return None, None
        rows = self._grouped.get((metric, end))
        if not rows:
            return None, None
        distinct = {_signature(row): row for row in rows}
        if len(distinct) > 1:
            return None, (
                f"multiple distinct {metric} facts at {end.isoformat()} ({len(distinct)} values); "
                "conflicting facts are not averaged or picked"
            )
        return next(iter(distinct.values())), None


def _signature(fact: FactRecord) -> tuple[object, ...]:
    return (fact.value, fact.semantics, fact.period.basis)


def _result(
    key: str,
    *,
    status: CalculationStatus,
    basis: str,
    value: Quantity | None = None,
    periods: tuple[FactPeriod, ...] = (),
    notes: tuple[str, ...] = (),
    inputs: tuple[str, ...] = (),
) -> MetricResult:
    """Build one rule result; the display label comes from :data:`METRIC_LABELS`."""

    return build_result(
        key,
        METRIC_LABELS[key],
        status=status,
        basis=basis,
        value=value,
        periods=periods,
        notes=notes,
        inputs=inputs,
    )


def _instant_before(period: FactPeriod) -> date | None:
    if period.start is None:
        return None
    return period.start - timedelta(days=1)


def _previous_year(period: FactPeriod) -> FactPeriod | None:
    if period.start is None:
        return None
    return FactPeriod(
        start=None,
        end=period.start - timedelta(days=1),
        basis=period.basis,
        label=None,
        days=None,
    )


def _year_labels(periods: Sequence[FactPeriod]) -> str:
    return ", ".join(_period_label(period) for period in periods)


def _inputs(series: _Series, ends: Sequence[date]) -> tuple[str, ...]:
    return tuple(_input_ref(series.facts[end]) for end in ends)


def _input_ref(fact: FactRecord) -> str:
    return f"{fact.metric}@{fact.period.end.isoformat()}"
