"""Read-only MCP server (mrd-intake D5): read-only engine, tool catalogue and
one call per tool against the seeded in-memory database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from investment_agent.candidates.lifecycle import CandidatePolicy, create_candidates_from_screen
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.mcp_server.readonly import readonly_engine, readonly_sessions, readonly_url
from investment_agent.mcp_server.server import TOOL_NAMES, build_server
from investment_agent.storage.repositories.earnings_events import EarningsEventRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.stock_directory import (
    DirectoryUpsert,
    StockDirectoryRepository,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.unit._candidate_support import (
    seed_classification,
    seed_draft,
    seed_financials,
    seed_listing,
    seed_result,
    seed_screen_run,
    seed_template,
)

# ---- read-only engine ------------------------------------------------------


def _settings(url: str) -> Settings:
    return Settings(api_key="secret", database_url=url)


@pytest.mark.parametrize(
    "url",
    ["sqlite+aiosqlite:///./data/app.db", "sqlite+aiosqlite:////srv/zcharlie/app.db"],
)
def test_readonly_url_rewrites_sqlite_as_ro_uri(url: str) -> None:
    rewritten = readonly_url(url)
    assert rewritten.startswith("sqlite+aiosqlite:///file:/")
    assert rewritten.endswith("app.db?mode=ro&uri=true")


@pytest.mark.parametrize(
    "url",
    ["postgresql+asyncpg://user:pw@db/app", "mysql://db/app", "sqlite+aiosqlite:///:memory:"],
)
def test_readonly_url_refuses_other_backends(url: str) -> None:
    with pytest.raises(NotImplementedError) as info:
        readonly_url(url)
    assert "pw" not in str(info.value)


async def test_readonly_engine_reads_but_rejects_writes(tmp_path) -> None:
    path = tmp_path / "ro.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE note (body TEXT)")
    seed.execute("INSERT INTO note VALUES ('kept')")
    seed.commit()
    seed.close()

    engine = readonly_engine(_settings(f"sqlite+aiosqlite:///{path}"))
    try:
        sessions = readonly_sessions(engine)
        async with sessions() as session:
            rows = (await session.execute(text("SELECT body FROM note"))).scalars().all()
            assert rows == ["kept"]
        for statement in (
            "INSERT INTO note VALUES ('leak')",
            "UPDATE note SET body = 'leak'",
            "DELETE FROM note",
            "CREATE TABLE other (x INTEGER)",
        ):
            async with sessions() as session:
                with pytest.raises(OperationalError, match="readonly"):
                    await session.execute(text(statement))
    finally:
        await engine.dispose()

    check = sqlite3.connect(path)
    assert check.execute("SELECT body FROM note").fetchall() == [("kept",)]
    check.close()


async def test_readonly_engine_never_creates_the_file(tmp_path) -> None:
    missing = tmp_path / "absent.db"
    engine = readonly_engine(_settings(f"sqlite+aiosqlite:///{missing}"))
    try:
        async with readonly_sessions(engine)() as session:
            with pytest.raises(OperationalError):
                await session.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    assert not missing.exists()


# ---- CLI -------------------------------------------------------------------


def test_cli_mcp_help_lists_serve_and_tools() -> None:
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "tools" in result.output


# ---- tools against the seeded in-memory database ---------------------------

TICKER = "600519.SH"
EXPECTED_TOOLS = [
    "list_watchlist",
    "get_stock_snapshot",
    "get_bars",
    "list_candidates",
    "get_candidate",
    "get_rule",
    "list_earnings_events",
    "get_data_health",
    "search_stocks",
]
FORBIDDEN_NAME_PARTS = ("set", "add", "update", "delete", "run", "draft")


def _weekdays_back(count: int) -> list[date]:
    days: list[date] = []
    cursor = shanghai_today() - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


async def _seed(session) -> dict[str, int]:
    """Listing, watchlist row, bars, financials, one open candidate with a draft,
    one forecast + one schedule capture and two directory rows."""

    captured = datetime.now(UTC) - timedelta(hours=2)
    await seed_template(session)
    listing = await seed_listing(session, TICKER, "贵州茅台")
    await seed_classification(session, listing.id)
    await seed_financials(session, listing.id)
    market = MarketSnapshotRepository(session)
    for index, day in enumerate(_weekdays_back(30)):
        value = Decimal(1500 + index)
        await market.insert(
            listing_id=listing.id,
            trade_date=day,
            open=value,
            high=value,
            low=value,
            close=value,
            volume=Decimal(100),
            provider="baostock",
            captured_at=captured,
            pe_ttm=Decimal("20.5"),
        )
    run_id = await seed_screen_run(session)
    await seed_result(session, run_id=run_id, listing=listing)
    outcome = await create_candidates_from_screen(
        session, run_id=run_id, policy=CandidatePolicy(), now=datetime.now(UTC)
    )
    candidate_id = outcome.created[0]["candidate_id"]
    draft = await seed_draft(session)
    # After the screen: a watchlist row would have made the screen skip the listing.
    await WatchlistRepository(session).add(listing.id, "seed", industry="白酒", provider="auto")
    events = EarningsEventRepository(session)
    await events.insert_forecast(
        listing_id=listing.id,
        report_period=date(2026, 9, 30),
        notice_date=date(2026, 10, 8),
        forecast_type="预增",
        indicator="归母净利润",
        profit_low=1.0e10,
        profit_high=1.2e10,
        yoy_low=0.1,
        yoy_high=0.2,
        content="预计净利润同比增长",
        provider="akshare",
        captured_at=captured,
        raw_ref="eastmoney:yjyg:600519",
    )
    await events.insert_schedule(
        listing_id=listing.id,
        report_period=date(2026, 9, 30),
        first_date=date(2026, 10, 28),
        scheduled_date=date(2026, 10, 28),
        actual_date=None,
        provider="akshare",
        captured_at=captured,
        raw_ref="eastmoney:yysj:600519",
    )
    await StockDirectoryRepository(session).upsert_all(
        [
            DirectoryUpsert(TICKER, "贵州茅台", "gzmt", "listed"),
            DirectoryUpsert("000858.SZ", "五粮液", "wly", "listed"),
        ],
        synced_on=shanghai_today(),
        captured_at=captured,
        provider="baostock",
    )
    await session.flush()
    return {"listing_id": listing.id, "candidate_id": candidate_id, "rule_id": draft.id}


@pytest_asyncio.fixture
async def mcp(db_session):
    ids = await _seed(db_session)

    @asynccontextmanager
    async def sessions():
        yield db_session

    server = build_server(_settings("sqlite+aiosqlite:///./unused.db"), sessions)
    return server, ids


async def _call(server, name: str, **arguments) -> dict:
    result = await server.call_tool(name, arguments)
    assert result.is_error is False
    assert result.structured_content is not None
    return result.structured_content


async def test_tool_catalogue_is_locked() -> None:
    server = build_server()
    names = [tool.name for tool in await server.list_tools()]
    assert names == EXPECTED_TOOLS == list(TOOL_NAMES)
    assert [str(resource.uri) for resource in await server.list_resources()] == [
        "zcharlie://health"
    ]


async def test_no_tool_name_suggests_a_write() -> None:
    for name in [tool.name for tool in await build_server().list_tools()]:
        for part in FORBIDDEN_NAME_PARTS:
            assert part not in name, (name, part)


async def test_list_watchlist(mcp) -> None:
    server, ids = mcp
    out = await _call(server, "list_watchlist")
    assert out["count"] == 1
    item = out["items"][0]
    assert item["ticker"] == TICKER
    assert item["name"] == "贵州茅台"
    assert item["listing_id"] == ids["listing_id"]
    assert item["provider"] == "auto"
    assert out["as_of"] == shanghai_today().isoformat()


async def test_get_stock_snapshot(mcp) -> None:
    server, _ids = mcp
    out = await _call(server, "get_stock_snapshot", ticker=TICKER)
    assert out["ticker"] == TICKER
    assert out["as_of"] == shanghai_today().isoformat()
    assert out["provider_policy"] == "target_pool"
    assert out["industry"]["csrc"]["code"] == "C15"
    assert out["industry"]["shenwan"]["code"] == "340501"
    assert out["market"]["provider"] == "baostock"
    assert out["market"]["close"] == 1529.0
    assert out["financial"]["report_period"] == "2026-06-30"
    assert out["financial"]["provider"] == "baostock"
    assert "pe_ttm" in out["factors"]
    assert out["segments"] is None

    # Point in time: nothing seeded was captured by 2026-01-15.
    earlier = await _call(server, "get_stock_snapshot", ticker=TICKER, as_of="2026-01-15")
    assert earlier["as_of"] == "2026-01-15"
    assert earlier["financial"] is None
    assert earlier["market"] is None
    assert earlier["factors"]["pe_ttm"]["quality"] == "missing"


async def test_get_bars(mcp) -> None:
    server, _ids = mcp
    out = await _call(server, "get_bars", ticker=TICKER, days=20)
    assert out["provider"] == "baostock,tencent"
    assert out["adjust"] == "none"
    assert out["count"] == 20
    bars = out["bars"]
    assert [bar["close"] for bar in bars] == [float(1510 + i) for i in range(20)]
    assert bars[0]["trade_date"] < bars[-1]["trade_date"]
    assert bars[-1]["provider"] == "baostock"
    assert bars[-1]["captured_at"].endswith("Z") or "+00:00" in bars[-1]["captured_at"]


async def test_list_and_get_candidate(mcp) -> None:
    server, ids = mcp
    page = await _call(server, "list_candidates", status="open")
    assert page["total"] == 1
    summary = page["items"][0]
    assert summary["id"] == ids["candidate_id"]
    assert summary["ticker"] == TICKER
    assert summary["status"] == "open"
    assert summary["latest_screen"]["reasons"][0]["factor"] == "roe"
    assert (await _call(server, "list_candidates", status="dismissed"))["total"] == 0

    detail = await _call(server, "get_candidate", candidate_id=ids["candidate_id"])
    assert detail["id"] == ids["candidate_id"]
    assert detail["draft_rule_detail"]["rule_instance_id"] == ids["rule_id"]
    assert detail["draft_rule_detail"]["draft_rationale"].startswith("valuation")
    assert [decision["kind"] for decision in detail["decisions"]] == ["nominate"]
    assert detail["segments"] == []


async def test_get_rule(mcp) -> None:
    server, ids = mcp
    out = await _call(server, "get_rule", rule_id=ids["rule_id"])
    assert out["rule_instance_id"] == ids["rule_id"]
    assert out["template_ref"] == "cand_test@1"
    assert out["params"] == {"pe_pct_max": 0.25}
    assert out["param_ranges"]["pe_pct_max"] == {"min": 0.1, "max": 0.6}
    assert out["status"] == "draft"


async def test_list_earnings_events(mcp) -> None:
    server, _ids = mcp
    out = await _call(server, "list_earnings_events")
    assert out["ticker"] is None
    forecast = out["forecasts"][0]
    assert forecast["ticker"] == TICKER
    assert forecast["forecast_type"] == "预增"
    assert forecast["profit_low"] == 10000000000.0
    assert forecast["provider"] == "akshare"
    assert forecast["raw_ref"] == "eastmoney:yjyg:600519"
    assert forecast["captured_at"]
    disclosure = out["disclosures"][0]
    assert disclosure["scheduled_date"] == "2026-10-28"
    assert disclosure["actual_date"] is None

    scoped = await _call(server, "list_earnings_events", ticker=TICKER, limit=1)
    assert len(scoped["forecasts"]) == 1 and len(scoped["disclosures"]) == 1


async def test_get_data_health_and_resource(mcp) -> None:
    server, _ids = mcp
    out = await _call(server, "get_data_health")
    assert out["as_of"] == shanghai_today().isoformat()
    assert out["target_universe"] == "CSI300"
    assert {"accumulation", "freshness", "collections", "providers"} <= out.keys()

    contents = list(await server.read_resource("zcharlie://health"))
    assert contents[0].mime_type == "application/json"
    assert json.loads(contents[0].content)["target_universe"] == "CSI300"


async def test_search_stocks(mcp) -> None:
    server, _ids = mcp
    out = await _call(server, "search_stocks", q="gzmt")
    assert out["query"] == "gzmt"
    assert [item["ticker"] for item in out["items"]] == [TICKER]
    assert out["items"][0]["on_watchlist"] is True
    assert out["items"][0]["in_universe"] is False
    by_code = await _call(server, "search_stocks", q="0008", limit=1)
    assert [item["name"] for item in by_code["items"]] == ["五粮液"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_stock_snapshot", {"ticker": "600519"}),
        ("get_stock_snapshot", {"ticker": "600519.HK"}),
        ("get_bars", {"ticker": TICKER, "days": 19}),
        ("get_bars", {"ticker": TICKER, "days": 401}),
        ("list_candidates", {"status": "closed"}),
        ("list_candidates", {"limit": 201}),
        ("list_earnings_events", {"ticker": "AAPL"}),
        ("search_stocks", {"q": "", "limit": 10}),
        ("search_stocks", {"q": "x", "limit": 0}),
    ],
)
async def test_arguments_are_validated(mcp, name: str, arguments: dict) -> None:
    server, _ids = mcp
    with pytest.raises(ToolError):
        await server.call_tool(name, arguments)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_stock_snapshot", {"ticker": "000001.SZ"}),
        ("get_bars", {"ticker": "000001.SZ"}),
        ("get_candidate", {"candidate_id": 999}),
        ("get_rule", {"rule_id": 999}),
        ("list_earnings_events", {"ticker": "000001.SZ"}),
    ],
)
async def test_unknown_rows_are_tool_errors(mcp, name: str, arguments: dict) -> None:
    server, _ids = mcp
    with pytest.raises(ToolError, match="not found"):
        await server.call_tool(name, arguments)


async def test_tools_without_a_database_fail_cleanly() -> None:
    with pytest.raises(ToolError, match="without a database"):
        await build_server().call_tool("list_watchlist", {})


def test_cli_mcp_tools_prints_the_catalogue() -> None:
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["mcp", "tools"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    for name in EXPECTED_TOOLS:
        assert name in result.output


def test_cli_mcp_serve_rejects_a_missing_project_dir(tmp_path) -> None:
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["mcp", "serve", "--project-dir", str(tmp_path / "nope")])
    assert result.exit_code != 0
