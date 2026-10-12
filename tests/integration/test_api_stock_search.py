"""API tests for GET /stocks/search (mrd-intake D4 combobox source)."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

from investment_agent.api.routes import stocks
from investment_agent.providers.baostock_market import QuarterlyFinancials
from investment_agent.providers.baostock_reference import IndustryEntry, IndustrySnapshot
from investment_agent.providers.errors import ProviderTransientError
from investment_agent.snapshots.directory import pinyin_initials
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.earnings_events import DisclosureScheduleRecord
from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.listing import ListingRepository
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


async def _seed_directory(rows=ROWS) -> None:
    async with session_scope() as session:
        await StockDirectoryRepository(session).upsert_all(
            [
                DirectoryUpsert(ticker=t, name=n, pinyin_initials=pinyin_initials(n), status=s)
                for t, n, s in rows
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


def test_empty_directory_is_unavailable_rather_than_no_match(api_client) -> None:
    response = api_client.get("/stocks/search?q=山东", headers=H)
    assert response.status_code == 503
    assert "字典尚未就绪" in response.json()["detail"]
    assert api_client.get("/stocks/search?q=", headers=H).json() == []


async def test_shandong_name_code_initials_and_full_pinyin(api_client) -> None:
    await _seed_directory([
        *ROWS,
        ("600350.SH", "山东高速", "listed"),
        ("600547.SH", "山东黄金", "listed"),
    ])
    for query in ("山东", "sd", "shandong", "SHANDONG"):
        response = api_client.get("/stocks/search", params={"q": query}, headers=H)
        assert response.status_code == 200
        assert _tickers(response.json()) == ["600350.SH", "600547.SH"]
    for query in ("60054", "sdhj", "shandonghuang", "shandonghuangjin"):
        assert _tickers(api_client.get("/stocks/search", params={"q": query}, headers=H).json()) == ["600547.SH"]
    # A healthy directory with an unknown query remains an ordinary empty result.
    assert api_client.get("/stocks/search?q=不存在", headers=H).json() == []


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
        "industry": None,
        "report_year": None,
        "disclosed_on": None,
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


async def test_search_industry_prefers_shenwan_level_one_and_uses_current_intervals(api_client, monkeypatch) -> None:
    await _seed_directory()
    today = date(2026, 10, 11)
    monkeypatch.setattr(stocks, "shanghai_today", lambda: today)
    async with session_scope() as session:
        for ticker, system, code, name, start, end in [
            ("600519.SH", "csrc", "C15", "酒、饮料和精制茶制造业", date(2021, 10, 4), None),
            ("600519.SH", "sw2021", "340501", "白酒Ⅲ", date(2021, 7, 30), None),
            ("000001.SZ", "csrc", "J66", "货币金融服务", date(2021, 10, 4), None),
            ("000001.SZ", "sw2021", "480100", "未来银行分类", date(2027, 1, 1), None),
            ("000002.SZ", "csrc", "K70", "已失效的行业", date(2021, 1, 1), today),
            ("000002.SZ", "sw2021", "999999", "无法识别的申万分类", today, None),
        ]:
            listing = await ListingRepository(session).ensure_a_share_listing(ticker, company_name=ticker)
            session.add(IndustryClassification(
                listing_id=listing.id, system=system, code=code, name=name,
                valid_from=start, valid_to=end, source="test",
                captured_at=datetime(2026, 10, 11, tzinfo=UTC),
            ))
    assert api_client.get("/stocks/search?q=gzmt", headers=H).json()[0]["industry"] == "食品饮料"
    assert api_client.get("/stocks/search?q=000001", headers=H).json()[0]["industry"] == "货币金融服务"
    assert api_client.get("/stocks/search?q=000002", headers=H).json()[0]["industry"] is None
    # Resolving an existing industry never instantiates an upstream client.
    def refuse(**kwargs):
        raise AssertionError("local industry must not query a provider")
    monkeypatch.setattr(stocks, "BaostockReferenceClient", refuse)
    assert api_client.get("/stocks/600519.SH/industry", headers=H).json() == {
        "ticker": "600519.SH", "industry": "食品饮料",
    }


async def test_missing_industry_is_fetched_for_selected_stock_and_cached(api_client, monkeypatch) -> None:
    await _seed_directory()
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def fetch_industry(self, as_of, symbol=None):
            calls.append(symbol)
            return IndustrySnapshot(
                as_of=as_of, effective_date=date(2021, 10, 4),
                entries=tuple(IndustryEntry(
                    ticker=ticker, company_name=name, industry_code="J66",
                    industry_name="货币金融服务", effective_date=date(2021, 10, 4),
                    raw_industry="J66货币金融服务", classification_label="证监会行业分类",
                ) for ticker, name in [("000001.SZ", "平安银行"), ("601318.SH", "中国平安")]),
                captured_at=datetime(2026, 10, 11, tzinfo=UTC), raw_rows=(),
            )
    monkeypatch.setattr(stocks, "BaostockReferenceClient", Client)
    for _ in range(2):
        response = api_client.get("/stocks/000001.SZ/industry", headers=H)
        assert response.status_code == 200
        assert response.json() == {"ticker": "000001.SZ", "industry": "货币金融服务"}
    assert calls == ["000001.SZ"]
    assert api_client.get("/stocks/search?q=000001", headers=H).json()[0]["industry"] == "货币金融服务"
    async with session_scope() as session:
        assert await ListingRepository(session).get_by_ticker("SSE", "601318") is None


async def test_industry_failure_and_invalid_requests(api_client, monkeypatch) -> None:
    await _seed_directory()
    class Client:
        def __init__(self, **kwargs):
            pass
        async def fetch_industry(self, as_of, symbol=None):
            raise ProviderTransientError("offline", provider="baostock")
    monkeypatch.setattr(stocks, "BaostockReferenceClient", Client)
    assert api_client.get("/stocks/000001.SZ/industry").status_code == 401
    assert api_client.get("/stocks/000001/industry", headers=H).status_code == 422
    assert api_client.get("/stocks/999999.SH/industry", headers=H).status_code == 404
    response = api_client.get("/stocks/000001.SZ/industry", headers=H)
    assert response.status_code == 503
    assert "行业暂未获取" in response.json()["detail"]
    assert api_client.get("/stocks/search?q=000001", headers=H).json()[0]["industry"] is None


async def _seed_financial(ticker, period, published, *, availability="actual", kind="annual"):
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(ticker, company_name=ticker)
        await FinancialSnapshotRepository(session).insert(
            listing_id=listing.id, report_period=period, period_kind=kind, fields={},
            provider="test", captured_at=datetime(2026, 2, 1, tzinfo=UTC),
            availability=availability, available_from=published,
        )


async def test_report_year_uses_actual_annual_disclosures_not_quarters_or_appointments(api_client, monkeypatch):
    await _seed_directory()
    monkeypatch.setattr(stocks, "shanghai_today", lambda: date(2026, 10, 11))
    await _seed_financial("600519.SH", date(2025, 12, 31), date(2026, 3, 30))
    await _seed_financial("600519.SH", date(2026, 9, 30), date(2026, 10, 8), kind="q3")
    await _seed_financial("600519.SH", date(2026, 12, 31), date(2027, 3, 30))
    await _seed_financial("000002.SZ", date(2025, 12, 31), date(2026, 3, 30), availability="approx")
    await _seed_financial("300750.SZ", date(2025, 12, 31), date(2026, 11, 1))
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("000001.SZ", company_name="平安银行")
        session.add(DisclosureScheduleRecord(
            listing_id=listing.id, report_period=date(2025, 12, 31),
            first_date=date(2026, 3, 15), scheduled_date=date(2026, 3, 20), actual_date=date(2026, 3, 18),
            captured_at=datetime(2026, 3, 19, tzinfo=UTC), provider="test",
        ))
        listing = await ListingRepository(session).ensure_a_share_listing("601318.SH", company_name="中国平安")
        session.add(DisclosureScheduleRecord(
            listing_id=listing.id, report_period=date(2025, 12, 31),
            first_date=date(2026, 3, 15), scheduled_date=date(2026, 3, 20), actual_date=None,
            captured_at=datetime(2026, 3, 19, tzinfo=UTC), provider="test",
        ))
    def refuse(**kwargs):
        raise AssertionError("confirmed latest annual report needs no provider call")
    monkeypatch.setattr(stocks, "BaostockMarketClient", refuse)
    for ticker, disclosed in [("600519.SH", "2026-03-30"), ("000001.SZ", "2026-03-18")]:
        item = api_client.get("/stocks/search", params={"q": ticker}, headers=H).json()[0]
        assert (item["report_year"], item["disclosed_on"]) == (2025, disclosed)
        assert api_client.get(f"/stocks/{ticker}/report-year", headers=H).json() == {
            "ticker": ticker, "report_year": 2025, "disclosed_on": disclosed,
        }
    for ticker in ("000002.SZ", "300750.SZ", "601318.SH"):
        item = api_client.get("/stocks/search", params={"q": ticker}, headers=H).json()[0]
        assert item["report_year"] is None and item["disclosed_on"] is None


async def test_older_report_year_is_checked_against_newer_disclosures(api_client, monkeypatch):
    await _seed_directory()
    monkeypatch.setattr(stocks, "shanghai_today", lambda: date(2026, 3, 1))
    await _seed_financial("600519.SH", date(2024, 12, 31), date(2025, 3, 30))
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def fetch_quarterly_financials(self, ticker, year, quarter):
            calls.append((ticker, year, quarter))
            # A newer report whose actual disclosure is still in the future.
            return QuarterlyFinancials(
                ticker=ticker, report_period=date(year, 12, 31), stat_date=date(year, 12, 31),
                pub_date=date(2026, 3, 30), fields={}, source_values={}, tables=(), raw_rows={},
                captured_at=datetime(2026, 3, 1, tzinfo=UTC),
            )
    monkeypatch.setattr(stocks, "BaostockMarketClient", Client)
    assert api_client.get("/stocks/search?q=gzmt", headers=H).json()[0]["report_year"] is None
    response = api_client.get("/stocks/600519.SH/report-year", headers=H)
    assert response.json() == {"ticker": "600519.SH", "report_year": 2024, "disclosed_on": "2025-03-30"}
    assert calls == [("600519.SH", 2025, 4)]


async def test_missing_annual_disclosure_is_fetched_and_cached_with_raw_source(api_client, monkeypatch):
    await _seed_directory()
    monkeypatch.setattr(stocks, "shanghai_today", lambda: date(2026, 10, 11))
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def fetch_quarterly_financials(self, ticker, year, quarter):
            calls.append((ticker, year, quarter))
            return QuarterlyFinancials(
                ticker=ticker, report_period=date(year, 12, 31), stat_date=date(year, 12, 31),
                pub_date=date(2026, 3, 30), fields={"net_profit": 123},
                source_values={"profit.netProfit": "123"}, tables=("profit",),
                raw_rows={"profit": {"pubDate": "2026-03-30", "netProfit": "123"}},
                captured_at=datetime(2026, 10, 11, tzinfo=UTC),
            )
    monkeypatch.setattr(stocks, "BaostockMarketClient", Client)
    for _ in range(2):
        response = api_client.get("/stocks/600519.SH/report-year", headers=H)
        assert response.status_code == 200
        assert response.json() == {"ticker": "600519.SH", "report_year": 2025, "disclosed_on": "2026-03-30"}
    assert calls == [("600519.SH", 2025, 4)]
    assert api_client.get("/stocks/search?q=gzmt", headers=H).json()[0]["report_year"] == 2025
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing("600519.SH")
        record = await FinancialSnapshotRepository(session).latest_available(listing.id, date(2026, 10, 11))
        assert record.availability == "actual" and record.fields["net_profit"] == 123
        assert record.raw_ref.startswith("snapshot-runs/stock-autofill/")


async def test_report_year_artifact_is_deterministic_per_ticker_and_period(api_client, monkeypatch):
    """P2-11: a repeated miss overwrites one deterministic artifact file instead
    of accumulating a new uuid4 file per call (upstream behaviour unchanged)."""

    await _seed_directory()
    monkeypatch.setattr(stocks, "shanghai_today", lambda: date(2026, 10, 11))

    class Client:
        def __init__(self, **kwargs):
            pass

        async def fetch_quarterly_financials(self, ticker, year, quarter):
            return QuarterlyFinancials(
                ticker=ticker, report_period=date(year, 12, 31), stat_date=date(year, 12, 31),
                pub_date=date(2026, 3, 30), fields={}, source_values={}, tables=(),
                raw_rows={"pubDate": "2026-03-30"},
                captured_at=datetime(2026, 10, 11, tzinfo=UTC),
            )

    monkeypatch.setattr(stocks, "BaostockMarketClient", Client)
    data_dir = Path(os.environ["DATA_DIR"])
    for _ in range(3):
        assert api_client.get("/stocks/600519.SH/report-year", headers=H).status_code == 200
    artifacts = sorted((data_dir / "snapshot-runs" / "stock-autofill").rglob("annual.json"))
    assert artifacts == [data_dir / "snapshot-runs" / "stock-autofill" / "600519.SH" / "2025-12-31" / "annual.json"]
    body = artifacts[0].read_text(encoding="utf-8")
    assert "600519.SH" in body and "2025-12-31" in body


async def test_report_year_missing_or_unavailable_never_guesses_a_year(api_client, monkeypatch):
    await _seed_directory()
    class Client:
        def __init__(self, **kwargs):
            pass
        async def fetch_quarterly_financials(self, ticker, year, quarter):
            return None
    monkeypatch.setattr(stocks, "BaostockMarketClient", Client)
    assert api_client.get("/stocks/600519.SH/report-year").status_code == 401
    assert api_client.get("/stocks/600519/report-year", headers=H).status_code == 422
    assert api_client.get("/stocks/999999.SH/report-year", headers=H).status_code == 404
    assert api_client.get("/stocks/600519.SH/report-year", headers=H).json() == {
        "ticker": "600519.SH", "report_year": None, "disclosed_on": None,
    }
    async def fail(self, ticker, year, quarter):
        raise ProviderTransientError("offline", provider="baostock")
    monkeypatch.setattr(Client, "fetch_quarterly_financials", fail)
    response = api_client.get("/stocks/600519.SH/report-year", headers=H)
    assert response.status_code == 503
    assert api_client.get("/stocks/search?q=gzmt", headers=H).json()[0]["report_year"] is None
