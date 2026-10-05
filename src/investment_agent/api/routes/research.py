"""Synchronous MVP API for one evidence-first A-share research run."""

from datetime import date
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from investment_agent.api.auth import get_settings, verify_api_key
from investment_agent.api.schemas.research import (
    ResearchSnapshotResponse,
    SingleStockResearchRequest,
    SingleStockResearchResponse,
)
from investment_agent.config import Settings
from investment_agent.research.ifind_chain import run_ifind_research_chain
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)

router = APIRouter(
    prefix="/research",
    tags=["research"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("/runs/{run_id}", response_model=ResearchSnapshotResponse)
async def get_research_snapshot(run_id: int) -> ResearchSnapshotResponse:
    async with session_scope() as session:
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Research snapshot for run {run_id} not found",
            )
        return ResearchSnapshotResponse(
            run_id=snapshot.pipeline_run_id,
            ticker=f"{snapshot.symbol}.{snapshot.market}",
            company_name=snapshot.company_name,
            industry=snapshot.industry,
            as_of=snapshot.as_of,
            report_year=snapshot.report_year,
            canonical_schema_version=snapshot.canonical_schema_version,
            scoring_version=snapshot.scoring_version,
            score_schema_version=snapshot.score_schema_version,
            score=snapshot.score_json,
            explanation=snapshot.explanation_json,
            artifacts=snapshot.artifact_manifest,
        )


@router.post(
    "/single-stock",
    response_model=SingleStockResearchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def research_single_stock(
    body: SingleStockResearchRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SingleStockResearchResponse:
    """Run the complete deterministic-score plus cited-explanation vertical slice.

    The MVP intentionally keeps this request synchronous.  A durable worker queue is
    an MVP exit criterion and will replace the request-bound execution model.
    """

    if not settings.ifind_proxy_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IFIND_PROXY_API_KEY is not configured",
        )

    try:
        result = await run_ifind_research_chain(
            settings=settings,
            proxy_url=settings.ifind_proxy_url,
            proxy_api_key=settings.ifind_proxy_api_key,
            ticker=body.ticker,
            industry=body.industry,
            report_year=body.report_year,
            as_of=body.as_of or date.today(),
            output_root=settings.data_dir / "research-runs",
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iFinD data gateway request failed",
        ) from exc

    report_path = result.artifact_dir / "agent_report.md"
    return SingleStockResearchResponse(
        run_id=result.run_id,
        status="success",
        ticker=f"{result.score['symbol']}.{result.score['market']}",
        company_name=str(result.score["name"]),
        business_score=float(result.score["business_score"]),
        research_confidence_score=float(result.score["research_confidence_score"]),
        workflow_label=str(result.score["suggestion"]),
        needs_agent_review=bool(result.score["needs_agent_review"]),
        artifact_dir=str(result.artifact_dir.resolve()),
        report_path=str(report_path.resolve()),
    )
