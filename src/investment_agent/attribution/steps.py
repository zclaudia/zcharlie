"""Attribution pipeline step functions.

Steps:
  attr_step1_data_ingestion: ensure 30d prices + recent news/ratings/financials in DB
  attr_step2_beta_decomposition: regress returns -> BetaDecomposition
  attr_step3_llm_attribution: run AttributionAgent loop -> AttributionResult
  attr_step4_render_and_persist: store AttributionReport + write markdown
"""

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.attribution.attribution_agent import AttributionAgent
from investment_agent.attribution.beta_decomposition import decompose
from investment_agent.domain.protocols import DataSource
from investment_agent.domain.strategy_types import (
    AttributionContext,
    AttributionResult,
    BetaDecomposition,
)
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.pipelines.steps import (
    _insert_news_if_new,
    _insert_rating_if_new,
    _upsert_daily_price,
)
from investment_agent.renderers.markdown_renderer import MarkdownRenderer
from investment_agent.storage.models.market_data import DailyPrice
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.repositories.attribution_report import (
    AttributionReportRepository,
)

log = structlog.get_logger(__name__)


async def attr_step1_data_ingestion(
    session: AsyncSession,
    *,
    listing: Listing,
    data_source: DataSource,
    as_of: date,
    benchmark_ticker: str = "SPY",
    sector_etf_ticker: str | None = None,
    window_days: int = 30,
) -> tuple[Listing, Listing | None, Listing | None]:
    """Pull window_days of prices for stock + benchmark + (optional) sector_etf,
    plus stock news/ratings.

    Returns (stock_listing, benchmark_listing|None, sector_etf_listing|None).
    """

    async def _ensure_listing(ticker: str | None) -> Listing | None:
        if ticker is None:
            return None
        stmt = select(Listing).where(Listing.ticker == ticker).limit(1)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            log.warning("attribution.benchmark_listing_missing", ticker=ticker)
            return None
        return existing

    benchmark = await _ensure_listing(benchmark_ticker)
    sector_etf = await _ensure_listing(sector_etf_ticker)

    targets: list[Listing] = [listing]
    for extra in (benchmark, sector_etf):
        if extra is not None:
            targets.append(extra)

    for t in targets:
        prices = await data_source.fetch_daily_prices(
            [t.ticker],
            start=as_of - timedelta(days=window_days + 5),
            end=as_of,
        )
        for record in prices:
            await _upsert_daily_price(session, t.id, record)

    news = await data_source.fetch_news(listing.ticker, days=7)
    for n in news:
        await _insert_news_if_new(session, listing.company_id, n)
    ratings = await data_source.fetch_analyst_ratings(listing.ticker, days=14)
    for r in ratings:
        await _insert_rating_if_new(session, listing.company_id, r)

    await session.flush()
    log.info(
        "attribution.step1.complete",
        ticker=listing.ticker,
        benchmark=benchmark.ticker if benchmark else None,
        sector_etf=sector_etf.ticker if sector_etf else None,
    )
    return listing, benchmark, sector_etf


async def attr_step2_beta_decomposition(
    session: AsyncSession,
    *,
    listing: Listing,
    benchmark: Listing,
    sector_etf: Listing | None,
    as_of: date,
    window_days: int = 30,
) -> BetaDecomposition:
    """Compute daily returns from DailyPrice rows, regress."""

    async def _returns(listing_id: int) -> list[float]:
        stmt = (
            select(DailyPrice)
            .where(DailyPrice.listing_id == listing_id)
            .where(DailyPrice.date <= as_of)
            .order_by(DailyPrice.date.desc())
            .limit(window_days + 1)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()  # oldest -> newest
        rets: list[float] = []
        for prev, curr in zip(rows, rows[1:], strict=False):
            p0 = float(prev.adjusted_close)
            p1 = float(curr.adjusted_close)
            if p0 > 0:
                rets.append((p1 - p0) / p0)
        return rets

    stock_rets = await _returns(listing.id)
    market_rets = await _returns(benchmark.id)
    sector_rets = await _returns(sector_etf.id) if sector_etf else None

    if sector_rets is not None:
        n = min(len(stock_rets), len(market_rets), len(sector_rets))
        stock_rets = stock_rets[-n:]
        market_rets = market_rets[-n:]
        sector_rets = sector_rets[-n:]
    else:
        n = min(len(stock_rets), len(market_rets))
        stock_rets = stock_rets[-n:]
        market_rets = market_rets[-n:]

    return decompose(
        stock_returns=stock_rets,
        market_returns=market_rets,
        sector_returns=sector_rets,
    )


async def attr_step3_llm_attribution(
    session: AsyncSession,
    *,
    context: AttributionContext,
    agent: AttributionAgent,
    as_of: date,
    timeout_sec: int = 120,
) -> AttributionResult:
    """Run the Attribution Agent loop with tools; returns structured AttributionResult."""
    return await asyncio.wait_for(
        agent.run_for_context(
            session=session,
            context=context,
            as_of=as_of.isoformat(),
        ),
        timeout=timeout_sec,
    )


async def attr_step4_render_and_persist(
    session: AsyncSession,
    *,
    pipeline_run_id: int,
    listing: Listing,
    beta: BetaDecomposition,
    result: AttributionResult,
    as_of: date,
    reports_dir: Path,
) -> str:
    """Persist AttributionReport row + write markdown file. Returns the markdown path."""
    renderer = MarkdownRenderer()
    company = await session.get(Company, listing.company_id)
    payload: dict[str, object] = {
        "ticker": listing.ticker,
        "company_name": company.canonical_name if company else "",
        "as_of": as_of.isoformat(),
        "beta": beta.model_dump(),
        "result": result.model_dump(),
    }
    content = await renderer.render_attribution(pipeline_run_id, payload)
    notif = await FileNotifier(reports_dir).send(
        content,
        channel_metadata={
            "kind": f"attribution_{listing.ticker}",
            "run_id": pipeline_run_id,
            "date": as_of.isoformat(),
        },
    )
    await AttributionReportRepository(session).upsert(
        pipeline_run_id=pipeline_run_id,
        listing_id=listing.id,
        analysis_date=as_of,
        price_change_pct=Decimal(str(beta.stock_return_pct)),
        beta_attribution=beta.model_dump(),
        triggering_events=[e.model_dump() for e in result.triggering_events],
        narrative=result.summary,
        valuation_delta=result.valuation_delta,
        markdown_path=notif.target,
    )
    log.info(
        "attribution.step4.complete",
        run_id=pipeline_run_id,
        ticker=listing.ticker,
        markdown=notif.target,
    )
    return notif.target
