"""Offline tests for Eastmoney's bank annual adapter."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from investment_agent.providers.eastmoney_annual import (
    EastmoneyAnnualDataError,
    EastmoneyAnnualSchemaError,
    RawAnnualResponse,
)
from investment_agent.providers.eastmoney_bank_annual import (
    BANK_ADAPTER_VERSION,
    BANK_ANNUAL_METRICS,
    EastmoneyBankAnnualCollector,
    parse_bank_main_indicators,
)

PERIOD = date(2025, 12, 31)


def body(*, root_version: object = "bank-source-v2", **changes: object) -> bytes:
    row: dict[str, object] = {
        "SECUCODE": "601939.SH",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "REPORT_TYPE": "年报",
        "NOTICE_DATE": "2026-03-28 00:00:00",
        "UPDATE_DATE": "2026-03-28 00:00:00",
        "CURRENCY": "CNY",
        "ORG_TYPE": "银行",
        "ROEJQ": 10.04,
        "NET_INTEREST_MARGIN": 1.34,
        "NONPERLOAN": 1.31,
        "BLDKBBL": 233.15,
    }
    row.update(changes)
    return json.dumps(
        {"success": True, "version": root_version, "result": {"pages": 1, "data": [row]}}
    ).encode()


class FakeSource:
    def __init__(self, answer: bytes | None = None) -> None:
        self.answer = answer or body()
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def fetch(self, url: str, params: object) -> RawAnnualResponse:
        self.calls.append((url, dict(params)))  # type: ignore[arg-type]
        return RawAnnualResponse(status=200, body=self.answer)


@pytest.mark.asyncio
async def test_collects_single_bank_year_with_exact_mapping_and_metadata() -> None:
    source = FakeSource()
    collector = EastmoneyBankAnnualCollector(
        source, min_interval_seconds=0, clock=lambda: datetime(2026, 10, 17, tzinfo=UTC)
    )
    result = await collector.collect_company_annual("601939.SH", PERIOD)

    assert result.adapter_version == BANK_ADAPTER_VERSION == "bank-annual-v1"
    assert tuple(result.fields) == BANK_ANNUAL_METRICS
    assert result.fields["roe_weighted_annual"].value == Decimal("0.1004")
    assert result.fields["net_interest_margin"].value == Decimal("0.0134")
    assert result.fields["npl_ratio"].value == Decimal("0.0131")
    assert result.fields["provision_coverage"].value == Decimal("2.3315")
    equity = result.fields["equity_parent"]
    assert equity.value is None
    assert equity.missing_reason == "unverified_mapping_reconcile_annual_report"
    assert result.source_rows["main"].notice_date == date(2026, 3, 28)
    assert result.source_rows["main"].update_date == date(2026, 3, 28)
    assert result.source_rows["main"].source_version == "bank-source-v2"
    assert result.company_type_source == "bank_main_metadata"
    assert len(source.calls) == 1
    params = source.calls[0][1]
    assert 'SECUCODE="601939.SH"' in params["filter"]
    assert PERIOD.isoformat() in params["filter"]
    assert len(result.requests) == 1 and result.requests[0].response_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"SECUCODE": "000001.SZ"},
        {"REPORT_DATE": "2024-12-31 00:00:00"},
        {"REPORT_TYPE": "三季报"},
        {"CURRENCY": "USD"},
        {"ORG_TYPE": "一般企业"},
        {"ORG_TYPE": None},
        {"ROEJQ": "NaN"},
        {"NOTICE_DATE": None},
    ],
)
def test_refuses_untrusted_rows(changes: dict[str, object]) -> None:
    with pytest.raises((EastmoneyAnnualDataError, EastmoneyAnnualSchemaError, ValueError)):
        parse_bank_main_indicators(
            body(**changes), secucode="601939.SH", wanted=PERIOD, response_hash="abc"
        )


def test_requires_all_four_indicator_columns_and_single_page() -> None:
    missing = json.loads(body())
    del missing["result"]["data"][0]["BLDKBBL"]
    with pytest.raises(EastmoneyAnnualSchemaError, match="columns"):
        parse_bank_main_indicators(
            json.dumps(missing).encode(), secucode="601939.SH", wanted=PERIOD, response_hash="x"
        )


def test_requires_org_type_column_even_when_the_fixture_excerpt_omits_it() -> None:
    raw = json.loads(body())
    del raw["result"]["data"][0]["ORG_TYPE"]
    with pytest.raises(EastmoneyAnnualSchemaError, match="ORG_TYPE"):
        parse_bank_main_indicators(
            json.dumps(raw).encode(), secucode="601939.SH", wanted=PERIOD, response_hash="x"
        )
    paged = json.loads(body())
    paged["result"]["pages"] = 2
    with pytest.raises(EastmoneyAnnualSchemaError, match="pagination"):
        parse_bank_main_indicators(
            json.dumps(paged).encode(), secucode="601939.SH", wanted=PERIOD, response_hash="x"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"ORG_TYPE": "一般企业"},
        {"NOTICE_DATE": ""},
        {"CURRENCY": "USD"},
        {"REPORT_TYPE": "2025年年报（更正）"},
        {"REPORT_TYPE": "2024年年报"},
        {"REPORT_DATE": "2025-12-31suffix"},
    ],
)
def test_requires_issuer_period_annual_classification_currency_and_notice(
    changes: dict[str, object],
) -> None:
    with pytest.raises((EastmoneyAnnualSchemaError, EastmoneyAnnualDataError)):
        parse_bank_main_indicators(
            body(**changes), secucode="601939.SH", wanted=PERIOD, response_hash="x"
        )


def test_null_update_date_is_preserved_and_source_version_is_opaque() -> None:
    parsed, _ = parse_bank_main_indicators(
        body(root_version="opaque-abc", UPDATE_DATE=None, REPORT_TYPE="2025年年报"),
        secucode="601939.SH",
        wanted=PERIOD,
        response_hash="raw-hash",
    )
    assert parsed.row is not None
    assert parsed.row.update_date is None
    assert parsed.row.source_version == "opaque-abc"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_nonfinite_json_values(value: str) -> None:
    raw = body().replace(b"10.04", value.encode())
    with pytest.raises(EastmoneyAnnualDataError):
        parse_bank_main_indicators(raw, secucode="601939.SH", wanted=PERIOD, response_hash="x")


def test_duplicate_rows_and_page_count_conflict_are_refused() -> None:
    duplicated = json.loads(body())
    duplicated["result"]["data"].append(duplicated["result"]["data"][0])
    with pytest.raises(EastmoneyAnnualDataError, match="duplicate"):
        parse_bank_main_indicators(
            json.dumps(duplicated).encode(), secucode="601939.SH", wanted=PERIOD, response_hash="x"
        )
    contradiction = json.loads(body())
    contradiction["result"]["pages"] = 0
    with pytest.raises(EastmoneyAnnualSchemaError, match="pagination"):
        parse_bank_main_indicators(
            json.dumps(contradiction).encode(),
            secucode="601939.SH",
            wanted=PERIOD,
            response_hash="x",
        )


def test_empty_annual_result_is_an_explicit_gap() -> None:
    empty = json.dumps(
        {"success": True, "version": "upstream-v1", "result": {"pages": 0, "data": []}}
    ).encode()
    parsed, org_type = parse_bank_main_indicators(
        empty, secucode="601939.SH", wanted=PERIOD, response_hash="x"
    )
    assert parsed.row is None and org_type == ""


@pytest.mark.asyncio
async def test_future_notice_date_is_refused_before_result_is_returned() -> None:
    source = FakeSource(body(NOTICE_DATE="2026-10-18 00:00:00"))
    collector = EastmoneyBankAnnualCollector(
        source,
        min_interval_seconds=0,
        clock=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(EastmoneyAnnualSchemaError, match="later than the capture"):
        await collector.collect_company_annual("601939.SH", PERIOD)


def test_rejects_html_and_non_success_response() -> None:
    for raw in (b"<html>error</html>", b'{"success":false,"message":"error"}'):
        with pytest.raises(EastmoneyAnnualSchemaError):
            parse_bank_main_indicators(raw, secucode="601939.SH", wanted=PERIOD, response_hash="x")


def test_tracked_minimal_fixture_omits_org_type_so_classifier_is_tested_separately() -> None:
    """The run-02 excerpts omit ORG_TYPE; local valid rows above add its observed value."""
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "fixtures/bank_run02/raw/financial/601939.SH_main.json"
    row = json.loads(fixture.read_text())["result"]["data"][0]
    assert "ORG_TYPE" not in row
