"""Data-health seed (moved from tests/integration/test_api_data_health.py)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.verification import IfindCallLedger
from tests.unit._health_support import add_market_run, add_overdue, seed_members

KEY = {"X-API-Key": "secret"}
FROZEN = "2026-10-15T04:00:00Z"  # Thu 12:00 Asia/Shanghai


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
