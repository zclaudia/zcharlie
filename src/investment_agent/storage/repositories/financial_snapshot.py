"""Immutable, versioned financial-report snapshots with availability dates."""

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import func, or_, select

from investment_agent.storage.models.pit_snapshot import (
    FINANCIAL_AVAILABILITY,
    FINANCIAL_PERIOD_KINDS,
    FinancialSnapshot,
)
from investment_agent.storage.repositories.base import Repository
from investment_agent.storage.repositories.pit import end_of_business_day_utc, to_utc


class FinancialSnapshotRepository(Repository[FinancialSnapshot]):
    model = FinancialSnapshot

    async def insert(
        self,
        *,
        listing_id: int,
        report_period: date,
        period_kind: str,
        fields: dict[str, object],
        provider: str,
        captured_at: datetime,
        availability: str,
        available_from: date | None = None,
        raw_ref: str | None = None,
        is_backfill: bool = False,
    ) -> FinancialSnapshot:
        """Insert a capture; a re-capture of the same period gets ``version + 1``."""

        if period_kind not in FINANCIAL_PERIOD_KINDS:
            raise ValueError(f"unsupported period_kind {period_kind!r}")
        if availability not in FINANCIAL_AVAILABILITY:
            raise ValueError(f"unsupported availability {availability!r}")
        if availability != "unknown" and available_from is None:
            raise ValueError(f"availability {availability!r} requires available_from")
        current = (
            await self.session.execute(
                select(func.max(FinancialSnapshot.version)).where(
                    FinancialSnapshot.listing_id == listing_id,
                    FinancialSnapshot.report_period == report_period,
                )
            )
        ).scalar_one()
        row = FinancialSnapshot(
            listing_id=listing_id,
            report_period=report_period,
            period_kind=period_kind,
            available_from=available_from,
            availability=availability,
            fields=dict(fields),
            provider=provider,
            captured_at=to_utc(captured_at),
            raw_ref=raw_ref,
            is_backfill=is_backfill,
            version=(current or 0) + 1,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def latest_available(
        self,
        listing_id: int,
        as_of: date,
        *,
        include_backfill: bool = True,
        provider: str | None = None,
    ) -> FinancialSnapshot | None:
        """Most recent report period usable on ``as_of`` (Asia/Shanghai date).

        Eligible rows were public by ``as_of`` (``available_from <= as_of``,
        ``unknown`` availability excluded) and known by the end of ``as_of``
        (``captured_at``). Backfilled rows are captured long after the fact;
        their knowledge time is reconstructed from ``available_from``, so they
        are eligible regardless of ``captured_at`` unless ``include_backfill``
        is false. Among eligible rows the latest report period wins, then the
        highest version. ``provider`` restricts the choice to one source.
        """

        cutoff = end_of_business_day_utc(as_of)
        known = FinancialSnapshot.captured_at < cutoff
        knowledge = or_(known, FinancialSnapshot.is_backfill.is_(True)) if include_backfill else known
        conditions = [
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.availability != "unknown",
            FinancialSnapshot.available_from.is_not(None),
            FinancialSnapshot.available_from <= as_of,
            knowledge,
        ]
        if provider is not None:
            conditions.append(FinancialSnapshot.provider == provider)
        stmt = (
            select(FinancialSnapshot)
            .where(*conditions)
            .order_by(
                FinancialSnapshot.report_period.desc(),
                FinancialSnapshot.version.desc(),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def total_shares_as_of(
        self, listing_id: int, as_of: date, *, provider: str = "baostock"
    ) -> tuple[float, FinancialSnapshot] | None:
        """Total shares usable on ``as_of`` and the snapshot they come from.

        The factor-time source of total shares for market cap (decision D6):
        ``fields["total_shares"]`` of the latest report available on ``as_of``
        (effective from its ``available_from`` = pubDate).
        """

        snapshot = await self.latest_available(listing_id, as_of, provider=provider)
        if snapshot is None:
            return None
        value = snapshot.fields.get("total_shares")
        if not isinstance(value, int | float) or value <= 0:
            return None
        return float(value), snapshot

    async def listings_with_period(
        self, listing_ids: Sequence[int], report_period: date, *, provider: str | None = None
    ) -> set[int]:
        """Listings that already have any capture of ``report_period``."""

        if not listing_ids:
            return set()
        conditions = [
            FinancialSnapshot.listing_id.in_(list(listing_ids)),
            FinancialSnapshot.report_period == report_period,
        ]
        if provider is not None:
            conditions.append(FinancialSnapshot.provider == provider)
        stmt = select(FinancialSnapshot.listing_id).where(*conditions).distinct()
        return set((await self.session.execute(stmt)).scalars().all())
