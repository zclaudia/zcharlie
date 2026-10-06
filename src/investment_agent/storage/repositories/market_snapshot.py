"""Immutable, versioned daily market snapshots.

Rows written by the historical backfill (P1-A4) carry ``is_backfill=True``:
they are captured years after their trade date, so readers decide whether
their knowledge time comes from ``captured_at`` (strict) or from the trade
date itself (prices are never restated) - see :meth:`series_as_of`.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select

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
        pe_ttm: Decimal | None = None,
        pb_mrq: Decimal | None = None,
        ps_ttm: Decimal | None = None,
        turnover: Decimal | None = None,
        trade_status: int | None = None,
        is_st: bool | None = None,
        is_backfill: bool = False,
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
            pe_ttm=pe_ttm,
            pb_mrq=pb_mrq,
            ps_ttm=ps_ttm,
            turnover=turnover,
            trade_status=trade_status,
            is_st=is_st,
            adjust=adjust,
            provider=provider,
            captured_at=to_utc(captured_at),
            raw_ref=raw_ref,
            is_backfill=is_backfill,
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
        provider: str | None = None,
        include_backfill: bool = True,
    ) -> list[MarketSnapshot]:
        """Latest version per trade date in ``[start, end]`` known at ``as_of_captured``.

        ``provider`` restricts the series to one source (``None`` = any).

        Knowledge time follows :meth:`FinancialSnapshotRepository.latest_available`:
        live rows count from their ``captured_at``. Backfilled rows
        (``is_backfill``) are captured long after the fact, but a bar is final
        once its trading day closes (raw and backward-adjusted prices are
        never restated), so with ``include_backfill`` (default) they are
        eligible regardless of ``captured_at`` - historical queries see them by
        trade date. ``include_backfill=False`` applies the strict
        ``captured_at <= as_of_captured`` rule to every row.
        """

        cutoff = to_utc(as_of_captured)
        known = MarketSnapshot.captured_at <= cutoff
        knowledge = (
            or_(known, MarketSnapshot.is_backfill.is_(True)) if include_backfill else known
        )
        conditions = [
            MarketSnapshot.listing_id == listing_id,
            MarketSnapshot.adjust == adjust,
            MarketSnapshot.trade_date >= start,
            MarketSnapshot.trade_date <= end,
            knowledge,
        ]
        if provider is not None:
            conditions.append(MarketSnapshot.provider == provider)
        latest = (
            select(
                MarketSnapshot.trade_date.label("trade_date"),
                func.max(MarketSnapshot.version).label("version"),
            )
            .where(*conditions)
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
        self, listing_ids: Sequence[int], *, adjust: str, provider: str | None = None
    ) -> dict[int, date]:
        """Last stored trade date per listing for one series (any version).

        ``provider`` limits it to one source, so baostock and iFinD keep
        independent incremental cursors.
        """

        if not listing_ids:
            return {}
        conditions = [
            MarketSnapshot.listing_id.in_(list(listing_ids)),
            MarketSnapshot.adjust == adjust,
        ]
        if provider is not None:
            conditions.append(MarketSnapshot.provider == provider)
        stmt = (
            select(MarketSnapshot.listing_id, func.max(MarketSnapshot.trade_date))
            .where(*conditions)
            .group_by(MarketSnapshot.listing_id)
        )
        return {
            listing_id: trade_date
            for listing_id, trade_date in (await self.session.execute(stmt)).tuples().all()
            if trade_date is not None
        }

    async def earliest_trade_dates(
        self,
        listing_ids: Sequence[int],
        *,
        adjust: str,
        provider: str | None = None,
        backfill_only: bool = False,
    ) -> dict[int, date]:
        """First stored trade date per listing for one series (any version).

        The backfill fetches up to the day before this date, so its rows never
        overlap live captures. ``backfill_only`` considers backfilled rows only.
        """

        if not listing_ids:
            return {}
        conditions = [
            MarketSnapshot.listing_id.in_(list(listing_ids)),
            MarketSnapshot.adjust == adjust,
        ]
        if provider is not None:
            conditions.append(MarketSnapshot.provider == provider)
        if backfill_only:
            conditions.append(MarketSnapshot.is_backfill.is_(True))
        stmt = (
            select(MarketSnapshot.listing_id, func.min(MarketSnapshot.trade_date))
            .where(*conditions)
            .group_by(MarketSnapshot.listing_id)
        )
        return {
            listing_id: trade_date
            for listing_id, trade_date in (await self.session.execute(stmt)).tuples().all()
            if trade_date is not None
        }
