"""/pipelines endpoints — Discovery run + read."""

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.pipeline import (
    DiscoveryRunRequest,
    PipelineRunCreated,
    PipelineRunOut,
    StockScoreOut,
)
from investment_agent.config import Settings
from investment_agent.pipelines.discovery import DiscoveryPipeline
from investment_agent.sources.yfinance_source import YFinanceSource
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.stock_score import StockScoreRepository
from investment_agent.strategies.registry import load_all_strategies

router = APIRouter(
    prefix="/pipelines",
    tags=["pipelines"],
    dependencies=[Depends(verify_api_key)],
)


async def _run_discovery(universe: str, as_of: date) -> None:
    settings = Settings()  # type: ignore[call-arg]
    strategies = load_all_strategies(
        tier2_config=settings.llm_tier2,
        tier3_config=settings.llm_tier3,
    )
    pipeline = DiscoveryPipeline(
        data_source=YFinanceSource(
            max_concurrent=settings.yfinance_max_concurrent,
        ),  # type: ignore[arg-type]
        strategies=strategies,
        settings=settings,
    )
    await pipeline.run(universe, as_of)


@router.post("/discovery/run", response_model=PipelineRunCreated, status_code=202)
async def run_discovery(
    req: DiscoveryRunRequest,
    background: BackgroundTasks,
    is_async: bool = Query(True, alias="async"),
) -> PipelineRunCreated:
    """Trigger Discovery pipeline.

    `?async=true` (default): pre-creates a PipelineRun, schedules the work,
    and returns immediately with the run_id (HTTP 202).

    `?async=false`: blocks until the pipeline finishes (or raises).
    """
    as_of = req.as_of or date.today()

    async with session_scope() as session:
        run = await PipelineRunRepository(session).create(
            kind="discovery",
            universe_code=req.universe_code,
            triggered_by="api",
        )
        created = PipelineRunCreated(
            run_id=run.id,
            status=run.status,
            started_at=run.started_at,
        )

    if is_async:
        background.add_task(_run_discovery, req.universe_code, as_of)
        return created

    await _run_discovery(req.universe_code, as_of)
    async with session_scope() as session:
        run = (
            await session.execute(
                select(PipelineRun).where(PipelineRun.id == created.run_id),
            )
        ).scalar_one()
        return PipelineRunCreated(
            run_id=run.id,
            status=run.status,
            started_at=run.started_at,
        )


@router.get("", response_model=list[PipelineRunOut])
async def list_runs(
    limit: int = 20,
    kind: str | None = None,
) -> list[PipelineRunOut]:
    async with session_scope() as session:
        stmt = (
            select(PipelineRun)
            .order_by(PipelineRun.started_at.desc())
            .limit(limit)
        )
        if kind:
            stmt = stmt.where(PipelineRun.kind == kind)
        rows = list((await session.execute(stmt)).scalars().all())
        return [_run_to_out(r) for r in rows]


@router.get("/{run_id}", response_model=PipelineRunOut)
async def get_run(run_id: int) -> PipelineRunOut:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"PipelineRun {run_id} not found",
            )
        return _run_to_out(run)


@router.get("/{run_id}/scores", response_model=list[StockScoreOut])
async def get_scores(run_id: int) -> list[StockScoreOut]:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"PipelineRun {run_id} not found",
            )
        scores = await StockScoreRepository(session).by_run(run_id)
        return [
            StockScoreOut(
                listing_id=s.listing_id,
                strategy_code=s.strategy_code,
                tier=s.tier,
                score=s.score,
                confidence=float(s.confidence),
                bull_thesis=s.bull_thesis,
                bear_thesis=s.bear_thesis,
            )
            for s in scores
        ]


def _run_to_out(r: PipelineRun) -> PipelineRunOut:
    return PipelineRunOut(
        run_id=r.id,
        kind=r.kind,
        universe_code=r.universe_code,
        listing_id=r.listing_id,
        status=r.status,
        stage=r.stage,
        attempt_count=r.attempt_count,
        max_attempts=r.max_attempts,
        available_at=r.available_at,
        claimed_at=r.claimed_at,
        worker_id=r.worker_id,
        heartbeat_at=r.heartbeat_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
        error_summary=r.error_summary,
        metadata=r.metadata_json,
    )
