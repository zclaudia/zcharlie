"""baostock daily bars and quarterly financials: offline fixture replay (P1-A7)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from investment_agent.providers.baostock_market import (
    BaostockMarketClient,
    canonical_baostock_financials,
    quarter_of,
    report_period_for,
)
from investment_agent.providers.baostock_reference import BaostockError, BaostockTransientError
from investment_agent.providers.errors import LookaheadDataError, ProviderDataError
from investment_agent.research.ifind_mapping import (
    BAOSTOCK_FIELD_MAPPINGS,
    FIELD_MAPPINGS,
    IFIND_FIELD_MAPPINGS,
    mapping_for,
)
from tests.unit._baostock_fake import FakeBaostockData

CAPTURE_TIME = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)  # 18:00 Asia/Shanghai


def _client(backend: FakeBaostockData, *, now: datetime = CAPTURE_TIME, **kwargs) -> BaostockMarketClient:
    kwargs.setdefault("retry_backoff_seconds", 0)
    return BaostockMarketClient(backend, clock=lambda: now, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Daily bars
# --------------------------------------------------------------------------- #


async def test_raw_daily_bars_match_ifind_close() -> None:
    backend = FakeBaostockData()
    series = await _client(backend).fetch_daily_bars(
        "600519.SH", date(2026, 9, 1), date(2026, 9, 30), "none"
    )
    assert series.ticker == "600519.SH" and series.adjust == "none"
    assert len(series.bars) == 21
    last = series.bars[-1]
    assert last.trade_date == date(2026, 9, 30)
    assert last.close == Decimal("1258.6200")  # equals the iFinD close
    assert last.volume == Decimal("3833098")
    assert last.amount == Decimal("4797246635.8800")
    # turn is a percent (0.3066%); stored as a fraction
    assert last.turnover == Decimal("0.003066")
    assert last.trade_status == 1 and last.is_st is False
    assert last.pe_ttm == Decimal("19.320898")
    assert last.pb_mrq == Decimal("6.262110")
    assert last.ps_ttm == Decimal("9.082149")
    assert [params["adjustflag"] for _, params in backend.calls if _ != "login" and _ != "logout"] == ["3"]
    assert backend.calls[0][0] == "login" and backend.calls[-1][0] == "logout"


async def test_backward_series_uses_adjustflag_1_and_never_forward() -> None:
    backend = FakeBaostockData()
    series = await _client(backend).fetch_daily_bars(
        "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "backward"
    )
    assert backend.methods("query_history_k_data_plus")[0]["adjustflag"] == "1"
    assert series.bars[0].close == Decimal("9652.6802453400")
    with pytest.raises(ValueError):
        await _client(backend).fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "forward"  # type: ignore[arg-type]
        )


def _backward_row(day: str, close: str, flag: str) -> dict[str, str]:
    return {"date": day, "code": "sh.600519", "open": close, "high": close, "low": close,
            "close": close, "volume": "1", "adjustflag": flag, "tradestatus": "1", "isST": "0"}


async def test_backward_series_accepts_unadjusted_rows_before_first_corporate_action() -> None:
    # Live 688223.SH / 302132.SZ: bars before the first dividend have factor 1
    # and baostock labels them "3" inside an adjustflag=1 answer.
    backend = FakeBaostockData(
        extra_rows={("sh.600519", "1"): [_backward_row("2026-09-29", "9600.0", "3")]}
    )
    series = await _client(backend).fetch_daily_bars(
        "600519.SH", date(2026, 9, 29), date(2026, 9, 30), "backward"
    )
    assert [bar.trade_date for bar in series.bars] == [date(2026, 9, 29), date(2026, 9, 30)]


async def test_backward_series_rejects_forward_adjusted_rows() -> None:
    backend = FakeBaostockData(
        extra_rows={("sh.600519", "1"): [_backward_row("2026-09-29", "1250.0", "2")]}
    )
    with pytest.raises(ProviderDataError, match="adjustflag"):
        await _client(backend).fetch_daily_bars(
            "600519.SH", date(2026, 9, 29), date(2026, 9, 30), "backward"
        )


async def test_raw_series_rejects_backward_rows() -> None:
    backend = FakeBaostockData(
        extra_rows={("sh.600519", "3"): [_backward_row("2026-09-29", "9600.0", "1")]}
    )
    with pytest.raises(ProviderDataError, match="adjustflag"):
        await _client(backend).fetch_daily_bars(
            "600519.SH", date(2026, 9, 29), date(2026, 9, 30), "none"
        )


async def test_suspended_rows_are_kept_and_flagged() -> None:
    series = await _client(FakeBaostockData()).fetch_daily_bars(
        "601059.SH", date(2026, 9, 1), date(2026, 9, 30), "none"
    )
    bar = series.bars[-1]
    assert bar.trade_date == date(2026, 9, 30)
    assert bar.suspended and bar.trade_status == 0
    assert bar.volume == Decimal(0)
    assert bar.amount is None and bar.turnover is None
    assert bar.close == Decimal("15.5600")


async def test_st_flag_and_negative_pe() -> None:
    series = await _client(FakeBaostockData()).fetch_daily_bars(
        "000016.SZ", date(2026, 9, 30), date(2026, 9, 30), "none"
    )
    (bar,) = series.bars
    assert bar.is_st is True and bar.suspended
    assert bar.pe_ttm is not None and bar.pe_ttm < 0


async def test_recent_ipo_history_starts_at_listing() -> None:
    series = await _client(FakeBaostockData()).fetch_daily_bars(
        "001248.SZ", date(2026, 6, 20), date(2026, 7, 10), "none"
    )
    assert series.bars[0].trade_date == date(2026, 7, 2)
    assert len(series.bars) == 7


async def test_bar_after_capture_date_is_lookahead() -> None:
    backend = FakeBaostockData(
        extra_rows={
            ("sh.600519", "3"): [
                {"date": "2026-10-07", "code": "sh.600519", "open": "1", "high": "1",
                 "low": "1", "close": "1", "volume": "1", "adjustflag": "3",
                 "tradestatus": "1", "isST": "0"}
            ]
        }
    )
    with pytest.raises(LookaheadDataError):
        await _client(backend).fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 10, 9), "none"
        )
    # The genuine holiday answer stops at the last trading day.
    series = await _client(FakeBaostockData()).fetch_daily_bars(
        "600519.SH", date(2026, 9, 30), date(2026, 10, 9), "none"
    )
    assert [bar.trade_date for bar in series.bars] == [date(2026, 9, 30)]


async def test_row_without_prices_is_skipped_not_fatal() -> None:
    backend = FakeBaostockData(
        extra_rows={
            ("sh.600519", "3"): [
                {"date": "2026-09-02", "code": "sh.600519", "adjustflag": "3", "tradestatus": "1"}
            ]
        }
    )
    series = await _client(backend).fetch_daily_bars(
        "600519.SH", date(2026, 9, 1), date(2026, 9, 3), "none"
    )
    assert series.skipped == ("2026-09-02",)
    assert [bar.trade_date for bar in series.bars] == [date(2026, 9, 1), date(2026, 9, 3)]


async def test_wrong_code_in_answer_is_a_data_error() -> None:
    backend = FakeBaostockData()
    backend.kdata[("sh.600519", "3")]["2026-09-30"] = [
        "2026-09-30", "sz.000001", *backend.kdata[("sh.600519", "3")]["2026-09-30"][2:]
    ]
    with pytest.raises(ProviderDataError):
        await _client(backend).fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "none"
        )


# --------------------------------------------------------------------------- #
# Sessions, retries
# --------------------------------------------------------------------------- #


async def test_session_logs_in_once_for_many_queries() -> None:
    backend = FakeBaostockData()
    client = _client(backend)
    async with client.session():
        assert client.in_session
        for symbol in ("600519.SH", "000001.SZ", "601398.SH"):
            for adjust in ("none", "backward"):
                await client.fetch_daily_bars(symbol, date(2026, 9, 30), date(2026, 9, 30), adjust)
        await client.fetch_quarterly_financials("600519.SH", 2026, 2)
    assert backend.logins == 1
    assert [method for method, _ in backend.calls].count("logout") == 1
    assert not client.in_session


async def test_transient_error_inside_session_relogs_in_and_retries() -> None:
    failures = {"left": 1}

    def fail(method: str, params: dict) -> tuple[str, str] | None:
        if method == "query_history_k_data_plus" and failures["left"]:
            failures["left"] -= 1
            return ("10001001", "not logged in")
        return None

    backend = FakeBaostockData(fail=fail)
    client = _client(backend)
    async with client.session():
        series = await client.fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "none"
        )
    assert len(series.bars) == 1
    assert backend.logins == 2  # session login + re-login before the retry


async def test_terminal_error_is_not_retried() -> None:
    backend = FakeBaostockData(fail=lambda method, params: ("10004011", "bad code"))
    with pytest.raises(BaostockError):
        await _client(backend).fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "none"
        )
    assert len(backend.methods("query_history_k_data_plus")) == 1


async def test_transient_errors_exhaust_attempts() -> None:
    backend = FakeBaostockData(fail=lambda method, params: OSError("socket closed"))
    with pytest.raises(BaostockTransientError):
        await _client(backend, max_attempts=2).fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "none"
        )
    assert len(backend.methods("query_history_k_data_plus")) == 2


# --------------------------------------------------------------------------- #
# Quarterly financials
# --------------------------------------------------------------------------- #


async def test_quarterly_financials_merge_four_tables() -> None:
    record = await _client(FakeBaostockData()).fetch_quarterly_financials("600519.SH", 2025, 4)
    assert record is not None
    assert record.report_period == date(2025, 12, 31)
    assert record.pub_date == date(2026, 4, 17)
    assert record.tables == ("profit", "balance", "cash_flow", "growth")
    fields = record.fields
    # baostock ratios are already fractions
    assert fields["roe"] == pytest.approx(0.344620)
    assert fields["gross_margin"] == pytest.approx(0.911796)
    assert fields["net_margin"] == pytest.approx(0.505279)
    assert fields["debt_ratio"] == pytest.approx(0.164154)
    assert fields["net_profit"] == pytest.approx(85310324833.67)
    assert fields["revenue"] == pytest.approx(172054171890.91)
    assert fields["total_shares"] == pytest.approx(1252270215.0)
    assert fields["float_shares"] == pytest.approx(1252270215.0)
    assert fields["eps_ttm"] == pytest.approx(65.736665)
    assert fields["current_ratio"] == pytest.approx(5.090027)
    assert fields["cfo_to_np"] == pytest.approx(0.721158)
    assert fields["cfo_to_revenue"] == pytest.approx(0.364386)
    assert fields["yoy_net_profit"] == pytest.approx(-0.045049)
    assert fields["yoy_equity"] == pytest.approx(0.049470)
    assert record.source_values["cash_flow.ebitToInterest"] == ""
    assert "profit.roeAvg" in record.source_values


async def test_bank_has_explicit_missing_fields() -> None:
    record = await _client(FakeBaostockData()).fetch_quarterly_financials("601398.SH", 2026, 1)
    assert record is not None
    assert record.pub_date == date(2026, 4, 30)
    assert record.fields["gross_margin"] is None
    assert record.fields["revenue"] is None
    assert record.fields["current_ratio"] is None
    assert {"gross_margin", "revenue"} <= set(record.missing_fields)
    assert record.fields["roe"] == pytest.approx(0.020286)


async def test_undisclosed_period_is_none_after_one_query() -> None:
    backend = FakeBaostockData()
    record = await _client(backend).fetch_quarterly_financials("600519.SH", 2026, 3)
    assert record is None
    assert [m for m, _ in backend.calls if m.startswith("query_")] == ["query_profit_data"]


async def test_pubdate_after_capture_is_lookahead() -> None:
    early = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)  # day before the H1 pubDate
    with pytest.raises(LookaheadDataError):
        await _client(FakeBaostockData(), now=early).fetch_quarterly_financials(
            "600519.SH", 2026, 2
        )


async def test_stat_date_mismatch_is_a_data_error() -> None:
    backend = FakeBaostockData()
    record = backend.quarterly[("query_profit_data", "sh.600519", "2026", "2")]
    row = list(record["rows"][0])
    row[2] = "2026-03-31"
    record["rows"] = [row]
    with pytest.raises(ProviderDataError):
        await _client(backend).fetch_quarterly_financials("600519.SH", 2026, 2)


def test_period_helpers() -> None:
    assert report_period_for(2025, 4) == date(2025, 12, 31)
    assert quarter_of(date(2026, 6, 30)) == (2026, 2)
    with pytest.raises(ValueError):
        report_period_for(2026, 5)
    with pytest.raises(ValueError):
        quarter_of(date(2026, 5, 31))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_declares_one_source_per_provider_and_field() -> None:
    keys = [(m.provider, m.payload_type, m.canonical_field) for m in FIELD_MAPPINGS]
    assert len(keys) == len(set(keys))
    assert all(m.provider == "ifind" for m in IFIND_FIELD_MAPPINGS)
    assert all(m.provider == "baostock" for m in BAOSTOCK_FIELD_MAPPINGS)
    required = {
        "roe", "net_margin", "gross_margin", "net_profit", "eps_ttm", "revenue",
        "total_shares", "float_shares", "debt_ratio", "current_ratio", "quick_ratio",
        "cfo_to_np", "cfo_to_revenue", "yoy_net_profit", "yoy_equity", "yoy_asset",
    }
    baostock_financial = {
        m.canonical_field for m in BAOSTOCK_FIELD_MAPPINGS if m.payload_type == "financial"
    }
    assert required <= baostock_financial
    assert "主营" in mapping_for("financial", "revenue", "baostock").unit_or_formula
    assert mapping_for("financial", "revenue").source_fields == (
        "ths_operating_total_revenue_stock",
    )


def test_canonical_extraction_ignores_unknown_tables() -> None:
    fields = canonical_baostock_financials({"profit": {"roeAvg": "0.1", "npMargin": ""}})
    assert fields["roe"] == pytest.approx(0.1)
    assert fields["net_margin"] is None
    assert fields["debt_ratio"] is None


# --------------------------------------------------------------------------- #
# Live smoke (network)
# --------------------------------------------------------------------------- #


@pytest.mark.network
async def test_live_baostock_market_smoke() -> None:
    client = BaostockMarketClient()
    async with client.session():
        raw = await client.fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "none"
        )
        backward = await client.fetch_daily_bars(
            "600519.SH", date(2026, 9, 30), date(2026, 9, 30), "backward"
        )
        record = await client.fetch_quarterly_financials("600519.SH", 2025, 4)
    assert raw.bars[0].close == Decimal("1258.6200")
    assert backward.bars[0].close > raw.bars[0].close
    assert record is not None and record.pub_date == date(2026, 4, 17)
    assert 0 < (record.fields["roe"] or 0) < 1  # a fraction, not a percent
