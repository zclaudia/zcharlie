"""baostock reference adapter: offline replay of recorded fixtures (P1-A1)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from investment_agent.providers.baostock_reference import (
    BaostockError,
    BaostockReferenceClient,
    BaostockTransientError,
    is_stock_code,
    parse_csrc_industry,
    to_baostock_code,
    to_canonical_ticker,
)
from investment_agent.providers.errors import LookaheadDataError, ProviderDataError
from investment_agent.research.retry_policy import is_transient_error

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "baostock"
# The fixtures were captured on 2026-10-06 (Asia/Shanghai).
CAPTURE_TIME = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


class _Status:
    def __init__(self, error_code: str = "0", error_msg: str = "success") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


class _ReplayResult:
    def __init__(self, record: dict[str, Any]) -> None:
        self.error_code: str = record["error_code"]
        self.error_msg: str = record["error_msg"]
        self.fields: list[str] = list(record["fields"])
        self.cur_page_num = record.get("cur_page_num")
        self.per_page_count = record.get("per_page_count")
        self._rows = [list(row) for row in record["rows"]]
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


class FakeBaostock:
    """Replays ``tests/fixtures/baostock/*.json`` keyed by (method, params)."""

    def __init__(self, *, login_code: str = "0") -> None:
        self.records: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            for record in json.loads(path.read_text(encoding="utf-8"))["requests"]:
                self.records[self._key(record["method"], record["params"])] = record
        self.login_code = login_code
        self.calls: list[str] = []
        self.logged_in = False

    @staticmethod
    def _key(method: str, params: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return method, tuple(sorted(params.items()))

    def login(self) -> _Status:
        self.calls.append("login")
        if self.login_code != "0":
            return _Status(self.login_code, "login failed")
        self.logged_in = True
        return _Status()

    def logout(self) -> _Status:
        self.calls.append("logout")
        self.logged_in = False
        return _Status()

    def _replay(self, method: str, params: dict[str, str]) -> _ReplayResult:
        assert self.logged_in, "query outside a login session"
        self.calls.append(method)
        return _ReplayResult(self.records[self._key(method, params)])

    def query_hs300_stocks(self, date: str = "") -> _ReplayResult:
        return self._replay("query_hs300_stocks", {"date": date})

    def query_sz50_stocks(self, date: str = "") -> _ReplayResult:
        return self._replay("query_sz50_stocks", {"date": date})

    def query_zz500_stocks(self, date: str = "") -> _ReplayResult:
        return self._replay("query_zz500_stocks", {"date": date})

    def query_all_stock(self, day: str = "") -> _ReplayResult:
        return self._replay("query_all_stock", {"day": day})

    def query_stock_industry(self, code: str = "", date: str = "") -> _ReplayResult:
        if code:
            return self._replay("query_stock_industry", {"code": code, "date": date})
        # Full-market query: synthesize from the recorded per-symbol answers.
        assert self.logged_in
        self.calls.append("query_stock_industry")
        rows: list[list[str]] = []
        fields: list[str] = []
        for (method, params), record in self.records.items():
            if method == "query_stock_industry" and dict(params)["date"] == date:
                rows.extend(record["rows"])
                fields = record["fields"]
        return _ReplayResult(
            {"error_code": "0", "error_msg": "success", "fields": fields, "rows": rows}
        )


def _client(backend: Any, *, now: datetime = CAPTURE_TIME, **kwargs: Any) -> BaostockReferenceClient:
    kwargs.setdefault("retry_backoff_seconds", 0)
    return BaostockReferenceClient(backend, clock=lambda: now, **kwargs)


# --------------------------------------------------------------------------- #
# Ticker mapping and industry parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("baostock_code", "ticker"),
    [
        ("sh.600000", "600000.SH"),
        ("sz.000001", "000001.SZ"),
        ("sz.300750", "300750.SZ"),
        ("sh.688981", "688981.SH"),
        ("bj.830799", "830799.BJ"),
    ],
)
def test_ticker_mapping_round_trip(baostock_code: str, ticker: str) -> None:
    assert to_canonical_ticker(baostock_code) == ticker
    assert to_baostock_code(ticker) == baostock_code
    assert to_baostock_code(to_canonical_ticker(baostock_code)) == baostock_code
    assert to_canonical_ticker(to_baostock_code(ticker)) == ticker


@pytest.mark.parametrize("bad", ["600000", "sh600000", "hk.00700", "sh.60000", "600000.HK"])
def test_ticker_mapping_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        to_canonical_ticker(bad)
    with pytest.raises(ValueError):
        to_baostock_code(bad)


def test_parse_csrc_industry() -> None:
    assert parse_csrc_industry("C15酒、饮料和精制茶制造业") == ("C15", "酒、饮料和精制茶制造业")
    assert parse_csrc_industry("J66货币金融服务") == ("J66", "货币金融服务")
    assert parse_csrc_industry("") == (None, None)
    assert parse_csrc_industry("  ") == (None, None)
    legacy = "制造业-饮料制造业-食品、饮料-酒精及饮料酒制造业"
    assert parse_csrc_industry(legacy) == (None, legacy)


# --------------------------------------------------------------------------- #
# Constituents
# --------------------------------------------------------------------------- #


async def test_fetch_csi300_constituents_from_fixture() -> None:
    backend = FakeBaostock()
    snapshot = await _client(backend).fetch_constituents("CSI300", date(2026, 9, 30))

    assert snapshot.index_code == "CSI300"
    assert snapshot.effective_date == date(2026, 9, 28)
    assert len(snapshot.members) == 300
    assert list(snapshot.members) == sorted(snapshot.members)
    assert "600519.SH" in snapshot.members
    assert snapshot.member_names["600519.SH"] == "贵州茅台"
    assert all(t.endswith((".SH", ".SZ", ".BJ")) for t in snapshot.members)
    assert len(snapshot.raw_rows) == 300
    assert snapshot.raw_rows[0]["code"].startswith(("sh.", "sz."))
    assert snapshot.captured_at == CAPTURE_TIME
    assert snapshot.source == "baostock"
    assert backend.calls == ["login", "query_hs300_stocks", "logout"]


async def test_historical_constituents_differ() -> None:
    client = _client(FakeBaostock())
    recent = await client.fetch_constituents("CSI300", date(2026, 9, 30))
    older = await client.fetch_constituents("CSI300", date(2025, 6, 30))
    assert older.effective_date == date(2025, 6, 30)
    assert len(older.members) == 300
    assert set(recent.members) != set(older.members)


@pytest.mark.parametrize(("index_code", "size"), [("SSE50", 50), ("CSI500", 500)])
async def test_other_indexes(index_code: str, size: int) -> None:
    snapshot = await _client(FakeBaostock()).fetch_constituents(
        index_code,  # type: ignore[arg-type]
        date(2026, 9, 30),
    )
    assert len(snapshot.members) == size
    assert snapshot.effective_date == date(2026, 9, 28)


# --------------------------------------------------------------------------- #
# Stock directory (query_all_stock, mrd-intake D4)
# --------------------------------------------------------------------------- #

# The all_stock fixture was captured on 2026-10-09 07:22 Asia/Shanghai.
DIRECTORY_CAPTURE_TIME = datetime(2026, 10, 8, 23, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("code", "stock"),
    [
        ("sh.600519", True),
        ("sh.688981", True),
        ("sz.000001", True),
        ("sz.002594", True),
        ("sz.300750", True),
        ("bj.830799", True),
        ("sh.000001", False),  # index
        ("sh.510300", False),  # ETF
        ("sz.159915", False),  # ETF
        ("sz.399001", False),  # index
        ("sh.60051", False),
        ("600519.SH", False),
    ],
)
def test_is_stock_code(code: str, stock: bool) -> None:
    assert is_stock_code(code) is stock


async def test_fetch_all_stocks_filters_indices_and_funds_and_maps_status() -> None:
    backend = FakeBaostock()
    client = _client(backend, now=DIRECTORY_CAPTURE_TIME)
    snapshot = await client.fetch_all_stocks(date(2026, 10, 8))

    assert snapshot.as_of == date(2026, 10, 8)
    assert snapshot.source == "baostock"
    assert snapshot.captured_at == DIRECTORY_CAPTURE_TIME
    assert len(snapshot.raw_rows) == 37  # every row the fixture holds
    tickers = [entry.ticker for entry in snapshot.entries]
    assert tickers == sorted(tickers)
    assert len(tickers) == 29
    assert "000001.SH" not in tickers and "510300.SH" not in tickers
    assert "159915.SZ" not in tickers and "399001.SZ" not in tickers
    by_ticker = {entry.ticker: entry for entry in snapshot.entries}
    assert by_ticker["600519.SH"].name == "贵州茅台" and by_ticker["600519.SH"].trading
    assert by_ticker["600053.SH"].name == "*ST九鼎"
    assert by_ticker["600293.SH"].trading is False  # tradeStatus 0
    assert by_ticker["688496.SH"].trading is False
    assert by_ticker["000002.SZ"].name == "万科A"
    assert backend.calls == ["login", "query_all_stock", "logout"]


async def test_fetch_all_stocks_non_trading_day_is_empty() -> None:
    snapshot = await _client(FakeBaostock(), now=DIRECTORY_CAPTURE_TIME).fetch_all_stocks(
        date(2026, 10, 9)
    )
    assert snapshot.is_empty and snapshot.entries == ()


async def test_fetch_all_stocks_rejects_a_future_day() -> None:
    backend = FakeBaostock()
    with pytest.raises(LookaheadDataError):
        await _client(backend, now=DIRECTORY_CAPTURE_TIME).fetch_all_stocks(date(2026, 10, 10))
    assert backend.calls == []  # guarded before any upstream call


async def test_pre_publication_constituents_are_empty_not_error() -> None:
    snapshot = await _client(FakeBaostock()).fetch_constituents("CSI300", date(2004, 1, 2))
    assert snapshot.is_empty
    assert snapshot.effective_date is None
    assert snapshot.members == ()


async def test_future_date_uses_update_date_as_effective_date() -> None:
    # Captured on 2026-10-06: baostock answers 2027-01-01 with 2026-10-05.
    snapshot = await _client(FakeBaostock()).fetch_constituents("CSI300", date(2027, 1, 1))
    assert snapshot.effective_date == date(2026, 10, 5)


async def test_rejects_update_date_after_capture_date() -> None:
    # Replaying the same answer with a capture date before its updateDate.
    early = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    with pytest.raises(LookaheadDataError):
        await _client(FakeBaostock(), now=early).fetch_constituents("CSI300", date(2027, 1, 1))


async def test_capture_date_uses_shanghai_calendar() -> None:
    # 2026-10-04T16:30Z is already 2026-10-05 00:30 in Shanghai.
    shanghai_morning = datetime(2026, 10, 4, 16, 30, tzinfo=UTC)
    snapshot = await _client(FakeBaostock(), now=shanghai_morning).fetch_constituents(
        "CSI300", date(2027, 1, 1)
    )
    assert snapshot.effective_date == date(2026, 10, 5)
    one_hour_earlier = datetime(2026, 10, 4, 15, 30, tzinfo=UTC)
    with pytest.raises(LookaheadDataError):
        await _client(FakeBaostock(), now=one_hour_earlier).fetch_constituents(
            "CSI300", date(2027, 1, 1)
        )


async def test_rejects_update_date_after_as_of() -> None:
    backend = FakeBaostock()
    record = backend.records[("query_hs300_stocks", (("date", "2026-09-30"),))]
    backend.records[("query_hs300_stocks", (("date", "2026-09-20"),))] = record
    with pytest.raises(LookaheadDataError):
        await _client(backend).fetch_constituents("CSI300", date(2026, 9, 20))


async def test_mixed_update_dates_are_rejected() -> None:
    backend = FakeBaostock()
    key = ("query_hs300_stocks", (("date", "2026-09-30"),))
    record = json.loads(json.dumps(backend.records[key]))
    record["rows"][0][0] = "2026-09-21"
    backend.records[key] = record
    with pytest.raises(ProviderDataError):
        await _client(backend).fetch_constituents("CSI300", date(2026, 9, 30))


# --------------------------------------------------------------------------- #
# Industry
# --------------------------------------------------------------------------- #


async def test_fetch_industry_for_one_symbol() -> None:
    snapshot = await _client(FakeBaostock()).fetch_industry(date(2026, 9, 30), "600519.SH")
    assert snapshot.system == "csrc"
    assert snapshot.effective_date == date(2026, 9, 28)
    (entry,) = snapshot.entries
    assert entry.ticker == "600519.SH"
    assert entry.company_name == "贵州茅台"
    assert (entry.industry_code, entry.industry_name) == ("C15", "酒、饮料和精制茶制造业")
    assert entry.raw_industry == "C15酒、饮料和精制茶制造业"
    assert entry.classification_label == "证监会行业分类"


@pytest.mark.parametrize(
    ("ticker", "code", "name"),
    [
        ("601398.SH", "J66", "货币金融服务"),  # bank
        ("601318.SH", "J68", "保险业"),  # insurer
        ("600900.SH", "D44", "电力、热力生产和供应业"),  # utility
        ("300750.SZ", "C38", "电气机械和器材制造业"),
    ],
)
async def test_industry_fixture_sectors(ticker: str, code: str, name: str) -> None:
    snapshot = await _client(FakeBaostock()).fetch_industry(date(2026, 9, 30), ticker)
    assert snapshot.by_ticker()[ticker].industry_code == code
    assert snapshot.by_ticker()[ticker].industry_name == name


async def test_empty_industry_maps_to_none() -> None:
    snapshot = await _client(FakeBaostock()).fetch_industry(date(2026, 9, 30), "600001.SH")
    (entry,) = snapshot.entries
    assert entry.industry_code is None
    assert entry.industry_name is None


async def test_legacy_industry_label_keeps_name_without_code() -> None:
    snapshot = await _client(FakeBaostock()).fetch_industry(date(2010, 1, 4), "600519.SH")
    (entry,) = snapshot.entries
    assert entry.industry_code is None
    assert entry.industry_name == "制造业-饮料制造业-食品、饮料-酒精及饮料酒制造业"
    assert entry.effective_date == date(2010, 1, 4)


async def test_full_market_industry() -> None:
    snapshot = await _client(FakeBaostock()).fetch_industry(date(2026, 9, 30))
    assert len(snapshot.entries) == 21
    assert [e.ticker for e in snapshot.entries] == sorted(e.ticker for e in snapshot.entries)
    assert snapshot.effective_date == date(2026, 9, 28)


async def test_industry_rejects_lookahead() -> None:
    early = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
    with pytest.raises(LookaheadDataError):
        await _client(FakeBaostock(), now=early).fetch_industry(date(2026, 9, 30), "600519.SH")


# --------------------------------------------------------------------------- #
# Errors, retries, session handling
# --------------------------------------------------------------------------- #


class _ErrorBackend(FakeBaostock):
    def __init__(self, codes: list[str]) -> None:
        super().__init__()
        self.codes = codes

    def query_hs300_stocks(self, date: str = "") -> _ReplayResult:
        self.calls.append("query_hs300_stocks")
        if self.codes:
            code = self.codes.pop(0)
            return _ReplayResult(
                {"error_code": code, "error_msg": "boom", "fields": [], "rows": []}
            )
        return super().query_hs300_stocks(date=date)


async def test_terminal_error_code_raises_typed_error_without_retry() -> None:
    backend = _ErrorBackend(["10004010"])
    with pytest.raises(BaostockError) as caught:
        await _client(backend).fetch_constituents("CSI300", date(2026, 9, 30))
    assert caught.value.error_code == "10004010"
    assert not is_transient_error(caught.value)
    assert backend.calls == ["login", "query_hs300_stocks", "logout"]


async def test_transient_error_is_retried_then_succeeds() -> None:
    backend = _ErrorBackend(["10002007"])
    snapshot = await _client(backend).fetch_constituents("CSI300", date(2026, 9, 30))
    assert len(snapshot.members) == 300
    assert backend.calls.count("login") == 2
    assert backend.calls.count("logout") == 2


async def test_transient_error_exhausts_retries() -> None:
    backend = _ErrorBackend(["10002002", "10002002"])
    with pytest.raises(BaostockTransientError) as caught:
        await _client(backend, max_attempts=2).fetch_constituents("CSI300", date(2026, 9, 30))
    assert is_transient_error(caught.value)


async def test_login_failure_is_classified() -> None:
    with pytest.raises(BaostockTransientError):
        await _client(FakeBaostock(login_code="10002001"), max_attempts=1).fetch_constituents(
            "CSI300", date(2026, 9, 30)
        )
    with pytest.raises(BaostockError):
        await _client(FakeBaostock(login_code="10001011"), max_attempts=1).fetch_constituents(
            "CSI300", date(2026, 9, 30)
        )


async def test_socket_error_is_transient() -> None:
    class Broken(FakeBaostock):
        def query_hs300_stocks(self, date: str = "") -> _ReplayResult:
            raise ConnectionResetError("reset by peer")

    backend = Broken()
    with pytest.raises(BaostockTransientError) as caught:
        await _client(backend, max_attempts=1).fetch_constituents("CSI300", date(2026, 9, 30))
    assert is_transient_error(caught.value)
    assert backend.calls[-1] == "logout"


async def test_timeout_is_transient() -> None:
    release = threading.Event()

    class Slow(FakeBaostock):
        def query_hs300_stocks(self, date: str = "") -> _ReplayResult:
            release.wait(5)
            return super().query_hs300_stocks(date=date)

    try:
        with pytest.raises(BaostockTransientError, match="timed out"):
            await _client(Slow(), max_attempts=1, timeout_seconds=0.05).fetch_constituents(
                "CSI300", date(2026, 9, 30)
            )
    finally:
        release.set()


async def test_truncated_paging_is_detected() -> None:
    backend = FakeBaostock()
    key = ("query_hs300_stocks", (("date", "2026-09-30"),))
    record = dict(backend.records[key])
    record["per_page_count"] = "300"  # a full page with no acknowledged next page
    backend.records[key] = record
    with pytest.raises(BaostockTransientError, match="truncated"):
        await _client(backend, max_attempts=1).fetch_constituents("CSI300", date(2026, 9, 30))


def test_retry_policy_classifies_provider_errors() -> None:
    assert is_transient_error(BaostockTransientError("x", provider="baostock"))
    assert not is_transient_error(BaostockError("x", provider="baostock"))
    assert not is_transient_error(LookaheadDataError("x", provider="baostock"))


# --------------------------------------------------------------------------- #
# Live smoke (network)
# --------------------------------------------------------------------------- #


@pytest.mark.network
async def test_live_baostock_smoke() -> None:
    client = BaostockReferenceClient()
    today = datetime.now(UTC).date()
    snapshot = await client.fetch_constituents("SSE50", today)
    assert len(snapshot.members) == 50
    assert snapshot.effective_date is not None
    assert snapshot.effective_date <= today
    industry = await client.fetch_industry(today, "600519.SH")
    assert industry.entries[0].industry_code == "C15"
