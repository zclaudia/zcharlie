"""/market endpoints: read-only daily bars for the browser sparkline (B3 / D7).

Only existing snapshots are read; nothing is captured here. The series is the
uncorrected (``adjust="none"``) baostock version, as decided in D7; the
provider stays pinned until the 02 close-quote plan widens the read side.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.market import MarketBar, MarketBarsOut
from investment_agent.clock import shanghai_today
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

router = APIRouter(
    prefix="/market",
    tags=["market"],
    dependencies=[Depends(verify_api_key_or_session)],
)

# Same shape the /app/stocks/{ticker} page accepts.
_TICKER = re.compile(r"\d{6}\.(SH|SZ|BJ)")
BARS_PROVIDER = "baostock"
BARS_ADJUST = "none"
# Trading days make up roughly 68% of calendar days; the lookback window is
# generous so ``days`` trading days always fit, then the tail is sliced.
_CALENDAR_DAYS_PER_BAR = 2
_WINDOW_SLACK_DAYS = 30


@router.get("/{ticker}/bars", response_model=MarketBarsOut)
async def market_bars(
    ticker: str,
    days: Annotated[int, Query(ge=20, le=400)] = 120,
) -> MarketBarsOut:
    """Last ``days`` closes known now (oldest first); empty when nothing is stored."""

    normalized = ticker.upper()
    if _TICKER.fullmatch(normalized) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not a canonical A-share ticker: {ticker}",
        )
    as_of = datetime.now(UTC)
    end = shanghai_today()
    start = end - timedelta(days=days * _CALENDAR_DAYS_PER_BAR + _WINDOW_SLACK_DAYS)
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing(normalized)
        if listing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Listing not found: {normalized}",
            )
        rows = await MarketSnapshotRepository(session).series_as_of(
            listing.id,
            start,
            end,
            as_of,
            adjust=BARS_ADJUST,
            provider=BARS_PROVIDER,
        )
    bars = [MarketBar(trade_date=row.trade_date, close=float(row.close)) for row in rows[-days:]]
    return MarketBarsOut(ticker=normalized, provider=BARS_PROVIDER, as_of=as_of, bars=bars)
