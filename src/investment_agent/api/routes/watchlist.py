"""/watchlist endpoints."""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.admin import (
    WatchlistAdd,
    WatchlistItemOut,
    WatchlistLatestResearch,
    WatchlistRefreshItemOut,
    WatchlistRefreshOut,
    WatchlistRefreshRequest,
    WatchlistUpdate,
)
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.research.watchlist_refresh import enqueue_watchlist_refresh
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.research_change import (
    ResearchChangeRepository,
)
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository

router = APIRouter(
    prefix="/watchlist",
    tags=["watchlist"],
    dependencies=[Depends(verify_api_key_or_session)],
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
            enabled=body.enabled,
        )
        return await _to_out(session, item)


@router.post(
    "/refresh",
    response_model=WatchlistRefreshOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_watchlist(
    body: WatchlistRefreshRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r".*\S.*",
        ),
    ] = None,
) -> WatchlistRefreshOut:
    """Queue independent research jobs for every enabled watchlist item."""

    if not settings.ifind_proxy_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IFIND_PROXY_API_KEY is not configured",
        )
    refresh_date = body.as_of or date.today()
    async with session_scope() as session:
        outcomes = await enqueue_watchlist_refresh(
            session,
            as_of=refresh_date,
            triggered_by="watchlist_api",
            idempotency_key=idempotency_key,
        )
    items = [WatchlistRefreshItemOut(**outcome.__dict__) for outcome in outcomes]
    return WatchlistRefreshOut(
        as_of=refresh_date,
        total=len(items),
        created_runs=sum(item.created for item in items),
        reused_runs=sum(item.run_id is not None and not item.created for item in items),
        failed=sum(item.status == "failed" for item in items),
        items=items,
    )


async def _to_out(session: AsyncSession, item: WatchlistItem) -> WatchlistItemOut:
    listing = await session.get(Listing, item.listing_id)
    if listing is None:
        raise ValueError(f"Listing {item.listing_id} not found")
    company = await session.get(Company, listing.company_id)
    if company is None:
        raise ValueError(f"Company {listing.company_id} not found")
    latest = await _latest_research(
        session,
        symbol=listing.ticker,
        market_code=listing.market_code,
    )
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
        latest_research=latest,
    )


async def _latest_research(
    session: AsyncSession,
    *,
    symbol: str,
    market_code: str,
) -> WatchlistLatestResearch | None:
    """Summarize the newest snapshot and its change set for one listing."""

    parsed = parse_a_share_ticker(
        format_listing_ticker(ticker=symbol, market_code=market_code)
    )
    if parsed is None:
        return None
    suffix = parsed[1].suffix
    history = await ResearchSnapshotRepository(session).history(
        symbol=symbol,
        market=suffix,
        limit=1,
    )
    if not history:
        return None
    snapshot = history[0]
    change_set = await ResearchChangeRepository(session).by_snapshot(snapshot.id)
    items = snapshot.score_json.get("items")
    score = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    freshness = score.get("freshness")
    freshness_ok: bool | None = None
    if isinstance(freshness, dict) and freshness:
        freshness_ok = all(
            block.get("is_stale") is False
            for block in freshness.values()
            if isinstance(block, dict)
        )
    return WatchlistLatestResearch(
        run_id=snapshot.pipeline_run_id,
        as_of=snapshot.as_of,
        strategy_label=_optional_str(score.get("strategy_label")),
        investment_score=_optional_float(score.get("investment_score")),
        research_confidence_score=_optional_float(score.get("research_confidence_score")),
        freshness_ok=freshness_ok,
        needs_agent_review=(
            score["needs_agent_review"]
            if isinstance(score.get("needs_agent_review"), bool)
            else None
        ),
        scoring_version=snapshot.scoring_version,
        changed_at=(
            _as_utc(change_set.notified_at if change_set.notified_at is not None else change_set.created_at)
            if change_set is not None
            else None
        ),
        meaningful_change_count=(
            change_set.meaningful_change_count if change_set is not None else None
        ),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive timestamps at the API boundary."""

    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


@router.patch("/{listing_id}", response_model=WatchlistItemOut)
async def update_watchlist_item(listing_id: int, body: WatchlistUpdate) -> WatchlistItemOut:
    async with session_scope() as session:
        item = await WatchlistRepository(session).set_enabled(
            listing_id, enabled=body.enabled
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail=f"No watchlist entry for listing_id={listing_id}",
            )
        return await _to_out(session, item)


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
