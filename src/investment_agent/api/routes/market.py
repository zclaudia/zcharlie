"""/market endpoints: read-only daily bars for the browser sparkline (B3 / D7).

Only existing snapshots are read; nothing is captured here. The series is the
uncorrected (``adjust="none"``) bar in the registry's provider precedence
(decision D2, ``config/field_sources.yaml`` ``close``): baostock whenever it
holds the day, else the Tencent close quote stored at 15:10, so the sparkline
shows today's close the same afternoon.

``load_bars`` / ``BARS_PROVIDER`` / ``BARS_ADJUST`` now live in
:mod:`investment_agent.api.views` (shared with the MCP ``get_bars`` tool) and
are re-exported here for existing importers.
"""

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.market import MarketBar, MarketBarsOut
from investment_agent.api.views import (
    BARS_ADJUST,  # noqa: F401  (re-export; the MCP get_bars tool imports it from api.views too)
    BARS_PROVIDER,
    load_bars,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository

router = APIRouter(
    prefix="/market",
    tags=["market"],
    dependencies=[Depends(verify_api_key_or_session)],
)

# Same shape the /app/stocks/{ticker} page accepts.
_TICKER = re.compile(r"\d{6}\.(SH|SZ|BJ)")


@router.get("/{ticker}/bars", response_model=MarketBarsOut)
async def market_bars(
    ticker: str,
    days: Annotated[int, Query(ge=5, le=1000)] = 120,
) -> MarketBarsOut:
    """Last ``days`` closes known now (oldest first); empty when nothing is stored."""

    normalized = ticker.upper()
    if _TICKER.fullmatch(normalized) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not a canonical A-share ticker: {ticker}",
        )
    as_of = datetime.now(UTC)
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing(normalized)
        if listing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Listing not found: {normalized}",
            )
        rows = await load_bars(session, listing.id, days, as_of=as_of)
    bars = [MarketBar(trade_date=row.trade_date, close=float(row.close)) for row in rows]
    return MarketBarsOut(ticker=normalized, provider=BARS_PROVIDER, as_of=as_of, bars=bars)
