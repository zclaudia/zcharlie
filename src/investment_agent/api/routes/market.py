"""/market endpoints: read-only daily bars for the browser sparkline (B3 / D7).

Only existing snapshots are read; nothing is captured here. The series is the
uncorrected (``adjust="none"``) bar in the registry's provider precedence
(decision D2, ``config/field_sources.yaml`` ``close``): baostock whenever it
holds the day, else the Tencent close quote stored at 15:10, so the sparkline
shows today's close the same afternoon.

``GET /market/bars?tickers=…`` answers a whole list page in one request: the
embedded Android console pays a full gateway round trip per request, and a
candidate or watch list used to issue one ``/{ticker}/bars`` call per row.

``load_bars`` / ``BARS_PROVIDER`` / ``BARS_ADJUST`` now live in
:mod:`investment_agent.api.views` (shared with the MCP ``get_bars`` tool) and
are re-exported here for existing importers.
"""

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.market import (
    MarketBar,
    MarketBarsBatchItem,
    MarketBarsBatchOut,
    MarketBarsOut,
)
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
# Larger than any list page (candidates, watchlist); keeps the URL short.
MAX_BATCH_TICKERS = 100


@router.get("/bars", response_model=MarketBarsBatchOut)
async def market_bars_batch(
    tickers: Annotated[str, Query(min_length=1, description="Comma-separated A-share tickers")],
    days: Annotated[int, Query(ge=5, le=1000)] = 120,
) -> MarketBarsBatchOut:
    """``/{ticker}/bars`` for several tickers at once; unknown listings are ``found: false``."""

    requested = list(
        dict.fromkeys(part.strip().upper() for part in tickers.split(",") if part.strip())
    )
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="no tickers given"
        )
    if len(requested) > MAX_BATCH_TICKERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"at most {MAX_BATCH_TICKERS} tickers per request",
        )
    invalid = [ticker for ticker in requested if _TICKER.fullmatch(ticker) is None]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not canonical A-share tickers: {', '.join(invalid)}",
        )
    as_of = datetime.now(UTC)
    items: list[MarketBarsBatchItem] = []
    async with session_scope() as session:
        listings = ListingRepository(session)
        for ticker in requested:
            listing = await listings.get_a_share_listing(ticker)
            if listing is None:
                items.append(MarketBarsBatchItem(ticker=ticker, found=False, bars=[]))
                continue
            rows = await load_bars(session, listing.id, days, as_of=as_of)
            bars = [MarketBar(trade_date=row.trade_date, close=float(row.close)) for row in rows]
            items.append(MarketBarsBatchItem(ticker=ticker, found=True, bars=bars))
    return MarketBarsBatchOut(provider=BARS_PROVIDER, as_of=as_of, days=days, items=items)


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
