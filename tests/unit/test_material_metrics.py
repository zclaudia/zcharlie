"""Deterministic financial metrics: rules, applicability and data-discipline tests (plan 08).

Fixture provenance (``tests/fixtures/materials/metrics/pilots.json``)
--------------------------------------------------------------------

* ``companies``: the three HK pilots and three US pilots. The six FY2024 amounts of the
  HK companies (revenue, profit, assets, equity, operating cash flow, fixed-asset cash
  spending) are the values cross-checked against the issuers' own reports in
  ``research/market-data-validation/remaining-validation.md`` section 1 and
  ``evidence/remaining-final-03/derived/native-hk/result.json``; the FY2021-FY2023 rows
  are the saved raw-response values, **not** re-checked against the reports, so they are
  fixture fact ``candidate``s. US values come from the saved company facts (the issuer's
  own filing figures, including the real 52/53-week fiscal-year boundaries) and are
  likewise candidates in this round.
* ``scenarios``: **constructed** fact sets used only to reach rule branches (a true zero
  base, a profit turning to a loss, a 371-day and a 400-day fiscal year, a missing middle
  year, a currency change, four period-end equities without a fifth, a balance sheet
  without total liabilities, and an all-parent-only profit). They are not evidence about
  any issuer and must not be reported as verified facts.

The tests assert statuses, reasons and exact decimals; they never re-derive a metric with
the same formula the module uses unless the plan asks for that internal check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import (
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    SubjectKind,
    Verification,
    fact_from_payload,
    fact_to_payload,
    validate_draft,
)
from investment_agent.materials.metrics import (
    FCF_ELIGIBLE_CAPEX,
    FIXED_GAPS,
    METRIC_KEYS,
    CompanyProfile,
    MetricResult,
    MetricSet,
    _yoy_change,
    compute_metrics,
    profile_from_identity,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "metrics" / "pilots.json"
)
FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
METRICS_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "investment_agent" / "materials" / "metrics.py"
).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Pilot:
    """One fixture fact set: its profile, real fiscal years and facts."""

    profile: CompanyProfile
    fiscal_years: tuple[FactPeriod, ...]
    facts: tuple[FactRecord, ...]

    def compute(self, fiscal_years: tuple[FactPeriod, ...] | None = None) -> MetricSet:
        return compute_metrics(
            facts=self.facts,
            profile=self.profile,
            fiscal_years=self.fiscal_years if fiscal_years is None else fiscal_years,
        )


def _build_fact(entry: dict[str, Any], year: dict[str, Any], block: dict[str, Any]) -> FactRecord:
    label = year["label"]
    raw_value = entry["values"].get(label)
    if entry["basis"] == "instant":
        period = FactPeriod(
            start=None,
            end=date.fromisoformat(year["end"]),
            basis=PeriodBasis.INSTANT,
            label=label,
            days=None,
        )
    else:
        period = FactPeriod(
            start=date.fromisoformat(year["start"]),
            end=date.fromisoformat(year["end"]),
            basis=PeriodBasis.ANNUAL,
            label=label,
            days=None,
        )
    unit = entry.get("currency_override", entry["unit"])
    currency = entry.get("currency_override", block["identity"]["report_currency"])
    published = block.get("source_published_date")
    record = FactRecord(
        metric=entry["metric"],
        period=period,
        semantics=FactSemantics(
            metric=entry["metric"],
            source_label=entry["source_labels"][label],
            source_field=None,
            accounting_standard=AccountingStandard(block["accounting_standard"]),
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope(entry["equity_scope"]),
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=None if raw_value is None else str(raw_value),
            raw_unit=unit,
            scale=entry["scale"],
            unit=unit,
            currency=currency,
            sign_convention=entry["sign_convention"],
            missing_reason=entry.get("missing_reason") if raw_value is None else None,
        ),
        provenance=FactProvenance(
            source=block["source"],
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=date.fromisoformat(published) if published else None,
            source_published_precision=DatePrecision.DATE if published else DatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=(
            Verification.VERIFIED
            if label in entry.get("verified_years", [])
            else Verification.CANDIDATE
        ),
        conflict="none",
        applicability=entry.get("applicability", "applicable"),
        conflict_note=None,
        original_locator=None,
        source_version=None,
    )
    # Round-trip through the frozen contract serializers so the fixture cannot carry a
    # fact shape or enum value the contract would reject.
    return fact_from_payload(fact_to_payload(record))


def _load(block: dict[str, Any]) -> Pilot:
    identity = block["identity"]
    profile = CompanyProfile(
        company_key=block.get("company_key", "fixture"),
        is_bank=identity["is_bank"],
        is_ads=identity["is_ads"],
        ads_ratio=identity["ads_ratio"],
        report_currency=identity["report_currency"],
        quote_currency=identity["quote_currency"],
        fiscal_year_end_note=identity["fiscal_year_end_note"],
    )
    years = tuple(
        FactPeriod(
            start=date.fromisoformat(year["start"]) if year.get("start") else None,
            end=date.fromisoformat(year["end"]),
            basis=PeriodBasis.ANNUAL,
            label=year["label"],
            days=None,
        )
        for year in block["fiscal_years"]
    )
    facts = tuple(
        _build_fact(entry, year, block)
        for entry in block["facts"]
        for year in block["fiscal_years"]
        if year["label"] in entry["values"]
    )
    return Pilot(profile=profile, fiscal_years=years, facts=facts)


def pilot(key: str) -> Pilot:
    block = dict(FIXTURE["companies"][key])
    block["company_key"] = key
    return _load(block)


def scenario(key: str) -> Pilot:
    block = dict(FIXTURE["scenarios"][key])
    block["company_key"] = f"scenario-{key}"
    return _load(block)


def picks(results: MetricSet, key: str) -> list[MetricResult]:
    return [result for result in results.metrics if result.key == key]


def at_end(results: MetricSet, key: str, end: str) -> MetricResult:
    matches = [
        result for result in picks(results, key) if result.period_end == date.fromisoformat(end)
    ]
    assert len(matches) == 1, f"expected one {key} result for {end}, got {len(matches)}"
    return matches[0]


def text_of(result: MetricResult) -> str:
    return " | ".join(result.notes)


# --- fixture integrity ---------------------------------------------------------


def test_fixture_2024_amounts_match_the_verified_table() -> None:
    """The six cross-checked FY2024 amounts of the three HK pilots, in the source unit."""

    expected = {
        "hsbc-holdings": [
            "62440000000",
            "22917000000",
            "3017048000000",
            "184973000000",
            "65305000000",
            "1344000000",
        ],
        "clp-holdings": [
            "90964000000",
            "11742000000",
            "233713000000",
            "104055000000",
            "23140000000",
            "15076000000",
        ],
        "tencent-holdings": [
            "660257000000",
            "194073000000",
            "1780995000000",
            "973548000000",
            "258521000000",
            "62927000000",
        ],
    }
    for key, amounts in expected.items():
        facts = pilot(key).facts
        assert amounts[0] in [fact.value.raw_value for fact in facts if "revenue" in fact.metric]
        assert amounts[1] in [fact.value.raw_value for fact in facts if "profit" in fact.metric]
        assert amounts[2] in [
            fact.value.raw_value for fact in facts if fact.metric == "total_assets"
        ]
        assert amounts[3] in [
            fact.value.raw_value for fact in facts if fact.metric == "equity_parent"
        ]
        assert amounts[4] in [
            fact.value.raw_value for fact in facts if fact.metric == "operating_cash_flow"
        ]
        assert amounts[5] in [fact.value.raw_value for fact in facts if fact.metric == "capex_ppe"]


@pytest.mark.parametrize(
    "key",
    [
        "hsbc-holdings",
        "clp-holdings",
        "tencent-holdings",
        "apple",
        "jpmorgan-chase",
        "tsmc",
    ],
)
def test_fixture_facts_are_contract_valid(key: str) -> None:
    """Every fixture company builds a storable draft: units, periods, values all valid."""

    block = FIXTURE["companies"][key]
    fixture_pilot = pilot(key)
    draft = DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=1,
        source=block["source"],
        schema_version=1,
        adapter_version="fixture.v1",
        captured_at=datetime(2026, 10, 12, tzinfo=UTC),
        payload={"facts": [fact_to_payload(fact) for fact in fixture_pilot.facts]},
    )
    assert validate_draft(draft) == ()


@pytest.mark.parametrize("key", ["revenue_base_zero", "profit_turned_to_loss", "cagr_52_53_weeks"])
def test_scenario_facts_are_contract_valid(key: str) -> None:
    block = FIXTURE["scenarios"][key]
    fixture_pilot = scenario(key)
    draft = DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=1,
        source=block["source"],
        schema_version=1,
        adapter_version="fixture.v1",
        captured_at=datetime(2026, 10, 12, tzinfo=UTC),
        payload={"facts": [fact_to_payload(fact) for fact in fixture_pilot.facts]},
    )
    assert validate_draft(draft) == ()


def test_fixture_identities_match_the_controlled_pilot_list() -> None:
    """Fixture profiles are mapped from task 01's identity list, not invented."""

    from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities

    identities = {
        identity.company_key: identity
        for identity in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    }
    # The identity list carries no is_bank field, so the flag is the hand-set input the
    # caller must state; these two issuers are the design's bank samples.
    bank_keys = {"hsbc-holdings", "jpmorgan-chase"}
    for key, block in FIXTURE["companies"].items():
        identity = identities[key]
        is_bank = block["identity"]["is_bank"]
        assert is_bank is (key in bank_keys), key
        profile = profile_from_identity(identity, is_bank=is_bank)
        assert profile.company_key == key
        assert profile.report_currency == block["identity"]["report_currency"]
        assert profile.quote_currency == block["identity"]["quote_currency"]
        assert profile.is_ads == block["identity"]["is_ads"]
        assert profile.ads_ratio == block["identity"]["ads_ratio"] == identity.ads_ratio
        assert profile.fiscal_year_end_note == block["identity"]["fiscal_year_end_note"]
    assert profile_from_identity(identities["tsmc"], is_bank=False).is_ads is True
    assert profile_from_identity(identities["hsbc-holdings"], is_bank=True).is_ads is False


