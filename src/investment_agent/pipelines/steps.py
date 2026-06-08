"""Discovery pipeline step functions. Each step is async and idempotent."""

import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TypedDict

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.protocols import DataSource
from investment_agent.domain.strategy_types import (
    DeepAnalysisContext,
    ListingSnapshot,
    ScoringContext,
)
from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    NewsRecord,
)
from investment_agent.sources.retry import RateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import StockScore
from investment_agent.storage.models.market_data import (
    AnalystRatingChange,
    DailyPrice,
    FinancialReport,
    FundamentalSnapshot,
    NewsItem,
)
from investment_agent.storage.models.ops import DeepAnalysisCache
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
    financials: list[FinancialReportRecord] = field(default_factory=list)
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
            try:
                financials = await data_source.fetch_financial_report(
                    listing.ticker, periods=[],
                )
                # Keep the 8 most-recent fiscal periods (covers 2 fiscal years
                # for YoY).
                financials.sort(key=lambda f: f.period_end, reverse=True)
                result.financials = financials[:8]
            except Exception as e:
                result.errors.append(f"financials: {e}")
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
    for batch, is_universe in ((universe_results, True), (aux_results, False)):
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
            if is_universe:
                for record in r.financials:
                    await _upsert_financial_report(
                        session, listing.company_id, record
                    )
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
                beta=record.beta,
                # volatility_30d intentionally left None at fetch time — Task 5
                # backfills it from DailyPrice history during snapshot construction.
                volatility_30d=None,
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


