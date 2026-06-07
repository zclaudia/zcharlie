"""Price repository — daily price upsert + lookups."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from investment_agent.storage.models.market_data import DailyPrice
from investment_agent.storage.repositories.base import Repository


class PriceRepository(Repository[DailyPrice]):
    model = DailyPrice

    async def upsert_daily_price(
        self,
        *,
        listing_id: int,
        date: date,
        open: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: int,
        adjusted_close: Decimal,
    ) -> DailyPrice:
        existing = await self.session.get(DailyPrice, (listing_id, date))
        if existing is None:
            existing = DailyPrice(
                listing_id=listing_id,
                date=date,
                open=open,
                high=high,
                low=low,
                close=close,
                volume=volume,
                adjusted_close=adjusted_close,
            )
            self.session.add(existing)
        else:
            existing.open = open
            existing.high = high
            existing.low = low
            existing.close = close
            existing.volume = volume
            existing.adjusted_close = adjusted_close
        await self.session.flush()
        return existing

    async def latest_close(self, listing_id: int) -> Decimal | None:
        stmt = (
            select(DailyPrice.close)
            .where(DailyPrice.listing_id == listing_id)
            .order_by(DailyPrice.date.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def range(
        self, listing_id: int, start: date, end: date
    ) -> list[DailyPrice]:
        stmt = (
            select(DailyPrice)
            .where(
                DailyPrice.listing_id == listing_id,
                DailyPrice.date >= start,
                DailyPrice.date <= end,
            )
            .order_by(DailyPrice.date.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