def test_result_shape_and_basis_are_complete() -> None:
    """Every result has a full basis, traceable inputs and a currency-free ratio unit."""

    for key in ("clp-holdings", "tencent-holdings", "apple", "jpmorgan-chase", "tsmc"):
        fixture_pilot = pilot(key)
        results = fixture_pilot.compute()
        assert [result.key for result in results.metrics[:2]] == ["revenue_yoy", "revenue_cagr_3y"]
        for result in results.metrics:
            assert result.key in METRIC_KEYS
            assert result.label
            assert result.basis, f"{result.key} basis must never be empty"
            assert "original reporting currency" in result.basis
            assert "no display conversion" in result.basis
            assert all("@" in ref for ref in result.inputs)
            if result.value is None:
                assert result.status is not CalculationStatus.OK
            else:
                assert result.status is CalculationStatus.OK
                assert result.value.unknown_reason is None
                if result.key == "fcf_simple":
                    assert result.value.unit == fixture_pilot.profile.report_currency
                    assert result.value.currency == fixture_pilot.profile.report_currency
                else:
                    assert result.value.unit == "ratio"
                    assert result.value.currency is None


# --- revenue rules -------------------------------------------------------------


def test_revenue_yoy_and_cagr_for_clp() -> None:
    """CLP: the FY2024 ratio and the four-year CAGR are the exact arithmetic results."""

    fixture_pilot = pilot("clp-holdings")
    results = fixture_pilot.compute()
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert yoy.status is CalculationStatus.OK
    assert yoy.value is not None
    assert yoy.value.value == (Decimal("90964000000") - Decimal("87169000000")) / Decimal(
        "87169000000"
    )
    assert "revenue_operating" in yoy.basis
    assert yoy.period_end == date(2024, 12, 31)

    cagr = at_end(results, "revenue_cagr_3y", "2024-12-31")
    assert cagr.status is CalculationStatus.OK
    assert cagr.value is not None
    expected = (Decimal("90964000000") / Decimal("83959000000")) ** (Decimal(1) / Decimal(3)) - 1
    assert abs(cagr.value.value - expected) <= Decimal("1e-12")
    assert cagr.inputs[0].startswith("revenue_operating@2021-12-31")
    assert len(cagr.periods) == 4


