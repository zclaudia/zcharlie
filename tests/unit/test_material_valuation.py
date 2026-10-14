"""Annual PE arithmetic and gating (plan 13): constructed probes, not issuer evidence.

Every EPS fact, price and FX record below is **constructed** for the acceptance
table's rule branches. None of them is evidence about an issuer's reported figures
and none may be quoted as one. The one recorded datum is Apple's 4-for-1 split with
2020-08-31 as the first split-adjusted trading day
(``research/market-data-validation/remaining-validation.md``, the source noted in
the module constant), used with its own date and ratio.

The tests assert statuses, note wording, exact decimal ratios and the closed
capability bits; no second formula re-derives the ratio.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.context import CONDITIONAL_CAPABILITIES, _capabilities
from investment_agent.materials.contracts import (
    AccountingStandard,
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
    Verification,
    fact_from_payload,
    fact_to_payload,
)
from investment_agent.materials.metrics import CompanyProfile, MetricResult
from investment_agent.materials.valuation import (
    CAPABILITY,
    DISABLED_REASONS,
    LABELS,
    SPLIT_EVENTS,
    UNSUPPORTED_GAPS,
    FxInput,
    PriceInput,
    ads_pe_gap,
    annual_pe,
)
from tests.support.purity import assert_no_io_imports, text_of_strings_and_docstrings

VALUATION_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "investment_agent" / "materials" / "valuation.py"
)


# --- constructed inputs ---------------------------------------------------------


def _eps(
    value: str | None,
    *,
    basis: PerShareBasis = PerShareBasis.BASIC,
    metric: str | None = None,
    currency: str = "USD",
    start: str = "2020-09-27",
    end: str = "2021-09-25",
    label: str = "FY2021",
) -> FactRecord:
    """One constructed EPS fact, round-tripped through the frozen contract serializers."""

    name = metric
    if name is None:
        name = "eps_diluted" if basis is PerShareBasis.DILUTED else "eps_basic"
    unit = f"{currency}/share" if name.startswith("eps_") else currency
    record = FactRecord(
        metric=name,
        period=FactPeriod(
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            basis=PeriodBasis.ANNUAL,
            label=label,
            days=None,
        ),
        semantics=FactSemantics(
            metric=name,
            source_label="constructed probe (not issuer evidence)",
            source_field=None,
            accounting_standard=AccountingStandard.US_GAAP,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.ORDINARY,
            per_share_basis=basis,
        ),
        value=FactValue(
            raw_value=value,
            raw_unit=unit,
            scale=1,
            unit=unit,
            currency=currency,
            sign_convention="as reported",
            missing_reason=None if value is not None else "not stated in the constructed probe",
        ),
        provenance=FactProvenance(
            source="constructed-probe",
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=date.fromisoformat(end),
            source_published_precision=DatePrecision.DATE,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.CANDIDATE,
        conflict="none",
        applicability="applicable",
    )
    return fact_from_payload(fact_to_payload(record))


def _profile(
    *,
    company_key: str = "apple",
    is_ads: bool = False,
    ads_ratio: str | None = None,
    report_currency: str = "USD",
    quote_currency: str = "USD",
) -> CompanyProfile:
    return CompanyProfile(
        company_key=company_key,
        is_bank=False,
        is_ads=is_ads,
        ads_ratio=ads_ratio,
        report_currency=report_currency,
        quote_currency=quote_currency,
        fiscal_year_end_note="constructed probe profile, not an issuer statement",
    )


def _price(
    close: str,
    *,
    trade_date: str = "2021-10-04",
    currency: str = "USD",
    is_split_adjusted: bool = True,
    share_basis: str | None = None,
) -> PriceInput:
    return PriceInput(
        close=Decimal(close),
        trade_date=date.fromisoformat(trade_date),
        currency=currency,
        share_basis=share_basis if share_basis is not None else _basis_name(is_split_adjusted),
        is_split_adjusted=is_split_adjusted,
    )


def _basis_name(is_split_adjusted: bool) -> str:
    return "split_adjusted" if is_split_adjusted else "as_traded"


def _fx(
    rate: str = "0.25",
    *,
    as_of: str,
    pair: str = "TWD/USD",
    precision: str = "date",
) -> FxInput:
    return FxInput(
        pair=pair,
        rate=Decimal(rate),
        as_of=date.fromisoformat(as_of),
        source="constructed-probe (not a real quoted rate)",
        source_date_precision=precision,
    )


APPLE_PROFILE = _profile()
FY2021_BASIC = _eps("6")
FY2021_DILUTED = _eps("5", basis=PerShareBasis.DILUTED)
# Apple's FY2019 ended before the recorded 2020-08-31 split; the period and its
# EPS amount are constructed, the split date and ratio are the recorded constants.
FY2019_BASIC = _eps("5", start="2018-09-29", end="2019-09-28", label="FY2019")
TSM_EPS = _eps(
    "40",
    basis=PerShareBasis.DILUTED,
    currency="TWD",
    start="2024-01-01",
    end="2024-12-31",
    label="FY2024",
)
TSM_PROFILE = _profile(
    company_key="tsmc",
    is_ads=True,
    ads_ratio="5",
    report_currency="TWD",
    quote_currency="USD",
)
# A TWD-reporting, USD-quoting non-ADS probe: only the currency branch is exercised.
TWD_PROFILE = _profile(
    company_key="constructed-twd-quoted",
    report_currency="TWD",
    quote_currency="USD",
)
USD_PRICE_2024 = _price("100", trade_date="2024-01-05")


def _text(result: MetricResult) -> str:
    return " ".join((result.key, result.label, result.basis, *result.notes))


# --- basic/diluted naming -------------------------------------------------------


def test_basic_and_diluted_are_separate_names_on_the_same_inputs() -> None:
    price = _price("150")
    basic = annual_pe(price=price, eps=FY2021_BASIC, profile=APPLE_PROFILE)
    diluted = annual_pe(price=price, eps=FY2021_DILUTED, profile=APPLE_PROFILE)

    assert basic.status is CalculationStatus.OK
    assert diluted.status is CalculationStatus.OK
    assert basic.key == "annual_pe_basic"
    assert diluted.key == "annual_pe_diluted"
    assert basic.label == "年度市盈率（基本每股）"
    assert diluted.label == "年度市盈率（稀释每股）"
    assert basic.value is not None and basic.value.value == Decimal("25")
    assert diluted.value is not None and diluted.value.value == Decimal("30")
    assert basic.value.unit == "ratio"
    assert basic.basis != diluted.basis
    assert "eps_basic" in basic.basis and "eps_diluted" in diluted.basis
    for result in (basic, diluted):
        assert "ttm" not in _text(result).lower()
        assert result.periods == (FY2021_BASIC.period,)
        assert any("explicitly injected" in note for note in result.notes)
        assert all(gap in result.notes for gap in UNSUPPORTED_GAPS)
    assert basic.inputs == ("eps_basic@2021-09-25", "close@2021-10-04")


def test_the_closed_capability_bit_does_not_gate_the_explicit_injection_path() -> None:
    """``CAPABILITY`` off by decision; the arithmetic still answers injected inputs."""

    assert CAPABILITY["annual_pe_basic"] is False
    result = annual_pe(price=_price("150"), eps=FY2021_BASIC, profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.OK
    assert any("stays disabled" in note for note in result.notes)


# --- EPS branches ---------------------------------------------------------------


def test_non_positive_eps_is_not_applicable_with_no_number() -> None:
    for raw in ("0", "-0.5"):
        result = annual_pe(price=_price("150"), eps=_eps(raw), profile=APPLE_PROFILE)
        assert result.status is CalculationStatus.NOT_APPLICABLE
        assert result.value is None
        assert result.key == "annual_pe_basic"
        assert "EPS non-positive" in " ".join(result.notes)


def test_missing_eps_value_is_missing_inputs_and_never_zero() -> None:
    result = annual_pe(price=_price("150"), eps=_eps(None), profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    joined = " ".join(result.notes)
    assert "never treated as zero" in joined
    assert "not stated in the constructed probe" in joined


def test_eps_without_a_per_share_basis_is_not_applicable() -> None:
    result = annual_pe(
        price=_price("150"), eps=_eps("5", basis=PerShareBasis.NA), profile=APPLE_PROFILE
    )
    assert result.status is CalculationStatus.NOT_APPLICABLE
    assert result.key == "annual_pe"
    assert result.value is None
    assert "per_share_basis is not_applicable" in result.basis
    assert "does not state a basic or diluted per-share basis" in " ".join(result.notes)


def test_a_non_eps_metric_is_rejected() -> None:
    profit = _eps("1000", metric="profit_parent", basis=PerShareBasis.NA)
    result = annual_pe(price=_price("150"), eps=profit, profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.NOT_APPLICABLE
    assert result.value is None
    assert "is not an EPS metric" in " ".join(result.notes)


# --- share basis ----------------------------------------------------------------


def test_split_between_the_eps_period_and_the_price_date_is_rejected() -> None:
    assert SPLIT_EVENTS["AAPL"] == (("2020-08-31", 4),)
    price = _price("120", trade_date="2020-09-01")
    result = annual_pe(price=price, eps=FY2019_BASIC, profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.NOT_APPLICABLE
    assert result.value is None
    joined = " ".join(result.notes)
    assert "share basis mismatch: split between EPS period and price date" in joined
    assert "4-for-1 split on 2020-08-31" in joined
    assert "2019-09-28" in joined and "2020-09-01" in joined
    assert "'split_adjusted'" in joined
    # The controlled key of the split table is the ticker; the pilot list company
    # key reaches the same event through the documented alias.
    by_ticker = annual_pe(
        price=price, eps=FY2019_BASIC, profile=_profile(company_key="AAPL")
    )
    assert by_ticker.notes[0] == result.notes[0]
    # An as-traded price after the split is still on the post-split count.
    as_traded = _price("120", trade_date="2020-09-01", is_split_adjusted=False)
    assert (
        annual_pe(price=as_traded, eps=FY2019_BASIC, profile=APPLE_PROFILE).status
        is CalculationStatus.NOT_APPLICABLE
    )


def test_split_adjusted_price_before_the_split_date_is_also_incomparable() -> None:
    """A split-adjusted close is on the newest share count whatever date it quotes.

    The recorded 2020-08-28 Yahoo close (the validation document's case) is already
    stated on the post-split count, so it cannot be divided by a pre-split EPS.
    """

    price = _price("124.8075", trade_date="2020-08-28")
    result = annual_pe(price=price, eps=FY2019_BASIC, profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.NOT_APPLICABLE
    assert result.value is None
    joined = " ".join(result.notes)
    assert "split" in joined
    assert "post-split share count" in joined and "2020-08-31" in joined
    # Both sides pre-split: an as-traded close before the split date is comparable.
    as_traded = _price("124.8075", trade_date="2020-08-28", is_split_adjusted=False)
    comparable = annual_pe(price=as_traded, eps=FY2019_BASIC, profile=APPLE_PROFILE)
    assert comparable.status is CalculationStatus.OK
    assert comparable.value is not None
    assert comparable.value.value == Decimal("124.8075") / Decimal("5")
    assert "same pre-split share count" in comparable.basis


def test_post_split_period_with_a_split_adjusted_price_computes() -> None:
    price = _price("160", trade_date="2021-10-04")
    result = annual_pe(price=price, eps=_eps("6.4"), profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.OK
    assert result.value is not None
    assert result.value.value == Decimal("25")
    assert "on or after the recorded split date" in result.basis
    assert "2020-08-31" in result.basis


def test_another_issuer_has_no_recorded_split_so_the_rule_does_not_fire() -> None:
    price = _price("120", trade_date="2020-09-01")
    result = annual_pe(
        price=price, eps=FY2019_BASIC, profile=_profile(company_key="clp-holdings")
    )
    assert result.status is CalculationStatus.OK
    assert "no split event is recorded" in result.basis


# --- price branches -------------------------------------------------------------


def test_missing_price_is_missing_inputs_with_a_disabled_reason() -> None:
    result = annual_pe(price=None, eps=FY2021_BASIC, profile=APPLE_PROFILE)
    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert DISABLED_REASONS[0] in result.notes
    assert any(reason in result.notes for reason in DISABLED_REASONS)


def test_a_non_finite_or_non_positive_close_is_never_a_price() -> None:
    infinite = annual_pe(price=_price("NaN"), eps=FY2021_BASIC, profile=APPLE_PROFILE)
    assert infinite.status is CalculationStatus.MISSING_INPUTS
    assert infinite.value is None
    for raw in ("0", "-3"):
        result = annual_pe(price=_price(raw), eps=FY2021_BASIC, profile=APPLE_PROFILE)
        assert result.status is CalculationStatus.NOT_APPLICABLE
        assert result.value is None
        assert "non-positive" in " ".join(result.notes)


# --- currency and FX ------------------------------------------------------------


def test_cross_currency_without_fx_does_not_invent_a_conversion() -> None:
    result = annual_pe(
        price=USD_PRICE_2024, eps=TSM_EPS, profile=TWD_PROFILE, fx=None
    )
    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    joined = " ".join(result.notes)
    assert "convenience" in joined and "not applied" in joined
    assert "TWD/USD" in joined
    assert DISABLED_REASONS[1] in result.notes
    assert "TWD" in result.basis and "USD" in result.basis


def test_fx_with_an_unknown_quote_precision_is_rejected() -> None:
    for precision in ("unknown", "UNKNOWN", ""):
        result = annual_pe(
            price=USD_PRICE_2024,
            eps=TSM_EPS,
            profile=TWD_PROFILE,
            fx=_fx("0.25", as_of="2024-01-05", precision=precision),
        )
        assert result.status is CalculationStatus.MISSING_INPUTS
        assert result.value is None
        assert "source_date_precision" in " ".join(result.notes)


def test_fx_pair_must_convert_in_the_required_direction() -> None:
    result = annual_pe(
        price=USD_PRICE_2024,
        eps=TSM_EPS,
        profile=TWD_PROFILE,
        fx=_fx("4", as_of="2024-01-05", pair="USD/TWD"),
    )
    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert "required pair is 'TWD/USD'" in " ".join(result.notes)


def test_fx_rate_must_be_a_positive_finite_number() -> None:
    for rate in ("0", "-1", "NaN"):
        result = annual_pe(
            price=USD_PRICE_2024,
            eps=TSM_EPS,
            profile=TWD_PROFILE,
            fx=_fx(rate, as_of="2024-01-05"),
        )
        assert result.status is CalculationStatus.MISSING_INPUTS
        assert result.value is None
        assert "not a positive finite decimal" in " ".join(result.notes)


def test_fx_within_the_lag_window_converts_at_the_injected_rate() -> None:
    result = annual_pe(
        price=USD_PRICE_2024,
        eps=TSM_EPS,
        profile=TWD_PROFILE,
        fx=_fx("0.25", as_of="2024-01-03"),
    )
    assert result.status is CalculationStatus.OK
    assert result.value is not None
    assert result.value.value == Decimal("10")
    assert any(
        "EPS converted from TWD to USD at the injected rate 0.25" in note for note in result.notes
    )
    assert "converted from TWD to USD" in result.basis
    assert result.inputs == ("eps_diluted@2024-12-31", "close@2024-01-05", "fx@2024-01-03")


def test_fx_beyond_the_lag_window_is_stale_and_never_carried_over() -> None:
    for as_of, lag in (("2024-01-01", 4), ("2023-12-26", 10), ("2024-01-15", 10)):
        result = annual_pe(
            price=USD_PRICE_2024,
            eps=TSM_EPS,
            profile=TWD_PROFILE,
            fx=_fx("0.25", as_of=as_of),
        )
        assert result.status is CalculationStatus.STALE_INPUTS
        assert result.value is None
        joined = " ".join(result.notes)
        assert f"{lag} days from the price trade date 2024-01-05" in joined
        assert "not silently carried over" in joined


def test_fx_at_the_window_boundary_is_accepted() -> None:
    # Exactly three days back from the price trade date: accepted, timestamp precision.
    result = annual_pe(
        price=USD_PRICE_2024,
        eps=TSM_EPS,
        profile=TWD_PROFILE,
        fx=_fx("0.25", as_of="2024-01-02", precision="timestamp"),
    )
    assert result.status is CalculationStatus.OK
    assert result.value is not None
    assert result.value.value == Decimal("10")


# --- ADS ------------------------------------------------------------------------


def test_ads_listing_gets_the_gap_list_and_no_number() -> None:
    result = annual_pe(price=None, eps=TSM_EPS, profile=TSM_PROFILE)
    assert result.status is CalculationStatus.NOT_APPLICABLE
    assert result.value is None
    assert result.key == "annual_pe_diluted"
    joined = " ".join(result.notes)
    assert "is an ADS listing" in joined
    assert "ADS ratio: 5 ordinary shares per ADS" in joined
    assert "effective window" in joined
    assert "same-date FX" in joined
    assert "share basis" in joined
    # Even with a price and a dated FX record injected, no ADS PE is produced.
    with_inputs = annual_pe(
        price=USD_PRICE_2024,
        eps=TSM_EPS,
        profile=TSM_PROFILE,
        fx=_fx("0.25", as_of="2024-01-05"),
    )
    assert with_inputs.status is CalculationStatus.NOT_APPLICABLE
    assert with_inputs.value is None
    assert "0.25" not in " ".join(with_inputs.notes)
    assert all(gap in with_inputs.notes for gap in UNSUPPORTED_GAPS)


def test_ads_pe_gap_lists_ratio_validity_fx_and_share_basis() -> None:
    gaps = ads_pe_gap(TSM_PROFILE, fx=None)
    assert len(gaps) == 4
    assert gaps[0].startswith("ADS ratio: 5 ordinary shares per ADS")
    assert gaps[1].startswith("ADS ratio validity:")
    assert gaps[2].startswith("same-date FX:")
    assert gaps[3].startswith("share basis:")
    missing_ratio = ads_pe_gap(
        _profile(
            company_key="tsmc",
            is_ads=True,
            ads_ratio=None,
            report_currency="TWD",
            quote_currency="USD",
        ),
        fx=None,
    )
    assert "carries no ordinary-shares-per-ADS ratio" in missing_ratio[0]
    supplied = ads_pe_gap(TSM_PROFILE, fx=_fx("0.25", as_of="2024-01-05"))
    assert "no accepted daily rate source" in supplied[2]
    assert ads_pe_gap(APPLE_PROFILE, fx=None) == ()


# --- capability bits and unsupported gaps ---------------------------------------


def test_capability_bits_are_false_here_and_in_the_09_read_model() -> None:
    assert CAPABILITY == {
        "annual_pe": False,
        "annual_pe_basic": False,
        "annual_pe_diluted": False,
    }
    assert all(bit is False for bit in CAPABILITY.values())
    assert len(DISABLED_REASONS) == 2
    assert DISABLED_REASONS[0] == "no verified daily price source in the first release"
    assert DISABLED_REASONS[1] == (
        "quote-currency conversion requires a verified same-date FX record"
    )
    assert LABELS["annual_pe"] == "年度市盈率"
    # Cross-task assertion, plan 09's read model: the conditional capability is off.
    assert "annual_pe" in CONDITIONAL_CAPABILITIES
    assert _capabilities(covered=True, has_material=True)["annual_pe"] is False
    assert _capabilities(covered=False, has_material=False)["annual_pe"] is False


def test_unsupported_valuations_are_listed_and_free_of_the_banned_name() -> None:
    joined = " ".join(UNSUPPORTED_GAPS).lower()
    for needle in ("average pe", "percentile", "trailing-twelve-month pe", "reverse dcf", "pb:"):
        assert needle in joined
    assert "ttm" not in joined
    result = annual_pe(price=_price("150"), eps=FY2021_BASIC, profile=APPLE_PROFILE)
    assert all(gap in result.notes for gap in UNSUPPORTED_GAPS)


# --- source-level discipline ----------------------------------------------------


def test_module_source_has_no_ttm_wording_and_is_pure() -> None:
    """No trailing-twelve-month wording and no IO/network/database imports.

    The guards are AST-based, not text-substring based, so they survive a
    module split — a future refactor can move the discipline along with the
    code and this test still follows it — and so a comment that merely
    mentions a library is not a build failure: only a real import counts. The
    wording guard reads string constants and docstrings, which is what the
    module can actually emit to a user; a comment is not output.
    """

    emitted = text_of_strings_and_docstrings(VALUATION_PATH)
    assert "ttm" not in emitted.lower()
    assert_no_io_imports(
        VALUATION_PATH,
        forbidden=(
            "httpx",
            "requests",
            "aiohttp",
            "sqlalchemy",
            "socket",
            "urllib",
            "psycopg",
            "asyncpg",
        ),
    )
    for word in ("买入", "卖出", "评分", "推荐", "建议"):
        assert word not in emitted, word
    assert "no network, no HTTP client" in emitted
