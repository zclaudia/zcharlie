"""Tencent batch close-quote client (mrd-intake B1): field positions from the
recorded answer, GBK decoding, 60-code batching, PIT guards (pending / lookahead),
suspension and ST, error mapping, circuit and ledger wiring. Offline (respx)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
import respx

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import (
    LookaheadDataError,
    ProviderDataError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from investment_agent.providers.ledger import ProviderCallEvent
from investment_agent.providers.tencent_quote import (
    BASE_URL,
    MAX_CODES_PER_REQUEST,
    TencentQuoteClient,
    closed_quote,
    from_tencent_code,
    parse_quote_lines,
    to_tencent_code,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tencent"
SAMPLE = (FIXTURES / "quotes_sample.txt").read_text(encoding="utf-8")
EDGE_CASES = (FIXTURES / "quotes_edge_cases.txt").read_text(encoding="utf-8")
TRADE_DATE = date(2026, 10, 8)
# 16:20 Asia/Shanghai on the trade date (after the 15:00 close).
CAPTURED_AT = datetime(2026, 10, 8, 8, 20, tzinfo=UTC)
POOL = ["600519.SH", "000001.SZ", "920748.BJ", "000002.SZ"]


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


def _client(**kwargs) -> TencentQuoteClient:
    kwargs.setdefault("retry_backoff_seconds", 0)
    kwargs.setdefault("sleep", NoSleep())
    return TencentQuoteClient(lambda: CAPTURED_AT, **kwargs)


def _requested_codes(request: httpx.Request) -> list[str]:
    path = unquote(request.url.path)
    assert path.startswith("/q="), path
    return path[len("/q=") :].split(",")


def _answer(text: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=text.encode("gbk"))


def _lines_for(codes: list[str], text: str) -> str:
    lines = {line.split("=", 1)[0]: line for line in text.splitlines() if line.startswith("v_")}
    return "\n".join(lines[f"v_{code}"] for code in codes if f"v_{code}" in lines) + "\n"


def _route(text: str = SAMPLE):
    """Answer each request with the fixture lines of the codes it asked for."""

    return respx.get(url__regex=rf"{BASE_URL}/q=.+").mock(
        side_effect=lambda request: _answer(_lines_for(_requested_codes(request), text))
    )


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


def test_code_mapping_both_ways():
    assert to_tencent_code("600519.SH") == "sh600519"
    assert to_tencent_code("000001.sz") == "sz000001"
    assert to_tencent_code("920748.BJ") == "bj920748"
    assert from_tencent_code("sh600519") == "600519.SH"
    assert from_tencent_code("bj920748") == "920748.BJ"
    with pytest.raises(ValueError):
        to_tencent_code("AAPL")
    with pytest.raises(ValueError):
        from_tencent_code("hk00700")


def test_recorded_answer_field_positions():
    rows = parse_quote_lines(SAMPLE)
    assert set(rows) == {"sh600519", "sz000001", "bj920748", "sz000002"}
    moutai = closed_quote(
        "600519.SH", rows["sh600519"], trade_date=TRADE_DATE, capture_date=TRADE_DATE
    )
    assert moutai.name == "贵州茅台"
    assert moutai.close == Decimal("1255.79")
    assert moutai.prev_close == Decimal("1258.62")
    assert moutai.open == Decimal("1252.90")
    assert (moutai.high, moutai.low) == (Decimal("1258.00"), Decimal("1242.00"))
    assert moutai.volume_shares == Decimal("2516800")  # 25168 手 × 100
    assert moutai.amount_cny == Decimal("3145410000")  # 314541 万元 × 1e4
    assert moutai.turnover == Decimal("0.0020")  # 0.20 % -> fraction
    assert moutai.pe == Decimal("19.28")
    assert moutai.time == datetime(2026, 10, 8, 16, 14, 57, tzinfo=SHANGHAI_TZ)
    assert moutai.trade_status == 1 and not moutai.suspended and not moutai.is_st
    bj = closed_quote("920748.BJ", rows["bj920748"], trade_date=TRADE_DATE, capture_date=TRADE_DATE)
    assert bj.name == "路桥信息" and bj.close == Decimal("32.47")
    assert bj.amount_cny == Decimal("138670100")  # 13867.01 万元
    assert bj.pe == Decimal("-38.73")  # loss-making: negative PE kept as reported
    vanke = closed_quote(
        "000002.SZ", rows["sz000002"], trade_date=TRADE_DATE, capture_date=TRADE_DATE
    )
    assert vanke.name == "万  科Ａ" and vanke.volume_shares == Decimal("735739400")


def test_suspended_and_st_rows():
    rows = parse_quote_lines(EDGE_CASES)
    halted = closed_quote(
        "000006.SZ", rows["sz000006"], trade_date=TRADE_DATE, capture_date=TRADE_DATE
    )
    assert halted.suspended and halted.trade_status == 0
    # Stored like a baostock suspended day: OHLC repeat the previous close.
    assert (halted.open, halted.high, halted.low, halted.close) == (Decimal("4.26"),) * 4
    assert halted.volume_shares == 0 and halted.amount_cny == 0 and halted.turnover == 0
    st = closed_quote("600671.SH", rows["sh600671"], trade_date=TRADE_DATE, capture_date=TRADE_DATE)
    assert st.is_st and st.name == "*ST目药" and st.close == Decimal("5.32")
    assert st.time == datetime(2026, 10, 8, 15, 0, 4, tzinfo=SHANGHAI_TZ)


def test_quote_lines_tolerate_unknown_codes_and_reject_garbage():
    rows = parse_quote_lines('v_pv_none_match="1";\nv_sh600000="";\n' + SAMPLE)
    assert "sh600000" not in rows and len(rows) == 4
    assert parse_quote_lines("") == {}
    with pytest.raises(ProviderDataError):
        parse_quote_lines("<html>503 Service Unavailable</html>")


def test_malformed_rows_are_data_errors():
    rows = parse_quote_lines(SAMPLE)
    short = rows["sh600519"][:20]
    with pytest.raises(ProviderDataError, match="expected >= 44 fields"):
        closed_quote("600519.SH", short, trade_date=TRADE_DATE, capture_date=TRADE_DATE)
    bad = list(rows["sh600519"])
    bad[3] = "n/a"
    with pytest.raises(ProviderDataError, match="field 3"):
        closed_quote("600519.SH", bad, trade_date=TRADE_DATE, capture_date=TRADE_DATE)
    bad = list(rows["sh600519"])
    bad[30] = "2026-10-08"
    with pytest.raises(ProviderDataError, match="field 30"):
        closed_quote("600519.SH", bad, trade_date=TRADE_DATE, capture_date=TRADE_DATE)


# --------------------------------------------------------------------------- #
# Client: batching, decoding, PIT guards
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fetch_close_decodes_gbk_and_keeps_request_order():
    route = _route()
    client = _client()
    batch = await client.fetch_close(POOL, TRADE_DATE)
    assert route.call_count == 1 and batch.requests == 1
    assert _requested_codes(route.calls[0].request) == [
        "sh600519", "sz000001", "bj920748", "sz000002",
    ]
    assert route.calls[0].request.headers["User-Agent"].startswith("Mozilla/5.0")
    assert list(batch.quotes) == POOL
    assert batch.quotes["600519.SH"].name == "贵州茅台"
    assert batch.captured_at == CAPTURED_AT and batch.trade_date == TRADE_DATE
    assert batch.pending == {} and batch.missing == {}
    assert len(batch.raw) == 1 and batch.raw[0].startswith('v_sh600519="1~贵州茅台')


@respx.mock
async def test_sixty_one_tickers_take_two_requests():
    tickers = [f"{600000 + index:06d}.SH" for index in range(MAX_CODES_PER_REQUEST + 1)]
    answers: list[list[str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        codes = _requested_codes(request)
        answers.append(codes)
        template = SAMPLE.splitlines()[0].split("=", 1)[1]
        return _answer("\n".join(f"v_{code}={template}" for code in codes))

    route = respx.get(url__regex=rf"{BASE_URL}/q=.+").mock(side_effect=respond)
    batch = await _client().fetch_close(tickers, TRADE_DATE)
    assert route.call_count == 2 and batch.requests == 2
    assert [len(codes) for codes in answers] == [60, 1]
    assert answers[1] == ["sh600060"]
    assert len(batch.quotes) == 61 and len(batch.raw) == 2
    # Duplicates are collapsed before chunking.
    batch = await _client().fetch_close(["600519.SH", "600519.sh"], TRADE_DATE)
    assert list(batch.quotes) == ["600519.SH"] and route.call_count == 3


@respx.mock
async def test_open_session_and_unquoted_day_are_pending():
    _route(EDGE_CASES)
    batch = await _client().fetch_close(["000001.SZ"], TRADE_DATE)
    assert batch.quotes == {} and "session not closed" in batch.pending["000001.SZ"]
    assert "14:35:00" in batch.pending["000001.SZ"]
    # The feed still shows 10-08 while the job asks for 10-09 (before the close).
    _route()
    later = datetime(2026, 10, 9, 7, 10, tzinfo=UTC)  # 15:10 CST on 10-09
    batch = await TencentQuoteClient(lambda: later).fetch_close(["600519.SH"], date(2026, 10, 9))
    assert batch.quotes == {}
    assert batch.pending["600519.SH"] == "latest quote is dated 2026-10-08; 2026-10-09 not quoted yet"


@respx.mock
async def test_quote_after_capture_date_is_lookahead():
    _route()
    early = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)  # capture date 10-07, quotes dated 10-08
    with pytest.raises(LookaheadDataError, match="dated 2026-10-08, after the capture date"):
        await TencentQuoteClient(lambda: early).fetch_close(["600519.SH"], date(2026, 10, 7))


@respx.mock
async def test_missing_codes_and_sessions_already_gone():
    _route()
    batch = await _client().fetch_close(["600519.SH", "600000.SH"], TRADE_DATE)
    assert set(batch.quotes) == {"600519.SH"}
    assert "not in the tencent answer" in batch.missing["600000.SH"]
    # Asking for an earlier day than the feed serves cannot be satisfied.
    batch = await _client().fetch_close(["600519.SH"], date(2026, 9, 30))
    assert batch.quotes == {}
    assert batch.missing["600519.SH"].startswith("feed already serves 2026-10-08")


@respx.mock
async def test_suspended_row_through_the_client():
    _route(EDGE_CASES)
    batch = await _client().fetch_close(["000006.SZ", "600671.SH"], TRADE_DATE)
    assert batch.quotes["000006.SZ"].suspended
    assert batch.quotes["600671.SH"].is_st and batch.pending == {}


# --------------------------------------------------------------------------- #
# Errors, circuit, ledger
# --------------------------------------------------------------------------- #


@respx.mock
async def test_transient_errors_are_retried_once_then_raised():
    route = respx.get(url__regex=rf"{BASE_URL}/q=.+").mock(
        return_value=httpx.Response(502, content=b"bad gateway")
    )
    sleep = NoSleep()
    capture = Capture()
    client = _client(sleep=sleep, on_call=capture)
    with pytest.raises(ProviderTransientError, match="HTTP 502"):
        await client.fetch_close(POOL, TRADE_DATE)
    assert route.call_count == 2 and client.requests == 2 and sleep.slept == [0]
    # One logical call reported, tickers counted, transient outcome.
    assert [(e.provider, e.endpoint, e.outcome, e.ticker_count) for e in capture.events] == [
        ("tencent", "q", "transient", 4)
    ]
    assert capture.events[0].duration_ms is not None

    route.mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderTransientError, match="timed out"):
        await _client().fetch_close(POOL, TRADE_DATE)
    route.mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderTransientError, match="ConnectError"):
        await _client().fetch_close(POOL, TRADE_DATE)
    route.mock(return_value=httpx.Response(429))
    with pytest.raises(ProviderTransientError, match="HTTP 429"):
        await _client(max_attempts=1).fetch_close(POOL, TRADE_DATE)


@respx.mock
async def test_client_errors_and_garbage_are_terminal():
    route = respx.get(url__regex=rf"{BASE_URL}/q=.+").mock(return_value=httpx.Response(404))
    capture = Capture()
    client = _client(on_call=capture)
    with pytest.raises(ProviderDataError, match="HTTP 404"):
        await client.fetch_close(POOL, TRADE_DATE)
    assert route.call_count == 1  # not retried
    assert capture.events[-1].outcome == "terminal"
    route.mock(return_value=httpx.Response(200, content=b"<html>blocked</html>"))
    with pytest.raises(ProviderDataError, match="no quote lines"):
        await client.fetch_close(POOL, TRADE_DATE)


@respx.mock
async def test_circuit_is_checked_and_recorded():
    route = _route()
    circuit = CircuitBreaker(lambda: datetime.now(UTC))
    capture = Capture()
    client = _client(circuit=circuit, on_call=capture)
    await client.fetch_close(POOL, TRADE_DATE)
    assert circuit.snapshot() == {}
    circuit.open_now("tencent", ProviderTransientError("HTTP 502", provider="tencent"))
    with pytest.raises(ProviderUnavailableError):
        await client.fetch_close(POOL, TRADE_DATE)
    assert route.call_count == 1  # the open circuit is a negative cache
    assert [event.outcome for event in capture.events] == ["ok", "skipped_open"]
    assert capture.events[-1].duration_ms is None


def test_constructor_validation():
    with pytest.raises(ValueError):
        TencentQuoteClient(lambda: CAPTURED_AT, chunk_size=0)
    with pytest.raises(ValueError):
        TencentQuoteClient(lambda: CAPTURED_AT, chunk_size=MAX_CODES_PER_REQUEST + 1)
    with pytest.raises(ValueError):
        TencentQuoteClient(lambda: CAPTURED_AT, max_attempts=0)


@respx.mock
async def test_naive_clock_is_rejected():
    _route()
    with pytest.raises(ValueError, match="timezone-aware"):
        await TencentQuoteClient(lambda: datetime(2026, 10, 8, 16, 20)).fetch_close(
            POOL, TRADE_DATE
        )