def test_hsbc_uses_the_net_credit_revenue_series() -> None:
    """HSBC has only the after-credit-loss revenue, and the basis names it."""

    results = pilot("hsbc-holdings").compute()
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert yoy.status is CalculationStatus.OK
    assert "revenue_net_credit" in yoy.basis
    assert "revenue_operating" not in yoy.basis


def test_revenue_yoy_with_zero_base_gives_the_amount_change() -> None:
    """A true zero prior period never becomes a growth rate."""

    results = scenario("revenue_base_zero").compute()
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert yoy.status is CalculationStatus.NOT_APPLICABLE
    assert yoy.value is None
    notes = text_of(yoy)
    assert "prior period is zero" in notes
    assert "61000000000" in notes
    assert "instead of a ratio" in notes


def test_revenue_yoy_requires_adjacent_complete_years() -> None:
    """A two-year gap is missing input, not a growth rate."""

    fixture_pilot = pilot("clp-holdings")
    years = (fixture_pilot.fiscal_years[0], fixture_pilot.fiscal_years[3])
    results = fixture_pilot.compute(fiscal_years=years)
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert yoy.status is CalculationStatus.MISSING_INPUTS
    assert yoy.value is None
    assert "not adjacent" in text_of(yoy)


# --- CAGR branches -------------------------------------------------------------


