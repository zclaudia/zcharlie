"""Discovery pipeline step functions. Each step is async and idempotent."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.protocols import DataSource
from investment_agent.domain.strategy_types import ListingSnapshot, ScoringContext
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
    FinancialReport,
    FundamentalSnapshot,
    NewsItem,
)
from investment_agent.storage.models.reference import (
    Company,
    Listing,
    UniverseMembership,
)
from investment_agent.storage.models.user_domain import AuxiliaryListing
from investment_agent.storage.repositories.stock_score import StockScoreRepository
from investment_agent.strategies.base import BaseStrategyAgent

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


async def step2_tier1_screening(
    session: AsyncSession,
    *,
    listings: list[Listing],
    strategies: dict[str, BaseStrategyAgent],
    as_of: date,
) -> dict[str, list[ListingSnapshot]]:
    """For each strategy, run its `screen()` against universe snapshots.

    Returns: {strategy_code: [passing snapshots]}.
    """
    snapshots = await _build_listing_snapshots(session, listings, as_of)

    out: dict[str, list[ListingSnapshot]] = {}
    for code, agent in strategies.items():
        passed = await agent.screen(snapshots)
        out[code] = passed
        log.info(
            "pipeline.step2.screened",
            strategy=code,
            total_candidates=len(snapshots),
            passed=len(passed),
        )
    return out


async def _build_listing_snapshots(
    session: AsyncSession,
    listings: list[Listing],
    as_of: date,
) -> list[ListingSnapshot]:
    """Build ListingSnapshot DTOs from current DB state for each listing.

    Avoids ORM lazy-load by explicitly fetching Company; eager-loads recent
    financial reports for TTM revenue / net income aggregation.
    """
    out: list[ListingSnapshot] = []
    for listing in listings:
        snap_row = await session.get(FundamentalSnapshot, (listing.id, as_of))

        stmt = (
            select(FinancialReport)
            .where(FinancialReport.company_id == listing.company_id)
            .order_by(FinancialReport.period_end.desc())
            .limit(4)
        )
        recent_4q = list((await session.execute(stmt)).scalars().all())

        revenues = [q.revenue for q in recent_4q if q.revenue is not None]
        revenue_ttm: Decimal | None = (
            sum(revenues, Decimal("0")) if revenues else None
        )
        nets = [q.net_income for q in recent_4q if q.net_income is not None]
        net_income_ttm: Decimal | None = (
            sum(nets, Decimal("0")) if nets else None
        )

        # Explicit fetch to avoid async lazy-load on listing.company.
        company = await session.get(Company, listing.company_id)
        company_name = company.canonical_name if company else ""
        sector_code = company.sector_code if company else None

        out.append(
            ListingSnapshot(
                listing_id=listing.id,
                company_id=listing.company_id,
                ticker=listing.ticker,
                market_code=listing.market_code,
                company_name=company_name,
                sector_code=sector_code,
                as_of=as_of,
                pe=snap_row.pe if snap_row else None,
                pb=snap_row.pb if snap_row else None,
                peg=snap_row.peg if snap_row else None,
                ps=snap_row.ps if snap_row else None,
                roe=snap_row.roe if snap_row else None,
                roa=snap_row.roa if snap_row else None,
                fcf_yield=snap_row.fcf_yield if snap_row else None,
                debt_to_equity=snap_row.debt_to_equity if snap_row else None,
                market_cap=snap_row.market_cap if snap_row else None,
                revenue_ttm=revenue_ttm,
                net_income_ttm=net_income_ttm,
            )
        )
    return out


async def step3_tier2_scoring(
    session: AsyncSession,
    *,
    candidates_by_strategy: dict[str, list[ListingSnapshot]],
    strategies: dict[str, BaseStrategyAgent],
    pipeline_run_id: int,
    max_concurrent: int = 8,
    timeout_sec: int = 90,
) -> int:
    """Run Tier 2 LLM scoring for each (strategy, listing) pair concurrently.

    Each (strategy, listing) task is bounded by `max_concurrent` semaphore and
    `timeout_sec` per-call timeout. Failures are logged but don't fail the run.
    Returns total number of scores persisted.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    score_repo = StockScoreRepository(session)
    n = 0

    async def _score_one(strategy_code: str, listing: ListingSnapshot) -> None:
        nonlocal n
        agent = strategies[strategy_code]
        ctx = await _build_scoring_context(session, listing)

        async with semaphore:
            try:
                score = await asyncio.wait_for(
                    agent.score(listing, ctx), timeout=timeout_sec
                )
                await score_repo.upsert(
                    pipeline_run_id=pipeline_run_id,
                    listing_id=listing.listing_id,
                    strategy_code=strategy_code,
                    tier=2,
                    score=score.score,
                    confidence=score.confidence,
                    bull_thesis=score.bull_thesis,
                    bear_thesis=score.bear_thesis,
                    key_metrics=dict(score.key_metrics_explained),
                )
                n += 1
            except Exception as e:
                log.warning(
                    "pipeline.step3.score_failed",
                    strategy=strategy_code,
                    ticker=listing.ticker,
                    error=str(e),
                )

    tasks: list[Awaitable[None]] = []
    for code, candidates in candidates_by_strategy.items():
        for listing in candidates:
            tasks.append(_score_one(code, listing))

    await asyncio.gather(*tasks)
    log.info("pipeline.step3.complete", scored=n, run_id=pipeline_run_id)
    return n


async def _build_scoring_context(
    session: AsyncSession, listing: ListingSnapshot
) -> ScoringContext:
    """V1: minimal context (empty peers/news/ratings).

    Future plan can enrich with sector peer comparables, recent news titles,
    and analyst rating changes.
    """
    return ScoringContext(
        listing=listing,
        peer_summary=[],
        recent_news_titles=[],
        recent_rating_changes=[],
    )
