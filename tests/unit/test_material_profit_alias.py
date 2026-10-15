"""The ROE-only US GAAP profit alias stays tied to its reviewed source fact."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import (
    FACT_ENTRY_KEYS,
    AccountingStandard,
    EquityScope,
    FactPeriod,
    FactRecord,
    PeriodBasis,
    ReportingScope,
    fact_from_payload,
)
from investment_agent.materials.metric_core import CompanyProfile
from investment_agent.materials.metrics import compute_metrics

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "config/research_material/annual_validation_2026-10-15.json"
)
APPLE_END = date(2025, 9, 27)


def _reviewed_apple_material() -> tuple[tuple[FactRecord, ...], tuple[FactPeriod, ...]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    company = next(row for row in manifest["companies"] if row["company_key"] == "apple")
    facts: list[FactRecord] = []
    years: list[FactPeriod] = []
    for year in company["fiscal_years"]:
        years.append(
            FactPeriod(
                start=date.fromisoformat(year["period_start"]),
                end=date.fromisoformat(year["period_end"]),
                basis=PeriodBasis.ANNUAL,
                label=year["label"],
            )
        )
        for item in year["facts"]:
            payload = item["fact"]
            facts.append(fact_from_payload({key: payload[key] for key in FACT_ENTRY_KEYS}))
    first = company["fiscal_years"][0]["beginning_equity_fact"]["fact"]
    facts.append(fact_from_payload({key: first[key] for key in FACT_ENTRY_KEYS}))
    return tuple(facts), tuple(years)


def _profile(**changes: object) -> CompanyProfile:
    profile = CompanyProfile(
        company_key="apple",
        is_bank=False,
        is_ads=False,
        ads_ratio=None,
        report_currency="USD",
        quote_currency="USD",
        fiscal_year_end_note="September fiscal year end",
    )
    return replace(profile, **changes)


def _latest_roe(facts: tuple[FactRecord, ...], years: tuple[FactPeriod, ...], **profile_changes):
    metric_set = compute_metrics(
        facts=facts,
        profile=_profile(**profile_changes),
        fiscal_years=years,
    )
    return next(
        result
        for result in metric_set.metrics
        if result.key == "roe_avg_equity" and result.period_end == APPLE_END
    )


def test_reviewed_apple_net_income_computes_roe_with_original_input_reference():
    facts, years = _reviewed_apple_material()
    net_income = next(
        fact for fact in facts if fact.metric == "net_profit" and fact.period.end == APPLE_END
    )

    result = _latest_roe(facts, years)

    assert result.status == CalculationStatus.OK
    assert result.value is not None
    assert result.basis.startswith("roe_avg_equity[FY2025] = profit[FY2025]")
    assert "numerator net_profit (equity_scope all_parent_equity_holders)" in result.basis
    assert any(
        "accepted net_profit" in note and "explicitly identifies US GAAP" in note
        for note in result.notes
    )
    assert net_income.semantics.source_label == "Net income"
    assert net_income.semantics.source_field == "Net income"
    assert net_income.provenance.accession == "0000320193-25-000079"
    assert result.inputs == (
        "net_profit@2025-09-27",
        "equity_parent@2025-09-27",
        "equity_parent@2024-09-28",
    )


def test_net_profit_alias_accepts_explicit_ordinary_scope_when_equity_matches():
    facts, years = _reviewed_apple_material()
    updated: list[FactRecord] = []
    for fact in facts:
        if (
            fact.metric == "net_profit"
            and fact.period.end == APPLE_END
            or fact.metric == "equity_parent"
            and fact.period.end
            in {
                date(2024, 9, 28),
                APPLE_END,
            }
        ):
            fact = replace(
                fact, semantics=replace(fact.semantics, equity_scope=EquityScope.ORDINARY)
            )
        updated.append(fact)

    result = _latest_roe(tuple(updated), years)

    assert result.status == CalculationStatus.OK
    assert result.value is not None
    assert "equity_scope ordinary_shareholders" in result.basis
    assert any("equity_scope=ordinary_shareholders" in note for note in result.notes)


def test_net_profit_with_unknown_or_total_equity_scope_is_not_used_for_roe():
    facts, years = _reviewed_apple_material()
    original = next(
        fact for fact in facts if fact.metric == "net_profit" and fact.period.end == APPLE_END
    )

    for scope, status in (
        (EquityScope.UNKNOWN, CalculationStatus.MISSING_INPUTS),
        (EquityScope.TOTAL, CalculationStatus.MISSING_INPUTS),
    ):
        changed = replace(
            original,
            semantics=replace(original.semantics, equity_scope=scope),
        )
        variant = tuple(changed if fact is original else fact for fact in facts)
        result = _latest_roe(variant, years)
        assert result.status == status
        assert result.value is None
        assert not any(ref.startswith("net_profit@") for ref in result.inputs)


def test_conflicting_same_scope_profit_aliases_do_not_select_a_numerator():
    facts, years = _reviewed_apple_material()
    net_income = next(
        fact for fact in facts if fact.metric == "net_profit" and fact.period.end == APPLE_END
    )
    competing = replace(
        net_income,
        metric="profit_parent",
        semantics=replace(
            net_income.semantics,
            metric="profit_parent",
            source_label="Profit attributable to parent shareholders",
            source_field="Profit attributable to parent shareholders",
        ),
        value=replace(net_income.value, raw_value="120000"),
    )

    result = _latest_roe((*facts, competing), years)

    assert result.status == CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert any("conflicting same-scope ROE profit aliases" in note for note in result.notes)


def test_without_net_profit_legacy_preference_ignores_lower_preference_conflict():
    facts, years = _reviewed_apple_material()
    net_income = next(
        fact for fact in facts if fact.metric == "net_profit" and fact.period.end == APPLE_END
    )
    parent_profit = replace(
        net_income,
        metric="profit_parent",
        semantics=replace(net_income.semantics, metric="profit_parent"),
    )
    ordinary_one = replace(
        net_income,
        metric="profit_ordinary_shareholders",
        semantics=replace(net_income.semantics, metric="profit_ordinary_shareholders"),
    )
    ordinary_two = replace(
        ordinary_one,
        value=replace(ordinary_one.value, raw_value="120000000"),
    )
    without_alias = tuple(fact for fact in facts if fact is not net_income)

    result = _latest_roe((*without_alias, parent_profit, ordinary_one, ordinary_two), years)

    assert result.status == CalculationStatus.OK
    assert result.inputs[0] == "profit_parent@2025-09-27"


@pytest.mark.parametrize(
    ("change", "mismatch"),
    (
        (lambda fact: replace(fact, value=replace(fact.value, currency="CNY")), "currency/unit"),
        (lambda fact: replace(fact, value=replace(fact.value, unit="CNY")), "currency/unit"),
        (
            lambda fact: replace(
                fact, semantics=replace(fact.semantics, accounting_standard=AccountingStandard.IFRS)
            ),
            "accounting standard",
        ),
        (
            lambda fact: replace(
                fact,
                semantics=replace(fact.semantics, reporting_scope=ReportingScope.PARENT_ONLY),
            ),
            "reporting scope",
        ),
        (
            lambda fact: replace(
                fact,
                period=replace(fact.period, start=date(2024, 9, 27)),
            ),
            "period start/end/basis",
        ),
        (
            lambda fact: replace(
                fact,
                period=replace(fact.period, basis=PeriodBasis.INSTANT),
            ),
            "period start/end/basis",
        ),
    ),
)
def test_same_scope_net_profit_alias_requires_comparable_basis_even_when_amounts_match(
    change, mismatch
):
    facts, years = _reviewed_apple_material()
    net_income = next(
        fact for fact in facts if fact.metric == "net_profit" and fact.period.end == APPLE_END
    )
    direct_profit = replace(
        net_income,
        metric="profit_parent",
        semantics=replace(net_income.semantics, metric="profit_parent"),
    )
    incomparable = change(direct_profit)

    result = _latest_roe((*facts, incomparable), years)

    assert result.status == CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert any("incomparable same-scope ROE profit aliases" in note for note in result.notes)
    assert any(mismatch in note for note in result.notes)


def test_net_profit_alias_requires_us_gaap_scope_and_original_currency():
    facts, years = _reviewed_apple_material()
    original = next(
        fact for fact in facts if fact.metric == "net_profit" and fact.period.end == APPLE_END
    )
    non_us_gaap = replace(
        original,
        semantics=replace(original.semantics, accounting_standard=AccountingStandard.IFRS),
    )
    non_us_facts = tuple(non_us_gaap if fact is original else fact for fact in facts)
    result = _latest_roe(non_us_facts, years)
    assert result.status == CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert any("requires explicit us_gaap" in note for note in result.notes)

    cross_currency = replace(original, value=replace(original.value, currency="CNY"))
    currency_facts = tuple(cross_currency if fact is original else fact for fact in facts)
    currency_result = _latest_roe(currency_facts, years)
    assert currency_result.status == CalculationStatus.NOT_APPLICABLE
    assert currency_result.value is None


def test_bank_and_ads_roe_exclusions_still_apply_to_net_profit_alias():
    facts, years = _reviewed_apple_material()

    for profile_changes in ({"is_bank": True}, {"is_ads": True}):
        result = _latest_roe(facts, years, **profile_changes)
        assert result.status == CalculationStatus.NOT_APPLICABLE
        assert result.value is None
