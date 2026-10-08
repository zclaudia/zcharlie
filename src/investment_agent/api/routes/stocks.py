"""/stocks endpoints: directory search for the watchlist / nomination combobox (D4).

Reads the local ``stock_directory`` only (synced weekly by ``universe_sync``);
nothing is fetched upstream per keystroke. Match order: code prefix, then
name substring, then pinyin-initials prefix; see
:meth:`~investment_agent.storage.repositories.stock_directory.StockDirectoryRepository.search`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.routes.data_health import _snapshot_universes
from investment_agent.api.schemas.stocks import StockSearchItem
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository
from investment_agent.storage.repositories.universe import UniverseRepository

router = APIRouter(
    prefix="/stocks",
    tags=["stocks"],
    dependencies=[Depends(verify_api_key_or_session)],
)

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50
SEARCH_QUERY_MAX_LENGTH = 32


@router.get("/search", response_model=list[StockSearchItem])
async def search_stocks(
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(max_length=SEARCH_QUERY_MAX_LENGTH)] = "",
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT_MAX)] = SEARCH_LIMIT_DEFAULT,
) -> list[StockSearchItem]:
    """Directory hits for ``q`` (blank -> ``[]``), at most ``limit``."""

    query = q.strip()
    if not query:
        return []
    async with session_scope() as session:
        rows = await StockDirectoryRepository(session).search(query, limit=limit)
        if not rows:
            return []
        tickers = {row.ticker for row in rows}
        universes = await _snapshot_universes(session, settings)
        members = await UniverseRepository(session).members_as_of(universes[0], shanghai_today())
        in_universe = {member.ticker for member in members} & tickers
        on_watchlist = await _watchlist_tickers(session) & tickers
    return [
        StockSearchItem(
            ticker=row.ticker,
            name=row.name,
            status=row.status,
            in_universe=row.ticker in in_universe,
            on_watchlist=row.ticker in on_watchlist,
        )
        for row in rows
    ]


async def _watchlist_tickers(session: AsyncSession) -> set[str]:
    stmt = select(Listing.ticker, Listing.market_code).join(
        WatchlistItem, WatchlistItem.listing_id == Listing.id
    )
    return {
        format_listing_ticker(ticker=ticker, market_code=market_code)
        for ticker, market_code in (await session.execute(stmt)).all()
    }
