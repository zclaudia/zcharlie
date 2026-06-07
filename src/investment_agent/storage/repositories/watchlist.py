"""Watchlist repository."""

from datetime import UTC, datetime

from sqlalchemy import select

from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.base import Repository


class WatchlistRepository(Repository[WatchlistItem]):
    model = WatchlistItem

    async def list_all(self) -> list[WatchlistItem]:
        stmt = select(WatchlistItem).order_by(WatchlistItem.added_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, listing_id: int, note: str | None = None) -> WatchlistItem:
        existing_stmt = select(WatchlistItem).where(
            WatchlistItem.listing_id == listing_id,
        )
        existing = (await self.session.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            if note is not None:
                existing.note = note
            await self.session.flush()
            return existing
        item = WatchlistItem(
            listing_id=listing_id,
            added_at=datetime.now(UTC),
            note=note,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def remove(self, listing_id: int) -> bool:
        stmt = select(WatchlistItem).where(WatchlistItem.listing_id == listing_id)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return False
        await self.session.delete(existing)
        await self.session.flush()
        return True
