"""Point-in-time industry classification intervals.

One row per ``(listing, system)`` interval ``[valid_from, valid_to)``. An
industry snapshot is applied entry by entry: an unchanged classification is a
no-op, a changed one closes the open interval and opens a new one at the
entry's effective date, and an empty classification closes the open interval.
Snapshots may be partial (one symbol); listings absent from a snapshot are
left untouched.

The historical backfill (P1-A4) rebuilds a window from whole-market snapshots
with :meth:`IndustryRepository.rebuild_history` instead (see :mod:`.intervals`).
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from typing import Protocol

import structlog
from sqlalchemy import or_, select

from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.repositories.base import Repository
from investment_agent.storage.repositories.intervals import (
    Span,
    boundaries,
    merge_timeline,
    plan_rows,
    scalar_spans,
    state_at,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pit import OutOfOrderSnapshotError, to_utc

log = structlog.get_logger(__name__)


class IndustryEntryLike(Protocol):
    @property
    def ticker(self) -> str: ...

    @property
    def company_name(self) -> str: ...

    @property
    def industry_code(self) -> str | None: ...

    @property
    def industry_name(self) -> str | None: ...

    @property
    def effective_date(self) -> date: ...


class IndustrySnapshotLike(Protocol):
    @property
    def entries(self) -> Sequence[IndustryEntryLike]: ...

    @property
    def system(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def captured_at(self) -> datetime: ...


@dataclass(frozen=True)
class IndustryChange:
    ticker: str
    listing_id: int
    effective_date: date
    old_code: str | None
    old_name: str | None
    new_code: str | None
    new_name: str | None


class DatedIndustrySnapshotLike(IndustrySnapshotLike, Protocol):
    @property
    def effective_date(self) -> date | None: ...


@dataclass(frozen=True)
class IndustryRebuild:
    """Outcome of :meth:`IndustryRepository.rebuild_history`."""

    listings: int
    reclassified: tuple[str, ...]
    """Tickers whose classification changed inside the window."""
    unclassified: tuple[str, ...]
    """Requested tickers that no snapshot classifies."""
    kept: int
    reused: int
    deleted: int
    inserted: int


IndustryValue = tuple[str | None, str]


class IndustryRepository(Repository[IndustryClassification]):
    model = IndustryClassification

    async def rebuild_history(
        self,
        ordered_snapshots: Sequence[DatedIndustrySnapshotLike],
        *,
        tickers: Collection[str],
    ) -> IndustryRebuild:
        """Recompute the classification history of ``tickers`` from snapshots.

        ``ordered_snapshots`` are whole-market answers ordered by query date
        (e.g. one per year start plus the current one). For each ticker the
        snapshots give a point per snapshot: its entry's ``(code, name)`` at
        the entry's effective date, or "unclassified" at the snapshot's
        effective date when the ticker is absent (not listed yet / delisted).
        Inside the window the points are authoritative; rows before it and
        changes recorded after it are kept (same merge as
        :meth:`UniverseRepository.rebuild_history`): no duplicates, no gaps,
        idempotent, provenance kept on rows that only move a bound.

        Only ``tickers`` are touched; entries of other listings are ignored and
        no listing is created for them.
        """

        system = ordered_snapshots[0].system if ordered_snapshots else "csrc"
        if any(snapshot.system != system for snapshot in ordered_snapshots):
            raise ValueError("industry snapshots of different systems cannot be merged")
        dated = [s for s in ordered_snapshots if s.effective_date is not None]
        captured_at = max((to_utc(s.captured_at) for s in dated), default=None)
        source = dated[-1].source if dated else "baostock"
        by_ticker = [{entry.ticker: entry for entry in s.entries} for s in dated]

        listings = ListingRepository(self.session)
        kept = reused = deleted = inserted = 0
        reclassified: list[str] = []
        unclassified: list[str] = []
        for ticker in sorted(set(tickers)):
            points_by_day: dict[date, IndustryValue | None] = {}
            name: str | None = None
            for snapshot, entries in zip(dated, by_ticker, strict=True):
                entry = entries.get(ticker)
                if entry is None:
                    assert snapshot.effective_date is not None
                    points_by_day[snapshot.effective_date] = None
                    continue
                name = entry.company_name or name
                points_by_day[entry.effective_date] = (
                    (entry.industry_code, entry.industry_name)
                    if entry.industry_name is not None
                    else None
                )
            if not points_by_day:
                continue
            points = sorted(points_by_day.items())
            if all(value is None for _, value in points):
                unclassified.append(ticker)
            listing = await listings.ensure_a_share_listing(ticker, company_name=name)
            rows = await self.history(listing.id, system=system)

            def span_of(row: IndustryClassification) -> Span[IndustryValue]:
                return Span((row.code, row.name), row.valid_from, row.valid_to)

            stored_spans = [span_of(row) for row in rows]
            timeline = merge_timeline(
                partial(state_at, stored_spans),
                boundaries(stored_spans),
                points,
            )
            desired = scalar_spans(timeline)
            window = (points[0][0], points[-1][0])
            if len({span.value for span in desired if _overlaps(span, *window)}) > 1:
                reclassified.append(ticker)
            plan = plan_rows(rows, desired, span_of)
            kept += len(plan.keep)
            for row in plan.delete:
                await self.session.delete(row)
                deleted += 1
            await self.session.flush()
            for row, span in sorted(plan.reuse, key=lambda pair: pair[1].valid_from):
                row.valid_from, row.valid_to = span.valid_from, span.valid_to
                reused += 1
                await self.session.flush()
            for span in plan.insert:
                assert captured_at is not None
                code, label = span.value
                self.session.add(
                    IndustryClassification(
                        listing_id=listing.id,
                        system=system,
                        code=code,
                        name=label,
                        valid_from=span.valid_from,
                        valid_to=span.valid_to,
                        source=source,
                        captured_at=captured_at,
                    )
                )
                inserted += 1
            await self.session.flush()
        result = IndustryRebuild(
            listings=len(set(tickers)),
            reclassified=tuple(reclassified),
            unclassified=tuple(unclassified),
            kept=kept,
            reused=reused,
            deleted=deleted,
            inserted=inserted,
        )
        log.info(
            "industry_history_rebuilt",
            system=system,
            snapshots=len(dated),
            listings=result.listings,
            reclassified=len(reclassified),
            kept=kept,
            reused=reused,
            deleted=deleted,
            inserted=inserted,
        )
        return result

    async def apply_industry_snapshot(
        self, snapshot: IndustrySnapshotLike
    ) -> list[IndustryChange]:
        listings = ListingRepository(self.session)
        captured_at = to_utc(snapshot.captured_at)
        changes: list[IndustryChange] = []
        for entry in snapshot.entries:
            listing = await listings.ensure_a_share_listing(
                entry.ticker, company_name=entry.company_name or None
            )
            change = await self._apply_entry(
                listing_id=listing.id,
                entry=entry,
                system=snapshot.system,
                source=snapshot.source,
                captured_at=captured_at,
            )
            if change is not None:
                changes.append(change)
        await self.session.flush()
        log.info(
            "industry_snapshot_applied",
            system=snapshot.system,
            entries=len(snapshot.entries),
            changed=len(changes),
        )
        return changes

    async def industry_as_of(
        self, listing_id: int, as_of: date, *, system: str = "csrc"
    ) -> IndustryClassification | None:
        stmt = (
            select(IndustryClassification)
            .where(
                IndustryClassification.listing_id == listing_id,
                IndustryClassification.system == system,
                IndustryClassification.valid_from <= as_of,
                or_(
                    IndustryClassification.valid_to.is_(None),
                    IndustryClassification.valid_to > as_of,
                ),
            )
            .order_by(IndustryClassification.valid_from.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def history(
        self, listing_id: int, *, system: str = "csrc"
    ) -> list[IndustryClassification]:
        stmt = (
            select(IndustryClassification)
            .where(
                IndustryClassification.listing_id == listing_id,
                IndustryClassification.system == system,
            )
            .order_by(IndustryClassification.valid_from)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _apply_entry(
        self,
        *,
        listing_id: int,
        entry: IndustryEntryLike,
        system: str,
        source: str,
        captured_at: datetime,
    ) -> IndustryChange | None:
        effective = entry.effective_date
        new_code, new_name = entry.industry_code, entry.industry_name
        if new_name is None:
            new_code = None
        rows = await self.history(listing_id, system=system)
        open_row = next((row for row in rows if row.valid_to is None), None)

        if open_row is None and new_name is None:
            return None
        if open_row is not None and (open_row.code, open_row.name) == (new_code, new_name):
            return None

        latest_change = max(
            [row.valid_from for row in rows] + [row.valid_to for row in rows if row.valid_to],
            default=None,
        )
        if latest_change is not None and effective < latest_change:
            raise OutOfOrderSnapshotError(
                f"industry entry for {entry.ticker} effective {effective.isoformat()} is "
                f"older than the latest recorded change {latest_change.isoformat()}"
            )

        change = IndustryChange(
            ticker=entry.ticker,
            listing_id=listing_id,
            effective_date=effective,
            old_code=open_row.code if open_row else None,
            old_name=open_row.name if open_row else None,
            new_code=new_code,
            new_name=new_name,
        )
        if open_row is not None:
            if open_row.valid_from == effective:
                # Same-day correction: the old interval would be empty.
                await self.session.delete(open_row)
                await self.session.flush()
            else:
                open_row.valid_to = effective
        if new_name is None:
            return change

        reopen = next(
            (
                row
                for row in rows
                if row.valid_to == effective and (row.code, row.name) == (new_code, new_name)
            ),
            None,
        )
        if reopen is not None:
            reopen.valid_to = None
            return change
        self.session.add(
            IndustryClassification(
                listing_id=listing_id,
                system=system,
                code=new_code,
                name=new_name,
                valid_from=effective,
                valid_to=None,
                source=source,
                captured_at=captured_at,
            )
        )
        await self.session.flush()
        return change


def _overlaps(span: Span[IndustryValue], start: date, end: date) -> bool:
    return span.valid_from <= end and (span.valid_to is None or span.valid_to > start)
