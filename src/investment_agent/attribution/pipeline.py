"""AttributionPipeline orchestrator."""

from datetime import date

import structlog
from sqlalchemy import select

from investment_agent.attribution import steps
from investment_agent.attribution.attribution_agent import AttributionAgent
from investment_agent.config import Settings
from investment_agent.domain.protocols import DataSource
from investment_agent.domain.strategy_types import AttributionContext
from investment_agent.pipelines.steps import _build_listing_snapshots
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)


class AttributionPipeline:
    def __init__(
        self,
        *,
        data_source: DataSource,
        agent: AttributionAgent,
        settings: Settings,
        benchmark_ticker: str = "SPY",
    ) -> None:
        self._data_source = data_source
        self._agent = agent
        self._settings = settings
        self._benchmark_ticker = benchmark_ticker

    async def run(
        self,
        ticker: str,
        as_of: date,
        market_code: str = "NASDAQ",
        *,
        run_id: int | None = None,
    ) -> int:
        """Run Attribution end-to-end for one ticker. Returns pipeline_run_id.

        ``run_id`` adopts a PipelineRun the caller already created; otherwise
        one is created here.
        """
        # 0. Resolve listing + create PipelineRun
        async with session_scope() as session:
            stmt = select(Listing).where(
                Listing.market_code == market_code,
                Listing.ticker == ticker,
            )
            listing = (await session.execute(stmt)).scalar_one_or_none()
            if listing is None:
                raise ValueError(f"Listing not found: {market_code}/{ticker}")
            listing_id = listing.id
            if run_id is None:
                run = await PipelineRunRepository(session).create(
                    kind="attribution",
                    listing_id=listing.id,
                    triggered_by="api",
                )
                run_id = run.id

        log.info(
            "attribution.run.started",
            run_id=run_id,
            ticker=ticker,
            as_of=as_of.isoformat(),
        )

        try:
            # 1. Data ingestion
            async with session_scope() as session:
                listing = (
                    await session.execute(
                        select(Listing).where(Listing.id == listing_id)
                    )
                ).scalar_one()
                listing, benchmark, sector_etf = await steps.attr_step1_data_ingestion(
                    session,
                    listing=listing,
                    data_source=self._data_source,
                    as_of=as_of,
                    benchmark_ticker=self._benchmark_ticker,
                )
                if benchmark is None:
                    raise RuntimeError(
                        f"Benchmark {self._benchmark_ticker} not in DB. "
                        f"Run `ia data backfill --tickers {self._benchmark_ticker}` first."
                    )
                benchmark_id = benchmark.id
                sector_etf_id = sector_etf.id if sector_etf else None

            # 2. beta decomposition + snapshot
            async with session_scope() as session:
                listing = (
                    await session.execute(
                        select(Listing).where(Listing.id == listing_id)
                    )
                ).scalar_one()
                benchmark = (
                    await session.execute(
                        select(Listing).where(Listing.id == benchmark_id)
                    )
                ).scalar_one()
                sector_etf = None
                if sector_etf_id is not None:
                    sector_etf = (
                        await session.execute(
                            select(Listing).where(Listing.id == sector_etf_id)
                        )
                    ).scalar_one()
                beta = await steps.attr_step2_beta_decomposition(
                    session,
                    listing=listing,
                    benchmark=benchmark,
                    sector_etf=sector_etf,
                    as_of=as_of,
                )
                snapshots = await _build_listing_snapshots(session, [listing], as_of)
                snapshot = snapshots[0]

            # 3. LLM Attribution
            async with session_scope() as session:
                context = AttributionContext(
                    listing=snapshot,
                    beta=beta,
                    recent_news=[],
                    recent_ratings=[],
                    recent_earnings=[],
                    related_companies=[],
                )
                attribution = await steps.attr_step3_llm_attribution(
                    session,
                    context=context,
                    agent=self._agent,
                    as_of=as_of,
                )

            # 4. Render + persist
            async with session_scope() as session:
                listing = (
                    await session.execute(
                        select(Listing).where(Listing.id == listing_id)
                    )
                ).scalar_one()
                reports_dir = self._settings.data_dir / "reports"
                await steps.attr_step4_render_and_persist(
                    session,
                    pipeline_run_id=run_id,
                    listing=listing,
                    beta=beta,
                    result=attribution,
                    as_of=as_of,
                    reports_dir=reports_dir,
                )

            async with session_scope() as session:
                await PipelineRunRepository(session).mark_success(run_id)

        except Exception as e:
            log.exception("attribution.failed", run_id=run_id)
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_failed(run_id, str(e))
            raise

        log.info("attribution.run.complete", run_id=run_id)
        return run_id
