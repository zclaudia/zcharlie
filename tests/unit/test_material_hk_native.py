"""Native-currency HK statement adapter (plan 03 acceptance table).

Fixture provenance (all cropped 2026-10-12 from the retained research evidence
``research/market-data-validation/evidence/remaining-final-03/raw/native/``,
rows copied verbatim — only row selection and ``result.count`` were adjusted so
each sample stays self-consistent; ``success``/``message``/``version`` are the
original ones):

* ``hk/00005/{income,balance,cashflow}.json`` — cropped from
  ``00005/{income,balance,cashflow-original}-four-years.json``; FY2024 + FY2023
  rows: the reconciled item codes plus a few unmapped rows (income 3, balance 2,
  cashflow 2 per outer year, i.e. 4 in total).
* ``hk/00002/*.json`` / ``hk/00700/*.json`` — same crop, FY2024 only.
* ``hk/00005/truncated.json`` — the FY2024 slice of the cropped HSBC income
  sample with ``result.pages`` set to 2: a test vector for the pagination check,
  not evidence.
* ``hk/00005/error_body.json`` — byte copy of the saved HTTP 200 failure body
  ``cashflow-discovery/RPT_HKF10_FN_CASHFLOW_SUMMARY.json``.
* ``hk/00005/html_body.json`` — constructed HTML error page (test vector).

The 2024 amounts asserted here are the 18 reconciled values of
``remaining-validation.md`` §1 / ``derived/native-hk/result.json`` (millions of
the issuer's native currency, e.g. HSBC 62,440m USD), plus the HSBC
reconciliation triples of §2; other years only prove structure, not reconciled
amounts.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from investment_agent.domain.security import ControlledIdentity, source_symbol
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    DatasetKind,
    FactPeriod,
    PeriodBasis,
    RawPayload,
    SubjectKind,
    validate_draft,
)
from investment_agent.materials.hk_native import (
    ADAPTER_VERSION,
    BASE,
    DEFAULT_PERIODS,
    ITEM_MAP,
    REPORTS,
    SOURCE,
    HkNativeAdapter,
)
from tests.unit._hk_us_support import pilot_identities

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "hk"
FETCHED_AT = datetime(2026, 10, 11, 15, 47, 50, tzinfo=UTC)
FROZEN_NOW = "2026-10-12T04:00:00Z"
SUBJECT_ID = 101

FY2024 = FactPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31), basis=PeriodBasis.ANNUAL)
FY2023 = FactPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31), basis=PeriodBasis.ANNUAL)

#: The requested periods that match each fixture crop.
FIXTURE_PERIODS: dict[str, tuple[FactPeriod, ...]] = {
    "00005": (FY2024, FY2023),
    "00002": (FY2024,),
    "00700": (FY2024,),
}

#: 2024 amounts in the statement's native currency units (元): the reconciled
#: millions of remaining-validation.md §1 x 1_000_000.
HSBC_2024 = {
    "revenue_net_credit": "62440000000",
    "profit_ordinary_shareholders": "22917000000",
    "total_assets": "3017048000000",
    "equity_parent": "184973000000",
    "operating_cash_flow": "65305000000",
    "capex_ppe": "1344000000",
}
CLP_2024 = {
    "revenue_operating": "90964000000",
    "profit_ordinary_shareholders": "11742000000",
    "total_assets": "233713000000",
    "equity_parent": "104055000000",
    "operating_cash_flow": "23140000000",
    "capex_ppe": "15076000000",
}
TENCENT_2024 = {
    "revenue_operating": "660257000000",
    "profit_ordinary_shareholders": "194073000000",
    "total_assets": "1780995000000",
    "equity_parent": "973548000000",
    "operating_cash_flow": "258521000000",
    "capex_ppe": "62927000000",
}
COMPANY_2024 = {"00005": HSBC_2024, "00002": CLP_2024, "00700": TENCENT_2024}

REPORT_KEYS = ("income", "balance", "cashflow")


def fixture_bytes(code: str, name: str) -> bytes:
    return FIXTURES.joinpath(code, f"{name}.json").read_bytes()


def fixture_body(code: str, name: str) -> dict[str, Any]:
    return json.loads(fixture_bytes(code, name).decode("utf-8"))


def body_bytes(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def raw_payload(name: str, content: bytes) -> RawPayload:
    return RawPayload(
        name=name,
        content=content,
        media_type="application/json",
        fetched_at=FETCHED_AT,
        source_url=BASE,
        http_status=200,
    )


def company(ticker: str) -> ControlledIdentity:
    return next(item for item in pilot_identities() if item.ticker == ticker)


def parse(
    ticker: str,
    *,
    periods: tuple[FactPeriod, ...] | None = None,
    raw: list[RawPayload] | None = None,
    subject_id: int | None = SUBJECT_ID,
    adapter: HkNativeAdapter | None = None,
):
    if raw is None:
        raw = [raw_payload(name, fixture_bytes(ticker, name)) for name in REPORT_KEYS]
    kwargs = {} if subject_id is None else {"subject_id": subject_id}
    return (adapter or HkNativeAdapter()).parse(
        company(ticker),
        raw=raw,
        periods=list(periods if periods is not None else FIXTURE_PERIODS[ticker]),
        **kwargs,
    )


def draft_for(result, report: str, end: date):
    matches = [d for d in result.drafts if d.payload["report"] == report and d.period.end == end]
    assert len(matches) == 1, f"expected one {report} draft at {end}, got {len(matches)}"
    return matches[0]


def fact(result, report: str, end: date, metric: str) -> dict[str, Any]:
    entries = [f for f in draft_for(result, report, end).payload["facts"] if f["metric"] == metric]
    assert len(entries) == 1, f"expected one {metric} fact, got {len(entries)}"
    return entries[0]


def amount(result, report: str, metric: str, end: date = FY2024.end) -> str | None:
    return fact(result, report, end, metric)["value"]["raw_value"]


def assert_all_drafts_valid(result) -> None:
    assert result.drafts
    for item in result.drafts:
        assert validate_draft(item) == ()


# --- happy paths -----------------------------------------------------------


def test_hsbc_2024_amounts_units_and_periods() -> None:
    result = parse("00005", periods=(FY2024, FY2023), subject_id=SUBJECT_ID)
    assert result.problems == (
        "unmapped_items: income/3",
        "unmapped_items: balance/2",
        "unmapped_items: cashflow/4",
    )
    assert len(result.drafts) == 6  # 3 tables x 2 requested periods
    assert_all_drafts_valid(result)
    for report, metric in (
        ("income", "revenue_net_credit"),
        ("income", "profit_ordinary_shareholders"),
        ("balance", "total_assets"),
        ("balance", "equity_parent"),
        ("cashflow", "operating_cash_flow"),
        ("cashflow", "capex_ppe"),
    ):
        entry = fact(result, report, FY2024.end, metric)
        assert entry["value"]["raw_value"] == HSBC_2024[metric]
        assert entry["value"]["raw_unit"] == "元"
        assert entry["value"]["scale"] == 1
        assert entry["value"]["unit"] == "USD"
        assert entry["value"]["currency"] == "USD"
        assert entry["value"]["missing_reason"] is None
    # Income and cashflow facts are annual periods with the provider's start;
    # balance facts are instants.
    assert fact(result, "income", FY2024.end, "revenue_net_credit")["period"] == {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "basis": "annual",
        "label": None,
        "days": None,
    }
    assert fact(result, "balance", FY2024.end, "total_assets")["period"]["basis"] == "instant"
    assert fact(result, "balance", FY2024.end, "total_assets")["period"]["start"] is None
    assert fact(result, "cashflow", FY2024.end, "operating_cash_flow")["period"]["basis"] == "annual"
    # 2023 amounts come from the same evidence crop (structure check only).
    assert amount(result, "income", "revenue_net_credit", FY2023.end) == "62611000000"
    assert amount(result, "balance", "total_assets", FY2023.end) == "3038677000000"


def test_clp_and_tencent_amounts_stay_in_their_report_currency() -> None:
    for ticker, expected in (("00002", CLP_2024), ("00700", TENCENT_2024)):
        result = parse(ticker)
        assert result.problems == (
            "unmapped_items: income/3",
            "unmapped_items: balance/2",
            "unmapped_items: cashflow/2",
        )
        assert_all_drafts_valid(result)
        currency = company(ticker).report_currency
        assert currency in ("HKD", "CNY")
        for report, metric in (
            ("income", "revenue_operating"),
            ("income", "profit_ordinary_shareholders"),
            ("balance", "total_assets"),
            ("balance", "equity_parent"),
            ("cashflow", "operating_cash_flow"),
            ("cashflow", "capex_ppe"),
        ):
            entry = fact(result, report, FY2024.end, metric)
            assert entry["value"]["raw_value"] == expected[metric]
            assert entry["value"]["unit"] == currency
            assert entry["value"]["currency"] == currency
        # No cross-currency mixing anywhere in this parse.
        for item in result.drafts:
            for entry in item.payload["facts"]:
                assert entry["value"]["currency"] == currency
                assert entry["value"]["unit"] == currency


def test_hsbc_missing_metrics_are_explicit_and_never_approximated() -> None:
    result = parse("00005", periods=(FY2024, FY2023))
    missing = {
        ("income", "revenue_gross"),
        ("income", "credit_loss_expense"),
        ("balance", "equity_ordinary"),
    }
    for report, metric in missing:
        entry = fact(result, report, FY2024.end, metric)
        assert entry["value"]["raw_value"] is None
        assert entry["value"]["missing_reason"]
        assert entry["missing_reason"] == entry["value"]["missing_reason"]
        # The missing metric is carried for every requested period, not omitted.
        assert fact(result, report, FY2023.end, metric)["value"]["raw_value"] is None
    reported = {
        entry["value"]["raw_value"]
        for item in result.drafts
        for entry in item.payload["facts"]
    }
    # The reconciliation figures (65,854m / 3,414m USD) and any 7.1884-style
    # conversion must not appear as values.
    assert "65854000000" not in reported
    assert "3414000000" not in reported
    assert "448843696000" not in reported
    assert all(
        value is None or value.isdigit() for value in reported
    ), "amounts must be plain decimal strings or null"


# --- structure checks ------------------------------------------------------


def _display_bodies() -> list[bytes]:
    # Row shapes and two amounts are from the saved display-currency response
    # (RPT_HKF10_FN_INCOME_PC.json: 利息收入 780,883,080,400; remaining-validation
    # §2 lists 448,843,696,000 for the 62,440m x 7.1884 display row); the report
    # name echo is added so the marker check is exercisable on the body.
    return [
        body_bytes(
            {
                "reportName": "RPT_HKF10_FN_INCOME_PC",
                "version": "0",
                "success": True,
                "message": "ok",
                "code": 0,
                "result": {
                    "pages": 1,
                    "count": 2,
                    "data": [
                        {"SECUCODE": "00005.HK", "STD_ITEM_CODE": "001001001",
                         "STD_ITEM_NAME": "利息收入", "AMOUNT": 780883080400},
                        {"SECUCODE": "00005.HK", "STD_ITEM_CODE": "001003999",
                         "STD_ITEM_NAME": "经营收入总额", "AMOUNT": 448843696000},
                    ],
                },
            }
        ),
        body_bytes({"success": True, "result": {"pages": 1, "count": 0, "data": []},
                    "source": "MAININDICATOR"}),
        body_bytes({"success": True, "marker": "some_PC_source", "result": {"pages": 1,
                                                                             "count": 0,
                                                                             "data": []}}),
    ]


@pytest.mark.parametrize("display_body", _display_bodies())
def test_display_currency_marker_rejects_the_whole_batch(display_body: bytes) -> None:
    raw = [raw_payload("balance", fixture_bytes("00005", "balance")),
           raw_payload("income", display_body),
           raw_payload("cashflow", fixture_bytes("00005", "cashflow"))]
    result = parse("00005", raw=raw)
    assert result.drafts == ()
    assert result.problems == ("display_currency_source_rejected: income",)


def test_upstream_error_body_is_a_failure_and_does_not_raise() -> None:
    error = fixture_bytes("00005", "error_body")
    only = parse("00005", raw=[raw_payload("income", error)])
    assert only.drafts == ()
    assert "upstream_success_false: income" in only.problems
    assert only.problems == (
        "upstream_success_false: income",
        "missing_raw: balance",
        "missing_raw: cashflow",
    )
    # The other tables keep working when one raw carries an HTTP 200 error body.
    mixed = parse(
        "00005",
        raw=[
            raw_payload("income", error),
            raw_payload("balance", fixture_bytes("00005", "balance")),
            raw_payload("cashflow", fixture_bytes("00005", "cashflow")),
        ],
    )
    assert "upstream_success_false: income" in mixed.problems
    assert {d.payload["report"] for d in mixed.drafts} == {"balance", "cashflow"}


def test_html_and_non_object_bodies_are_not_json() -> None:
    html = parse("00005", raw=[raw_payload("income", fixture_bytes("00005", "html_body"))])
    assert html.drafts == ()
    assert html.problems[:1] == ("not_json: income",)
    array_body = parse("00005", raw=[raw_payload("income", b"[1, 2, 3]")])
    assert array_body.problems[:1] == ("not_json: income",)
    broken = parse("00005", raw=[raw_payload("income", b"{not json")])
    assert broken.problems[:1] == ("not_json: income",)


def test_pagination_truncation_is_reported() -> None:
    truncated = parse("00005", raw=[raw_payload("income", fixture_bytes("00005", "truncated"))])
    assert truncated.problems[:1] == ("pagination_truncated: income",)
    assert truncated.drafts == ()
    body = fixture_body("00005", "income")
    body["result"]["count"] += 1  # len(data) != count
    mismatch = parse("00005", raw=[raw_payload("income", body_bytes(body))])
    assert mismatch.problems[:1] == ("pagination_truncated: income",)


def test_duplicate_item_code_blocks_the_table() -> None:
    body = fixture_body("00005", "income")
    duplicate = copy.deepcopy(body["result"]["data"][3])
    assert duplicate["STD_ITEM_CODE"] == "001003999"
    body["result"]["data"].append(duplicate)
    body["result"]["count"] += 1
    result = parse(
        "00005",
        raw=[
            raw_payload("income", body_bytes(body)),
            raw_payload("balance", fixture_bytes("00005", "balance")),
            raw_payload("cashflow", fixture_bytes("00005", "cashflow")),
        ],
    )
    assert any(problem.startswith("duplicate_item_code: income") for problem in result.problems)
    assert {d.payload["report"] for d in result.drafts} == {"balance", "cashflow"}


def test_unexpected_period_is_reported() -> None:
    body = fixture_body("00005", "income")
    body["result"]["data"][0]["STD_REPORT_DATE"] = "2022-12-31 00:00:00"  # not requested
    result = parse(
        "00005",
        raw=[
            raw_payload("income", body_bytes(body)),
            raw_payload("balance", fixture_bytes("00005", "balance")),
            raw_payload("cashflow", fixture_bytes("00005", "cashflow")),
        ],
    )
    assert any(problem.startswith("unexpected_period: income") for problem in result.problems)
    assert {d.payload["report"] for d in result.drafts} == {"balance", "cashflow"}
    # A requested period the response does not carry fails the same check.
    wrong_year = parse("00005", raw=[raw_payload("income", fixture_bytes("00005", "income"))],
                       periods=(FY2023,))
    assert wrong_year.problems[:1] == (
        "unexpected_period: income reported ['2023-12-31', '2024-12-31'] != requested "
        "['2023-12-31']",
    )


def test_currency_mismatch_blocks_only_its_table() -> None:
    body = fixture_body("00005", "income")
    for row in body["result"]["data"]:
        row["CURRENCY_CODE"] = "HKD"  # HSBC reports in USD
    result = parse(
        "00005",
        raw=[
            raw_payload("income", body_bytes(body)),
            raw_payload("balance", fixture_bytes("00005", "balance")),
            raw_payload("cashflow", fixture_bytes("00005", "cashflow")),
        ],
    )
    assert "currency_mismatch: income provider ['HKD'] != report_currency 'USD'" in result.problems
    assert {d.payload["report"] for d in result.drafts} == {"balance", "cashflow"}


def test_cashflow_nesting_and_outer_report_date() -> None:
    result = parse("00005")
    outer = fixture_body("00005", "cashflow")["result"]["data"]
    assert [row["REPORT_DATE"][:10] for row in outer] == ["2024-12-31", "2023-12-31"]
    assert amount(result, "cashflow", "operating_cash_flow") == "65305000000"
    assert amount(result, "cashflow", "capex_ppe") == "1344000000"
    # A shape change (no nested VALUE_LIST) is a table-level failure.
    body = fixture_body("00005", "cashflow")
    del body["result"]["data"][0]["VALUE_LIST"]
    shape = parse("00005", raw=[raw_payload("cashflow", body_bytes(body))])
    assert "row_shape_invalid: cashflow" in shape.problems
    assert all(item.payload["report"] != "cashflow" for item in shape.drafts)
    # An outer report date outside the requested window fails the period check.
    body = fixture_body("00005", "cashflow")
    body["result"]["data"][0]["REPORT_DATE"] = "2022-12-31 00:00:00"
    period = parse("00005", raw=[raw_payload("cashflow", body_bytes(body))])
    assert any(problem.startswith("unexpected_period: cashflow") for problem in period.problems)


def test_missing_result_data_and_empty_rows() -> None:
    missing = parse(
        "00005",
        raw=[raw_payload("income", body_bytes({"success": True, "result": None}))],
    )
    assert missing.problems[:1] == ("missing_result_data: income",)
    assert missing.drafts == ()
    empty = parse(
        "00005",
        raw=[
            raw_payload(
                "income", body_bytes({"success": True, "result": {"pages": 1, "count": 0,
                                                                  "data": []}})
            )
        ],
    )
    assert empty.problems[:1] == ("missing_result_data: income",)


def test_unsupported_period_is_reported() -> None:
    instant = FactPeriod(start=None, end=date(2024, 12, 31), basis=PeriodBasis.INSTANT)
    result = parse("00005", periods=(instant,))
    assert result.drafts == ()
    expected = (
        "unsupported_period: 2024-12-31 has basis instant; the HK annual endpoints only "
        "serve annual reports"
    )
    assert expected in result.problems


def test_missing_raw_and_unknown_raw_names() -> None:
    result = parse(
        "00005",
        raw=[
            raw_payload("income", fixture_bytes("00005", "income")),
            raw_payload("balance", fixture_bytes("00005", "balance")),
            raw_payload("notes", fixture_bytes("00005", "income")),  # unknown name: ignored
        ],
    )
    assert "missing_raw: cashflow" in result.problems
    assert {d.payload["report"] for d in result.drafts} == {"income", "balance"}
    duplicated = parse(
        "00005",
        raw=[
            raw_payload("income", fixture_bytes("00005", "income")),
            raw_payload("income", fixture_bytes("00005", "income")),
        ],
    )
    assert "duplicate_raw: income" in duplicated.problems


def test_invalid_amount_is_reported_and_not_invented() -> None:
    body = fixture_body("00005", "income")
    body["result"]["data"][3]["AMOUNT"] = "not-a-number"
    result = parse("00005", raw=[raw_payload("income", body_bytes(body))])
    assert "invalid_amount: income/001003999 is not a number" in result.problems
    income = draft_for(result, "income", FY2024.end)
    assert "revenue_net_credit" not in {entry["metric"] for entry in income.payload["facts"]}


def test_null_amount_becomes_an_explicit_missing_fact() -> None:
    body = fixture_body("00005", "income")
    body["result"]["data"][3]["AMOUNT"] = None  # mapped row without a value
    result = parse("00005", raw=[raw_payload("income", body_bytes(body))])
    entry = fact(result, "income", FY2024.end, "revenue_net_credit")
    assert entry["value"]["raw_value"] is None
    assert entry["value"]["missing_reason"]
    assert entry["value"]["raw_unit"] == "元"
    assert entry["value"]["currency"] == "USD"
    assert not any(problem.startswith("invalid_amount") for problem in result.problems)


def test_unmapped_rows_are_counted_per_table_and_per_draft() -> None:
    result = parse("00005", periods=(FY2024, FY2023))
    assert result.problems == (
        "unmapped_items: income/3",
        "unmapped_items: balance/2",
        "unmapped_items: cashflow/4",
    )
    assert draft_for(result, "income", FY2024.end).payload["unmapped_item_count"] == 3
    assert draft_for(result, "income", FY2023.end).payload["unmapped_item_count"] == 0
    assert draft_for(result, "balance", FY2024.end).payload["unmapped_item_count"] == 2
    assert draft_for(result, "cashflow", FY2024.end).payload["unmapped_item_count"] == 2
    assert draft_for(result, "cashflow", FY2023.end).payload["unmapped_item_count"] == 2
    mapped_codes = {code for report, code in ITEM_MAP if report == "income"}
    assert mapped_codes  # the map is the only source of facts


# --- requests --------------------------------------------------------------


def test_requests_carry_the_controlled_secucode_with_leading_zeros() -> None:
    hsbc = company("00005")
    requests = HkNativeAdapter().requests(hsbc, periods=())
    assert [request.name for request in requests] == list(REPORTS)
    assert all(request.url == BASE for request in requests)
    assert source_symbol(hsbc, SOURCE) == "00005.HK"  # from the pilot list
    assert not source_symbol(hsbc, SOURCE).startswith("5.")
    expected_filter = (
        '(SECUCODE="00005.HK")(REPORT_DATE>=\'2021-01-01\')'
        "(REPORT_DATE<='2024-12-31')(REPORT_TYPE=\"年报\")"
    )
    for request, report_name in zip(requests, REPORTS.values(), strict=True):
        params = dict(request.params)
        assert params == {
            "reportName": report_name,
            "columns": "ALL",
            "filter": expected_filter,
            "pageNumber": "1",
            "pageSize": "500",
        }
    assert [period.end for period in DEFAULT_PERIODS] == [
        date(year, 12, 31) for year in range(2021, 2025)
    ]


def test_requests_window_follows_explicit_periods() -> None:
    requests = HkNativeAdapter().requests(company("00700"), periods=(FY2024,))
    params = dict(requests[0].params)
    assert params["filter"] == (
        '(SECUCODE="00700.HK")(REPORT_DATE>=\'2024-01-01\')'
        "(REPORT_DATE<='2024-12-31')(REPORT_TYPE=\"年报\")"
    )


def test_requests_refuse_an_unbound_identity() -> None:
    unbound = ControlledIdentity(
        market_code="HKEX",
        ticker="09988",
        company_key="example",
        company_name="Example Limited",
        company_name_zh="示例",
        security_type=company("00005").security_type,
        quote_currency="HKD",
        report_currency="HKD",
        fiscal_year_end_note="12-31",
        sources=(),
        valid_from=date(2026, 10, 11),
    )
    with pytest.raises(ValueError, match="no 'eastmoney.hk.native.v1' binding"):
        HkNativeAdapter().requests(unbound, periods=())


# --- issuer-specific conventions ------------------------------------------


def test_capex_sign_conventions_stay_issuer_specific() -> None:
    texts = {}
    for ticker in ("00005", "00002", "00700"):
        entry = fact(parse(ticker), "cashflow", FY2024.end, "capex_ppe")
        texts[ticker] = entry["value"]["sign_convention"]
        assert entry["value"]["raw_value"] == COMPANY_2024[ticker]["capex_ppe"]
    assert len(set(texts.values())) == 3, texts
    assert "net purchases" in texts["00005"]
    assert "capital expenditure" in texts["00002"]
    assert "prepayments" in texts["00700"]
    # Only the bank's line is flagged not applicable; the other two stay usable.
    assert fact(parse("00002"), "cashflow", FY2024.end, "capex_ppe")["applicability"] == (
        "applicable"
    )
    assert fact(parse("00700"), "cashflow", FY2024.end, "capex_ppe")["applicability"] == (
        "applicable"
    )


def test_hsbc_capex_is_not_applicable_for_a_bank() -> None:
    entry = fact(parse("00005"), "cashflow", FY2024.end, "capex_ppe")
    assert entry["applicability"] == "not_applicable"
    assert "bank" in entry["value"]["sign_convention"]
    assert entry["value"]["raw_value"] == "1344000000"  # value kept, use flagged


# --- draft envelope and fact metadata -------------------------------------


@freeze_time(FROZEN_NOW)
def test_draft_envelope_and_capture_time() -> None:
    result = parse("00005", periods=(FY2024, FY2023), subject_id=42)
    assert_all_drafts_valid(result)
    for item in result.drafts:
        assert item.kind is DatasetKind.ANNUAL_FACTS
        assert item.subject_kind is SubjectKind.COMPANY
        assert item.subject_id == 42
        assert item.source == SOURCE
        assert item.adapter_version == ADAPTER_VERSION
        assert item.schema_version == SCHEMA_VERSION
        assert item.period.basis is PeriodBasis.ANNUAL
        assert item.period.end in {FY2024.end, FY2023.end}
        assert item.captured_at == datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
        assert set(item.payload) == {
            "facts",
            "report",
            "report_name",
            "account_standard",
            "fiscal_year_label",
            "unmapped_item_count",
            "provider",
        }
        assert item.payload["report_name"] == REPORTS[item.payload["report"]]
        assert item.payload["provider"] == {"secucode": "00005.HK", "report_type": "年报"}
        assert item.payload["fiscal_year_label"] == "12-31"
    assert draft_for(result, "income", FY2024.end).payload["account_standard"] == "国际会计准则"
    assert draft_for(result, "balance", FY2024.end).payload["account_standard"] == "国际会计准则"


def test_fact_metadata_labels_locators_and_provenance() -> None:
    result = parse("00005", periods=(FY2024, FY2023))
    entry = fact(result, "income", FY2024.end, "revenue_net_credit")
    assert entry["source_label"] == "经营收入总额"
    assert entry["source_field"] == "001003999"
    assert entry["semantics"]["source_label"] == "经营收入总额"
    assert entry["semantics"]["accounting_standard"] == "ifrs"
    assert entry["semantics"]["reporting_scope"] == "consolidated"
    assert entry["semantics"]["equity_scope"] == "unknown"
    assert entry["semantics"]["per_share_basis"] == "not_applicable"
    assert entry["verification"] == "candidate"
    assert entry["conflict"] == "none"
    assert entry["provenance"]["source"] == SOURCE
    assert entry["provenance"]["original_hash"] == hashlib.sha256(
        fixture_bytes("00005", "income")
    ).hexdigest()
    assert entry["provenance"]["source_published_precision"] == "unknown"
    assert entry["provenance"]["source_published_at"] is None
    assert entry["provenance"]["source_published_date"] is None
    assert entry["provenance"]["accepted_at"] is None
    assert entry["provenance"]["source_available_at"] is None
    version = fixture_body("00005", "income")["version"]
    assert entry["source_version"] == version
    # The locator points at the verified FY2024 document page and is only used
    # for the documented fiscal year.
    assert entry["original_locator"].startswith("https://www.hsbc.com/")
    assert entry["original_locator"].endswith("?download=1 p12")
    assert fact(result, "cashflow", FY2024.end, "operating_cash_flow")[
        "original_locator"
    ].endswith("p18")
    assert fact(result, "income", FY2023.end, "revenue_net_credit")["original_locator"] is None
    # Balance items are all-parent equity; profit is ordinary shareholders'.
    assert fact(result, "balance", FY2024.end, "equity_parent")["semantics"][
        "equity_scope"
    ] == "all_parent_equity_holders"
    assert fact(result, "balance", FY2024.end, "equity_ordinary")["semantics"][
        "equity_scope"
    ] == "ordinary_shareholders"
    # CLP reports under Hong Kong standards; Tencent under IFRS.
    clp = fact(parse("00002"), "income", FY2024.end, "revenue_operating")
    assert clp["semantics"]["accounting_standard"] == "hkfrs"
    tencent = fact(parse("00700"), "income", FY2024.end, "revenue_operating")
    assert tencent["semantics"]["accounting_standard"] == "ifrs"


def test_parse_requires_a_company_subject_id() -> None:
    with pytest.raises(ValueError, match="subject_id"):
        parse("00005", subject_id=None)
