"""Versioned dividend events and their collection coverage (migration 017).

Knowledge rules (as in the other PIT repositories): a live capture is known
from ``captured_at``; backfilled rows (``is_backfill``) are captured long
after the fact and are known from their ``available_from`` (events) or
unconditionally (coverage), since the provider history is not restated.

The latest coverage check known on a date comes from the append-only
``dividend_coverage_check`` history (migration 020); a backfilled year whose
checks all happened later reports its first (reconstructed) check, which
never changes, so results for a past ``as_of`` are reproducible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select

from investment_agent.storage.models.pit_snapshot import (
    DividendCoverage,
    DividendCoverageCheck,
    DividendEventSnapshot,
)
from investment_agent.storage.repositories.base import Repository
from investment_agent.storage.repositories.pit import end_of_business_day_utc, to_utc

# Payload columns compared to decide whether a re-capture is a new version.
EVENT_PAYLOAD_COLUMNS: tuple[str, ...] = (
    "record_date",
    "pay_date",
    "announce_date",
    "implement_date",
    "available_from",
    "cash_per_share_pre_tax",
    "stock_per_share",
    "reserve_per_share",
)


def _stored_utc(value: datetime) -> datetime:
    """Stored timestamps are UTC; SQLite returns them naive."""

    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _same(stored: object, new: object) -> bool:
    if isinstance(stored, Decimal) or isinstance(new, Decimal):
        if stored is None or new is None:
            return stored is new
        return Decimal(str(stored)) == Decimal(str(new))
    return stored == new


class DividendRepository(Repository[DividendEventSnapshot]):
    model = DividendEventSnapshot

    async def latest_events(
        self, listing_id: int, ex_dates: Sequence[date] | None = None
    ) -> dict[date, DividendEventSnapshot]:
        """Latest stored version per ex-date (all providers)."""

        conditions = [DividendEventSnapshot.listing_id == listing_id]
        if ex_dates is not None:
            if not ex_dates:
                return {}
            conditions.append(DividendEventSnapshot.ex_date.in_(list(ex_dates)))
        stmt = (
            select(DividendEventSnapshot)
            .where(*conditions)
            .order_by(DividendEventSnapshot.ex_date, DividendEventSnapshot.version)
        )
        latest: dict[date, DividendEventSnapshot] = {}
        for row in (await self.session.execute(stmt)).scalars():
            latest[row.ex_date] = row
        return latest

    async def upsert_event(
        self,
        *,
        listing_id: int,
        ex_date: date,
        available_from: date,
        operate_year: int,
        provider: str,
        captured_at: datetime,
        payload: Mapping[str, object],
        details: Mapping[str, object],
        raw_ref: str | None = None,
        is_backfill: bool = False,
        latest: DividendEventSnapshot | None = None,
    ) -> DividendEventSnapshot | None:
        """Insert a capture unless it equals the latest version; returns the new row.

        ``payload`` holds :data:`EVENT_PAYLOAD_COLUMNS` (``available_from`` is
        passed separately). ``latest`` may be given to save a query.
        """

        values = {**payload, "available_from": available_from}
        unknown = set(values) - set(EVENT_PAYLOAD_COLUMNS)
        if unknown:
            raise ValueError(f"unknown dividend payload columns {sorted(unknown)}")
        if latest is None:
            latest = (await self.latest_events(listing_id, [ex_date])).get(ex_date)
        if latest is not None and all(
            _same(getattr(latest, name), values.get(name)) for name in EVENT_PAYLOAD_COLUMNS
        ):
            return None
        row = DividendEventSnapshot(
            listing_id=listing_id,
            ex_date=ex_date,
            operate_year=operate_year,
            details=dict(details),
            provider=provider,
            captured_at=to_utc(captured_at),
            raw_ref=raw_ref,
            is_backfill=is_backfill,
            version=(latest.version if latest is not None else 0) + 1,
            **values,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_coverage(
        self,
        *,
        listing_id: int,
        provider: str,
        year: int,
        checked_at: datetime,
        event_count: int,
        is_backfill: bool = False,
    ) -> DividendCoverage:
        """Mark ``year`` as collected (first check kept, latest check updated)."""

        stmt = select(DividendCoverage).where(
            DividendCoverage.listing_id == listing_id,
            DividendCoverage.provider == provider,
            DividendCoverage.year == year,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        checked = to_utc(checked_at)
        if row is None:
            row = DividendCoverage(
                listing_id=listing_id,
                provider=provider,
                year=year,
                first_checked_at=checked,
                last_checked_at=checked,
                event_count=event_count,
                is_backfill=is_backfill,
            )
            self.session.add(row)
        else:
            row.last_checked_at = checked
            row.event_count = event_count
        self.session.add(
            DividendCoverageCheck(
                listing_id=listing_id,
                provider=provider,
                year=year,
                checked_at=checked,
                event_count=event_count,
                is_backfill=is_backfill,
            )
        )
        await self.session.flush()
        return row

    async def covered_years(
        self, listing_ids: Sequence[int], years: Sequence[int], *, provider: str
    ) -> dict[int, dict[int, DividendCoverage]]:
        """``{listing_id: {year: coverage}}`` for the given years (any knowledge time)."""

        if not listing_ids or not years:
            return {}
        stmt = select(DividendCoverage).where(
            DividendCoverage.listing_id.in_(list(listing_ids)),
            DividendCoverage.year.in_(list(years)),
            DividendCoverage.provider == provider,
        )
        result: dict[int, dict[int, DividendCoverage]] = {}
        for row in (await self.session.execute(stmt)).scalars():
            result.setdefault(row.listing_id, {})[row.year] = row
        return result

    async def coverage_as_of(
        self, listing_id: int, years: Sequence[int], as_of: date, *, provider: str
    ) -> dict[int, DividendCoverage]:
        """Coverage rows of ``years`` known by the end of ``as_of``."""

        cutoff = end_of_business_day_utc(as_of)
        stmt = select(DividendCoverage).where(
            DividendCoverage.listing_id == listing_id,
            DividendCoverage.provider == provider,
            DividendCoverage.year.in_(list(years)),
            or_(DividendCoverage.first_checked_at < cutoff, DividendCoverage.is_backfill.is_(True)),
        )
        return {row.year: row for row in (await self.session.execute(stmt)).scalars()}

    async def last_checks_as_of(
        self, listing_id: int, years: Sequence[int], as_of: date, *, provider: str
    ) -> dict[int, datetime]:
        """Latest coverage check of each year known by the end of ``as_of``.

        Years covered on ``as_of`` without a check by then (backfilled years)
        report their first check (``first_checked_at``, never updated).
        """

        coverage = await self.coverage_as_of(listing_id, years, as_of, provider=provider)
        if not coverage:
            return {}
        cutoff = end_of_business_day_utc(as_of)
        stmt = (
            select(DividendCoverageCheck.year, func.max(DividendCoverageCheck.checked_at))
            .where(
                DividendCoverageCheck.listing_id == listing_id,
                DividendCoverageCheck.provider == provider,
                DividendCoverageCheck.year.in_(list(coverage)),
                DividendCoverageCheck.checked_at < cutoff,
            )
            .group_by(DividendCoverageCheck.year)
        )
        known = {year: _stored_utc(at) for year, at in (await self.session.execute(stmt)).all()}
        return {
            year: known.get(year) or _stored_utc(row.first_checked_at)
            for year, row in sorted(coverage.items())
        }

    async def events_as_of(
        self,
        listing_id: int,
        start: date,
        as_of: date,
        *,
        provider: str,
    ) -> list[DividendEventSnapshot]:
        """Events with ``start <= ex_date <= as_of`` public and known on ``as_of``.

        Public: ``available_from <= as_of``. Known: captured by the end of
        ``as_of`` or backfilled. Latest eligible version per ex-date.
        """

        cutoff = end_of_business_day_utc(as_of)
        stmt = (
            select(DividendEventSnapshot)
            .where(
                DividendEventSnapshot.listing_id == listing_id,
                DividendEventSnapshot.provider == provider,
                DividendEventSnapshot.ex_date >= start,
                DividendEventSnapshot.ex_date <= as_of,
                DividendEventSnapshot.available_from <= as_of,
                or_(
                    DividendEventSnapshot.captured_at < cutoff,
                    DividendEventSnapshot.is_backfill.is_(True),
                ),
            )
            .order_by(DividendEventSnapshot.ex_date, DividendEventSnapshot.version)
        )
        latest: dict[date, DividendEventSnapshot] = {}
        for row in (await self.session.execute(stmt)).scalars():
            latest[row.ex_date] = row
        return [latest[key] for key in sorted(latest)]
