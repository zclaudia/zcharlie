"""The checked-in review keeps the original facts beside its audited choices."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.annual_validation import _validate_conflict_resolutions
from investment_agent.materials.contracts import (
    FactPeriod,
    PeriodBasis,
    fact_from_payload,
    fact_to_payload,
)
from investment_agent.materials.equity_reconciliation import evaluate_equity_reconciliation
from investment_agent.materials.equity_scope_review import evaluate_equity_scope_review
from investment_agent.materials.metrics import compute_metrics, profile_from_identity

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "config/research_material/annual_validation_2026-10-15.json"
)


def _companies():
    return {row["company_key"]: row for row in json.loads(MANIFEST.read_text())["companies"]}


@pytest.mark.parametrize("key", ["apple", "costco", "hsbc-holdings"])
def test_closed_company_facts_round_trip_and_core_metrics_accept_reviewed_inputs(key):
    company = _companies()[key]
    identities = {item.company_key: item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)}
    facts = {}
    periods = []
    for year in company["fiscal_years"]:
        periods.append(
            FactPeriod(
                start=date.fromisoformat(year["period_start"]),
                end=date.fromisoformat(year["period_end"]),
                basis=PeriodBasis.ANNUAL,
                label=year["label"],
            )
        )
        for field in [*year["facts"], year["beginning_equity_fact"]]:
            fact = fact_from_payload(field["fact"])
            assert fact_to_payload(fact) == field["fact"]
            facts[(fact.metric, fact.period.end)] = fact
    computed = compute_metrics(
        facts=tuple(facts.values()),
        fiscal_years=periods,
        profile=profile_from_identity(identities[key], is_bank=key == "hsbc-holdings"),
    )
    core = [
        item
        for item in computed.metrics
        if item.key in {"roe_avg_equity", "revenue_yoy", "revenue_cagr_3y"}
    ]
    assert len(core) == 6
    assert all(
        item.status == CalculationStatus.OK
        or (
            key == "hsbc-holdings"
            and item.key == "roe_avg_equity"
            and item.status == CalculationStatus.NOT_APPLICABLE
        )
        for item in core
    )


def test_hsbc_derived_opening_is_distinct_from_preserved_ifrs4_original():
    company = _companies()["hsbc-holdings"]
    result = evaluate_equity_reconciliation(company["equity_reconciliations"][0])
    opening = company["fiscal_years"][0]["beginning_equity_fact"]
    assert opening["fact"]["value"]["raw_value"] == result["output"]["value"] == "166601"
    assert opening["calculation_ref"] == result["calculation_ref"]
    assert opening["fact"]["provenance"]["source"] == "material.equity_reconciliation"
    assert opening["evidence"]["kind"] == "derived_calculation"
    assert "document_id" not in opening["evidence"]
    assert company["superseded_facts"][0]["fact"]["value"]["raw_value"] == "175836"
    assert company["superseded_facts"][0]["fact"]["provenance"]["source"] == "issuer.annual_report"
    assert company["conflicts"][0]["resolution_ref"] == result["calculation_ref"]


def test_costco_scope_choices_keep_total_equity_and_nci_audit():
    company = _companies()["costco"]
    result = evaluate_equity_scope_review(company["equity_scope_reviews"][0])
    assert result["review_ref"] == company["conflicts"][0]["resolution_ref"]
    assert {field["fact"]["value"]["raw_value"] for field in company["superseded_facts"]} == {
        "20647",
        "25058",
    }
    assert all(field["fact"]["metric"] == "total_equity" for field in company["superseded_facts"])
    first = company["fiscal_years"][0]
    assert first["beginning_equity_fact"]["fact"]["value"]["raw_value"] == "20642"
    assert first["beginning_equity_fact"]["fact"]["metric"] == "equity_parent"
    assert (
        next(field for field in first["facts"] if field["category"] == "equity")["fact"]["metric"]
        == "equity_parent"
    )
    assert [period["nci"]["amount"] for period in result["inputs"]["periods"]] == [5, 0]


@pytest.mark.parametrize("changed", ["none", "period", "affected_period_ends", "company_key"])
def test_scope_conflict_resolution_is_bound_to_its_company_and_periods(changed):
    company = _companies()["costco"]
    result = evaluate_equity_scope_review(company["equity_scope_reviews"][0])
    if changed == "company_key":
        company[changed] = "apple"
    elif changed == "period":
        company["conflicts"][0][changed] = "unrelated fiscal year"
    elif changed == "affected_period_ends":
        company["conflicts"][0][changed] = ["2021-08-29"]
    unresolved, issues, summaries = _validate_conflict_resolutions(
        company, {}, set(), {}, {result["review_id"]: result}
    )
    assert unresolved is (changed != "none")
    assert bool(issues) is (changed != "none")
    assert bool(summaries) is (changed == "none")
