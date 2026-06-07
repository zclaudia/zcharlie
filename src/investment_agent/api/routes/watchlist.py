"""/watchlist endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.admin import WatchlistAdd, WatchlistItemOut
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.watchlist import WatchlistRepository

router = APIRouter(
    prefix="/watchlist",
    tags=["watchlist"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("", response_model=list[WatchlistItemOut])
async def list_watchlist() -> list[WatchlistItemOut]:
    async with session_scope() as session:
        items = await WatchlistRepository(session).list_all()
        out: list[WatchlistItemOut] = []
        for item in items:
            listing = await session.get(Listing, item.listing_id)
            out.append(
                WatchlistItemOut(
                    listing_id=item.listing_id,
                    ticker=listing.ticker if listing else None,
                    market_code=listing.market_code if listing else None,
                    added_at=item.added_at,
                    note=item.note,
                ),
            )
        return out


@router.post("", response_model=WatchlistItemOut, status_code=201)
async def add_watchlist(body: WatchlistAdd) -> WatchlistItemOut:
    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == body.market_code,
                    Listing.ticker == body.ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Listing not found: {body.market_code}/{body.ticker}",
            )
        item = await WatchlistRepository(session).add(listing.id, body.note)
        return WatchlistItemOut(
            listing_id=item.listing_id,
            ticker=listing.ticker,
            market_code=listing.market_code,
            added_at=item.added_at,
            note=item.note,
        )


@router.delete("/{listing_id}", status_code=204)
async def remove_watchlist(listing_id: int) -> Response:
    async with session_scope() as session:
        removed = await WatchlistRepository(session).remove(listing_id)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"No watchlist entry for listing_id={listing_id}",
            )
        return Response(status_code=204)
