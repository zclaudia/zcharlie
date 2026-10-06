"""/attribute endpoints — Attribution pipeline trigger + read."""

from datetime import date
from typing import Annotated, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key
from investment_agent.api.routes.pipelines import fail_if_unfinished
from investment_agent.api.schemas.attribution import (
    AttributionReportOut,
    AttributionRunCreated,
    AttributionRunRequest,
)
from investment_agent.api.schemas.listing import TICKER_PATTERN
from investment_agent.attribution.attribution_agent import AttributionAgent
from investment_agent.attribution.pipeline import AttributionPipeline
from investment_agent.clock import US_LEGACY_TZ, today_in
from investment_agent.config import Settings
from investment_agent.sources.yfinance_source import YFinanceSource
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import AttributionReport, PipelineRun
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.attribution_report import (
    AttributionReportRepository,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

TickerPath = Annotated[str, Path(pattern=TICKER_PATTERN)]

router = APIRouter(
    prefix="/attribute",
    tags=["attribution"],
    dependencies=[Depends(verify_api_key)],
)


async def _run_attribution(
    ticker: str,
    as_of: date,
    market: str,
    benchmark: str,
    run_id: int,
) -> None:
    """Run Attribution against the PipelineRun the route already returned."""

    try:
        settings = Settings()  # type: ignore[call-arg]
        agent = AttributionAgent(tier3_config=settings.llm_tier3)
        pipeline = AttributionPipeline(
            data_source=YFinanceSource(
                max_concurrent=settings.yfinance_max_concurrent,
            ),  # type: ignore[arg-type]
            agent=agent,
            settings=settings,
            benchmark_ticker=benchmark,
        )
        await pipeline.run(ticker, as_of, market_code=market, run_id=run_id)
    except Exception as exc:
        await fail_if_unfinished(run_id, exc)
        raise


@router.post(
    "/{ticker}",
    response_model=AttributionRunCreated,
    status_code=202,
)
async def trigger_attribution(
    ticker: TickerPath,
    req: AttributionRunRequest,
    background: BackgroundTasks,
    is_async: bool = Query(True, alias="async"),
) -> AttributionRunCreated:
    """Trigger Attribution pipeline.

    `?async=true` (default): pre-creates run, schedules work, returns HTTP 202.
    `?async=false`: blocks until pipeline finishes.
    """
    as_of = req.as_of or today_in(US_LEGACY_TZ)

    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == req.market_code,
                    Listing.ticker == ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Listing not found: {req.market_code}/{ticker}",
            )
        run = await PipelineRunRepository(session).create(
            kind="attribution",
            listing_id=listing.id,
            triggered_by="api",
        )
        created = AttributionRunCreated(
            run_id=run.id,
            status=run.status,
            started_at=run.started_at,
        )

    if is_async:
        background.add_task(
            _run_attribution,
            ticker,
            as_of,
            req.market_code,
            req.benchmark_ticker,
            created.run_id,
        )
        return created

    await _run_attribution(
        ticker,
        as_of,
        req.market_code,
        req.benchmark_ticker,
        created.run_id,
    )
    async with session_scope() as session:
        run = (
            await session.execute(
                select(PipelineRun).where(PipelineRun.id == created.run_id),
            )
        ).scalar_one()
        return AttributionRunCreated(
            run_id=run.id,
            status=run.status,
            started_at=run.started_at,
        )


@router.get("/{ticker}/latest", response_model=AttributionReportOut)
async def latest_attribution(
    ticker: TickerPath,
    market: str = "NASDAQ",
) -> AttributionReportOut:
    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == market,
                    Listing.ticker == ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Listing not found: {market}/{ticker}",
            )
        rows = await AttributionReportRepository(session).latest_for_listing(
            listing.id,
            limit=1,
        )
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No attribution report for {ticker}",
            )
        return _report_out(rows[0], ticker=ticker)


@router.get("/runs/{run_id}", response_model=AttributionReportOut)
async def get_attribution_run(run_id: int) -> AttributionReportOut:
    async with session_scope() as session:
        report = await session.get(AttributionReport, run_id)
        if report is None:
            raise HTTPException(
                status_code=404,
                detail=f"No AttributionReport for run {run_id}",
            )
        listing = await session.get(Listing, report.listing_id)
        return _report_out(
            report,
            ticker=listing.ticker if listing else None,
        )


def _report_out(
    r: AttributionReport,
    *,
    ticker: str | None,
) -> AttributionReportOut:
    return AttributionReportOut(
        run_id=r.pipeline_run_id,
        listing_id=r.listing_id,
        ticker=ticker,
        analysis_date=r.analysis_date,
        price_change_pct=float(r.price_change_pct),
        narrative=r.narrative,
        valuation_delta=r.valuation_delta,
        triggering_events=cast(
            "list[dict[str, object]]",
            list(r.triggering_events),
        ),
        beta_attribution=dict(r.beta_attribution),
        markdown_path=r.markdown_path,
    )