def test_cagr_missing_middle_year_is_named() -> None:
    """A dropped middle-year fact names the missing year instead of interpolating."""

    results = scenario("cagr_missing_year").compute()
    cagr = at_end(results, "revenue_cagr_3y", "2024-12-31")
    assert cagr.status is CalculationStatus.MISSING_INPUTS
    assert cagr.value is None
    notes = text_of(cagr)
    assert "revenue_operating is missing at 2023-12-31" in notes


def test_cagr_year_dropped_from_the_fiscal_year_list_is_named() -> None:
    """A hole inside a four-entry window names the missing fiscal year span."""

    fixture_pilot = pilot("clp-holdings")
    years = tuple(year for year in fixture_pilot.fiscal_years if year.label != "FY2023")
    fabricated = FactPeriod(
        start=date(2020, 1, 1),
        end=date(2020, 12, 31),
        basis=PeriodBasis.ANNUAL,
        label="FY2020",
        days=None,
    )
    results = fixture_pilot.compute(fiscal_years=(fabricated,) + years)
    cagr = at_end(results, "revenue_cagr_3y", "2024-12-31")
    assert cagr.status is CalculationStatus.MISSING_INPUTS
    assert cagr.value is None
    notes = text_of(cagr)
    assert "no complete fiscal year between 2022-12-31 and 2024-01-01" in notes
    assert "FY2022 to FY2024 is not consecutive" in notes


def test_cagr_52_53_week_window_is_ok_with_a_note() -> None:
    """364 against 371 days is a 52/53-week difference, not a length change."""

    results = scenario("cagr_52_53_weeks").compute()
    cagr = at_end(results, "revenue_cagr_3y", "2025-01-06")
    assert cagr.status is CalculationStatus.OK
    assert cagr.value is not None
    assert "52/53-week fiscal year; lengths 364/364/364/371 days" in text_of(cagr)


