"""Earnings events (mrd-intake C1): forecasts, disclosure schedule, segments."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.earnings_events import (
    BusinessSegmentRecord,
    DisclosureScheduleRecord,
    EarningsForecastRecord,
)

SEGMENT_KIND_PRECEDENCE: tuple[str, ...] = ("product", "industry", "region")
"""Which 主营构成 breakdown the readers show first (one kind per period)."""


_AMOUNT = Decimal("0.01")
_FRACTION = Decimal("0.000001")


def _amount(value: float | Decimal | None) -> Decimal | None:
    """CNY amounts at the column scale (Numeric(20, 2)); ``str`` keeps the shortest repr."""

    return None if value is None else Decimal(str(value)).quantize(_AMOUNT)


def _fraction(value: float | Decimal | None) -> Decimal | None:
    """Fractions at the column scale (Numeric(18, 6))."""

    return None if value is None else Decimal(str(value)).quantize(_FRACTION)


@dataclass(frozen=True)
class SegmentRow:
    """Content of one ``business_segment`` row (the dedup key of a capture)."""

    kind: str
    name: str
    revenue: Decimal | None
    profit: Decimal | None
    margin: Decimal | None
    share: Decimal | None


@dataclass(frozen=True)
class LatestSegments:
    report_period: date
    captured_at: datetime
    kind: str
    rows: tuple[BusinessSegmentRecord, ...]
    """Rows of the chosen ``kind``, revenue descending."""


class EarningsEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- forecasts ---------------------------------------------------------- #

    async def forecast_keys(
        self, listing_ids: Sequence[int], report_period: date
    ) -> set[tuple[int, date]]:
        """``(listing_id, notice_date)`` of every stored forecast of the period."""

        if not listing_ids:
            return set()
        stmt = select(EarningsForecastRecord.listing_id, EarningsForecastRecord.notice_date).where(
            EarningsForecastRecord.report_period == report_period,
            EarningsForecastRecord.listing_id.in_(list(set(listing_ids))),
        )
        return {(int(listing_id), notice) for listing_id, notice in await self.session.execute(stmt)}

    async def insert_forecast(
        self,
        *,
        listing_id: int,
        report_period: date,
        notice_date: date,
        forecast_type: str,
        indicator: str,
        profit_low: float | None,
        profit_high: float | None,
        yoy_low: float | None,
        yoy_high: float | None,
        content: str,
        provider: str,
        captured_at: datetime,
        raw_ref: str | None,
    ) -> EarningsForecastRecord:
        row = EarningsForecastRecord(
            listing_id=listing_id,
            report_period=report_period,
            notice_date=notice_date,
            forecast_type=forecast_type,
            indicator=indicator,
            profit_low=_amount(profit_low),
            profit_high=_amount(profit_high),
            yoy_low=_fraction(yoy_low),
            yoy_high=_fraction(yoy_high),
            content=content,
            provider=provider,
            captured_at=captured_at,
            raw_ref=raw_ref,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def forecasts_for(
        self, listing_id: int, *, known_by: datetime | None = None
    ) -> list[EarningsForecastRecord]:
        """Forecasts of one listing, newest period / notice first (PIT by ``known_by``)."""

        stmt = select(EarningsForecastRecord).where(EarningsForecastRecord.listing_id == listing_id)
        if known_by is not None:
            stmt = stmt.where(EarningsForecastRecord.captured_at <= known_by)
        stmt = stmt.order_by(
            EarningsForecastRecord.report_period.desc(), EarningsForecastRecord.notice_date.desc()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def recent_forecasts(
        self, *, listing_id: int | None = None, limit: int
    ) -> list[EarningsForecastRecord]:
        """Newest forecasts (by notice date, then capture) across listings or of one."""

        stmt = select(EarningsForecastRecord)
        if listing_id is not None:
            stmt = stmt.where(EarningsForecastRecord.listing_id == listing_id)
        stmt = stmt.order_by(
            EarningsForecastRecord.notice_date.desc(),
            EarningsForecastRecord.captured_at.desc(),
            EarningsForecastRecord.id.desc(),
        ).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    # -- disclosure schedule ------------------------------------------------ #

    async def recent_schedules(
        self, *, listing_id: int | None = None, limit: int
    ) -> list[DisclosureScheduleRecord]:
        """Newest schedule captures (by capture, then scheduled date) across
        listings or of one; every capture of a listing / period is a row."""

        stmt = select(DisclosureScheduleRecord)
        if listing_id is not None:
            stmt = stmt.where(DisclosureScheduleRecord.listing_id == listing_id)
        stmt = stmt.order_by(
            DisclosureScheduleRecord.captured_at.desc(),
            DisclosureScheduleRecord.scheduled_date.desc(),
            DisclosureScheduleRecord.id.desc(),
        ).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_schedules(
        self, listing_ids: Sequence[int], report_period: date
    ) -> dict[int, DisclosureScheduleRecord]:
        """Latest capture per listing of the period."""

        if not listing_ids:
            return {}
        stmt = (
            select(DisclosureScheduleRecord)
            .where(
                DisclosureScheduleRecord.report_period == report_period,
                DisclosureScheduleRecord.listing_id.in_(list(set(listing_ids))),
            )
            .order_by(DisclosureScheduleRecord.listing_id, DisclosureScheduleRecord.captured_at)
        )
        latest: dict[int, DisclosureScheduleRecord] = {}
        for row in (await self.session.execute(stmt)).scalars():
            latest[row.listing_id] = row  # ascending captured_at: the last one wins
        return latest

    async def insert_schedule(
        self,
        *,
        listing_id: int,
        report_period: date,
        first_date: date,
        scheduled_date: date,
        actual_date: date | None,
        provider: str,
        captured_at: datetime,
        raw_ref: str | None,
    ) -> DisclosureScheduleRecord:
        row = DisclosureScheduleRecord(
            listing_id=listing_id,
            report_period=report_period,
            first_date=first_date,
            scheduled_date=scheduled_date,
            actual_date=actual_date,
            provider=provider,
            captured_at=captured_at,
            raw_ref=raw_ref,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    # -- business segments -------------------------------------------------- #

    async def latest_segment_rows(
        self, listing_id: int, report_period: date
    ) -> list[BusinessSegmentRecord]:
        """Rows of the latest capture of (listing, period); empty when never captured."""

        latest_at = (
            select(func.max(BusinessSegmentRecord.captured_at))
            .where(
                BusinessSegmentRecord.listing_id == listing_id,
                BusinessSegmentRecord.report_period == report_period,
            )
            .scalar_subquery()
        )
        stmt = (
            select(BusinessSegmentRecord)
            .where(
                BusinessSegmentRecord.listing_id == listing_id,
                BusinessSegmentRecord.report_period == report_period,
                BusinessSegmentRecord.captured_at == latest_at,
            )
            .order_by(BusinessSegmentRecord.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def insert_segments(
        self,
        *,
        listing_id: int,
        report_period: date,
        rows: Sequence[SegmentRow],
        provider: str,
        captured_at: datetime,
        raw_ref: str | None,
    ) -> int:
        for item in rows:
            self.session.add(
                BusinessSegmentRecord(
                    listing_id=listing_id,
                    report_period=report_period,
                    kind=item.kind,
                    name=item.name,
                    revenue=item.revenue,
                    profit=item.profit,
                    margin=item.margin,
                    share=item.share,
                    provider=provider,
                    captured_at=captured_at,
                    raw_ref=raw_ref,
                )
            )
        await self.session.flush()
        return len(rows)

    async def latest_segments(
        self, listing_id: int, *, as_of: date | None = None, known_by: datetime | None = None
    ) -> LatestSegments | None:
        """The newest report period's latest capture, one ``kind`` (product first).

        ``as_of`` limits the report period; ``known_by`` the capture instant
        (point in time for readers that must not see later captures).
        """

        stmt = select(
            BusinessSegmentRecord.report_period,
            func.max(BusinessSegmentRecord.captured_at),
        ).where(BusinessSegmentRecord.listing_id == listing_id)
        if as_of is not None:
            stmt = stmt.where(BusinessSegmentRecord.report_period <= as_of)
        if known_by is not None:
            stmt = stmt.where(BusinessSegmentRecord.captured_at <= known_by)
        stmt = (
            stmt.group_by(BusinessSegmentRecord.report_period)
            .order_by(BusinessSegmentRecord.report_period.desc())
            .limit(1)
        )
        head = (await self.session.execute(stmt)).first()
        if head is None:
            return None
        report_period, captured_at = head
        rows = list(
            (
                await self.session.execute(
                    select(BusinessSegmentRecord)
                    .where(
                        BusinessSegmentRecord.listing_id == listing_id,
                        BusinessSegmentRecord.report_period == report_period,
                        BusinessSegmentRecord.captured_at == captured_at,
                    )
                    .order_by(BusinessSegmentRecord.id)
                )
            ).scalars()
        )
        kinds = {row.kind for row in rows}
        kind = next((name for name in SEGMENT_KIND_PRECEDENCE if name in kinds), None)
        if kind is None:
            return None
        chosen = sorted(
            (row for row in rows if row.kind == kind),
            key=lambda row: (row.revenue is None, -(row.revenue or 0)),
        )
        return LatestSegments(
            report_period=report_period, captured_at=captured_at, kind=kind, rows=tuple(chosen)
        )


def segment_row(
    *,
    kind: str,
    name: str,
    revenue: float | None,
    profit: float | None,
    margin: float | None,
    share: float | None,
) -> SegmentRow:
    return SegmentRow(
        kind=kind,
        name=name,
        revenue=_amount(revenue),
        profit=_amount(profit),
        margin=_fraction(margin),
        share=_fraction(share),
    )


def stored_segment_row(record: BusinessSegmentRecord) -> SegmentRow:
    """A stored row at the same scale as :func:`segment_row` (for the unchanged check)."""

    return SegmentRow(
        kind=record.kind,
        name=record.name,
        revenue=_amount(record.revenue),
        profit=_amount(record.profit),
        margin=_fraction(record.margin),
        share=_fraction(record.share),
    )


__all__ = [
    "SEGMENT_KIND_PRECEDENCE",
    "EarningsEventRepository",
    "LatestSegments",
    "SegmentRow",
    "segment_row",
    "stored_segment_row",
]
