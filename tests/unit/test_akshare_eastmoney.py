"""AkShare Eastmoney adapter (P1-A11): fixtures replay, schema locks, units, throttle."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from investment_agent.providers.akshare_eastmoney import (
    FORECAST_COLUMNS,
    FORECAST_RAW_COLUMNS,
    FORECAST_TYPES,
    SCHEDULE_COLUMNS,
    SEGMENT_COLUMNS,
    SUMMARY_CODE_COLUMN,
    SUMMARY_COLUMNS,
    SUMMARY_NOTICE_COLUMN,
    AkshareEastmoneyClient,
    EastmoneyError,
    EastmoneySchemaError,
    EastmoneyTransientError,
    RawTable,
    _EastmoneyRequests,
    _NoData,
    _table,
    normalize_forecast_type,
    parse_business_segments,
    parse_cash_flow_statement,
    parse_disclosure_schedule,
    parse_earnings_forecasts,
    parse_period_summaries,
    to_eastmoney_symbol,
)
from investment_agent.providers.errors import LookaheadDataError
from tests.unit._eastmoney_fake import (
    CAPTURED_AT,
    EVENTS_CAPTURED_AT,
    FakeEastmoneyBackend,
    NoSleep,
    cash_flow_table,
    fake_client,
    forecast_frame_rows,
    forecast_table,
    schedule_table,
    segment_table,
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


def test_notice_after_capture_defers_only_that_listing():
    tables = {
        endpoint: summary_table(endpoint, "20260630") for endpoint in SUMMARY_COLUMNS
    }
    captured_at = datetime(2026, 9, 5, tzinfo=UTC)
    baseline = parse_period_summaries(tables, report_period=H1_2026, captured_at=captured_at)
    assert baseline.summaries and not baseline.deferred
    # One listing's income statement is dated the day after the capture (evening release).
    lrb = tables["stock_lrb_em"]
    victim = str(lrb.rows[0][SUMMARY_CODE_COLUMN])
    rows = (
        {**lrb.rows[0], SUMMARY_NOTICE_COLUMN: "2026-09-06"},
        *lrb.rows[1:],
    )
    tables["stock_lrb_em"] = replace(lrb, rows=rows)

    parsed = parse_period_summaries(tables, report_period=H1_2026, captured_at=captured_at)

    assert [ticker.split(".")[0] for ticker, _ in parsed.deferred] == [victim]
    assert parsed.deferred[0][1] == date(2026, 9, 6)
    assert set(baseline.summaries) - set(parsed.summaries) == {parsed.deferred[0][0]}
    assert len(parsed.summaries) == len(baseline.summaries) - 1
    # The next capture on the notice date stores it.
    later = parse_period_summaries(
        tables, report_period=H1_2026, captured_at=datetime(2026, 9, 6, tzinfo=UTC)
    )
    assert not later.deferred
    assert len(later.summaries) == len(baseline.summaries)


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


# --------------------------------------------------------------------------- #
# Earnings events (mrd-intake C1): forecasts, disclosure schedule, segments
# --------------------------------------------------------------------------- #

Q3_2026 = date(2026, 9, 30)


def _events_client(backend: FakeEastmoneyBackend | None = None) -> AkshareEastmoneyClient:
    return fake_client(backend, clock=EVENTS_CAPTURED_AT)


def test_event_fixture_columns_are_the_locked_columns():
    table = forecast_table()
    assert table.columns == FORECAST_COLUMNS
    assert set(table.rows[0]) >= FORECAST_RAW_COLUMNS
    assert schedule_table().columns == SCHEDULE_COLUMNS
    assert segment_table("SH600519").columns == SEGMENT_COLUMNS
    # akshare's frame keeps the midpoints of the range the backend keeps.
    by_key = {
        (row["SECURITY_CODE"], row["PREDICT_FINANCE_CODE"], row["NOTICE_DATE"][:10]): row
        for row in table.rows
    }
    for frame_row in forecast_frame_rows():
        raw = by_key[(frame_row["股票代码"], _code_of(frame_row["预测指标"]), frame_row["公告日期"][:10])]
        if raw["PREDICT_AMT_LOWER"] is not None and raw["PREDICT_AMT_UPPER"] is not None:
            midpoint = (raw["PREDICT_AMT_LOWER"] + raw["PREDICT_AMT_UPPER"]) / 2
            assert frame_row["预测数值"] == pytest.approx(midpoint, rel=1e-6)
        assert frame_row["预告类型"] == raw["PREDICT_TYPE"]


def _code_of(indicator: str) -> str:
    return {
        "归属于上市公司股东的净利润": "004",
        "扣除非经常性损益后的净利润": "005",
        "每股收益": "003",
        "营业收入": "006",
        "扣除后营业收入": "008",
        "净利润": "002",
        "主营业务收入": "001",
    }[indicator]


async def test_forecasts_keep_one_row_per_notice_with_the_range_in_cny_and_fractions():
    result = await _events_client().fetch_earnings_forecasts(H1_2026)
    assert result.captured_at == EVENTS_CAPTURED_AT
    assert set(result.forecasts) >= {"600030.SH", "000002.SZ", "002107.SZ", "920081.BJ"}
    (citic,) = result.forecasts["600030.SH"]
    assert citic.indicator_code == "004" and citic.notice_date == date(2026, 7, 11)
    assert citic.forecast_type == "预增" and citic.profit_low == citic.profit_high == 23_343_000_000
    assert citic.yoy_low == pytest.approx(0.7015) and citic.yoy_high == pytest.approx(0.7015)
    (vanke,) = result.forecasts["000002.SZ"]
    assert (vanke.forecast_type, vanke.profit_low, vanke.profit_high) == ("增亏", -15e9, -12e9)
    assert vanke.yoy_low == pytest.approx(-0.2556) and vanke.yoy_high == pytest.approx(-0.0045)
    assert "亏损" in vanke.content
    # 301583 only forecast EPS and 扣非净利润: the 扣非 row is kept as the fallback indicator.
    (fallback,) = result.forecasts["301583.SZ"]
    assert fallback.indicator_code == "005"
    assert result.skipped_indicators > 0
    assert {item.forecast_type for items in result.forecasts.values() for item in items} == {
        *FORECAST_TYPES, "不确定",
    }
    rows = result.raw_rows_for(["600030.SH"])
    assert {row["PREDICT_FINANCE_CODE"] for row in rows} == {"004", "005"}


def test_forecast_type_normalisation_falls_back_to_the_summary_text():
    assert normalize_forecast_type("预增", "") == "预增"
    assert normalize_forecast_type("不确定", "预计净利润变动") == "不确定"
    assert normalize_forecast_type(None, "预计2026年1-6月净利润扭亏为盈") == "扭亏"
    assert normalize_forecast_type("", "净利润预计略减并且减亏") == "略减"  # FORECAST_TYPES order
    assert normalize_forecast_type("new type", "无法判断") == "不确定"


def test_forecast_schema_and_lookahead_guards():
    table = forecast_table()
    with pytest.raises(EastmoneySchemaError, match="columns changed"):
        parse_earnings_forecasts(
            RawTable(table.columns[:-1], table.rows), report_period=H1_2026,
            captured_at=EVENTS_CAPTURED_AT,
        )
    stripped = tuple({k: v for k, v in row.items() if k != "PREDICT_AMT_LOWER"} for row in table.rows)
    with pytest.raises(EastmoneySchemaError, match="PREDICT_AMT_LOWER"):
        parse_earnings_forecasts(
            RawTable(table.columns, stripped), report_period=H1_2026, captured_at=EVENTS_CAPTURED_AT
        )
    with pytest.raises(EastmoneySchemaError, match="asked for 2026-09-30"):
        parse_earnings_forecasts(table, report_period=Q3_2026, captured_at=EVENTS_CAPTURED_AT)
    # A notice dated after the capture date is deferred row by row (Eastmoney dates
    # evening releases with the next day): the rest of the market answer is kept.
    early = parse_earnings_forecasts(
        table, report_period=H1_2026, captured_at=datetime(2026, 8, 18, 12, tzinfo=UTC)
    )
    assert early.deferred and all(day > date(2026, 8, 18) for _ticker, day in early.deferred)
    assert all(
        item.notice_date <= date(2026, 8, 18)
        for items in early.forecasts.values()
        for item in items
    )
    assert early.forecasts  # earlier notices survive
    missing_type = tuple({**row, "PREDICT_TYPE": None} for row in table.rows)
    parsed = parse_earnings_forecasts(
        RawTable(table.columns, missing_type), report_period=H1_2026, captured_at=EVENTS_CAPTURED_AT
    )
    # Without PREDICT_TYPE the summary text decides; a plain "盈利 … 同比增长" text has no
    # type word, so it is 不确定 (the raw value is kept as None).
    (citic,) = parsed.forecasts["600030.SH"]
    assert citic.forecast_type == "不确定" and citic.forecast_type_raw is None
    tagged = tuple(
        {**row, "PREDICT_TYPE": None, "PREDICT_CONTENT": "预计净利润扭亏为盈"} for row in table.rows
    )
    parsed = parse_earnings_forecasts(
        RawTable(table.columns, tagged), report_period=H1_2026, captured_at=EVENTS_CAPTURED_AT
    )
    assert parsed.forecasts["600030.SH"][0].forecast_type == "扭亏"


def test_one_next_day_notice_does_not_fail_the_market_answer():
    # 2026-10-09 19:00 CST: 600176.SH carried NOTICE_DATE 2026-10-10 and the whole
    # CSI300 run failed with LookaheadDataError. Now only that row waits.
    table = forecast_table()
    template = next(row for row in table.rows if row["SECURITY_CODE"] == "600030")
    tomorrow = {
        **template, "SECURITY_CODE": "600176", "SECURITY_NAME_ABBR": "中国巨石",
        "NOTICE_DATE": "2026-10-10 00:00:00",
    }
    captured = datetime(2026, 10, 9, 11, tzinfo=UTC)  # 19:00 CST
    parsed = parse_earnings_forecasts(
        RawTable(table.columns, (*table.rows, tomorrow)), report_period=H1_2026,
        captured_at=captured,
    )
    assert ("600176.SH", date(2026, 10, 10)) in parsed.deferred
    assert "600176.SH" not in parsed.forecasts
    assert "600030.SH" in parsed.forecasts
    # The next capture on the notice date stores it.
    later = parse_earnings_forecasts(
        RawTable(table.columns, (*table.rows, tomorrow)), report_period=H1_2026,
        captured_at=datetime(2026, 10, 12, 11, tzinfo=UTC),
    )
    assert later.forecasts["600176.SH"][0].notice_date == date(2026, 10, 10)
    assert later.deferred == ()


async def test_undisclosed_forecast_period_is_empty():
    result = await _events_client().fetch_earnings_forecasts(Q3_2026)
    assert result.forecasts == {} and result.report_period == Q3_2026


async def test_disclosure_schedule_takes_the_latest_change():
    result = await _events_client().fetch_disclosure_schedule(Q3_2026)
    assert len(result.appointments) == 14 and result.duplicates == 0
    first = result.appointments["002107.SZ"]
    assert (first.first_date, first.scheduled_date, first.actual_date) == (
        date(2026, 10, 10), date(2026, 10, 10), None,
    )
    moved = result.appointments["300633.SZ"]
    assert moved.first_date == date(2026, 10, 22) and moved.scheduled_date == date(2026, 10, 29)
    assert moved.changes == (date(2026, 10, 29),)
    earlier = result.appointments["600661.SH"]
    assert earlier.scheduled_date == date(2026, 10, 20) < earlier.first_date
    assert result.raw_rows_for(["600519.SH"])[0]["首次预约时间"].startswith("2026-10-31")


def test_disclosure_schedule_from_a_frame_with_nat_change_dates():
    # 2026-10-09: stock_yysj_em returns pandas NaT for "no change"; NaT subclasses
    # datetime and isoformat() gives "NaT", which used to fail every row.
    pd = pytest.importorskip("pandas")
    table = schedule_table()
    date_columns = [column for column in table.columns if "时间" in column or "日期" in column]
    frame = pd.DataFrame(list(table.rows), columns=list(table.columns))
    for column in date_columns:
        frame[column] = pd.to_datetime(frame[column])  # None -> NaT, as akshare returns
    assert frame["一次变更日期"].isna().any()
    parsed = parse_disclosure_schedule(
        _table(frame), report_period=Q3_2026, captured_at=EVENTS_CAPTURED_AT
    )
    assert len(parsed.appointments) == 14
    unchanged = parsed.appointments["002107.SZ"]
    assert unchanged.changes == () and unchanged.actual_date is None
    assert unchanged.scheduled_date == unchanged.first_date == date(2026, 10, 10)
    assert parsed.appointments["300633.SZ"].changes == (date(2026, 10, 29),)


def test_disclosure_schedule_guards():
    table = schedule_table()
    with pytest.raises(EastmoneySchemaError, match="columns changed"):
        parse_disclosure_schedule(
            RawTable((*table.columns, "extra"), table.rows), report_period=Q3_2026,
            captured_at=EVENTS_CAPTURED_AT,
        )
    published = tuple(
        {**row, "实际披露时间": "2026-10-12T00:00:00"} if row["股票代码"] == "002107" else row
        for row in table.rows
    )
    # Live 2026-10-09: 002107.SZ published that evening with 实际披露时间 2026-10-10. The
    # appointment is kept without the actual date (deferred), the rest of the answer too.
    early = parse_disclosure_schedule(
        RawTable(table.columns, published), report_period=Q3_2026, captured_at=EVENTS_CAPTURED_AT
    )
    assert early.appointments["002107.SZ"].actual_date is None
    assert early.deferred_actual == (("002107.SZ", date(2026, 10, 12)),)
    assert len(early.appointments) == 14
    parsed = parse_disclosure_schedule(
        RawTable(table.columns, published), report_period=Q3_2026,
        captured_at=datetime(2026, 10, 12, 11, tzinfo=UTC),
    )
    assert parsed.appointments["002107.SZ"].actual_date == date(2026, 10, 12)
    duplicated = RawTable(table.columns, (*table.rows, table.rows[0]))
    assert parse_disclosure_schedule(
        duplicated, report_period=Q3_2026, captured_at=EVENTS_CAPTURED_AT
    ).duplicates == 1


async def test_business_segments_units_and_kinds():
    statement = await _events_client().fetch_business_segments("600519.SH")
    assert statement is not None and statement.captured_at == EVENTS_CAPTURED_AT
    assert list(statement.periods)[:2] == [H1_2026, date(2025, 12, 31)]
    latest = statement.periods[H1_2026]
    products = [item for item in latest if item.kind == "product"]
    assert [item.name for item in products] == ["茅台酒", "其他系列酒", "其他(补充)"]
    moutai = products[0]
    assert moutai.revenue == pytest.approx(77_724_437_925.48)
    assert moutai.share == pytest.approx(0.856909) and moutai.margin == pytest.approx(0.922793)
    assert moutai.profit == pytest.approx(71_723_553_696.78)
    assert {item.kind for item in latest} == {"product", "region"}
    assert statement.raw_rows_for([H1_2026])[0]["主营构成"] == "茅台酒"
    assert await _events_client().fetch_business_segments("999999.SH") is None


def test_business_segment_guards():
    table = segment_table("SH600519")
    with pytest.raises(EastmoneySchemaError, match="got a row of 600519"):
        parse_business_segments(table, ticker="000001.SZ", captured_at=EVENTS_CAPTURED_AT)
    with pytest.raises(EastmoneySchemaError, match="columns changed"):
        parse_business_segments(
            RawTable(table.columns[1:], table.rows), ticker="600519.SH",
            captured_at=EVENTS_CAPTURED_AT,
        )
    with pytest.raises(LookaheadDataError, match="report period 2026-06-30"):
        parse_business_segments(
            table, ticker="600519.SH", captured_at=datetime(2026, 6, 1, tzinfo=UTC)
        )
    odd = tuple({**row, "分类类型": None} if row["主营构成"] == "国内" else row for row in table.rows)
    parsed = parse_business_segments(
        RawTable(table.columns, odd), ticker="600519.SH", captured_at=EVENTS_CAPTURED_AT
    )
    assert parsed.skipped_kinds == ("None",)
    assert "国内" not in {item.name for item in parsed.periods[H1_2026]}


def test_requests_shim_collects_datacenter_rows_and_empty_business_analysis():
    class Exceptions:
        RequestException = OSError
        ConnectionError = ConnectionError
        Timeout = TimeoutError

    def fake(payload):
        return SimpleNamespace(
            get=lambda url, *a, **k: SimpleNamespace(status_code=200, json=lambda: payload, text=""),
            exceptions=Exceptions,
        )

    sink: list[dict[str, object]] = []
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    shim = _EastmoneyRequests(
        fake({"result": {"pages": 1, "data": [{"SECURITY_CODE": "600519"}]}}), timeout=5, sink=sink
    )
    shim.get(url).json()
    assert sink == [{"SECURITY_CODE": "600519"}]
    f10 = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
    with pytest.raises(_NoData):
        _EastmoneyRequests(fake({"zyfw": [], "zygcfx": [], "jyps": []}), timeout=5).get(f10).json()


@pytest.mark.network
async def test_live_period_summaries_and_cash_flow_statement():
    client = AkshareEastmoneyClient(min_interval_seconds=1.0)
    result = await client.fetch_period_summaries(FY2024)
    assert len(result.summaries) > 5000
    assert result.summaries["600519.SH"].fields["debt_ratio"] == pytest.approx(0.19, abs=0.01)
    statement = await client.fetch_cash_flow_statement("600519.SH")
    assert statement is not None and FY2024 in statement.periods
    assert await client.fetch_cash_flow_statement("999999.SH") is None
