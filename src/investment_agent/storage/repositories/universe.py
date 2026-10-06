"""Point-in-time index constituents on ``universe`` / ``universe_membership``.

Each membership row is an interval ``[valid_from, valid_to)``; an open row
(``valid_to IS NULL``) is a current member. Applying a constituent snapshot
diffs it against the open rows: members that left are closed at the snapshot's
effective date and new members are opened at it. Re-applying the same
snapshot is a no-op.

Live syncs only move forward in time. The historical backfill (P1-A4) uses
:meth:`UniverseRepository.rebuild_history` instead, which recomputes the
intervals of a window from ordered snapshots and merges them with the rows
recorded before and after it (see :mod:`.intervals`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy import or_, select

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.storage.models.reference import Listing, Universe, UniverseMembership
from investment_agent.storage.repositories.base import Repository
from investment_agent.storage.repositories.intervals import (
    Span,
    boundaries,
    merge_timeline,
    plan_rows,
    set_spans,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pit import OutOfOrderSnapshotError, to_utc

log = structlog.get_logger(__name__)

KNOWN_UNIVERSES: dict[str, tuple[str, str]] = {
    "CSI300": ("沪深300", "CSI 300 index constituents (point-in-time)"),
    "SSE50": ("上证50", "SSE 50 index constituents (point-in-time)"),
    "CSI500": ("中证500", "CSI 500 index constituents (point-in-time)"),
}


class ConstituentSnapshotLike(Protocol):
    """What the repository needs from a provider constituent snapshot."""

    @property
    def effective_date(self) -> date | None: ...

    @property
    def members(self) -> Sequence[str]: ...

    @property
    def member_names(self) -> Mapping[str, str]: ...

    @property
    def captured_at(self) -> datetime: ...

    @property
    def source(self) -> str: ...


@dataclass(frozen=True)
class UniverseDiff:
    universe_code: str
    effective_date: date | None
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


@dataclass(frozen=True)
class MembershipRebuild:
    """Outcome of :meth:`UniverseRepository.rebuild_history`."""

    universe_code: str
    window_start: date
    first_effective: date
    last_effective: date
    change_dates: tuple[date, ...]
    """Effective dates inside the window where the member set changed."""
    listings: tuple[str, ...]
    """Every ticker that was a member at some point of the window (sorted)."""
    kept: int
    reused: int
    deleted: int
    inserted: int

    @property
    def changed(self) -> bool:
        return bool(self.reused or self.deleted or self.inserted)


@dataclass(frozen=True)
class UniverseMember:
    listing_id: int
    ticker: str
    valid_from: date
    valid_to: date | None
    source: str | None


class UniverseRepository(Repository[Universe]):
    model = Universe

    async def ensure_universe(self, code: str, *, name: str | None = None) -> Universe:
        existing = await self.session.get(Universe, code)
        if existing is not None:
            return existing
        known = KNOWN_UNIVERSES.get(code)
        if known is None and name is None:
            raise ValueError(f"unknown universe {code!r}; pass a name to create it")
        universe = Universe(
            code=code,
            name=name or (known[0] if known else code),
            description=known[1] if known else None,
        )
        self.session.add(universe)
        await self.session.flush()
        return universe

    async def apply_constituent_snapshot(
        self, universe_code: str, snapshot: ConstituentSnapshotLike
    ) -> UniverseDiff:
        await self.ensure_universe(universe_code)
        effective = snapshot.effective_date
        if effective is None:
            if snapshot.members:
                raise ValueError("a non-empty constituent snapshot needs an effective date")
            # Empty answer (index not yet published): nothing to record.
            return UniverseDiff(universe_code, None, (), ())

        listings = ListingRepository(self.session)
        target: dict[int, str] = {}
        for ticker in snapshot.members:
            listing = await listings.ensure_a_share_listing(
                ticker, company_name=snapshot.member_names.get(ticker) or None
            )
            target[listing.id] = ticker

        rows = list(
            (
                await self.session.execute(
                    select(UniverseMembership).where(
                        UniverseMembership.universe_code == universe_code
                    )
                )
            )
            .scalars()
            .all()
        )
        open_rows = {row.listing_id: row for row in rows if row.valid_to is None}
        added_ids = sorted(target.keys() - open_rows.keys())
        removed_ids = sorted(open_rows.keys() - target.keys())
        if not added_ids and not removed_ids:
            return UniverseDiff(universe_code, effective, (), ())

        latest_change = max(
            [row.valid_from for row in rows] + [row.valid_to for row in rows if row.valid_to],
            default=None,
        )
        if latest_change is not None and effective < latest_change:
            raise OutOfOrderSnapshotError(
                f"{universe_code} snapshot effective {effective.isoformat()} is older than "
                f"the latest recorded change {latest_change.isoformat()}"
            )

        captured_at = to_utc(snapshot.captured_at)
        for listing_id in removed_ids:
            row = open_rows[listing_id]
            if row.valid_from == effective:
                # Same-day correction: the interval would be empty.
                await self.session.delete(row)
            else:
                row.valid_to = effective
        for listing_id in added_ids:
            reopen = next(
                (r for r in rows if r.listing_id == listing_id and r.valid_to == effective),
                None,
            )
            if reopen is not None:
                # Removed and re-added on the same effective date: continuous membership.
                reopen.valid_to = None
                continue
            self.session.add(
                UniverseMembership(
                    universe_code=universe_code,
                    listing_id=listing_id,
                    valid_from=effective,
                    valid_to=None,
                    source=snapshot.source,
                    captured_at=captured_at,
                )
            )
        await self.session.flush()

        removed_tickers = await self._tickers(removed_ids)
        diff = UniverseDiff(
            universe_code=universe_code,
            effective_date=effective,
            added=tuple(sorted(target[listing_id] for listing_id in added_ids)),
            removed=tuple(sorted(removed_tickers.values())),
        )
        log.info(
            "universe_snapshot_applied",
            universe_code=universe_code,
            effective_date=effective.isoformat(),
            added=len(diff.added),
            removed=len(diff.removed),
        )
        return diff

    async def rebuild_history(
        self,
        universe_code: str,
        ordered_snapshots: Sequence[ConstituentSnapshotLike],
        *,
        window_start: date,
    ) -> MembershipRebuild:
        """Recompute membership intervals from snapshots ordered by effective date.

        Inside ``[first effective date, last effective date]`` the snapshots
        are authoritative; rows before the window and changes recorded after
        it (e.g. a live sync) are kept, so the result has no duplicates or
        gaps. Unchanged rows are not touched, rows that only move a bound keep
        their ``source`` / ``captured_at``, new rows carry the snapshots'
        source and latest capture time. Running it twice with the same
        snapshots changes nothing. The caller's transaction makes it atomic.

        Empty (pre-publication) snapshots are ignored. The first non-empty
        snapshot must be effective on or before ``window_start``.
        """

        await self.ensure_universe(universe_code)
        usable = _ordered_snapshots(universe_code, ordered_snapshots)
        first = usable[0][0]
        last = usable[-1][0]
        if first > window_start:
            raise ValueError(
                f"{universe_code} snapshots start {first.isoformat()}, after the window "
                f"start {window_start.isoformat()}"
            )

        listings = ListingRepository(self.session)
        ticker_by_id: dict[int, str] = {}
        id_by_ticker: dict[str, int] = {}
        for _, snapshot in reversed(usable):  # the latest name names a new listing
            for ticker in snapshot.members:
                if ticker in id_by_ticker:
                    continue
                listing = await listings.ensure_a_share_listing(
                    ticker, company_name=snapshot.member_names.get(ticker) or None
                )
                id_by_ticker[ticker] = listing.id
                ticker_by_id[listing.id] = ticker
        points = [
            (effective, frozenset(id_by_ticker[ticker] for ticker in snapshot.members))
            for effective, snapshot in usable
        ]

        rows = list(
            (
                await self.session.execute(
                    select(UniverseMembership).where(
                        UniverseMembership.universe_code == universe_code
                    )
                )
            )
            .scalars()
            .all()
        )

        def span_of(row: UniverseMembership) -> Span[bool]:
            return Span(True, row.valid_from, row.valid_to)

        def stored(day: date) -> frozenset[int]:
            return frozenset(row.listing_id for row in rows if span_of(row).contains(day))

        timeline = merge_timeline(stored, boundaries(span_of(row) for row in rows), points)
        desired = set_spans(timeline)
        rows_by_listing: dict[int, list[UniverseMembership]] = {}
        for row in rows:
            rows_by_listing.setdefault(row.listing_id, []).append(row)

        source = usable[-1][1].source
        captured_at = max(to_utc(snapshot.captured_at) for _, snapshot in usable)
        kept = reused = deleted = inserted = 0
        additions: list[UniverseMembership] = []

        def new_row(
            listing_id: int, span: Span[bool], row_source: str | None, at: datetime | None
        ) -> UniverseMembership:
            return UniverseMembership(
                universe_code=universe_code,
                listing_id=listing_id,
                valid_from=span.valid_from,
                valid_to=span.valid_to,
                source=row_source,
                captured_at=at,
            )

        for listing_id in sorted(rows_by_listing.keys() | desired.keys()):
            plan = plan_rows(
                rows_by_listing.get(listing_id, []), desired.get(listing_id, []), span_of
            )
            kept += len(plan.keep)
            for row in plan.delete:
                await self.session.delete(row)
                deleted += 1
            for row, span in plan.reuse:
                reused += 1
                if row.valid_from == span.valid_from:
                    row.valid_to = span.valid_to
                    continue
                # valid_from is part of the primary key: replace the row, keep provenance.
                await self.session.delete(row)
                additions.append(new_row(listing_id, span, row.source, row.captured_at))
            for span in plan.insert:
                inserted += 1
                additions.append(new_row(listing_id, span, source, captured_at))
        await self.session.flush()
        self.session.add_all(additions)
        await self.session.flush()

        in_window = {
            listing_id
            for listing_id, spans in desired.items()
            if any(
                span.valid_from <= last and (span.valid_to is None or span.valid_to > first)
                for span in spans
            )
        }
        ticker_by_id.update(await self._tickers(sorted(in_window - ticker_by_id.keys())))
        result = MembershipRebuild(
            universe_code=universe_code,
            window_start=window_start,
            first_effective=first,
            last_effective=last,
            change_dates=tuple(day for day, _ in timeline if first < day <= last),
            listings=tuple(sorted(ticker_by_id[listing_id] for listing_id in in_window)),
            kept=kept,
            reused=reused,
            deleted=deleted,
            inserted=inserted,
        )
        log.info(
            "universe_history_rebuilt",
            universe_code=universe_code,
            first=first.isoformat(),
            last=last.isoformat(),
            changes=len(result.change_dates),
            listings=len(result.listings),
            kept=kept,
            reused=reused,
            deleted=deleted,
            inserted=inserted,
        )
        return result

    async def members_between(
        self, universe_code: str, start: date, end: date
    ) -> list[UniverseMember]:
        """Every membership interval overlapping ``[start, end]`` (survivorship set)."""

        stmt = (
            select(UniverseMembership, Listing)
            .join(Listing, Listing.id == UniverseMembership.listing_id)
            .where(
                UniverseMembership.universe_code == universe_code,
                UniverseMembership.valid_from <= end,
                or_(
                    UniverseMembership.valid_to.is_(None),
                    UniverseMembership.valid_to > start,
                ),
            )
        )
        members = [
            UniverseMember(
                listing_id=membership.listing_id,
                ticker=format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
                valid_from=membership.valid_from,
                valid_to=membership.valid_to,
                source=membership.source,
            )
            for membership, listing in (await self.session.execute(stmt)).all()
        ]
        return sorted(members, key=lambda member: (member.ticker, member.valid_from))

    async def members_as_of(self, universe_code: str, as_of: date) -> list[UniverseMember]:
        stmt = (
            select(UniverseMembership, Listing)
            .join(Listing, Listing.id == UniverseMembership.listing_id)
            .where(
                UniverseMembership.universe_code == universe_code,
                UniverseMembership.valid_from <= as_of,
                or_(
                    UniverseMembership.valid_to.is_(None),
                    UniverseMembership.valid_to > as_of,
                ),
            )
        )
        members = [
            UniverseMember(
                listing_id=membership.listing_id,
                ticker=format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
                valid_from=membership.valid_from,
                valid_to=membership.valid_to,
                source=membership.source,
            )
            for membership, listing in (await self.session.execute(stmt)).all()
        ]
        return sorted(members, key=lambda member: member.ticker)

    async def _tickers(self, listing_ids: Sequence[int]) -> dict[int, str]:
        if not listing_ids:
            return {}
        rows = (
            await self.session.execute(select(Listing).where(Listing.id.in_(listing_ids)))
        ).scalars()
        return {
            listing.id: format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
            for listing in rows
        }


def _ordered_snapshots(
    universe_code: str, snapshots: Sequence[ConstituentSnapshotLike]
) -> list[tuple[date, ConstituentSnapshotLike]]:
    """Non-empty snapshots keyed by effective date; same-date duplicates collapse."""

    usable: list[tuple[date, ConstituentSnapshotLike]] = []
    for snapshot in snapshots:
        effective = snapshot.effective_date
        if effective is None:
            if snapshot.members:
                raise ValueError("a non-empty constituent snapshot needs an effective date")
            continue  # pre-publication answer
        if usable:
            previous_date, previous = usable[-1]
            if effective < previous_date:
                raise ValueError("constituent snapshots must be ordered by effective date")
            if effective == previous_date:
                if set(previous.members) != set(snapshot.members):
                    raise ValueError(
                        f"{universe_code} snapshots effective {effective.isoformat()} "
                        "disagree on members"
                    )
                continue
        usable.append((effective, snapshot))
    if not usable:
        raise ValueError(f"no non-empty {universe_code} snapshot to rebuild from")
    return usable
