"""Overdue periodic reports (P1-A6): detection, resolution, point-in-time reads."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import or_, select

from investment_agent.storage.models.pit_snapshot import ReportOverdue
from investment_agent.storage.repositories.base import Repository


class ReportOverdueRepository(Repository[ReportOverdue]):
    model = ReportOverdue

    async def record(
        self,
        *,
        listing_id: int,
        report_period: date,
        deadline: date,
        detected_on: date,
        provider: str,
        run_id: int | None = None,
    ) -> bool:
        """Record an overdue report once per (listing, period); ``True`` if new.

        A period already recorded (open or resolved) is left untouched, so
        repeated scans never re-notify and resolved history is never reopened.
        """

        existing = await self._get(listing_id, report_period)
        if existing is not None:
            return False
        self.session.add(
            ReportOverdue(
                listing_id=listing_id,
                report_period=report_period,
                deadline=deadline,
                detected_on=detected_on,
                provider=provider,
                detected_run_id=run_id,
            )
        )
        await self.session.flush()
        return True

    async def resolve(
        self,
        *,
        listing_id: int,
        report_period: date,
        resolved_on: date,
        run_id: int | None = None,
    ) -> bool:
        """Close an open overdue record with the late report's pubDate; ``True`` if closed."""

        existing = await self._get(listing_id, report_period)
        if existing is None or existing.resolved_on is not None:
            return False
        existing.resolved_on = resolved_on
        existing.resolved_run_id = run_id
        await self.session.flush()
        return True

    async def for_period(
        self, report_period: date, listing_ids: Sequence[int] | None = None
    ) -> list[ReportOverdue]:
        stmt = select(ReportOverdue).where(ReportOverdue.report_period == report_period)
        if listing_ids is not None:
            stmt = stmt.where(ReportOverdue.listing_id.in_(list(listing_ids)))
        return list((await self.session.execute(stmt.order_by(ReportOverdue.listing_id))).scalars())

    async def open_as_of(self, listing_id: int, as_of: date) -> list[ReportOverdue]:
        """Periods known overdue on ``as_of`` and not yet resolved by then."""

        stmt = (
            select(ReportOverdue)
            .where(
                ReportOverdue.listing_id == listing_id,
                ReportOverdue.detected_on <= as_of,
                or_(ReportOverdue.resolved_on.is_(None), ReportOverdue.resolved_on > as_of),
            )
            .order_by(ReportOverdue.report_period)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def _get(self, listing_id: int, report_period: date) -> ReportOverdue | None:
        stmt = select(ReportOverdue).where(
            ReportOverdue.listing_id == listing_id,
            ReportOverdue.report_period == report_period,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
