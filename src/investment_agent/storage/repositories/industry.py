"""Point-in-time industry classification intervals.

One row per ``(listing, system)`` interval ``[valid_from, valid_to)``. An
industry snapshot is applied entry by entry: an unchanged classification is a
no-op, a changed one closes the open interval and opens a new one at the
entry's effective date, and an empty classification closes the open interval.
Snapshots may be partial (one symbol); listings absent from a snapshot are
left untouched.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy import or_, select

from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.repositories.base import Repository
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


class IndustryRepository(Repository[IndustryClassification]):
    model = IndustryClassification

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