def test_cagr_fiscal_length_change_is_not_applicable() -> None:
    """A 400-day year against 364 days is a length change, so no annualised rate."""

    results = scenario("cagr_length_jump").compute()
    cagr = at_end(results, "revenue_cagr_3y", "2025-02-04")
    assert cagr.status is CalculationStatus.NOT_APPLICABLE
    assert cagr.value is None
    notes = text_of(cagr)
    assert "fiscal year length changed" in notes
    assert "364/364/364/400" in notes


def test_cagr_uses_the_real_apple_52_53_week_boundaries() -> None:
    """Apple's own 52/53-week spans (363/363/370/363 days) keep their true length."""

    fixture_pilot = pilot("apple")
    results = fixture_pilot.compute()
    cagr = at_end(results, "revenue_cagr_3y", "2024-09-28")
    assert cagr.status is CalculationStatus.OK
    assert "52/53-week fiscal year; lengths 363/363/370/363 days" in text_of(cagr)
    yoy = at_end(results, "revenue_yoy", "2024-09-28")
    assert yoy.status is CalculationStatus.OK
    assert "period lengths differ (370 vs 363 days" in text_of(yoy)


# --- bank and ADS applicability ------------------------------------------------


def test_hsbc_bank_applicability_rules() -> None:
    """HSBC is a bank: ROE and CFO/profit are refused, debt ratio is shown with a caveat."""

    fixture_pilot = pilot("hsbc-holdings")
    results = fixture_pilot.compute()
    for result in picks(results, "roe_avg_equity") + picks(results, "cfo_to_profit"):
        assert result.status is CalculationStatus.NOT_APPLICABLE
        assert result.value is None
        assert "bank" in text_of(result)

    debt = at_end(results, "debt_ratio", "2024-12-31")
    assert debt.status is CalculationStatus.OK
    assert "bank" in text_of(debt)
    assert debt.value is not None
    assert debt.value.unit == "ratio" and debt.value.currency is None
    assert debt.value.value == Decimal("2824775000000") / Decimal("3017048000000")

    fcf = at_end(results, "fcf_simple", "2024-12-31")
    assert fcf.status is CalculationStatus.NOT_APPLICABLE
    assert fcf.value is None
    assert "net purchases and sales" in text_of(fcf)
    assert "bank" in text_of(fcf)


def test_roe_average_equity_definition_and_scope() -> None:
    """CLP FY2024 ROE uses the two period-end equities and states the average definition."""

    results = pilot("clp-holdings").compute()
    roe = at_end(results, "roe_avg_equity", "2024-12-31")
    assert roe.status is CalculationStatus.OK
    assert roe.value is not None
    average = (Decimal("104055000000") + Decimal("102331000000")) / 2
    assert roe.value.value == Decimal("11742000000") / average
    assert "average of two period ends" in roe.basis
    assert "numerator profit_parent" in roe.basis
    assert "denominator equity_parent at the two period ends" in roe.basis
    assert "@2023-12-31" in " ".join(roe.inputs)
    assert len(roe.periods) == 3
    assert roe.periods[0].end == date(2023, 12, 31)

    first_year = at_end(results, "roe_avg_equity", "2021-12-31")
    assert first_year.status is CalculationStatus.MISSING_INPUTS
    assert first_year.value is None
    assert "beginning equity unavailable; end-only equity is not substituted" in text_of(first_year)


def test_roe_needs_the_period_end_before_the_year() -> None:
    """Four period-end equities give three returns; the first year has no substitute."""

    fixture_pilot = scenario("roe_needs_n_plus_1")
    results = fixture_pilot.compute()
    missing = at_end(results, "roe_avg_equity", "2021-12-31")
    assert missing.status is CalculationStatus.MISSING_INPUTS
    assert missing.value is None
    assert "2020-12-31" in text_of(missing)
    for end in ("2022-12-31", "2023-12-31", "2024-12-31"):
        result = at_end(results, "roe_avg_equity", end)
        assert result.status is CalculationStatus.OK
        assert result.value is not None


