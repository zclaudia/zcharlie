"""Eastmoney annual financial adapter (industry peers WP7): field mapping from
the design table, Decimal-exact amounts, missing vs schema-error semantics,
retry / Retry-After behaviour, request ledger, host allow-list, ctype guard.
Fully offline: a fake injected source (and one httpx.MockTransport round trip
of the real source); conftest forbids real akshare and ``-m 'not network'``
forbids real HTTP."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.eastmoney_annual import (
    ALLOWED_HOSTS,
    DATACENTER_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    F10_BASE_URL,
    MAIN_FINADATA_PATH,
    MAIN_FINADATA_REPORT,
    OPERATION_CTYPE,
    PROVIDER,
    CompanyTypeUnknownError,
    EastmoneyAnnualCollector,
    EastmoneyAnnualDataError,
    EastmoneyAnnualError,
    EastmoneyAnnualSchemaError,
    EastmoneyAnnualTransientError,
    HttpEastmoneyAnnualSource,
    RawAnnualResponse,
    parse_ctype,
)
from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.providers.ledger import ProviderCallEvent

TICKER = "600519.SH"
SYMBOL = "SH600519"
SECUCODE = "600519.SH"
PERIOD = date(2025, 12, 31)
CLOCK = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
BACKOFF = 2.0

RESPONSE_ORDER = ("f10_ctype", "f10_income", "f10_balance", "f10_cashflow", "main_fina")


class NoSleep:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


class Capture:
    def __init__(self) -> None:
        self.events: list[ProviderCallEvent] = []

    async def __call__(self, event: ProviderCallEvent) -> None:
        self.events.append(event)


class FakeSource:
    """Answers from a queued list of :class:`RawAnnualResponse` / exceptions."""

    def __init__(self, responses: list[RawAnnualResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def fetch(self, url: str, params: dict[str, str]) -> RawAnnualResponse:
        self.calls.append((url, dict(params)))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def operations(self) -> list[str]:
        return [url.rsplit("/", 1)[-1] for url, _ in self.calls]


class SavedCache:
    """Returns one saved body for one operation (reuse seam)."""

    def __init__(self, operation: str, body: bytes) -> None:
        self.operation = operation
        self.body = body
        self.asked: list[str] = []

    def saved_response(self, operation: str, params: dict[str, str]) -> bytes | None:
        self.asked.append(operation)
        return self.body if operation == self.operation else None


# --------------------------------------------------------------------------- #
# Answer builders (row shapes per the run-02 evidence)
# --------------------------------------------------------------------------- #


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "SECUCODE": SECUCODE,
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "REPORT_TYPE": "年报",
        "NOTICE_DATE": "2026-04-25 00:00:00",
        "UPDATE_DATE": "2026-04-25 00:00:00",
        "CURRENCY": "CNY",
    }
    row.update(overrides)
    return row


def income_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "OPERATE_INCOME": 25731010647.32,
        "TOTAL_OPERATE_INCOME": 25898434324.18,
        "PARENT_NETPROFIT": 86228146421.62,
    }
    row.update(overrides)
    return _row(**row)


def balance_row(**overrides: object) -> dict[str, object]:
    row = {
        "TOTAL_PARENT_EQUITY": 200456087654.32,
        "CONTRACT_LIAB": 32338664843.91,
    }
    row.update(overrides)
    return _row(**row)


def cashflow_row(**overrides: object) -> dict[str, object]:
    row = {"NETCASH_OPERATE": 92435296907.06}
    row.update(overrides)
    return _row(**row)


def main_row(**overrides: object) -> dict[str, object]:
    row = {"ROEJQ": 8.62}
    row.update(overrides)
    return _row(**row)


def statement_body(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"version": "4.0", "data": rows, "success": True, "message": "ok", "code": 0},
        ensure_ascii=False,
    ).encode("utf-8")


def main_body(rows: list[dict[str, object]], *, pages: int = 1) -> bytes:
    return json.dumps(
        {
            "version": "4.0",
            "result": {
                "pages": pages,
                "pageNo": 1,
                "pageSize": 100,
                "total": len(rows),
                "data": rows,
            },
            "success": True,
            "message": "ok",
            "code": 0,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def ctype_page(ctype: str = "4") -> bytes:
    return (
        "<!DOCTYPE html><html><head><title>F10</title></head><body>"
        f'<input type="hidden" id="hidctype" value="{ctype}" />'
        "</body></html>"
    ).encode()


def ok(body: bytes, *, status: int = 200, retry_after: str | None = None) -> RawAnnualResponse:
    return RawAnnualResponse(status=status, body=body, retry_after=retry_after)


def happy_source(**row_overrides: object) -> FakeSource:
    """A queue of five 200 answers: ctype, income, balance, cashflow, main.

    Keyword ``<table>_row`` overrides patch the single row of that table,
    e.g. ``happy_source(balance_row={"CONTRACT_LIAB": None})``.
    """

    def row_for(table: str, builder: object) -> dict[str, object]:
        override = row_overrides.get(f"{table}_row")
        if isinstance(override, dict):
            return builder(**override)  # type: ignore[operator]
        return builder()  # type: ignore[operator]

    return FakeSource(
        [
            ok(ctype_page()),
            ok(statement_body([row_for("income", income_row)])),
            ok(statement_body([row_for("balance", balance_row)])),
            ok(statement_body([row_for("cashflow", cashflow_row)])),
            ok(main_body([row_for("main", main_row)])),
        ]
    )


def happy_bodies() -> dict[str, bytes]:
    return {
        OPERATION_CTYPE: ctype_page(),
        "income": statement_body([income_row()]),
        "balance": statement_body([balance_row()]),
        "cashflow": statement_body([cashflow_row()]),
        "main": main_body([main_row()]),
    }


def statements_source(**row_overrides: object) -> FakeSource:
    """A queue of four 200 answers without the ctype page (income, balance,
    cashflow, main) - for a caller-provided or reused company type."""

    def row_for(table: str, builder: object) -> dict[str, object]:
        override = row_overrides.get(f"{table}_row")
        if isinstance(override, dict):
            return builder(**override)  # type: ignore[operator]
        return builder()  # type: ignore[operator]

    return FakeSource(
        [
            ok(statement_body([row_for("income", income_row)])),
            ok(statement_body([row_for("balance", balance_row)])),
            ok(statement_body([row_for("cashflow", cashflow_row)])),
            ok(main_body([row_for("main", main_row)])),
        ]
    )


def make_collector(
    source: FakeSource,
    *,
    sleep: NoSleep | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    **kwargs: object,
) -> EastmoneyAnnualCollector:
    return EastmoneyAnnualCollector(
        source,
        clock=lambda: CLOCK,
        max_retries=max_retries,
        retry_backoff_seconds=BACKOFF,
        min_interval_seconds=0.0,
        sleep=sleep or NoSleep(),
        **kwargs,  # type: ignore[arg-type]
    )


def collect(source: FakeSource, **kwargs: object):
    collector = make_collector(source, **kwargs)
    return collector, collector.collect_company_annual(TICKER, PERIOD)


async def collected(source: FakeSource, **kwargs: object):
    """Run a happy-path collection; returns ``(collector, result)``."""

    collector = make_collector(source, **kwargs)
    result = await collector.collect_company_annual(TICKER, PERIOD)
    return collector, result


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _unquote_number(body: bytes, marker: str, literal: str) -> bytes:
    """Turn a quoted JSON string into a raw JSON number literal."""

    text = body.decode("utf-8")
    quoted = json.dumps(marker)
    assert quoted in text
    return text.replace(quoted, literal).encode("utf-8")


# --------------------------------------------------------------------------- #
# ctype metadata parsing
# --------------------------------------------------------------------------- #


def test_parse_ctype_handles_attribute_orders_and_refuses_unknowns():
    assert parse_ctype('<input id="hidctype" value="4">') == "4"
    assert parse_ctype("<INPUT VALUE='3' ID=\"hidctype\">") == "3"
    assert parse_ctype('<input id="hidctype" value="" />') is None
    assert parse_ctype('<input id="hidctype" value="general" />') is None
    assert parse_ctype("<html><body>no metadata</body></html>") is None


# --------------------------------------------------------------------------- #
# Field mapping (the design table)
# --------------------------------------------------------------------------- #


async def test_every_field_is_mapped_from_its_design_table_source():
    bodies = happy_bodies()
    source = happy_source()
    collector, task = collect(source)
    result = await task

    assert set(result.fields) == {
        "operating_revenue",
        "net_profit_parent",
        "revenue_total_check",
        "equity_parent",
        "contract_liabilities",
        "operating_cash_flow",
        "roe_weighted_annual",
    }
    revenue = result.fields["operating_revenue"]
    assert revenue.source_table == "income" and revenue.source_field == "OPERATE_INCOME"
    assert revenue.value == Decimal("25731010647.32")
    assert revenue.unit == "CNY" and revenue.currency == "CNY"
    assert revenue.raw_value == "25731010647.32" and revenue.raw_unit == "CNY"
    assert not revenue.check_only
    assert revenue.response_hash == sha(bodies["income"])

    profit = result.fields["net_profit_parent"]
    assert profit.source_field == "PARENT_NETPROFIT"
    assert profit.value == Decimal("86228146421.62") and profit.currency == "CNY"

    check = result.fields["revenue_total_check"]
    assert check.source_field == "TOTAL_OPERATE_INCOME"
    assert check.value == Decimal("25898434324.18")
    assert check.check_only  # 核对项语义: carried, marked, never a ratio denominator
    assert check.unit == "CNY"

    equity = result.fields["equity_parent"]
    assert equity.source_table == "balance" and equity.source_field == "TOTAL_PARENT_EQUITY"
    assert equity.value == Decimal("200456087654.32")
    liabilities = result.fields["contract_liabilities"]
    assert liabilities.source_field == "CONTRACT_LIAB"
    assert liabilities.value == Decimal("32338664843.91")

    cfo = result.fields["operating_cash_flow"]
    assert cfo.source_table == "cashflow" and cfo.source_field == "NETCASH_OPERATE"
    assert cfo.value == Decimal("92435296907.06")

    roe = result.fields["roe_weighted_annual"]
    assert roe.source_table == "main" and roe.source_field == "ROEJQ"

    assert result.company_type == "4" and result.company_type_source == "f10_metadata"
    assert result.ticker == SECUCODE and result.report_period == PERIOD
    assert result.provider == PROVIDER
    assert result.missing_metrics() == ()
    assert collector.request_log is not None


async def test_roe_percent_divided_by_100_exactly_once_with_raw_kept():
    source = happy_source(main_row={"ROEJQ": 34.73})
    result = (await collected(source))[1]
    roe = result.fields["roe_weighted_annual"]
    assert roe.value == Decimal("0.3473")  # 34.73 % -> fraction, once
    assert roe.value != Decimal("0.003473")  # a second /100 would be this
    assert roe.raw_value == "34.73" and roe.raw_unit == "percent"
    assert roe.unit == "fraction" and roe.currency is None
    assert str(roe.value) == "0.3473"  # decimal string keeps the exact digits


async def test_amounts_keep_decimal_precision_without_a_float_round_trip():
    # A literal float() cannot represent: a float round trip would corrupt the
    # digits, parse_float=Decimal keeps them exact.
    source = happy_source()
    source.responses[1] = ok(
        _unquote_number(
            statement_body([income_row(OPERATE_INCOME="placeholder")]),
            marker="placeholder",
            literal="123456789012345678.90123",
        )
    )
    result = (await collected(source))[1]
    revenue = result.fields["operating_revenue"]
    assert revenue.value == Decimal("123456789012345678.90123")
    assert str(revenue.value) == "123456789012345678.90123"
    assert revenue.raw_value == "123456789012345678.90123"


# --------------------------------------------------------------------------- #
# Missing vs schema error semantics
# --------------------------------------------------------------------------- #


async def test_upstream_null_is_a_missing_field_not_an_error():
    source = happy_source(balance_row={"CONTRACT_LIAB": None})
    result = (await collected(source))[1]
    field = result.fields["contract_liabilities"]
    assert field.value is None and field.missing_reason == "upstream_null"
    assert field.raw_value is None and field.raw_unit == "CNY"
    assert field.currency == "CNY"
    assert field.response_hash  # the value is traceable to its answer
    assert "contract_liabilities" in result.missing_metrics()
    equity = result.fields["equity_parent"]
    assert equity.value == Decimal("200456087654.32")  # siblings still parsed


async def test_undisclosed_period_is_a_gap_not_an_error():
    source = FakeSource(
        [
            ok(ctype_page()),
            ok(statement_body([])),  # income: annual report not disclosed yet
            ok(statement_body([balance_row()])),
            ok(statement_body([cashflow_row()])),
            ok(main_body([], pages=1)),  # main indicators: no row either
        ]
    )
    result = (await collected(source))[1]
    for metric in ("operating_revenue", "net_profit_parent", "roe_weighted_annual"):
        field = result.fields[metric]
        assert field.value is None and field.missing_reason == "no_statement_row"
        assert field.response_hash is None
    assert result.fields["equity_parent"].value == Decimal("200456087654.32")
    assert set(result.source_rows) == {"balance", "cashflow"}


async def test_missing_declared_column_is_a_schema_error_and_not_retried():
    source = happy_source()
    source.responses[2] = ok(
        statement_body([{k: v for k, v in balance_row().items() if k != "CONTRACT_LIAB"}])
    )
    collector, task = collect(source)
    with pytest.raises(EastmoneyAnnualSchemaError, match="CONTRACT_LIAB"):
        await task
    # ctype + income + the single (failed) balance attempt: nothing was retried.
    assert len(source.calls) == 3
    assert [record.outcome for record in collector.request_log] == ["ok", "ok", "ok"]
    assert source.operations()[-1] == "zcfzbAjaxNew"


async def test_html_at_200_is_a_schema_error_without_retry():
    source = FakeSource([ok(ctype_page()), ok(b"<html>blocked</html>")])
    capture = Capture()
    collector, task = collect(source, on_call=capture)
    with pytest.raises(EastmoneyAnnualSchemaError, match="non-JSON"):
        await task
    assert len(source.calls) == 2  # ctype ok, income attempted exactly once
    # The HTTP request itself succeeded (200, recorded), but the logical call
    # is terminal in the provider ledger - and it was never retried.
    assert [e.outcome for e in capture.events] == ["ok", "terminal"]
    last = collector.request_log[-1]
    assert last.status == 200 and last.outcome == "ok"
    assert last.response_sha256 == sha(b"<html>blocked</html>")


async def test_truncated_pagination_is_an_error_without_retry():
    source = happy_source()
    source.responses[4] = ok(main_body([main_row(), main_row()], pages=2))
    collector, task = collect(source)
    with pytest.raises(EastmoneyAnnualSchemaError, match="truncated"):
        await task
    assert len(source.calls) == 5  # the main answer was not re-requested
    assert collector.request_log[-1].status == 200


async def test_missing_pagination_information_is_an_error():
    source = happy_source()
    source.responses[4] = ok(
        json.dumps({"result": {"data": [main_row()]}, "success": True}, ensure_ascii=False).encode(
            "utf-8"
        )
    )
    with pytest.raises(EastmoneyAnnualSchemaError, match="pagination"):
        (await collected(source))[1]


async def test_duplicate_report_period_is_a_data_error_and_not_retried():
    source = happy_source()
    source.responses[1] = ok(statement_body([income_row(), income_row()]))
    collector, task = collect(source)
    with pytest.raises(EastmoneyAnnualDataError, match="duplicate report period") as excinfo:
        await task
    # A duplicate period is a data error, not the schema-error subclass: the
    # rows exist, their structure is intact - the payload cannot be trusted.
    assert not isinstance(excinfo.value, EastmoneyAnnualSchemaError)
    assert len(source.calls) == 2  # one income attempt only
    assert collector.request_log[-1].outcome == "ok"  # HTTP itself succeeded


async def test_row_for_another_period_or_company_is_a_schema_error():
    wrong_period = happy_source()
    wrong_period.responses[1] = ok(statement_body([income_row(REPORT_DATE="2024-12-31 00:00:00")]))
    with pytest.raises(EastmoneyAnnualSchemaError, match="2024-12-31"):
        (await collected(wrong_period))[1]

    wrong_company = happy_source()
    wrong_company.responses[3] = ok(statement_body([cashflow_row(SECUCODE="000858.SZ")]))
    with pytest.raises(EastmoneyAnnualSchemaError, match="000858.SZ"):
        (await collected(wrong_company))[1]


async def test_non_cny_currency_is_refused():
    source = happy_source(cashflow_row={"CURRENCY": "HKD"})
    with pytest.raises(EastmoneyAnnualDataError, match="CURRENCY"):
        (await collected(source))[1]


async def test_non_finite_and_non_numeric_values_are_refused():
    source = happy_source()
    source.responses[1] = ok(
        statement_body([income_row(OPERATE_INCOME="n/a")]).replace(b'"n/a"', b"NaN")
    )
    with pytest.raises(EastmoneyAnnualDataError, match="non-finite"):
        (await collected(source))[1]

    source = happy_source()
    source.responses[1] = ok(statement_body([income_row(OPERATE_INCOME=True)]))
    with pytest.raises(EastmoneyAnnualDataError, match="not an exact number"):
        (await collected(source))[1]


# --------------------------------------------------------------------------- #
# Retry policy: 5xx / network / 429 / zero retries on schema errors
# --------------------------------------------------------------------------- #


async def test_transient_5xx_retries_twice_then_raises():
    source = FakeSource(
        [
            ok(ctype_page()),
            ok(b"boom", status=503),
            ok(b"boom", status=503),
            ok(b"boom", status=503),
        ]
    )
    sleep = NoSleep()
    capture = Capture()
    collector, task = collect(source, sleep=sleep, on_call=capture)
    with pytest.raises(EastmoneyAnnualTransientError, match="HTTP 503"):
        await task
    assert len(source.calls) == 4  # 1 ctype + 3 income attempts
    assert sleep.slept == [BACKOFF, BACKOFF * 2]
    assert [record.status for record in collector.request_log] == [200, 503, 503, 503]
    assert all(record.outcome == "transient" for record in collector.request_log[1:])
    assert [(e.endpoint, e.outcome) for e in capture.events] == [
        (OPERATION_CTYPE, "ok"),
        ("f10_income", "transient"),
    ]


async def test_429_sleeps_retry_after_then_retries():
    bodies = happy_bodies()
    source = FakeSource(
        [
            ok(bodies[OPERATION_CTYPE]),
            ok(b"slow down", status=429, retry_after="7"),
            ok(bodies["income"]),
            ok(bodies["balance"]),
            ok(bodies["cashflow"]),
            ok(bodies["main"]),
        ]
    )
    sleep = NoSleep()
    collector, task = collect(source, sleep=sleep)
    result = await task
    assert result.fields["operating_revenue"].value == Decimal("25731010647.32")
    assert sleep.slept == [7.0]  # obeyed Retry-After, not the backoff
    obeyed = collector.request_log[1]
    assert obeyed.status == 429 and obeyed.retry_after_seconds == 7.0
    assert obeyed.outcome == "transient"
    assert obeyed.response_sha256 == sha(b"slow down")
    assert collector.request_log[-1].attempt == 1  # the last (main) call, not a retry


async def test_429_without_retry_after_falls_back_to_backoff():
    bodies = happy_bodies()
    source = FakeSource(
        [
            ok(bodies[OPERATION_CTYPE]),
            ok(b"slow down", status=429),
            ok(bodies["income"]),
            ok(bodies["balance"]),
            ok(bodies["cashflow"]),
            ok(bodies["main"]),
        ]
    )
    sleep = NoSleep()
    collector, task = collect(source, sleep=sleep)
    await task
    assert sleep.slept == [BACKOFF]
    assert collector.request_log[1].retry_after_seconds is None


async def test_retry_after_http_date_is_resolved_against_the_clock():
    bodies = happy_bodies()
    source = FakeSource(
        [
            ok(bodies[OPERATION_CTYPE]),
            ok(b"later", status=429, retry_after="Sun, 11 Oct 2026 00:00:00 GMT"),
            ok(bodies["income"]),
            ok(bodies["balance"]),
            ok(bodies["cashflow"]),
            ok(bodies["main"]),
        ]
    )
    sleep = NoSleep()
    collector, task = collect(source, sleep=sleep)
    await task
    assert sleep.slept == [16 * 3600.0]  # CLOCK is 2026-10-10 08:00 UTC
    assert collector.request_log[1].retry_after_seconds == 16 * 3600.0


async def test_network_error_is_retried_and_recorded_without_a_status():
    bodies = happy_bodies()
    source = FakeSource(
        [
            ok(bodies[OPERATION_CTYPE]),
            EastmoneyAnnualTransientError("connection reset", provider=PROVIDER),
            ok(bodies["income"]),
            ok(bodies["balance"]),
            ok(bodies["cashflow"]),
            ok(bodies["main"]),
        ]
    )
    sleep = NoSleep()
    collector, task = collect(source, sleep=sleep)
    result = await task
    assert result.fields["operating_revenue"].value == Decimal("25731010647.32")
    failed = collector.request_log[1]
    assert failed.status is None and failed.outcome == "transient"
    assert failed.response_sha256 is None and "connection reset" in (failed.error or "")
    assert sleep.slept == [BACKOFF]


async def test_max_retries_zero_makes_transient_failures_immediate():
    source = FakeSource([ok(ctype_page()), ok(b"boom", status=500)])
    sleep = NoSleep()
    collector, task = collect(source, sleep=sleep, max_retries=0)
    with pytest.raises(EastmoneyAnnualTransientError, match="HTTP 500"):
        await task
    assert len(source.calls) == 2 and sleep.slept == []


async def test_constructor_rejects_negative_knobs():
    with pytest.raises(ValueError):
        EastmoneyAnnualCollector(max_retries=-1)
    with pytest.raises(ValueError):
        EastmoneyAnnualCollector(min_interval_seconds=-0.5)


# --------------------------------------------------------------------------- #
# Request ledger, records, reuse
# --------------------------------------------------------------------------- #


async def test_request_log_records_every_attempt_with_hash_and_params():
    bodies = happy_bodies()
    source = happy_source()
    collector, task = collect(source)
    result = await task

    assert [record.operation for record in result.requests] == list(RESPONSE_ORDER)
    ctype, income, balance, cashflow, main = result.requests
    assert ctype.url == f"{F10_BASE_URL}/PC_HSF10/NewFinanceAnalysis/Index"
    assert ctype.params == {"type": "web", "code": "sh600519"}
    assert income.url == f"{F10_BASE_URL}/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew"
    assert income.params == {
        "companyType": "4",
        "reportDateType": "0",
        "reportType": "1",
        "dates": "2025-12-31",
        "code": SYMBOL,
    }
    assert main.url == f"{DATACENTER_BASE_URL}{MAIN_FINADATA_PATH}"
    assert main.params["reportName"] == MAIN_FINADATA_REPORT
    assert main.params["filter"] == f"(SECUCODE=\"{SECUCODE}\")(REPORT_DATE in ('2025-12-31'))"
    for record, body in zip(
        result.requests,
        [
            bodies[OPERATION_CTYPE],
            bodies["income"],
            bodies["balance"],
            bodies["cashflow"],
            bodies["main"],
        ],
        strict=True,
    ):
        assert record.at == CLOCK
        assert record.status == 200 and record.outcome == "ok" and record.attempt == 1
        assert record.response_sha256 == sha(body)
        assert record.retry_after_seconds is None
        assert record.duration_ms is not None


async def test_caller_provided_company_type_skips_the_f10_page():
    source = statements_source()  # no ctype page in the queue
    collector = make_collector(source)
    result = await collector.collect_company_annual(TICKER, PERIOD, company_type="4")
    assert result.company_type == "4" and result.company_type_source == "caller_provided"
    assert [record.operation for record in result.requests] == list(RESPONSE_ORDER[1:])
    assert source.operations() == ["lrbAjaxNew", "zcfzbAjaxNew", "xjllbAjaxNew", "get"]

    unusable = happy_source()
    collector2 = make_collector(unusable)
    with pytest.raises(CompanyTypeUnknownError, match="not usable"):
        await collector2.collect_company_annual(TICKER, PERIOD, company_type="general")
    assert unusable.calls == []  # refused before any request


async def test_ctype_unknown_fails_the_company_without_statement_requests():
    source = FakeSource([ok(b"<html>no metadata</html>")])
    capture = Capture()
    collector, task = collect(source, on_call=capture)
    with pytest.raises(CompanyTypeUnknownError, match="600519.*not collectible"):
        await task
    assert len(source.calls) == 1  # only the F10 page: no guessed statement calls
    assert source.operations() == ["Index"]
    # The HTTP call returned 200 (recorded), but the logical call is terminal.
    assert collector.request_log[0].status == 200
    assert [e.outcome for e in capture.events] == ["terminal"]


async def test_http_404_on_the_ctype_page_is_company_not_collectible():
    source = FakeSource([ok(b"not found", status=404)])
    with pytest.raises(CompanyTypeUnknownError, match="HTTP 404"):
        (await collected(source))[1]
    assert len(source.calls) == 1


async def test_saved_response_reuse_avoids_the_http_request():
    bodies = happy_bodies()
    cache = SavedCache(OPERATION_CTYPE, bodies[OPERATION_CTYPE])
    source = statements_source()  # no ctype page: the cache answers that step
    collector = make_collector(source, cache=cache)
    result = await collector.collect_company_annual(TICKER, PERIOD)
    assert cache.asked[0] == OPERATION_CTYPE
    assert "Index" not in source.operations()
    reused = result.requests[0]
    assert reused.outcome == "reused" and reused.attempt == 0 and reused.status is None
    assert reused.response_sha256 == sha(bodies[OPERATION_CTYPE])
    assert result.company_type == "4" and result.company_type_source == "f10_metadata"
    # The remaining four steps still hit the source.
    assert len(source.calls) == 4


async def test_circuit_open_blocks_requests_and_reports_skipped_open():
    circuit = CircuitBreaker(lambda: CLOCK)
    source = happy_source()
    capture = Capture()
    collector, task = collect(source, circuit=circuit, on_call=capture)
    circuit.open_now(PROVIDER, EastmoneyAnnualTransientError("HTTP 503", provider=PROVIDER))
    with pytest.raises(ProviderUnavailableError):
        await task
    assert source.calls == []
    assert [e.outcome for e in capture.events] == ["skipped_open"]


# --------------------------------------------------------------------------- #
# Argument guards
# --------------------------------------------------------------------------- #


async def test_non_annual_period_and_unknown_ticker_are_rejected():
    source = happy_source()
    collector = make_collector(source)
    with pytest.raises(ValueError, match="annual report period"):
        await collector.collect_company_annual(TICKER, date(2025, 6, 30))
    with pytest.raises(ValueError, match="canonical A-share ticker"):
        await collector.collect_company_annual("AAPL", PERIOD)
    assert source.calls == []  # refused before any request


async def test_defaults_match_the_plan():
    assert DEFAULT_TIMEOUT_SECONDS == 30.0
    assert DEFAULT_MAX_RETRIES == 2
    assert set(ALLOWED_HOSTS) == {
        "emweb.securities.eastmoney.com",
        "datacenter.eastmoney.com",
    }


# --------------------------------------------------------------------------- #
# Real HTTP source (httpx.MockTransport): allow-list, timeout, Retry-After
# --------------------------------------------------------------------------- #


async def test_source_refuses_hosts_outside_the_allow_list():
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"request left the allow-list: {request.url}")

    guarded = HttpEastmoneyAnnualSource(transport=httpx.MockTransport(fail_if_called))
    for url in (
        "https://example.com/PC_HSF10/NewFinanceAnalysis/Index",
        "https://eastmoney.com.evil.com/securities/api/data/v1/get",
        "http://emweb.securities.eastmoney.com.evil.tld/x",
    ):
        with pytest.raises(EastmoneyAnnualError, match="allow"):
            await guarded.fetch(url, {})


@respx.mock
async def test_source_roundtrip_carries_status_body_and_retry_after():
    route = respx.get(url__regex=rf"{F10_BASE_URL}/PC_HSF10/.*").mock(
        return_value=httpx.Response(429, content=b"slow down", headers={"Retry-After": "7"})
    )
    source = HttpEastmoneyAnnualSource()
    response = await source.fetch(
        f"{F10_BASE_URL}/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew", {"code": SYMBOL}
    )
    assert route.call_count == 1
    assert route.calls[0].request.headers["User-Agent"].startswith("Mozilla/5.0")
    assert response.status == 429
    assert response.body == b"slow down"
    assert response.retry_after == "7"

    respx.get(url__regex=rf"{F10_BASE_URL}/PC_HSF10/.*").mock(
        side_effect=httpx.ConnectTimeout("slow")
    )
    with pytest.raises(EastmoneyAnnualTransientError, match="timed out"):
        await source.fetch(
            f"{F10_BASE_URL}/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew", {"code": SYMBOL}
        )
