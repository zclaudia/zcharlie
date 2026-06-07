"""Discovery pipeline step functions. Each step is async and idempotent."""

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.protocols import DataSource
from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FundamentalSnapshotRecord,
    NewsRecord,
)
from investment_agent.sources.retry import RateLimiter
from investment_agent.storage.models.market_data import (
    AnalystRatingChange,
    DailyPrice,
    FundamentalSnapshot,
    NewsItem,
)
from investment_agent.storage.models.reference import Listing, UniverseMembership
from investment_agent.storage.models.user_domain import AuxiliaryListing

log = structlog.get_logger(__name__)


@dataclass
class _FetchResult:
    """Per-listing fetch result carried between gather + persist phases."""

    ticker: str
    prices: list[DailyPriceRecord] = field(default_factory=list)
    snapshot: FundamentalSnapshotRecord | None = None
    news: list[NewsRecord] = field(default_factory=list)
    ratings: list[AnalystRatingRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def step1_data_ingestion(
    session: AsyncSession,
    *,
    universe_code: str,
    data_source: DataSource,
    as_of: date,
    pipeline_run_id: int,
    max_concurrent: int = 10,
) -> tuple[list[Listing], list[Listing]]:
    """Pull EOD prices, fundamentals, news, ratings for all universe listings
    plus all enabled auxiliary listings.

    Returns (universe_listings, auxiliary_listings).
    """
    log.info(
        "pipeline.step1.start",
        universe_code=universe_code,
        as_of=as_of.isoformat(),
        run_id=pipeline_run_id,
    )

    # 1. Fetch universe listings active on `as_of`.
    stmt = (
        select(Listing)
        .join(UniverseMembership, UniverseMembership.listing_id == Listing.id)
        .where(
            UniverseMembership.universe_code == universe_code,
            (UniverseMembership.valid_to.is_(None))
            | (UniverseMembership.valid_to >= as_of),
            UniverseMembership.valid_from <= as_of,
        )
    )
    universe_listings = list((await session.execute(stmt)).scalars().all())

    # 2. Fetch enabled auxiliary listings.
    aux_stmt = (
        select(Listing)
        .join(AuxiliaryListing, AuxiliaryListing.listing_id == Listing.id)
        .where(AuxiliaryListing.enabled.is_(True))
    )
    aux_listings = list((await session.execute(aux_stmt)).scalars().all())

    # 3. Concurrently fetch data (auxiliary is lighter — no fundamentals snapshot).
    limiter = RateLimiter(max_concurrent=max_concurrent)
    start = as_of - timedelta(days=7)

    async def _fetch_universe_one(listing: Listing) -> _FetchResult:
        result = _FetchResult(ticker=listing.ticker)
        async with limiter:
            try:
                result.prices = await data_source.fetch_daily_prices(
                    [listing.ticker], start=start, end=as_of
                )
            except Exception as e:
                result.errors.append(f"prices: {e}")
            try:
                result.snapshot = await data_source.fetch_fundamental_snapshot(
                    listing.ticker, as_of
                )
            except Exception as e:
                result.errors.append(f"snapshot: {e}")
            try:
                result.news = await data_source.fetch_news(listing.ticker, days=1)
            except Exception as e:
                result.errors.append(f"news: {e}")
            try:
                result.ratings = await data_source.fetch_analyst_ratings(
                    listing.ticker, days=1
                )
            except Exception as e:
                result.errors.append(f"ratings: {e}")
        return result

    async def _fetch_aux_one(listing: Listing) -> _FetchResult:
        """Auxiliary: only prices + news + ratings (no fundamental snapshot)."""
        result = _FetchResult(ticker=listing.ticker)
        async with limiter:
            try:
                result.prices = await data_source.fetch_daily_prices(
                    [listing.ticker], start=start, end=as_of
                )
            except Exception as e:
                result.errors.append(f"prices: {e}")
            try:
                result.news = await data_source.fetch_news(listing.ticker, days=1)
            except Exception as e:
                result.errors.append(f"news: {e}")
            try:
                result.ratings = await data_source.fetch_analyst_ratings(
                    listing.ticker, days=1
                )
            except Exception as e:
                result.errors.append(f"ratings: {e}")
        return result

    universe_results = await asyncio.gather(
        *[_fetch_universe_one(item) for item in universe_listings],
        return_exceptions=False,
    )
    aux_results = await asyncio.gather(
        *[_fetch_aux_one(item) for item in aux_listings],
        return_exceptions=False,
    )

    # 4. Persist (idempotent upserts / dedupe).
    listings_by_ticker: dict[str, Listing] = {
        item.ticker: item for item in universe_listings + aux_listings
    }
    total_errors = 0
    for batch in (universe_results, aux_results):
        for r in batch:
            listing = listings_by_ticker[r.ticker]
            for price in r.prices:
                await _upsert_daily_price(session, listing.id, price)
            if r.snapshot is not None:
                await _upsert_fundamental_snapshot(session, listing.id, r.snapshot)
            for news in r.news:
                await _insert_news_if_new(session, listing.company_id, news)
            for rating in r.ratings:
                await _insert_rating_if_new(session, listing.company_id, rating)
            total_errors += len(r.errors)

    await session.flush()
    log.info(
        "pipeline.step1.complete",
        universe_n=len(universe_listings),
        aux_n=len(aux_listings),
        errors=total_errors,
        run_id=pipeline_run_id,
    )
    return universe_listings, aux_listings


async def _upsert_daily_price(
    session: AsyncSession, listing_id: int, record: DailyPriceRecord
) -> None:
    existing = await session.get(DailyPrice, (listing_id, record.date))
    if existing is None:
        session.add(
            DailyPrice(
                listing_id=listing_id,
                date=record.date,
                open=record.open,
                high=record.high,
                low=record.low,
                close=record.close,
                volume=record.volume,
                adjusted_close=record.adjusted_close,
            )
        )


async def _upsert_fundamental_snapshot(
    session: AsyncSession, listing_id: int, record: FundamentalSnapshotRecord
) -> None:
    existing = await session.get(FundamentalSnapshot, (listing_id, record.date))
    if existing is None:
        session.add(
            FundamentalSnapshot(
                listing_id=listing_id,
                date=record.date,
                pe=record.pe,
                pb=record.pb,
                peg=record.peg,
                ps=record.ps,
                roe=record.roe,
                roa=record.roa,
                fcf_yield=record.fcf_yield,
                debt_to_equity=record.debt_to_equity,
                market_cap=record.market_cap,
            )
        )


async def _insert_news_if_new(
    session: AsyncSession, company_id: int, record: NewsRecord
) -> None:
    # Dedupe by URL — no unique constraint enforced at DB level.
    stmt = select(NewsItem).where(NewsItem.url == record.url)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return
    session.add(
        NewsItem(
            company_id=company_id,
            published_at=record.published_at,
            source=record.source,
            title=record.title,
            url=record.url,
            summary=record.summary,
        )
    )


async def _insert_rating_if_new(
    session: AsyncSession, company_id: int, record: AnalystRatingRecord
) -> None:
    # Dedupe by (company, date, firm, action).
    stmt = select(AnalystRatingChange).where(
        AnalystRatingChange.company_id == company_id,
        AnalystRatingChange.date == record.date,
        AnalystRatingChange.firm == record.firm,
        AnalystRatingChange.action == record.action,
    )
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return
    session.add(
        AnalystRatingChange(
            company_id=company_id,
            date=record.date,
            firm=record.firm,
            action=record.action,
            from_rating=record.from_rating,
            to_rating=record.to_rating,
            price_target=record.price_target,
        )
    )