def test_tsm_ads_roe_is_not_applicable_but_other_metrics_are_allowed() -> None:
    """ADS blocks only the ordinary-share return; the balance and cash metrics still run."""

    fixture_pilot = pilot("tsmc")
    results = fixture_pilot.compute()
    for result in picks(results, "roe_avg_equity"):
        assert result.status is CalculationStatus.NOT_APPLICABLE
        assert "ADS" in text_of(result)
        assert result.value is None
    debt = at_end(results, "debt_ratio", "2024-12-31")
    assert debt.status is CalculationStatus.OK
    assert debt.value is not None
    assert debt.value.value == Decimal("2412493100000") / Decimal("6691764700000")
    fcf = at_end(results, "fcf_simple", "2024-12-31")
    assert fcf.status is CalculationStatus.OK
    assert fcf.value is not None
    assert fcf.value.currency == "TWD"
    assert fixture_pilot.profile.ads_ratio == "5"


# --- capex equivalence and FCF -------------------------------------------------


def test_clp_fcf_is_operating_cash_flow_minus_capex() -> None:
    """The whitelisted CLP capex convention gives an exact four-year difference."""

    fixture_pilot = pilot("clp-holdings")
    results = fixture_pilot.compute()
    for fy in ("FY2021", "FY2022", "FY2023", "FY2024"):
        year_facts = {fact.metric: fact for fact in fixture_pilot.facts if fact.period.label == fy}
        result = at_end(results, "fcf_simple", year_facts["capex_ppe"].period.end.isoformat())
        assert result.status is CalculationStatus.OK
        assert result.value is not None
        expected = Decimal(year_facts["operating_cash_flow"].value.raw_value) - Decimal(
            year_facts["capex_ppe"].value.raw_value
        )
        assert result.value.value == expected


def test_clp_fcf_2024_value() -> None:
    assert frozenset({"capital expenditure"}) == FCF_ELIGIBLE_CAPEX
    result = at_end(pilot("clp-holdings").compute(), "fcf_simple", "2024-12-31")
    assert result.value is not None
    assert (
        result.value.value
        == Decimal("23140000000") - Decimal("15076000000")
        == Decimal("8064000000")
    )


def test_tencent_capex_definition_is_rejected_by_name() -> None:
    results = pilot("tencent-holdings").compute()
    fcf = at_end(results, "fcf_simple", "2024-12-31")
    assert fcf.status is CalculationStatus.NOT_APPLICABLE
    assert fcf.value is None
    notes = text_of(fcf)
    assert "prepayments incl. construction in progress / investment property" in notes
    assert "construction in progress" in notes


def test_jpm_missing_capex_is_missing_inputs_never_zero() -> None:
    """JPM has no capital-expenditure fact: missing input, not zero, not not-applicable."""

    results = pilot("jpmorgan-chase").compute()
    for result in picks(results, "fcf_simple"):
        assert result.status is CalculationStatus.MISSING_INPUTS
        assert result.value is None
        assert "never treated as zero" in text_of(result)
        assert "capital-expenditure tag is present" in text_of(result)


def test_cash_conversion_ratio_uses_the_ordinary_shareholders_profit() -> None:
    """The ratio divides by the ordinary-shareholders profit for every non-bank pilot."""

    clp = at_end(pilot("clp-holdings").compute(), "cfo_to_profit", "2024-12-31")
    assert clp.status is CalculationStatus.OK
    assert clp.value is not None
    assert clp.value.value == Decimal("23140000000") / Decimal("11742000000")
    assert clp.value.unit == "ratio" and clp.value.currency is None
    assert "numerator operating_cash_flow, denominator profit_ordinary_shareholders" in clp.basis

    apple = at_end(pilot("apple").compute(), "cfo_to_profit", "2024-09-28")
    assert apple.status is CalculationStatus.OK
    assert apple.value is not None
    assert apple.value.value == Decimal("118254000000") / Decimal("93736000000")

    tsm = at_end(pilot("tsmc").compute(), "cfo_to_profit", "2024-12-31")
    assert tsm.status is CalculationStatus.OK
    assert tsm.value is not None
    assert tsm.value.value == Decimal("1826177100000") / Decimal("1158380200000")


