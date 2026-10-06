"""Resolve a collection payload's scope to the listings it covers."""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.snapshots.collect import SnapshotPreconditionError
from investment_agent.snapshots.jobs import FinancialSnapshotPayload, MarketSnapshotPayload
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.universe import UniverseRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

_A_SHARE_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


async def scope_members(
    session: AsyncSession,
    payload: MarketSnapshotPayload | FinancialSnapshotPayload,
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
