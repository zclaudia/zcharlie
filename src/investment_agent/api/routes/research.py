"""Durable queue API for evidence-first A-share research runs."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from investment_agent.api.auth import get_settings, verify_api_key
from investment_agent.api.schemas.research import (
    ResearchRunAccepted,
    ResearchSnapshotResponse,
    SingleStockResearchRequest,
)
from investment_agent.config import Settings
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)
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
    response_model=ResearchRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def research_single_stock(
    body: SingleStockResearchRequest,
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
) -> ResearchRunAccepted:
    """Persist a single-stock research request for an independent worker."""

    if not settings.ifind_proxy_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IFIND_PROXY_API_KEY is not configured",
        )

    request_json: dict[str, object] = body.model_dump(mode="json")
    request_json["as_of"] = (body.as_of or date.today()).isoformat()
    try:
        async with session_scope() as session:
            run, created = await PipelineRunRepository(session).enqueue(
                kind="ifind_research",
                idempotency_key=idempotency_key,
                triggered_by="api",
                request_json=request_json,
            )
    except IdempotencyPayloadMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return ResearchRunAccepted(
        run_id=run.id,
        status=run.status,
        stage=run.stage,
        created=created,
        status_url=f"/pipelines/{run.id}",
        result_url=f"/research/runs/{run.id}",
    )
