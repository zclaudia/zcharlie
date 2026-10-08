"""``GET /health/data`` (P1-E2): auth, shape, read-only, dashboard merge, latency."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from freezegun import freeze_time
from sqlalchemy import func, insert, select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.verification import IfindCallLedger
from investment_agent.web.session import SESSION_COOKIE_NAME
from tests.unit._health_support import (
    UNIVERSE,
    add_financial,
    add_industry,
    add_market_run,
    add_overdue,
    add_run,
    completeness,
    seed_members,
)

KEY = {"X-API-Key": "secret"}
FROZEN = "2026-10-15T04:00:00Z"  # Thu 12:00 Asia/Shanghai

TOP_LEVEL = {
    "generated_at",
    "as_of",
    "threshold",
    "target_universe",
    "warnings",
    "accumulation",
    "freshness",
    "universes",
    "industry",
    "collections",
    "backfill",
    "verification",
    "overdue",
    "providers",
}


async def _seed_basic() -> None:
    async with session_scope() as session:
        ids = await seed_members(session, 3)
        await add_market_run(session, date(2026, 10, 9))
        await add_market_run(session, date(2026, 10, 12))
        await add_market_run(
            session,
            date(2026, 10, 14),
            status="failed",
            ratio_collected=(3, 0),
            finished_at=datetime(2026, 10, 14, 11, 0, tzinfo=UTC),
        )
        await add_overdue(session, ids[0], date(2026, 6, 30))
        session.add(
            IfindCallLedger(
                at=datetime(2026, 10, 15, 3, 0, tzinfo=UTC),
                endpoint="/stock/history",
                ticker_count=3,
                outcome="ok",
                source="verification",
            )
        )


def test_data_health_requires_auth(api_client) -> None:
    assert api_client.get("/health/data").status_code == 401
    assert api_client.get("/health/data", headers={"X-API-Key": "nope"}).status_code == 401
    assert api_client.get("/healthz").status_code == 200, "liveness stays public"


def test_data_health_accepts_the_web_session(api_client) -> None:
    login = api_client.post(
        "/app/login", data={"api_key": "secret", "next": "/app"}, follow_redirects=False
    )
    assert login.status_code == 303
    assert SESSION_COOKIE_NAME in api_client.cookies
    assert api_client.get("/health/data").status_code == 200


def test_data_health_shape_on_an_empty_database(api_client) -> None:
    with freeze_time(FROZEN, tick=True):
        body = api_client.get("/health/data", headers=KEY).json()
    assert set(body) == TOP_LEVEL
    assert body["target_universe"] == "CSI300"
    assert body["as_of"] == "2026-10-15"
    assert body["accumulation"]["target_weeks"] == 8
    assert body["accumulation"]["streak_weeks"] == 0
    assert body["collections"] == []
    assert {w["level"] for w in body["warnings"]} <= {"info"}
    assert body["verification"]["budget"]["cap"] == 150


async def test_data_health_reports_seeded_state_and_stays_read_only(api_client) -> None:
    await _seed_basic()

    async def counts() -> tuple[int, int]:
        async with session_scope() as session:
            runs = (await session.execute(select(func.count()).select_from(PipelineRun))).scalar_one()
            ledger = (
                await session.execute(select(func.count()).select_from(IfindCallLedger))
            ).scalar_one()
        return int(runs), int(ledger)

    before = await counts()
    with freeze_time(FROZEN, tick=True):
        response = api_client.get("/health/data", headers=KEY)
    assert response.status_code == 200
    assert await counts() == before, "GET /health/data must not write"
    body = response.json()
    assert body["accumulation"]["streak_weeks"] == 2  # W41 (Oct 9) and W42 (Oct 12)
    market = body["collections"][0]
    assert market["key"] == "market_snapshot:baostock:CSI300"
    assert market["last_run"]["status"] == "failed"
    assert [point["status"] for point in market["trend"]] == ["success", "success", "failed"]
    assert body["overdue"]["open"] == 1
    assert body["overdue"]["items"][0]["ticker"] == "600000.SH"
    assert body["verification"]["budget"]["used"] == 1
    codes = {w["code"]: w["level"] for w in body["warnings"]}
    assert codes["collection_failed"] == "error"
    assert codes["report_overdue"] == "warn"
    text = response.text
    for secret in ("secret", "proxy", "api_key"):
        assert secret not in text, secret

    assert api_client.post("/health/data", headers=KEY).status_code == 405


async def _seed_large(listings: int = 300, days: int = 30) -> None:
    trade_days: list[date] = []
    current = date(2026, 8, 31)
    while len(trade_days) < days:
        if current.weekday() < 5 and current != date(2026, 9, 25):
            trade_days.append(current)
        current += timedelta(days=1)
    captured = datetime(2026, 10, 1, tzinfo=UTC)
    async with session_scope() as session:
        ids = await seed_members(session, listings)
        rows = [
            {
                "listing_id": listing_id,
                "trade_date": day,
                "open": Decimal("10"),
                "high": Decimal("11"),
                "low": Decimal("9"),
                "close": Decimal("10.5"),
                "volume": Decimal("1000"),
                "adjust": adjust,
                "provider": "baostock",
                "captured_at": captured,
                "is_backfill": False,
                "version": 1,
            }
            for listing_id in ids
            for day in trade_days
            for adjust in ("none", "backward")
        ]
        await session.execute(insert(MarketSnapshot), rows)
        for day in trade_days:
            missing = {"600001.SH": ("no_data", "suspended")}
            await add_market_run(
                session, day, ratio_collected=(listings, listings - 1), missing=missing
            )
        await add_financial(session, ids, date(2026, 6, 30))
        for listing_id in ids[:200]:
            await add_industry(session, listing_id, "sw2021")
        await add_run(
            session,
            "universe_sync",
            {"universe_code": UNIVERSE, "as_of": "2026-10-12"},
            metadata={"members": listings, "added": [], "removed": []},
        )
        for chunk in range(15):
            symbols = [f"{600000 + chunk * 20 + index:06d}.SH" for index in range(20)]
            await add_run(
                session,
                "snapshot_backfill",
                {"universe_code": UNIVERSE, "as_of": "2026-10-07", "part": "prices", "symbols": symbols, "chunk": chunk},
                metadata={"collected_symbols": symbols, "completeness": completeness(20, 20)},
                key=f"snapshot_backfill:CSI300:2026-10-07:prices:p5a6q4:c{chunk:03d}",
            )


async def test_data_health_latency_on_a_moderate_database(api_client) -> None:
    await _seed_large()
    with freeze_time(FROZEN, tick=True):
        assert api_client.get("/health/data", headers=KEY).status_code == 200  # warm-up
        started = time.perf_counter()
        response = api_client.get("/health/data", headers=KEY)
        elapsed = time.perf_counter() - started
    assert response.status_code == 200
    body = response.json()
    assert body["freshness"]["market"]["members"] == 300
    assert body["freshness"]["market"]["members_at_latest"] == 300
    assert body["accumulation"]["weeks_with_snapshots"] >= 6
    assert body["backfill"]["parts"][2]["done"] == 300
    # Generous bound for slow CI; measured ~25 ms on the real-size database copy.
    assert elapsed < 1.0, f"/health/data took {elapsed:.3f}s"