def test_parent_profit_with_ordinary_scope_is_the_same_definition() -> None:
    """A parent-keyed profit already scoped to ordinary shareholders is accepted, and said so."""

    results = scenario("cash_profit_parent_ordinary_scope").compute()
    ratio = at_end(results, "cfo_to_profit", "2024-12-31")
    assert ratio.status is CalculationStatus.OK
    assert ratio.value is not None
    assert ratio.value.value == Decimal(2)
    assert "same definition as the ordinary-shareholders profit" in text_of(ratio)


def test_apple_fcf_uses_the_payments_to_acquire_ppe_convention() -> None:
    results = pilot("apple").compute()
    fcf = at_end(results, "fcf_simple", "2024-09-28")
    assert fcf.status is CalculationStatus.OK
    assert fcf.value is not None
    assert fcf.value.value == Decimal("118254000000") - Decimal("9447000000")


# --- data discipline -----------------------------------------------------------


def test_profit_turned_to_loss_stops_the_cash_conversion_ratio() -> None:
    results = scenario("profit_turned_to_loss").compute()
    current = at_end(results, "cfo_to_profit", "2024-12-31")
    assert current.status is CalculationStatus.NOT_APPLICABLE
    assert current.value is None
    notes = text_of(current)
    assert "turned to loss" in notes and "loss" in notes
    assert "1200000000" in notes and "-450000000" in notes
    prior = at_end(results, "cfo_to_profit", "2023-12-31")
    assert prior.status is CalculationStatus.OK
    assert prior.value is not None
    assert prior.value.value == Decimal("2000000000") / Decimal("1200000000")


def test_yoy_change_rule_for_a_profit_series() -> None:
    """The shared year-over-year rule states a loss instead of a negative-base ratio."""

    value, status, notes = _yoy_change(
        current=Decimal("-450000000"),
        previous=Decimal("1200000000"),
        current_label="FY2024",
        previous_label="FY2023",
        series="profit_ordinary_shareholders",
        profit_series=True,
    )
    assert value is None
    assert status is CalculationStatus.NOT_APPLICABLE
    assert "turned to loss" in notes[0]
    assert "1200000000" in notes[0] and "-450000000" in notes[0]

    negative_base, status, notes = _yoy_change(
        current=Decimal("1000"),
        previous=Decimal("-5"),
        current_label="FY2024",
        previous_label="FY2023",
        series="profit_ordinary_shareholders",
        profit_series=True,
    )
    assert negative_base is None
    assert status is CalculationStatus.NOT_APPLICABLE
    assert "prior period is negative" in notes[0]


def test_currency_change_is_not_converted() -> None:
    """A year reported in another currency stops both the ratio and the trend."""

    results = scenario("currency_changed").compute()
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert yoy.status is CalculationStatus.NOT_APPLICABLE
    assert yoy.value is None
    assert "reporting currency changed" in text_of(yoy)
    cagr = at_end(results, "revenue_cagr_3y", "2024-12-31")
    assert cagr.status is CalculationStatus.NOT_APPLICABLE
    assert cagr.value is None
    assert "reporting currency changed" in text_of(cagr)
    # the offending year is named, no converted value appears anywhere
    assert "revenue_operating@2024-12-31 is reported in CNY" in text_of(cagr)
    assert cagr.inputs == (
        "revenue_operating@2021-12-31",
        "revenue_operating@2022-12-31",
        "revenue_operating@2023-12-31",
        "revenue_operating@2024-12-31",
    )


def test_debt_ratio_is_not_derived_when_liabilities_are_missing() -> None:
    """A balance sheet without total liabilities yields missing input, not a back-out."""

    results = scenario("debt_ratio_without_liabilities").compute()
    debt = at_end(results, "debt_ratio", "2024-12-31")
    assert debt.status is CalculationStatus.MISSING_INPUTS
    assert debt.value is None
    notes = text_of(debt)
    assert "not derived from total assets minus equity" in notes
    assert "interest-bearing-debt ratio is not a substitute" in notes