async def _upsert_financial_report(
    session: AsyncSession,
    company_id: int,
    record: FinancialReportRecord,
) -> None:
    existing = await session.get(
        FinancialReport, (company_id, record.fiscal_period),
    )
    if existing is None:
        session.add(
            FinancialReport(
                company_id=company_id,
                fiscal_period=record.fiscal_period,
                period_end=record.period_end,
                revenue=record.revenue,
                gross_profit=record.gross_profit,
                net_income=record.net_income,
                fcf=record.fcf,
                total_assets=record.total_assets,
                total_equity=record.total_equity,
                raw_data=record.raw_data or {},
            )
        )
    else:
        existing.period_end = record.period_end
        existing.revenue = record.revenue
        existing.gross_profit = record.gross_profit
        existing.net_income = record.net_income
        existing.fcf = record.fcf
        existing.total_assets = record.total_assets
        existing.total_equity = record.total_equity
        existing.raw_data = record.raw_data or {}


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
    *,
    candidates_by_strategy: dict[str, list[ListingSnapshot]],
    strategies: dict[str, BaseStrategyAgent],
    pipeline_run_id: int,
    max_concurrent: int = 8,
    timeout_sec: int = 90,
) -> int:
    """Run Tier 2 LLM scoring for each (strategy, listing) pair concurrently.

    Each task opens its own `session_scope()` to avoid sharing an AsyncSession
    across concurrent flushes (SQLAlchemy async sessions are not concurrency
    safe). Tasks are bounded by `max_concurrent` semaphore and `timeout_sec`
    per-call timeout. Failures are logged but don't fail the run.
    Returns total number of scores persisted.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    counter = {"n": 0}
    lock = asyncio.Lock()

    async def _score_one(strategy_code: str, listing: ListingSnapshot) -> None:
        agent = strategies[strategy_code]
        async with semaphore:
            try:
                async with session_scope() as session:
                    ctx = await _build_scoring_context(session, listing)
                    score = await asyncio.wait_for(
                        agent.score(listing, ctx),
                        timeout=timeout_sec,
                    )
                    repo = StockScoreRepository(session)
                    await repo.upsert(
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
                async with lock:
                    counter["n"] += 1
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
    log.info(
        "pipeline.step3.complete", scored=counter["n"], run_id=pipeline_run_id
    )
    return counter["n"]


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


async def step4_tier3_deep_analysis(
    session: AsyncSession,
    *,
    pipeline_run_id: int,
    strategies: dict[str, BaseStrategyAgent],
    top_n: int = 10,
    cache_days: int = 90,
    max_concurrent: int = 4,
    timeout_sec: int = 180,
) -> list[int]:
    """Pick Top-N candidates (cross-strategy avg of Tier-2 score), run deep analysis.

    Returns: listing_ids that received deep analysis.
    """
    # 1. Compute Top-N listing_ids by cross-strategy average Tier-2 score.
    stmt = (
        select(
            StockScore.listing_id,
            func.avg(StockScore.score).label("avg_score"),
        )
        .where(
            StockScore.pipeline_run_id == pipeline_run_id,
            StockScore.tier == 2,
        )
        .group_by(StockScore.listing_id)
        .order_by(func.avg(StockScore.score).desc())
        .limit(top_n)
    )
    rows = list((await session.execute(stmt)).all())
    top_listing_ids = [int(r[0]) for r in rows]

    if not top_listing_ids:
        log.info("pipeline.step4.no_candidates", run_id=pipeline_run_id)
        return []

    # 2. Build snapshots for top listings.
    list_stmt = select(Listing).where(Listing.id.in_(top_listing_ids))
    top_listings = list((await session.execute(list_stmt)).scalars().all())
    as_of = date.today()
    snapshots = await _build_listing_snapshots(session, top_listings, as_of)
    snap_by_lid = {s.listing_id: s for s in snapshots}

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _deep_one(listing_id: int, strategy_code: str) -> None:
        snap = snap_by_lid.get(listing_id)
        if snap is None:
            return
        agent = strategies[strategy_code]
        async with semaphore:
            try:
                # Each task opens its own session_scope() so concurrent
                # writes in _check_or_run_deep_analysis don't nuke
                # sibling tasks' pending writes.
                async with session_scope() as inner:
                    analysis_json = await asyncio.wait_for(
                        _check_or_run_deep_analysis(
                            inner,
                            listing_id=listing_id,
                            strategy_code=strategy_code,
                            agent=agent,
                            snapshot=snap,
                            cache_days=cache_days,
                        ),
                        timeout=timeout_sec,
                    )
                    # Attach deep_analysis to the existing Tier-2 row
                    # (preserves the strategy's numeric score, bumps tier to
                    # 3). If no Tier-2 row exists, insert a new row carrying
                    # just the deep_analysis payload.
                    existing = (
                        await inner.execute(
                            select(StockScore).where(
                                StockScore.pipeline_run_id == pipeline_run_id,
                                StockScore.listing_id == listing_id,
                                StockScore.strategy_code == strategy_code,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        existing.tier = 3
                        existing.deep_analysis = json.dumps(analysis_json)
                        await inner.flush()
                    else:
                        inner_repo = StockScoreRepository(inner)
                        await inner_repo.upsert(
                            pipeline_run_id=pipeline_run_id,
                            listing_id=listing_id,
                            strategy_code=strategy_code,
                            tier=3,
                            score=0,
                            confidence=0.0,
                            deep_analysis=json.dumps(analysis_json),
                        )
            except Exception as e:
                log.warning(
                    "pipeline.step4.deep_failed",
                    strategy=strategy_code,
                    listing_id=listing_id,
                    error=str(e),
                )

    tasks: list[Awaitable[None]] = []
    for lid in top_listing_ids:
        for code in strategies:
            tasks.append(_deep_one(lid, code))
    await asyncio.gather(*tasks)

    log.info(
        "pipeline.step4.complete",
        top_n=len(top_listing_ids),
        run_id=pipeline_run_id,
    )
    return top_listing_ids


async def _check_or_run_deep_analysis(
    session: AsyncSession,
    *,
    listing_id: int,
    strategy_code: str,
    agent: BaseStrategyAgent,
    snapshot: ListingSnapshot,
    cache_days: int,
) -> dict[str, object]:
    """Return analysis_json from cache or live LLM call.

    Cache is keyed on (listing_id, latest_financial_report_period,
    strategy_code) so different strategies get separate rows.
    """
    # 1. Resolve latest financial report period for this listing.
    period_stmt = (
        select(FinancialReport.fiscal_period)
        .where(FinancialReport.company_id == snapshot.company_id)
        .order_by(FinancialReport.period_end.desc())
        .limit(1)
    )
    period = (await session.execute(period_stmt)).scalar_one_or_none()
    if period is None:
        period = "unknown"

    cutoff = datetime.now(UTC) - timedelta(days=cache_days)

    # 2. Lookup by full 3-key unique constraint.
    cache_stmt = select(DeepAnalysisCache).where(
        DeepAnalysisCache.listing_id == listing_id,
        DeepAnalysisCache.latest_financial_report_period == period,
        DeepAnalysisCache.strategy_code == strategy_code,
    )
    cached = (await session.execute(cache_stmt)).scalar_one_or_none()
    if cached is not None:
        # SQLite drops tzinfo on read despite DateTime(timezone=True). Treat
        # naive datetimes as UTC so comparison with `cutoff` is safe.
        generated_at = cached.generated_at
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        if generated_at > cutoff:
            return cached.analysis_json

    # 3. Cache miss (or stale) → live LLM call.
    ctx = await _build_deep_analysis_context(session, snapshot)
    analysis = await agent.deep_analyze(snapshot, ctx)
    analysis_json: dict[str, object] = analysis.model_dump()

    # 4. Insert new row or refresh stale row.
    now = datetime.now(UTC)
    if cached is None:
        session.add(
            DeepAnalysisCache(
                listing_id=listing_id,
                latest_financial_report_period=period,
                strategy_code=strategy_code,
                analysis_json=analysis_json,
                generated_at=now,
            )
        )
    else:
        cached.analysis_json = analysis_json
        cached.generated_at = now
    await session.flush()
    return analysis_json


async def _build_deep_analysis_context(
    session: AsyncSession, snapshot: ListingSnapshot
) -> DeepAnalysisContext:
    """V1: minimal context. Future plan can add financials_history /
    peer_comparables / known_relationships / auxiliary_context / recent_news_full.
    """
    # TODO: enrich with multi-quarter financials history, peer comparables,
    # known company relationships, auxiliary listing context, and recent news.
    return DeepAnalysisContext(listing=snapshot)


class HitRateReview(TypedDict):
    hit_rate: float | None
    n_picks: int
    n_outperformed: int
    benchmark_return: float | None
    picks: list[dict[str, object]]
    note: str | None


async def step7_hit_rate_review(
    session: AsyncSession,
    *,
    pipeline_run_id: int,
    universe_code: str,
    as_of: date,
    benchmark_ticker: str = "SPY",
) -> HitRateReview:
    """Compare prior Discovery run's top picks' returns vs benchmark return.

    Returns a HitRateReview summary; also writes it to pipeline_run.metadata_json.
    """
    from investment_agent.storage.models.analysis import (
        DiscoveryReport,
        PipelineRun,
    )
    from investment_agent.storage.models.market_data import DailyPrice
    from investment_agent.storage.models.reference import Listing

    # 1. Find the immediately-preceding Discovery run for same universe.
    prev_stmt = (
        select(PipelineRun)
        .where(
            PipelineRun.kind == "discovery",
            PipelineRun.universe_code == universe_code,
            PipelineRun.id != pipeline_run_id,
            PipelineRun.status == "success",
        )
        .order_by(PipelineRun.started_at.desc())
        .limit(1)
    )
    prev_run = (await session.execute(prev_stmt)).scalar_one_or_none()
    if prev_run is None:
        review: HitRateReview = {
            "hit_rate": None,
            "n_picks": 0,
            "n_outperformed": 0,
            "benchmark_return": None,
            "picks": [],
            "note": (
                "No prior Discovery run for this universe — "
                "skipping hit-rate review."
            ),
        }
        await _attach_hit_rate(session, pipeline_run_id, review)
        return review

    prev_report = await session.get(DiscoveryReport, prev_run.id)
    if prev_report is None or not prev_report.consensus_picks:
        review = {
            "hit_rate": None,
            "n_picks": 0,
            "n_outperformed": 0,
            "benchmark_return": None,
            "picks": [],
            "note": "Prior run had no consensus picks.",
        }
        await _attach_hit_rate(session, pipeline_run_id, review)
        return review

    pick_listing_ids: list[int] = []
    for entry in prev_report.consensus_picks:
        if isinstance(entry, dict) and isinstance(entry.get("listing_id"), int):
            pick_listing_ids.append(int(entry["listing_id"]))
    if not pick_listing_ids:
        review = {
            "hit_rate": None,
            "n_picks": 0,
            "n_outperformed": 0,
            "benchmark_return": None,
            "picks": [],
            "note": "Prior consensus picks had no listing_id entries.",
        }
        await _attach_hit_rate(session, pipeline_run_id, review)
        return review

    # 2. Compute single-day return for a listing using the two most recent
    #    price rows on/before as_of.
    async def _return_for(listing_id: int) -> float | None:
        price_stmt = (
            select(DailyPrice)
            .where(
                DailyPrice.listing_id == listing_id,
                DailyPrice.date <= as_of,
            )
            .order_by(DailyPrice.date.desc())
            .limit(2)
        )
        prices = list((await session.execute(price_stmt)).scalars().all())
        if len(prices) < 2:
            return None
        today_close = float(prices[0].adjusted_close)
        prev_close = float(prices[1].adjusted_close)
        if prev_close == 0:
            return None
        return (today_close - prev_close) / prev_close

    # 3. Benchmark: prefer SPY, fall back to universe-wide average return.
    bench_stmt = select(Listing).where(Listing.ticker == benchmark_ticker).limit(1)
    bench_listing = (await session.execute(bench_stmt)).scalar_one_or_none()
    benchmark_return: float | None = (
        await _return_for(bench_listing.id) if bench_listing else None
    )

    if benchmark_return is None:
        from investment_agent.storage.models.reference import UniverseMembership

        uni_stmt = select(UniverseMembership.listing_id).where(
            UniverseMembership.universe_code == universe_code,
        )
        uni_ids = [
            int(row[0]) for row in (await session.execute(uni_stmt)).all()
        ]
        uni_returns: list[float] = []
        for lid in uni_ids:
            r = await _return_for(lid)
            if r is not None:
                uni_returns.append(r)
        benchmark_return = (
            sum(uni_returns) / len(uni_returns) if uni_returns else None
        )

    picks_payload: list[dict[str, object]] = []
    n_out = 0
    n_eval = 0
    for lid in pick_listing_ids:
        ret = await _return_for(lid)
        if ret is None or benchmark_return is None:
            picks_payload.append(
                {"listing_id": lid, "return": ret, "outperformed": None}
            )
            continue
        outperformed = ret > benchmark_return
        picks_payload.append(
            {
                "listing_id": lid,
                "return": round(ret, 4),
                "outperformed": outperformed,
            }
        )
        n_eval += 1
        if outperformed:
            n_out += 1

    hit_rate = (n_out / n_eval) if n_eval > 0 else None
    review = {
        "hit_rate": hit_rate,
        "n_picks": len(pick_listing_ids),
        "n_outperformed": n_out,
        "benchmark_return": (
            round(benchmark_return, 4) if benchmark_return is not None else None
        ),
        "picks": picks_payload,
        "note": None if n_eval > 0 else "Insufficient price history to evaluate.",
    }
    await _attach_hit_rate(session, pipeline_run_id, review)
    log.info(
        "pipeline.step7.complete",
        run_id=pipeline_run_id,
        hit_rate=hit_rate,
        n_picks=len(pick_listing_ids),
    )
    return review


async def _attach_hit_rate(
    session: AsyncSession,
    pipeline_run_id: int,
    review: HitRateReview,
) -> None:
    from investment_agent.storage.models.analysis import PipelineRun

    run = await session.get(PipelineRun, pipeline_run_id)
    if run is None:
        return
    run.metadata_json = {**run.metadata_json, "hit_rate_review": dict(review)}
    await session.flush()
