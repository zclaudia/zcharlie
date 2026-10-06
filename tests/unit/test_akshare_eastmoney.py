"""AkShare Eastmoney adapter (P1-A11): fixtures replay, schema locks, units, throttle."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from investment_agent.providers.akshare_eastmoney import (
    SUMMARY_COLUMNS,
    AkshareEastmoneyClient,
    EastmoneyError,
    EastmoneySchemaError,
    EastmoneyTransientError,
    RawTable,
    _EastmoneyRequests,
    _NoData,
    parse_cash_flow_statement,
    parse_period_summaries,
    to_eastmoney_symbol,
)
from investment_agent.providers.errors import LookaheadDataError
from tests.unit._eastmoney_fake import (
    CAPTURED_AT,
    FakeEastmoneyBackend,
    NoSleep,
    cash_flow_table,
    fake_client,
    summary_table,
)

FY2024 = date(2024, 12, 31)
H1_2026 = date(2026, 6, 30)


def test_fixture_columns_are_the_locked_columns():
    for endpoint, columns in SUMMARY_COLUMNS.items():
        for period in ("20241231", "20260630"):
            assert summary_table(endpoint, period).columns == columns


async def test_period_summaries_units_and_fields():
    client = fake_client()
    result = await client.fetch_period_summaries(FY2024)
    assert client.calls == 3
    moutai = result.summaries["600519.SH"]
    # 归母净利润 (PARENT_NETPROFIT) in CNY, not 净利润 incl. minority (89.3 bn)
    assert moutai.fields["net_profit_parent"] == pytest.approx(86_228_146_421.62, rel=1e-6)
    assert moutai.fields["revenue_total"] == pytest.approx(174_144_069_958.25, rel=1e-6)
    assert moutai.fields["operating_cash_flow"] == pytest.approx(92_463_692_589.0, rel=1e-6)
    # 资产负债率 is published in percent; stored as a fraction
    assert moutai.source_values["stock_zcfz_em.资产负债率"] == pytest.approx(19.0447556579)
    assert moutai.fields["debt_ratio"] == pytest.approx(0.190447556579)
    assert moutai.fields["debt_ratio"] == pytest.approx(
        moutai.fields["total_liabilities"] / moutai.fields["total_assets"], rel=1e-9
    )
    # the summaries carry the latest notice touching the row (FY2025 report)
    assert moutai.notice_date == date(2026, 4, 17)
    assert moutai.endpoints == ("stock_lrb_em", "stock_xjll_em", "stock_zcfz_em")
    assert result.empty_endpoints == ()


async def test_financial_stocks_have_the_four_summary_fields():
    result = await fake_client().fetch_period_summaries(FY2024)
    for ticker in ("601398.SH", "601318.SH", "600030.SH", "000001.SZ"):
        summary = result.summaries[ticker]
        for name in ("net_profit_parent", "revenue_total", "operating_cash_flow", "debt_ratio"):
            assert summary.fields[name] is not None, (ticker, name)
    assert result.summaries["601398.SH"].fields["debt_ratio"] == pytest.approx(0.91833012281)


async def test_empty_field_is_missing_not_an_error():
    table = summary_table("stock_lrb_em", "20241231")
    rows = tuple(
        {**row, "净利润": None} if row["股票代码"] == "601318" else row for row in table.rows
    )
    backend = FakeEastmoneyBackend(
        overrides={("stock_lrb_em", "20241231"): replace(table, rows=rows)}
    )
    result = await fake_client(backend).fetch_period_summaries(FY2024)
    insurer = result.summaries["601318.SH"]
    assert insurer.fields["net_profit_parent"] is None
    assert insurer.missing_fields == ["net_profit_parent"]


async def test_undisclosed_period_is_empty():
    client = fake_client()
    result = await client.fetch_period_summaries(date(2026, 9, 30))
    assert result.summaries == {}
    assert result.empty_endpoints == ("stock_lrb_em", "stock_xjll_em", "stock_zcfz_em")


@pytest.mark.parametrize("endpoint", list(SUMMARY_COLUMNS))
async def test_column_change_is_a_terminal_schema_error(endpoint):
    table = summary_table(endpoint, "20241231")
    renamed = tuple(column if column != "公告日期" else "公告时间" for column in table.columns)
    backend = FakeEastmoneyBackend(
        overrides={(endpoint, "20241231"): RawTable(renamed, table.rows)}
    )
    with pytest.raises(EastmoneySchemaError, match="columns changed"):
        await fake_client(backend).fetch_period_summaries(FY2024)
    extra = RawTable((*table.columns, "新列"), table.rows)
    backend = FakeEastmoneyBackend(overrides={(endpoint, "20241231"): extra})
    with pytest.raises(EastmoneySchemaError, match="unexpected"):
        await fake_client(backend).fetch_period_summaries(FY2024)


async def test_mislabelled_debt_ratio_is_detected():
    table = summary_table("stock_zcfz_em", "20241231")
    rows = tuple({**row, "资产负债率": row["股东权益合计"]} for row in table.rows)
    backend = FakeEastmoneyBackend(
        overrides={("stock_zcfz_em", "20241231"): replace(table, rows=rows)}
    )
    with pytest.raises(EastmoneySchemaError, match="mapping drift"):
        await fake_client(backend).fetch_period_summaries(FY2024)


def test_notice_after_capture_is_lookahead():
    tables = {
        endpoint: summary_table(endpoint, "20260630") for endpoint in SUMMARY_COLUMNS
    }
    with pytest.raises(LookaheadDataError):
        parse_period_summaries(
            tables, report_period=H1_2026, captured_at=datetime(2026, 8, 20, tzinfo=UTC)
        )


async def test_cash_flow_statement_restated_and_fcf():
    statement = await fake_client().fetch_cash_flow_statement("600519.SH")
    assert statement is not None
    h1_2025 = statement.periods[date(2025, 6, 30)]
    assert h1_2025.notice_date == date(2025, 8, 13)
    assert h1_2025.update_date == date(2026, 8, 15)
    assert h1_2025.restated
    h1_2026 = statement.periods[H1_2026]
    assert not h1_2026.restated
    assert h1_2026.operating_cash_flow == pytest.approx(70_690_750_119.06, rel=1e-9)
    assert h1_2026.capital_expenditure == pytest.approx(832_142_752.28, rel=1e-9)
    assert h1_2026.free_cash_flow == pytest.approx(70_690_750_119.06 - 832_142_752.28)
    assert h1_2026.currency == "CNY"


async def test_cash_flow_statement_of_financial_stocks():
    client = fake_client()
    for ticker in ("601318.SH", "601398.SH", "600030.SH"):
        statement = await client.fetch_cash_flow_statement(ticker)
        assert statement is not None
        latest = statement.periods[H1_2026]
        assert latest.operating_cash_flow is not None
        assert latest.capital_expenditure is not None


async def test_unknown_symbol_has_no_statement():
    assert await fake_client().fetch_cash_flow_statement("000333.SZ") is None


def test_cash_flow_column_loss_and_foreign_rows_are_schema_errors():
    table = cash_flow_table("SH600519")
    missing = RawTable(tuple(c for c in table.columns if c != "CONSTRUCT_LONG_ASSET"), table.rows)
    with pytest.raises(EastmoneySchemaError, match="CONSTRUCT_LONG_ASSET"):
        parse_cash_flow_statement(missing, ticker="600519.SH", captured_at=CAPTURED_AT)
    with pytest.raises(EastmoneySchemaError, match="got a row of 600519"):
        parse_cash_flow_statement(table, ticker="000858.SZ", captured_at=CAPTURED_AT)


def test_symbol_mapping():
    assert to_eastmoney_symbol("600519.SH") == "SH600519"
    assert to_eastmoney_symbol("000333.SZ") == "SZ000333"
    assert to_eastmoney_symbol("920001.BJ") == "BJ920001"
    with pytest.raises(ValueError):
        to_eastmoney_symbol("600519")


async def test_calls_are_spaced_by_the_minimum_interval():
    sleep = NoSleep()
    now = [100.0]
    client = AkshareEastmoneyClient(
        FakeEastmoneyBackend(),
        clock=lambda: CAPTURED_AT,
        min_interval_seconds=1.5,
        sleep=sleep,
        monotonic=lambda: now[0],
    )
    await client.fetch_period_summaries(FY2024)
    # three back-to-back calls at a frozen clock: two waits of the full interval
    assert sleep.slept == [1.5, 1.5]


async def test_transient_errors_are_retried_then_raised():
    backend = FakeEastmoneyBackend(
        errors=[EastmoneyTransientError("boom", provider="akshare"), None, None, None]
    )
    client = fake_client(backend, max_attempts=2)
    result = await client.fetch_period_summaries(FY2024)
    assert len(result.summaries) == 9 and client.calls == 4
    backend = FakeEastmoneyBackend(
        errors=[EastmoneyTransientError("boom", provider="akshare")] * 2
    )
    with pytest.raises(EastmoneyTransientError):
        await fake_client(backend, max_attempts=2).fetch_cash_flow_statement("600519.SH")
    backend = FakeEastmoneyBackend(errors=[EastmoneyError("HTTP 404", provider="akshare")])
    client = fake_client(backend, max_attempts=3)
    with pytest.raises(EastmoneyError):
        await client.fetch_cash_flow_statement("600519.SH")
    assert client.calls == 1  # terminal errors are not retried


def test_requests_shim_classifies_answers():
    class Exceptions:
        RequestException = OSError
        ConnectionError = ConnectionError
        Timeout = TimeoutError

    def fake_get(status=200, payload=None, text="", raises=None):
        def get(url, *args, **kwargs):
            assert kwargs["timeout"] == 5
            if raises:
                raise raises
            return SimpleNamespace(status_code=status, json=lambda: payload, text=text)

        return SimpleNamespace(get=get, exceptions=Exceptions)

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    shim = _EastmoneyRequests(fake_get(payload={"result": None, "success": False}), timeout=5)
    with pytest.raises(_NoData):
        shim.get(url).json()
    shim = _EastmoneyRequests(fake_get(payload={"result": {"pages": 1}}), timeout=5)
    assert shim.get(url).json() == {"result": {"pages": 1}}
    with pytest.raises(EastmoneyTransientError):
        _EastmoneyRequests(fake_get(status=502), timeout=5).get(url)
    with pytest.raises(EastmoneyTransientError):
        _EastmoneyRequests(fake_get(raises=ConnectionError("reset")), timeout=5).get(url)
    with pytest.raises(EastmoneyError):
        _EastmoneyRequests(fake_get(status=403), timeout=5).get(url)
    f10 = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index"
    with pytest.raises(_NoData):
        _ = _EastmoneyRequests(fake_get(text="<html></html>"), timeout=5).get(f10).text
    page = '<input id="hidctype" value="4">'
    assert _EastmoneyRequests(fake_get(text=page), timeout=5).get(f10).text == page


@pytest.mark.network
async def test_live_period_summaries_and_cash_flow_statement():
    client = AkshareEastmoneyClient(min_interval_seconds=1.0)
    result = await client.fetch_period_summaries(FY2024)
    assert len(result.summaries) > 5000
    assert result.summaries["600519.SH"].fields["debt_ratio"] == pytest.approx(0.19, abs=0.01)
    statement = await client.fetch_cash_flow_statement("600519.SH")
    assert statement is not None and FY2024 in statement.periods
    assert await client.fetch_cash_flow_statement("999999.SH") is None