def test_all_parent_profit_does_not_replace_the_ordinary_figure() -> None:
    """Only an ordinary-shareholders profit (or the identical parent scope) is used."""

    results = scenario("cash_profit_parent_only").compute()
    ratio = at_end(results, "cfo_to_profit", "2024-12-31")
    assert ratio.status is CalculationStatus.MISSING_INPUTS
    assert ratio.value is None
    notes = text_of(ratio)
    assert "not the ordinary-shareholders profit" in notes
    assert "not substituted" in notes


def test_candidate_facts_are_flagged_in_the_notes() -> None:
    """Years not yet checked against the issuer report are visible as candidates."""

    results = pilot("clp-holdings").compute()
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert "candidates (not verified against the issuer's report)" in text_of(yoy)
    rows = [fact for fact in pilot("clp-holdings").facts]
    assert any(fact.verification is Verification.VERIFIED for fact in rows)
    assert any(fact.verification is Verification.CANDIDATE for fact in rows)


def test_conflicting_facts_are_reported_and_not_picked() -> None:
    """Two different revenue facts for one year are a conflict, never an average."""

    fixture_pilot = pilot("clp-holdings")
    template = next(
        fact
        for fact in fixture_pilot.facts
        if fact.metric == "revenue_operating" and fact.period.end == date(2024, 12, 31)
    )
    conflicting = FactRecord(
        metric=template.metric,
        period=template.period,
        semantics=template.semantics,
        value=FactValue(
            raw_value="123456789",
            raw_unit="HKD",
            scale=1,
            unit="HKD",
            currency="HKD",
            sign_convention=template.value.sign_convention,
        ),
        provenance=template.provenance,
        verification=Verification.CANDIDATE,
        conflict="unresolved",
        applicability="applicable",
        conflict_note="two sources disagree",
    )
    results = compute_metrics(
        facts=fixture_pilot.facts + (conflicting,),
        profile=fixture_pilot.profile,
        fiscal_years=fixture_pilot.fiscal_years,
    )
    yoy = at_end(results, "revenue_yoy", "2024-12-31")
    assert yoy.status is CalculationStatus.MISSING_INPUTS
    assert yoy.value is None
    assert "multiple distinct revenue_operating facts at 2024-12-31" in text_of(yoy)
    assert "not averaged or picked" in text_of(yoy)


def test_gaps_state_the_fixed_margin_holes_and_every_reason() -> None:
    """Gaps carry the two fixed margin statements plus each non-OK metric reason."""

    results = pilot("hsbc-holdings").compute()
    for fixed in FIXED_GAPS:
        assert fixed in results.gaps
    joined = " ".join(results.gaps)
    assert "gross_margin:" in joined and "net_margin:" in joined
    assert "roe_avg_equity" in joined and "not_applicable" in joined
    assert "fcf_simple" in joined and "bank" in joined
    assert "net purchases and sales" in text_of(at_end(results, "fcf_simple", "2024-12-31"))
    assert all(gap.strip() for gap in results.gaps)

    complete = scenario("roe_needs_n_plus_1").compute()
    assert any("roe_avg_equity (2021-12-31): missing_inputs" in gap for gap in complete.gaps)
    assert sum(1 for gap in complete.gaps if gap.startswith("roe_avg_equity")) == 1


def test_module_is_pure_and_free_of_advice_wording() -> None:
    """No IO/network/database imports, no trading advice, no equity-FCF naming."""

    forbidden_imports = (
        "import httpx",
        "import requests",
        "import aiohttp",
        "import sqlalchemy",
        "import socket",
        "import urllib",
        "import psycopg",
        "import asyncpg",
        "from sqlalchemy",
        "from httpx",
        "from requests",
    )
    for needle in forbidden_imports:
        assert needle not in METRICS_SOURCE, needle
    for word in ("买入", "卖出", "评分", "推荐", "建议"):
        assert word not in METRICS_SOURCE, word
    assert "FCFE" not in METRICS_SOURCE
    assert "not an equity" in METRICS_SOURCE
