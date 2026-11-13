"""Point-in-time access to saved wmshare dividend metrics."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.pit_snapshot import WmshareDividendSnapshot
from investment_agent.storage.repositories.pit import end_of_business_day_utc


async def latest_as_of(
    session: AsyncSession, listing_id: int, as_of: date, *, history: bool = False
) -> WmshareDividendSnapshot | None:
    stmt = (
        select(WmshareDividendSnapshot)
        .where(
            WmshareDividendSnapshot.listing_id == listing_id,
            WmshareDividendSnapshot.captured_at < end_of_business_day_utc(as_of),
            (
                WmshareDividendSnapshot.history_available
                if history
                else WmshareDividendSnapshot.snapshot_available
            ).is_(True),
        )
        .order_by(WmshareDividendSnapshot.captured_at.desc(), WmshareDividendSnapshot.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
