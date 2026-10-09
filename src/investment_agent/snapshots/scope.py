"""Resolve a collection payload's scope to the listings it covers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.snapshots.collect import SnapshotPreconditionError
from investment_agent.snapshots.jobs import (
    CloseQuotePayload,
    DividendSnapshotPayload,
    EarningsEventPayload,
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
)
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import HoldingItem, WatchlistItem
from investment_agent.storage.repositories.universe import UniverseRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

_A_SHARE_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


async def scope_members(
    session: AsyncSession,
    payload: (
        MarketSnapshotPayload
        | FinancialSnapshotPayload
        | DividendSnapshotPayload
        | CloseQuotePayload
        | EarningsEventPayload
    ),
    as_of: date,
) -> dict[str, int]:
    """Ticker -> listing id for the payload's scope on ``as_of``.

    * ``universe`` - point-in-time index members (``universe_membership``);
    * ``watchlist`` - enabled watchlist items that are A-share listings.

    An empty scope is a :class:`SnapshotPreconditionError` (terminal).
    """

    if payload.scope == "universe":
        assert payload.universe_code is not None  # enforced by the payload model
        members = await UniverseRepository(session).members_as_of(payload.universe_code, as_of)
        if not members:
            raise SnapshotPreconditionError(
                f"{payload.universe_code} has no members on {as_of.isoformat()}; "
                "run universe_sync first"
            )
        return {member.ticker: member.listing_id for member in members}

    items = await WatchlistRepository(session).list_enabled()
    listing_ids = [item.listing_id for item in items]
    listings = (
        (await session.execute(select(Listing).where(Listing.id.in_(listing_ids))))
        .scalars()
        .all()
        if listing_ids
        else []
    )
    resolved = {
        ticker: listing.id
        for listing in listings
        if _A_SHARE_TICKER.fullmatch(
            ticker := format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
        )
    }
    if not resolved:
        raise SnapshotPreconditionError("the watchlist has no enabled A-share items")
    return dict(sorted(resolved.items()))


@dataclass(frozen=True)
class UserPools:
    """A-share ticker -> listing id of the user's pools (mrd-intake C1 / B2)."""

    watchlist: dict[str, int]
    """Enabled watchlist items."""
    candidates: dict[str, int]
    """Open candidates."""
    holdings: dict[str, int]

    @property
    def members(self) -> dict[str, int]:
        return dict(sorted({**self.watchlist, **self.candidates, **self.holdings}.items()))

    def pools_of(self, ticker: str) -> list[str]:
        return [
            name
            for name, pool in (
                ("watchlist", self.watchlist),
                ("candidate", self.candidates),
                ("holding", self.holdings),
            )
            if ticker in pool
        ]


async def a_share_tickers(session: AsyncSession, listing_ids: Sequence[int]) -> dict[str, int]:
    """Canonical A-share ticker -> listing id for ``listing_ids`` (others dropped)."""

    if not listing_ids:
        return {}
    listings = (
        await session.execute(select(Listing).where(Listing.id.in_(list(set(listing_ids)))))
    ).scalars()
    return {
        ticker: listing.id
        for listing in listings
        if _A_SHARE_TICKER.fullmatch(
            ticker := format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
        )
    }


async def user_pools(session: AsyncSession) -> UserPools:
    """Enabled watchlist ∪ open candidates ∪ holdings (each may be empty)."""

    watchlist_ids = list(
        (
            await session.execute(
                select(WatchlistItem.listing_id).where(WatchlistItem.enabled.is_(True))
            )
        ).scalars()
    )
    candidate_ids = list(
        (
            await session.execute(
                select(CandidateRecord.listing_id).where(CandidateRecord.status == "open")
            )
        ).scalars()
    )
    holding_ids = list((await session.execute(select(HoldingItem.listing_id))).scalars())
    return UserPools(
        watchlist=await a_share_tickers(session, watchlist_ids),
        candidates=await a_share_tickers(session, candidate_ids),
        holdings=await a_share_tickers(session, holding_ids),
    )
