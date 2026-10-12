"""Evidence-service seed helpers (moved from tests/test_evidence_services.py)."""

from datetime import UTC, date, datetime, timedelta

from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.listing import ListingRepository

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PERIOD = date(2025, 12, 31)


async def seed(session):
    repo = ListingRepository(session)
    first = await repo.ensure_a_share_listing("600519.SH", company_name="贵州茅台")
    second = await repo.ensure_a_share_listing("000001.SZ", company_name="平安银行")
    row = await FinancialSnapshotRepository(session).insert(
        listing_id=first.id,
        report_period=PERIOD,
        period_kind="annual",
        fields={"net_profit": 123456789, "roe": 0.20},
        provider="baostock",
        captured_at=AS_OF - timedelta(days=20),
        availability="actual",
        available_from=date(2026, 4, 17),
    )
    return first, second, row
