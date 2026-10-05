"""/watchlist endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.admin import WatchlistAdd, WatchlistItemOut
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
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
        return [await _to_out(session, item) for item in items]


@router.post("", response_model=WatchlistItemOut, status_code=201)
async def add_watchlist(body: WatchlistAdd) -> WatchlistItemOut:
    async with session_scope() as session:
        watchlist_repository = WatchlistRepository(session)
        parsed = parse_a_share_ticker(body.ticker)
        industry = body.industry
        report_year = body.report_year
        if parsed is not None:
            symbol, market = parsed
            if body.market_code is not None and body.market_code != market.market_code:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"market_code {body.market_code} conflicts with ticker suffix "
                        f"{market.suffix}"
                    ),
                )
            listing = await ListingRepository(session).get_by_ticker(
                market.market_code,
                symbol,
            )
            history = await ResearchSnapshotRepository(session).history(
                symbol=symbol,
                market=market.suffix,
                limit=1,
            )
            latest_snapshot = history[0] if history else None
            name = body.name or (
                latest_snapshot.company_name if latest_snapshot is not None else None
            )
            industry = industry or (
                latest_snapshot.industry if latest_snapshot is not None else None
            )
            report_year = report_year or (
                latest_snapshot.report_year if latest_snapshot is not None else None
            )
            existing_item = (
                await watchlist_repository.get_by_listing(listing.id)
                if listing is not None
                else None
            )
            if industry is None and existing_item is not None:
                industry = existing_item.industry
            if report_year is None and existing_item is not None:
                report_year = existing_item.report_year
            if industry is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "industry is required when no prior research snapshot or "
                        "watchlist metadata exists"
                    ),
                )
            if listing is None:
                if name is None:
                    raise HTTPException(
                        status_code=422,
                        detail="name is required when no prior research snapshot exists",
                    )
                existing_market = await session.get(Market, market.market_code)
                if existing_market is None:
                    session.add(
                        Market(
                            code=market.market_code,
                            name=market.name,
                            timezone=market.timezone,
                            currency=market.currency,
                        )
                    )
                    await session.flush()
                listing = await ListingRepository(session).upsert_listing(
                    market_code=market.market_code,
                    ticker=symbol,
                    company_name=name,
                )
        else:
            market_code = body.market_code or "NASDAQ"
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.market_code == market_code,
                        Listing.ticker == body.ticker,
                    ),
                )
            ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Listing not found: {body.market_code or 'NASDAQ'}/{body.ticker}",
            )
        existing_item = await watchlist_repository.get_by_listing(listing.id)
        provider = body.provider or (
            existing_item.provider
            if existing_item is not None
            else "ifind" if parsed is not None else "auto"
        )
        item = await watchlist_repository.add(
            listing.id,
            body.note,
            industry=industry,
            provider=provider,
            report_year=report_year,
        )
        return await _to_out(session, item)


async def _to_out(session: AsyncSession, item: WatchlistItem) -> WatchlistItemOut:
    listing = await session.get(Listing, item.listing_id)
    if listing is None:
        raise ValueError(f"Listing {item.listing_id} not found")
    company = await session.get(Company, listing.company_id)
    if company is None:
        raise ValueError(f"Company {listing.company_id} not found")
    return WatchlistItemOut(
        listing_id=item.listing_id,
        ticker=format_listing_ticker(
            ticker=listing.ticker,
            market_code=listing.market_code,
        ),
        market_code=listing.market_code,
        name=company.canonical_name,
        industry=item.industry,
        provider=item.provider,
        report_year=item.report_year,
        enabled=item.enabled,
        added_at=_as_utc(item.added_at),
        updated_at=_as_utc(item.updated_at),
        note=item.note,
        last_refresh_status=item.last_refresh_status,
        last_refreshed_at=(
            _as_utc(item.last_refreshed_at)
            if item.last_refreshed_at is not None
            else None
        ),
        last_refresh_error=item.last_refresh_error,
        last_research_run_id=item.last_research_run_id,
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive timestamps at the API boundary."""

    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


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
