"""Immutable, versioned daily market snapshots."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from investment_agent.storage.models.pit_snapshot import MARKET_ADJUST_KINDS, MarketSnapshot
from investment_agent.storage.repositories.base import Repository
from investment_agent.storage.repositories.pit import to_utc


class MarketSnapshotRepository(Repository[MarketSnapshot]):
    model = MarketSnapshot

    async def insert(
        self,
        *,
        listing_id: int,
        trade_date: date,
        open: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
        provider: str,
        captured_at: datetime,
        adjust: str = "none",
        amount: Decimal | None = None,
        total_shares: Decimal | None = None,
        raw_ref: str | None = None,
    ) -> MarketSnapshot:
        """Insert a capture; a re-capture of the same key gets ``version + 1``."""

        if adjust not in MARKET_ADJUST_KINDS:
            raise ValueError(f"unsupported adjust {adjust!r}")
        current = (
            await self.session.execute(
                select(func.max(MarketSnapshot.version)).where(
                    MarketSnapshot.listing_id == listing_id,
                    MarketSnapshot.trade_date == trade_date,
                    MarketSnapshot.adjust == adjust,
                )
            )
        ).scalar_one()
        row = MarketSnapshot(
            listing_id=listing_id,
            trade_date=trade_date,
            open=open,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
            total_shares=total_shares,
            adjust=adjust,
            provider=provider,
            captured_at=to_utc(captured_at),
            raw_ref=raw_ref,
            version=(current or 0) + 1,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def series_as_of(
        self,
        listing_id: int,
        start: date,
        end: date,
        as_of_captured: datetime,
        *,
        adjust: str = "none",
    ) -> list[MarketSnapshot]:
        """Latest version per trade date in ``[start, end]`` known at ``as_of_captured``."""

        cutoff = to_utc(as_of_captured)
        latest = (
            select(
                MarketSnapshot.trade_date.label("trade_date"),
                func.max(MarketSnapshot.version).label("version"),
            )
            .where(
                MarketSnapshot.listing_id == listing_id,
                MarketSnapshot.adjust == adjust,
                MarketSnapshot.trade_date >= start,
                MarketSnapshot.trade_date <= end,
                MarketSnapshot.captured_at <= cutoff,
            )
            .group_by(MarketSnapshot.trade_date)
            .subquery()
        )
        stmt = (
            select(MarketSnapshot)
            .join(
                latest,
                (MarketSnapshot.trade_date == latest.c.trade_date)
                & (MarketSnapshot.version == latest.c.version),
            )
            .where(
                MarketSnapshot.listing_id == listing_id,
                MarketSnapshot.adjust == adjust,
            )
            .order_by(MarketSnapshot.trade_date)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_trade_dates(
        self, listing_ids: Sequence[int], *, adjust: str
    ) -> dict[int, date]:
        """Last stored trade date per listing for one series (any version)."""

        if not listing_ids:
            return {}
        stmt = (
            select(MarketSnapshot.listing_id, func.max(MarketSnapshot.trade_date))
            .where(
                MarketSnapshot.listing_id.in_(list(listing_ids)),
                MarketSnapshot.adjust == adjust,
            )
            .group_by(MarketSnapshot.listing_id)
        )
        return {
            listing_id: trade_date
            for listing_id, trade_date in (await self.session.execute(stmt)).tuples().all()
            if trade_date is not None
        }

    async def latest_total_shares(
        self, listing_ids: Sequence[int]
    ) -> dict[int, MarketSnapshot]:
        """Latest raw (``adjust="none"``) row carrying ``total_shares`` per listing."""

        if not listing_ids:
            return {}
        latest = (
            select(
                MarketSnapshot.listing_id.label("listing_id"),
                func.max(MarketSnapshot.trade_date).label("trade_date"),
            )
            .where(
                MarketSnapshot.listing_id.in_(list(listing_ids)),
                MarketSnapshot.adjust == "none",
                MarketSnapshot.total_shares.is_not(None),
            )
            .group_by(MarketSnapshot.listing_id)
            .subquery()
        )
        stmt = (
            select(MarketSnapshot)
            .join(
                latest,
                (MarketSnapshot.listing_id == latest.c.listing_id)
                & (MarketSnapshot.trade_date == latest.c.trade_date),
            )
            .where(
                MarketSnapshot.adjust == "none",
                MarketSnapshot.total_shares.is_not(None),
            )
            .order_by(MarketSnapshot.version)
        )
        rows: dict[int, MarketSnapshot] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            rows[row.listing_id] = row  # highest version wins
        return rows
