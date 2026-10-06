"""Point-in-time index constituents on ``universe`` / ``universe_membership``.

Each membership row is an interval ``[valid_from, valid_to)``; an open row
(``valid_to IS NULL``) is a current member. Applying a constituent snapshot
diffs it against the open rows: members that left are closed at the snapshot's
effective date and new members are opened at it. Re-applying the same
snapshot is a no-op.
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
