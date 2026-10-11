"""Verified dividend events and named yield definitions (plan 14).

Fixture provenance (``research/market-data-validation`` is read-only and not in this
worktree, so the checked values are written out here)
------------------------------------------------------------------------------------
* HSBC 2024 interim dividends: ``evidence/remaining-final-03/raw/dividends/hsbc-official.html``
  (the issuer's own dividend timetable: announcement, ex-dividend, record and payment
  dates, the declared US$ amounts, and the "Exchange rate determined" rows). The
  30 Apr 2024 row states "first interim of US$0.10 and special dividend of US$0.21", which
  is why the ordinary and special parts are two events. The timetable also shows
  parenthesised HK$ approximations for the 2024 interims, but their FX announcements are
  not in the saved evidence and the combined row's HK$ figure is not split per component,
  so the fixture carries only the declared USD amounts for them.
* HSBC 2024 fourth interim final amount: ``.../hsbc-fx.pdf`` (14 April 2025 announcement:
  US$0.36 per ordinary share, US$1 = HK$7.752798, approximately HK$2.791007, and
  US$1.80 per ADS with each ADS representing five ordinary shares). The issuer's stated
  HK$2.791007 is the **rounded** figure; ``0.36 × 7.752798`` is 2.79100728, and the module
  must use the issuer's number, never its own product.
* CLP 2024 fourth interim: ``.../clp-declaration.pdf`` (HKEX cash dividend announcement of
  24 February 2025: HKD 1.26 per share, ex-date 07 Mar 2025, record 11 Mar 2025, payment
  20 Mar 2025); the declaration date and the amount are the same document and the amount
  is in the payment currency, so the final amount is determined at announcement.
* Apple 2025 first quarter: ``.../apple-declaration.html`` (30 January 2025 newsroom:
  US$0.25 per share, payable 13 February 2025 to holders of record 10 February 2025). No
  exchange source was used to check the ex-dividend date, so it is **not** supplied here
  and must stay ``None``.
* The price and the USD/HKD rate are the 2025-03-10 observations recorded in
  ``remaining-validation.md``: HSBC ``Close`` about HK$88.05 (section 6) and the HKMA
  USD/HKD 7.77 (section 4). They are the caller's stated inputs, not the issuer's
  payment-date rate.
* ``EX_DATE_ONLY_RAW`` and the mixed ordinary/ADS sets built in the yield tests are
  constructed to reach rule branches; they are not evidence about any issuer.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import DatePrecision
from investment_agent.materials.dividends import (
    CAPABILITY,
    DividendCoverage,
    DividendEvent,
    DividendType,
    FxInput,
    ShareClass,
    YieldDefinition,
    dividend_yield,
    event_gaps,
    event_view,
    known_amount_at,
    net_dividend_yield,
)

HSBC_TIMETABLE = "hsbc-official.html (HSBC dividend information and timetable)"
HSBC_FX = "hsbc-fx.pdf (14 Apr 2025 fourth interim dividend exchange rate)"
CLP_DECLARATION = "clp-declaration.pdf (HKEX cash dividend announcement, 24 Feb 2025)"
APPLE_NEWSROOM = "apple-declaration.html (Apple newsroom, 30 Jan 2025)"
CONSTRUCTED = "constructed for a rule branch, not evidence about any issuer"

D_2025_02_18 = date(2025, 2, 18)
D_2025_02_19 = date(2025, 2, 19)
D_2025_03_06 = date(2025, 3, 6)
D_2025_03_10 = date(2025, 3, 10)
D_2025_04_13 = date(2025, 4, 13)
D_2025_04_14 = date(2025, 4, 14)


def _hsbc_interim(
    amount: str,
    kind: str,
    announced: str,
    ex: str,
    record: str,
    pay: str,
    locator: str,
    **extra: Any,
) -> dict[str, Any]:
    event = {
        "declared_currency": "USD",
        "declared_amount": amount,
        "share_class": "ordinary",
        "kind": kind,
        "announced_on": announced,
        "ex_date": ex,
        "record_date": record,
        "pay_date": pay,
        "fiscal_year": 2024,
        "source_ref": HSBC_TIMETABLE,
        "locator": locator,
    }
    event.update(extra)
    return event


HSBC_2024_FIRST_INTERIM_RAW = _hsbc_interim(
    "0.10", "ordinary", "2024-04-30", "2024-05-09", "2024-05-10", "2024-06-21",
    "2024 First interim & Special dividend",
)
HSBC_2024_SPECIAL_RAW = _hsbc_interim(
    "0.21", "special", "2024-04-30", "2024-05-09", "2024-05-10", "2024-06-21",
    "2024 First interim & Special dividend (special dividend of US$0.21)",
)
HSBC_2024_SECOND_INTERIM_RAW = _hsbc_interim(
    "0.10", "ordinary", "2024-07-31", "2024-08-15", "2024-08-16", "2024-09-27",
    "2024 Second interim",
)
HSBC_2024_THIRD_INTERIM_RAW = _hsbc_interim(
    "0.10", "ordinary", "2024-10-29", "2024-11-07", "2024-11-08", "2024-12-19",
    "2024 Third interim",
)
HSBC_2024_FOURTH_INTERIM_RAW = _hsbc_interim(
    "0.36", "ordinary", "2025-02-19", "2025-03-06", "2025-03-07", "2025-04-25",
    "2024 Fourth interim",
    final_currency="HKD",
    final_amount="2.791007",
    final_fx_rate="7.752798",
    final_fx_source=HSBC_FX,
    final_amount_determined_on="2025-04-14",
)
HSBC_2024_FOURTH_ADS_RAW = {
    "declared_currency": "USD",
    "declared_amount": "1.80",
    "share_class": "ads",
    "kind": "ordinary",
    "announced_on": "2025-04-14",
    "ex_date": None,
    "record_date": "2025-03-07",
    "pay_date": "2025-04-25",
    "final_currency": "USD",
    "final_amount": "1.80",
    "final_amount_determined_on": "2025-04-14",
    "fiscal_year": 2024,
    "source_ref": HSBC_FX,
    "locator": "ADS paragraph (US$1.80 per ADS, five ordinary shares per ADS)",
}
CLP_2024_FOURTH_INTERIM_RAW = {
    "declared_currency": "HKD",
    "declared_amount": "1.26",
    "share_class": "ordinary",
    "kind": "ordinary",
    "announced_on": "2025-02-24",
    "ex_date": "2025-03-07",
    "record_date": "2025-03-11",
    "pay_date": "2025-03-20",
    "final_currency": "HKD",
    "final_amount": "1.26",
    "final_amount_determined_on": "2025-02-24",
    "fiscal_year": 2024,
    "source_ref": CLP_DECLARATION,
    "locator": "Cash Dividend Announcement, Fourth Interim Dividend for 2024",
}
APPLE_2025_FIRST_QUARTER_RAW: dict[str, Any] = {
    "declared_currency": "USD",
    "declared_amount": "0.25",
    "share_class": "ordinary",
    "kind": "ordinary",
    "announced_on": "2025-01-30",
    # No exchange source confirmed the ex-dividend date: the key is absent on purpose.
    "record_date": "2025-02-10",
    "pay_date": "2025-02-13",
    "final_currency": "USD",
    "final_amount": "0.25",
    "final_amount_determined_on": "2025-01-30",
    "fiscal_year": 2025,
    "source_ref": APPLE_NEWSROOM,
    "locator": "Q1 2025 results, cash dividend paragraph",
}
EX_DATE_ONLY_RAW: dict[str, Any] = {
    "declared_currency": "USD",
    "declared_amount": "0.10",
    "share_class": "ordinary",
    "kind": "ordinary",
    "ex_date": "2025-03-06",
    "fiscal_year": 2024,
    "source_ref": CONSTRUCTED,
}


def hsbc_2024_fiscal_year_events() -> tuple[DividendEvent, ...]:
    """The five HSBC dividends declared for FY2024 (four interims, one of them special)."""

    return tuple(
        event_view(raw)
        for raw in (
            HSBC_2024_FIRST_INTERIM_RAW,
            HSBC_2024_SPECIAL_RAW,
            HSBC_2024_SECOND_INTERIM_RAW,
            HSBC_2024_THIRD_INTERIM_RAW,
            HSBC_2024_FOURTH_INTERIM_RAW,
        )
    )


def hsbc_price(rates: dict[str, Decimal] | None = None) -> FxInput:
    return FxInput(
        price=Decimal("88.05"),
        price_currency="HKD",
        as_of=D_2025_03_10,
        source_ref="2025-03-10 HSBC close (remaining-validation.md section 6)",
        rates={} if rates is None else rates,
    )


# --- the knowable-amount timeline ----------------------------------------------


def test_hsbc_fourth_interim_timeline_keeps_the_final_amount_in_april() -> None:
    event = event_view(HSBC_2024_FOURTH_INTERIM_RAW)

    for at in (D_2025_02_18,):
        assert known_amount_at(event, at) == (None, "unknown")

    for at in (D_2025_02_19, D_2025_03_06, date(2025, 3, 7), D_2025_03_10, D_2025_04_13):
        assert known_amount_at(event, at) == (Decimal("0.36"), "declared:USD")

    assert known_amount_at(event, D_2025_04_14) == (Decimal("2.791007"), "final:HKD")
    assert known_amount_at(event, date(2025, 4, 25)) == (Decimal("2.791007"), "final:HKD")
    # The ex-dividend date is 2025-03-06; on that day the final HKD figure did not exist.
    assert known_amount_at(event, D_2025_03_06)[0] != Decimal("2.791007")
    assert known_amount_at(event, D_2025_03_10)[0] != Decimal("2.791007")


def test_available_from_is_the_determination_date_not_the_ex_date() -> None:
    event = event_view(HSBC_2024_FOURTH_INTERIM_RAW)

    assert event.ex_date == D_2025_03_06
    assert event.available_from == D_2025_04_14
    assert event.available_from != event.ex_date
    assert event.date_precision is DatePrecision.DATE
    assert event.availability_basis == "final"


def test_an_ex_date_only_event_has_no_availability_date() -> None:
    event = event_view(EX_DATE_ONLY_RAW)

    assert event.ex_date == D_2025_03_06
    assert event.announced_on is None
    assert event.available_from is None
    assert event.date_precision is DatePrecision.UNKNOWN
    assert event.availability_basis == "unknown"
    assert known_amount_at(event, D_2025_04_14) == (None, "unknown")


def test_declared_amount_without_a_determination_date_stays_declared() -> None:
    event = event_view(HSBC_2024_THIRD_INTERIM_RAW)

    assert event.final_amount_determined_on is None
    assert event.available_from == event.announced_on == date(2024, 10, 29)
    assert event.availability_basis == "declared"
    assert known_amount_at(event, date(2030, 1, 1)) == (Decimal("0.10"), "declared:USD")


def test_final_fx_record_uses_the_issuers_rounded_amount() -> None:
    event = event_view(HSBC_2024_FOURTH_INTERIM_RAW)

    assert event.final_fx_rate == Decimal("7.752798")
    assert event.final_fx_source == HSBC_FX
    assert event.final_amount_determined_on == D_2025_04_14
    assert event.final_currency == "HKD"
    assert event.final_amount == Decimal("2.791007")
    # 0.36 x 7.752798 is 2.79100728; the issuer states 2.791007, so the module must not
    # recompute the final amount from its own product.
    assert event.declared_amount * event.final_fx_rate == Decimal("2.79100728")
    assert event.final_amount != event.declared_amount * event.final_fx_rate


def test_clp_fourth_interim_is_determined_at_the_announcement() -> None:
    event = event_view(CLP_2024_FOURTH_INTERIM_RAW)

    assert event.final_amount_determined_on == event.announced_on == date(2025, 2, 24)
    assert event.available_from == date(2025, 2, 24)
    assert event.availability_basis == "final"
    assert known_amount_at(event, date(2025, 2, 23)) == (None, "unknown")
    assert known_amount_at(event, date(2025, 2, 24)) == (Decimal("1.26"), "final:HKD")
    assert event.ex_date == date(2025, 3, 7)
    assert event.record_date == date(2025, 3, 11)
    assert event.pay_date == date(2025, 3, 20)


def test_apple_event_keeps_the_unverified_ex_date_empty() -> None:
    assert "ex_date" not in APPLE_2025_FIRST_QUARTER_RAW
    event = event_view(APPLE_2025_FIRST_QUARTER_RAW)

    assert event.declared_amount == Decimal("0.25")
    assert event.declared_currency == "USD"
    assert event.record_date == date(2025, 2, 10)
    assert event.pay_date == date(2025, 2, 13)
    assert event.announced_on == date(2025, 1, 30)
    assert event.ex_date is None
    assert any("ex-dividend date" in gap for gap in event_gaps((event,)))


# --- ordinary versus special ----------------------------------------------------


def test_ordinary_and_special_are_separate_events_and_totals() -> None:
    events = hsbc_2024_fiscal_year_events()
    specials = [event for event in events if event.kind is DividendType.SPECIAL]

    assert len(events) == 5
    assert [event.declared_amount for event in specials] == [Decimal("0.21")]
    assert sum(event.declared_amount for event in events) == Decimal("0.87")
    assert sum(
        event.declared_amount for event in events if event.kind is DividendType.ORDINARY
    ) == Decimal("0.66")

    with_special = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5, source_ref=HSBC_TIMETABLE),
    )
    without_special = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=False,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5, source_ref=HSBC_TIMETABLE),
    )

    assert with_special.status is CalculationStatus.OK
    assert without_special.status is CalculationStatus.OK
    assert "0.87 USD" in with_special.basis
    assert "0.66 USD" in without_special.basis
    assert with_special.basis != without_special.basis
    assert any("special dividend event(s) are excluded" in note for note in without_special.notes)
    assert with_special.value is not None and without_special.value is not None
    assert with_special.value.value.quantize(Decimal("0.0000001")) == Decimal("0.0767734")
    assert without_special.value.value.quantize(Decimal("0.0000001")) == Decimal("0.0582419")
    assert with_special.value.unit == "ratio"


def test_fiscal_year_declared_is_not_the_announcement_year() -> None:
    events = hsbc_2024_fiscal_year_events()
    price = hsbc_price({"USD": Decimal("7.77")})

    # The fourth interim was announced on 2025-02-19 but belongs to FY2024, so an FY2025
    # window holds nothing and is reported as missing events, never as a zero total.
    fy2025 = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=price,
        fiscal_year=2025,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5),
    )
    assert fy2025.status is CalculationStatus.MISSING_INPUTS
    assert fy2025.value is None
    assert any("5 of the 5 event(s)" in note for note in fy2025.notes)

    fy2024 = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=price,
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5),
    )
    assert fy2024.status is CalculationStatus.OK
    assert "0.87 USD" in fy2024.basis


# --- yield window definitions ---------------------------------------------------


def test_yield_definitions_are_named_and_select_different_events() -> None:
    events = hsbc_2024_fiscal_year_events()
    price = hsbc_price({"USD": Decimal("7.77")})
    results = {
        YieldDefinition.FISCAL_YEAR_DECLARED: dividend_yield(
            events,
            definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
            include_special=True,
            price=price,
            fiscal_year=2024,
            as_of=D_2025_03_10,
            coverage=DividendCoverage(expected_events=5),
        ),
        YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE: dividend_yield(
            events,
            definition=YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE.value,
            include_special=True,
            price=price,
            as_of=D_2025_03_10,
            coverage=DividendCoverage(expected_events=5),
        ),
        YieldDefinition.TRAILING_TWELVE_MONTHS_PAID: dividend_yield(
            events,
            definition=YieldDefinition.TRAILING_TWELVE_MONTHS_PAID.value,
            include_special=True,
            price=price,
            as_of=D_2025_03_10,
            coverage=DividendCoverage(expected_events=4),
        ),
    }

    for definition, result in results.items():
        assert result.status is CalculationStatus.OK
        assert definition.value in result.basis
        assert "pre-tax issuer-declared gross amounts" in result.basis
        assert "ADR fees not deducted" in result.basis
    assert results[YieldDefinition.FISCAL_YEAR_DECLARED].periods == ()
    bases = [result.basis for result in results.values()]
    assert len(set(bases)) == 3

    # Same events, three windows: FY2024 declared 0.87, TTM by ex-date 0.87 (the fourth
    # interim went ex on 2025-03-06 but only its declared USD 0.36 is knowable), and TTM by
    # payment date 0.51 because the fourth interim is paid after 2025-03-10.
    assert "0.87 USD" in results[YieldDefinition.FISCAL_YEAR_DECLARED].basis
    assert "0.87 USD" in results[YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE].basis
    assert "0.51 USD" in results[YieldDefinition.TRAILING_TWELVE_MONTHS_PAID].basis
    for definition, period_start in (
        (YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE, date(2024, 3, 11)),
        (YieldDefinition.TRAILING_TWELVE_MONTHS_PAID, date(2024, 3, 11)),
    ):
        periods = results[definition].periods
        assert len(periods) == 1
        assert periods[0].start == period_start
        assert periods[0].end == D_2025_03_10


def test_unknown_definition_raises_and_windows_must_be_stated() -> None:
    events = hsbc_2024_fiscal_year_events()
    with pytest.raises(ValueError, match="unknown dividend-yield definition"):
        dividend_yield(
            events,
            definition="dividend_yield",
            include_special=True,
            price=hsbc_price(),
            fiscal_year=2024,
            as_of=D_2025_03_10,
            coverage=DividendCoverage(expected_events=5),
        )

    no_fiscal_year = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price(),
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5),
    )
    assert no_fiscal_year.status is CalculationStatus.MISSING_INPUTS
    assert any("no fiscal year was stated" in note for note in no_fiscal_year.notes)

    no_as_of = dividend_yield(
        events,
        definition=YieldDefinition.TRAILING_TWELVE_MONTHS_PAID.value,
        include_special=True,
        price=hsbc_price(),
        coverage=DividendCoverage(expected_events=4),
    )
    assert no_as_of.status is CalculationStatus.MISSING_INPUTS
    assert no_as_of.value is None
    assert any("as-of date" in note for note in no_as_of.notes)


# --- incomplete coverage never becomes a number --------------------------------


def test_one_event_with_a_price_but_incomplete_coverage_yields_no_value() -> None:
    result = dividend_yield(
        (event_view(HSBC_2024_THIRD_INTERIM_RAW),),
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5, source_ref=HSBC_TIMETABLE),
    )

    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert any("4 of the 5 event(s)" in note for note in result.notes)
    assert any("no partial total is produced" in note for note in result.notes)


def test_no_coverage_statement_means_no_yield() -> None:
    result = dividend_yield(
        hsbc_2024_fiscal_year_events(),
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
    )

    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert any("no coverage was stated" in note for note in result.notes)


def test_a_non_positive_expected_event_count_is_refused() -> None:
    with pytest.raises(ValueError, match="expected_events must be positive"):
        dividend_yield(
            hsbc_2024_fiscal_year_events(),
            definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
            include_special=True,
            price=hsbc_price(),
            fiscal_year=2024,
            as_of=D_2025_03_10,
            coverage=DividendCoverage(expected_events=0),
        )


def test_missing_price_or_conversion_never_degrades_to_a_bare_percentage() -> None:
    events = hsbc_2024_fiscal_year_events()
    coverage = DividendCoverage(expected_events=5)
    without_price = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=None,
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=coverage,
    )
    assert without_price.status is CalculationStatus.MISSING_INPUTS
    assert without_price.value is None
    assert any("no price (yield denominator)" in note for note in without_price.notes)

    without_rate = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price(),
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=coverage,
    )
    assert without_rate.status is CalculationStatus.MISSING_INPUTS
    assert without_rate.value is None
    rate_gaps = [note for note in without_rate.notes if "no USD/HKD conversion" in note]
    assert len(rate_gaps) == 1


def test_an_amount_not_knowable_at_the_as_of_date_is_withheld() -> None:
    events = hsbc_2024_fiscal_year_events()
    coverage = DividendCoverage(expected_events=5, source_ref=HSBC_TIMETABLE)
    price = hsbc_price({"USD": Decimal("7.77")})

    before = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=price,
        fiscal_year=2024,
        as_of=D_2025_02_18,
        coverage=coverage,
    )
    assert before.status is CalculationStatus.MISSING_INPUTS
    assert before.value is None
    assert any("not knowable at 2025-02-18" in note for note in before.notes)

    announced = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=price,
        fiscal_year=2024,
        as_of=D_2025_02_19,
        coverage=coverage,
    )
    assert announced.status is CalculationStatus.OK
    assert announced.value is not None
    assert "0.87 USD" in announced.basis


def test_an_event_without_its_window_date_is_reported_not_dropped() -> None:
    # Apple's ex-dividend date is unverified, so the event cannot be placed in an ex-date
    # window: it is reported instead of being silently dropped from the total.
    event = event_view(APPLE_2025_FIRST_QUARTER_RAW)
    assert event.ex_date is None
    result = dividend_yield(
        (*hsbc_2024_fiscal_year_events(), event),
        definition=YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=5),
    )

    assert result.status is CalculationStatus.MISSING_INPUTS
    assert result.value is None
    assert any("cannot be placed in the trailing twelve months" in note for note in result.notes)


# --- ADS and net amounts --------------------------------------------------------


def test_ads_net_yield_is_not_applicable_with_the_reason() -> None:
    ads_event = event_view(HSBC_2024_FOURTH_ADS_RAW)

    assert ads_event.share_class is ShareClass.ADS
    assert ads_event.declared_amount == Decimal("1.80")

    net = net_dividend_yield(
        (ads_event,),
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
    )
    assert net.status is CalculationStatus.NOT_APPLICABLE
    assert net.value is None
    reasons = " ".join(net.notes)
    assert "tax" in reasons
    assert "ADR fees" in reasons
    assert "five ordinary shares" in reasons


def test_ordinary_net_yield_is_also_not_applicable() -> None:
    net = net_dividend_yield(
        (event_view(HSBC_2024_FOURTH_INTERIM_RAW),),
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
    )
    assert net.status is CalculationStatus.NOT_APPLICABLE
    assert net.value is None
    assert any("withholding tax" in note for note in net.notes)


def test_mixed_share_classes_are_not_summed_and_ads_are_not_priced_as_ordinary() -> None:
    events = (*hsbc_2024_fiscal_year_events(), event_view(HSBC_2024_FOURTH_ADS_RAW))
    mixed = dividend_yield(
        events,
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=6),
    )
    assert mixed.status is CalculationStatus.NOT_APPLICABLE
    assert mixed.value is None
    assert any("share classes are mixed" in note for note in mixed.notes)

    ads_only = dividend_yield(
        (event_view(HSBC_2024_FOURTH_ADS_RAW),),
        definition=YieldDefinition.FISCAL_YEAR_DECLARED.value,
        include_special=True,
        price=hsbc_price({"USD": Decimal("7.77")}),
        fiscal_year=2024,
        as_of=D_2025_03_10,
        coverage=DividendCoverage(expected_events=1),
    )
    assert ads_only.status is CalculationStatus.NOT_APPLICABLE
    assert any("per ads security while the price is per ordinary security" in note
               for note in ads_only.notes)


# --- event gaps, capability and refusal to guess -------------------------------


def test_event_gaps_list_dates_and_mixed_sets() -> None:
    apple = event_view(APPLE_2025_FIRST_QUARTER_RAW)
    clp = event_view(CLP_2024_FOURTH_INTERIM_RAW)
    ads = event_view(HSBC_2024_FOURTH_ADS_RAW)

    gaps = event_gaps((apple, clp, ads))
    assert any("no ex-dividend date confirmed by an exchange source" in gap for gap in gaps)
    assert any("share classes are mixed (ads, ordinary)" in gap for gap in gaps)
    assert any("declared currencies differ (HKD, USD)" in gap for gap in gaps)

    ex_only = event_view(EX_DATE_ONLY_RAW)
    assert any("no announcement date" in gap for gap in event_gaps((ex_only,)))


def test_capability_flags_stay_off() -> None:
    assert CAPABILITY == {"dividends": False, "dividend_yield": False}
    assert CAPABILITY["dividend_yield"] is False


def test_no_clock_time_is_fabricated() -> None:
    for raw in (
        HSBC_2024_FOURTH_INTERIM_RAW,
        CLP_2024_FOURTH_INTERIM_RAW,
        APPLE_2025_FIRST_QUARTER_RAW,
    ):
        event = event_view(raw)
        for value in (
            event.announced_on,
            event.ex_date,
            event.record_date,
            event.pay_date,
            event.final_amount_determined_on,
            event.available_from,
        ):
            assert value is None or not isinstance(value, datetime)


def test_event_view_accepts_date_objects_and_ignores_import_metadata() -> None:
    event = event_view(
        {
            **HSBC_2024_FOURTH_INTERIM_RAW,
            "announced_on": date(2025, 2, 19),
            "ex_date": date(2025, 3, 6),
            "imported_at_utc": "2026-10-11T15:36:16+00:00",
        }
    )

    assert event.announced_on == date(2025, 2, 19)
    assert event.ex_date == date(2025, 3, 6)
    assert event.available_from == D_2025_04_14


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "kind": None}, "missing 'kind'"),
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "kind": "interim"}, "is not one of"),
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "share_class": "common"}, "is not one of"),
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "declared_amount": 0.36}, "must be a string"),
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "declared_amount": "0"}, "is not positive"),
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "declared_currency": "GBP"}, "pilot dividend"),
        ({**HSBC_2024_FOURTH_INTERIM_RAW, "source_ref": None}, "missing 'source_ref'"),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "final_amount": None},
            "final_amount is missing",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "final_currency": None},
            "final_currency is missing",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "final_fx_source": None},
            "final_fx_source is missing",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "final_fx_rate": "0"},
            "is not positive",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "announced_on": datetime(2025, 2, 19, 9, 0)},
            "not a datetime",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "announced_on": "2025-02-19T09:00:00"},
            "carries a clock time",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "final_amount_determined_on": "2025-02-01"},
            "precedes",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "announced_on": "19 Feb 2025"},
            "is not an ISO date",
        ),
        (
            {**HSBC_2024_FOURTH_INTERIM_RAW, "fiscal_year": "2024"},
            "fiscal-year label",
        ),
    ],
)
def test_event_view_refuses_to_guess(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        event_view(raw)


def test_amount_and_currency_must_travel_together() -> None:
    event = event_view(CLP_2024_FOURTH_INTERIM_RAW)
    assert event.declared_currency == "HKD"
    assert event.final_currency == "HKD"
    assert known_amount_at(event, date(2025, 2, 24)) == (Decimal("1.26"), "final:HKD")
