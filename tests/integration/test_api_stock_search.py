"""API tests for GET /stocks/search (mrd-intake D4 combobox source)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from investment_agent.snapshots.directory import pinyin_initials
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.stock_directory import (
    DirectoryUpsert,
    StockDirectoryRepository,
)
from investment_agent.web.session import SESSION_COOKIE_NAME
from tests.unit._snapshot_support import seed_universe, seed_watchlist

H = {"X-API-Key": "secret"}
ROWS = [
    ("600519.SH", "贵州茅台", "listed"),
    ("600518.SH", "ST康美", "delisted"),
    ("600516.SH", "方大炭素", "listed"),
    ("000001.SZ", "平安银行", "listed"),
    ("601318.SH", "中国平安", "listed"),
    ("000002.SZ", "万科A", "suspended"),
    ("300750.SZ", "宁德时代", "listed"),
    ("000100.SZ", "TCL科技", "listed"),
]


async def _seed_directory() -> None:
    async with session_scope() as session:
        await StockDirectoryRepository(session).upsert_all(
            [
                DirectoryUpsert(ticker=t, name=n, pinyin_initials=pinyin_initials(n), status=s)
                for t, n, s in ROWS
            ],
            synced_on=date(2026, 10, 8),
            captured_at=datetime(2026, 10, 9, 0, 30, tzinfo=UTC),
            provider="baostock",
        )


def _tickers(body: list[dict[str, object]]) -> list[object]:
    return [item["ticker"] for item in body]


def test_search_requires_auth(api_client) -> None:
    assert api_client.get("/stocks/search?q=600").status_code == 401
    assert api_client.get("/stocks/search?q=600", headers={"X-API-Key": "nope"}).status_code == 401


async def test_blank_query_returns_nothing_without_touching_the_directory(api_client) -> None:
    await _seed_directory()
    assert api_client.get("/stocks/search", headers=H).json() == []
    assert api_client.get("/stocks/search?q=", headers=H).json() == []
    assert api_client.get("/stocks/search?q=%20%20", headers=H).json() == []


async def test_three_way_match_and_order(api_client) -> None:
    await _seed_directory()
    code = api_client.get("/stocks/search?q=6005", headers=H).json()
    assert _tickers(code) == ["600516.SH", "600519.SH", "600518.SH"]  # delisted last
    assert code[1] == {
        "ticker": "600519.SH",
        "name": "贵州茅台",
        "status": "listed",
        "in_universe": False,
        "on_watchlist": False,
    }
    assert code[2]["status"] == "delisted"
    assert _tickers(api_client.get("/stocks/search?q=600519.SH", headers=H).json()) == ["600519.SH"]
    assert _tickers(api_client.get("/stocks/search?q=平安", headers=H).json()) == [
        "000001.SZ",
        "601318.SH",
    ]
    assert _tickers(api_client.get("/stocks/search?q=gzmt", headers=H).json()) == ["600519.SH"]
    assert _tickers(api_client.get("/stocks/search?q=GZMT", headers=H).json()) == ["600519.SH"]
    assert _tickers(api_client.get("/stocks/search?q=wka", headers=H).json()) == ["000002.SZ"]
    # Latin letters in a name outrank the pinyin matches for the same text.
    assert _tickers(api_client.get("/stocks/search?q=t", headers=H).json())[0] == "000100.SZ"
    assert api_client.get("/stocks/search?q=zzzz", headers=H).json() == []


async def test_limit_bounds(api_client) -> None:
    await _seed_directory()
    assert len(api_client.get("/stocks/search?q=0&limit=1", headers=H).json()) == 1
    assert len(api_client.get("/stocks/search?q=0", headers=H).json()) == 3
    assert api_client.get("/stocks/search?q=0&limit=0", headers=H).status_code == 422
    assert api_client.get("/stocks/search?q=0&limit=51", headers=H).status_code == 422
    assert api_client.get("/stocks/search?q=" + "a" * 33, headers=H).status_code == 422


async def test_universe_and_watchlist_flags(api_client) -> None:
    await _seed_directory()
    await seed_universe(["600519.SH", "000001.SZ"])  # CSI300, the default target pool
    await seed_watchlist(["000001.SZ", "300750.SZ"], disabled=("300750.SZ",))
    by_ticker = {
        item["ticker"]: item for item in api_client.get("/stocks/search?q=0", headers=H).json()
    }
    assert by_ticker["000001.SZ"]["in_universe"] and by_ticker["000001.SZ"]["on_watchlist"]
    assert not by_ticker["000002.SZ"]["in_universe"] and not by_ticker["000002.SZ"]["on_watchlist"]
    maotai = api_client.get("/stocks/search?q=600519", headers=H).json()[0]
    assert maotai["in_universe"] and not maotai["on_watchlist"]
    # A disabled watchlist item still counts as "on the watchlist".
    catl = api_client.get("/stocks/search?q=ndsd", headers=H).json()[0]
    assert catl["on_watchlist"] and not catl["in_universe"]


async def test_session_cookie_works_for_the_browser(api_client) -> None:
    await _seed_directory()
    login = api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert login.status_code == 200 and SESSION_COOKIE_NAME in api_client.cookies
    response = api_client.get("/stocks/search?q=gzmt")
    assert response.status_code == 200
    assert _tickers(response.json()) == ["600519.SH"]
