"""Durable queue API for evidence-first A-share research runs."""

from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.dashboard import StockSnapshotSummary
from investment_agent.api.schemas.research import (
    ResearchArtifactOut,
    ResearchChangeSetOut,
    ResearchRunAccepted,
    ResearchRunDetail,
    ResearchRunResult,
    ResearchRunSummary,
    SingleStockResearchRequest,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import (
    PipelineRun,
    ResearchChangeSet,
    ResearchSnapshot,
)
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)
from investment_agent.storage.repositories.research_change import (
    ResearchChangeRepository,
)
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
    score_item_from_payload,
)

router = APIRouter(
    prefix="/research",
    tags=["research"],
    dependencies=[Depends(verify_api_key_or_session)],
)


@router.get("/runs", response_model=list[ResearchRunSummary])
async def list_research_runs(
    run_status: str | None = Query(None, alias="status", max_length=16),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[ResearchRunSummary]:
    async with session_scope() as session:
        statement = (
            select(PipelineRun, ResearchSnapshot, ResearchChangeSet)
            .outerjoin(
                ResearchSnapshot,
                ResearchSnapshot.pipeline_run_id == PipelineRun.id,
            )
            .outerjoin(
                ResearchChangeSet,
                ResearchChangeSet.current_snapshot_id == ResearchSnapshot.id,
            )
            .where(PipelineRun.kind == "ifind_research")
            .order_by(PipelineRun.id.desc())
            .offset(offset)
            .limit(limit)
        )
        if run_status is not None:
            statement = statement.where(PipelineRun.status == run_status)
        rows = (await session.execute(statement)).all()
        return [_to_summary(row[0], row[1], row[2]) for row in rows]


@router.get("/stocks/{ticker}/history", response_model=list[StockSnapshotSummary])
async def stock_snapshot_history(
    ticker: str,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[StockSnapshotSummary]:
    """Bounded, paginated snapshot history for one A-share ticker."""

    parsed = parse_a_share_ticker(ticker)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid A-share ticker: {ticker}",
        )
    symbol, market = parsed
    async with session_scope() as session:
        snapshots = await ResearchSnapshotRepository(session).history(
            symbol=symbol,
            market=market.suffix,
            limit=limit,
            offset=offset,
        )
        summaries: list[StockSnapshotSummary] = []
        for snapshot in snapshots:
            change_set = await ResearchChangeRepository(session).by_snapshot(snapshot.id)
            item = score_item_from_payload(snapshot.score_json)
            summaries.append(
                StockSnapshotSummary(
                    run_id=snapshot.pipeline_run_id,
                    as_of=snapshot.as_of,
                    report_year=snapshot.report_year,
                    strategy_label=_optional_str(item.get("strategy_label")),
                    investment_score=_optional_float(item.get("investment_score")),
                    business_score=_optional_float(item.get("business_score")),
                    research_confidence_score=_optional_float(
                        item.get("research_confidence_score")
                    ),
                    needs_agent_review=(
                        item["needs_agent_review"]
                        if isinstance(item.get("needs_agent_review"), bool)
                        else None
                    ),
                    canonical_schema_version=snapshot.canonical_schema_version,
                    scoring_version=snapshot.scoring_version,
                    score_schema_version=snapshot.score_schema_version,
                    has_meaningful_changes=(
                        change_set.has_meaningful_changes if change_set is not None else None
                    ),
                    meaningful_change_count=(
                        change_set.meaningful_change_count if change_set is not None else None
                    ),
                    created_at=snapshot.created_at,
                )
            )
        return summaries


@router.get("/runs/{run_id}", response_model=ResearchRunDetail)
async def get_research_run(run_id: int) -> ResearchRunDetail:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        if run is None or run.kind != "ifind_research":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Research run {run_id} not found",
            )
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
        change_set = await ResearchChangeRepository(session).by_run(run_id)
        summary = _to_summary(run, snapshot, change_set)
        result = _to_result(snapshot) if snapshot is not None else None
        return ResearchRunDetail(
            **summary.model_dump(),
            request=run.request_json,
            result=result,
            change=(
                _to_change_out(change_set, run_id=run_id)
                if change_set is not None
                else None
            ),
            industry_peer_context=(
                snapshot.industry_peer_context if snapshot is not None else None
            ),
        )


@router.get(
    "/runs/{run_id}/changes",
    response_model=ResearchChangeSetOut,
)
async def get_research_changes(run_id: int) -> ResearchChangeSetOut:
    async with session_scope() as session:
        change_set = await ResearchChangeRepository(session).by_run(run_id)
        if change_set is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Research changes for run {run_id} not found",
            )
        return _to_change_out(change_set, run_id=run_id)


@router.get("/runs/{run_id}/change-notification")
async def download_change_notification(
    run_id: int,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    async with session_scope() as session:
        change_set = await ResearchChangeRepository(session).by_run(run_id)
        if change_set is None or change_set.notification_path is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Research change notification for run {run_id} not found",
            )
        path = _resolve_notification_path(
            settings=settings,
            run_id=run_id,
            raw_path=change_set.notification_path,
        )
        return FileResponse(path, filename=path.name)


@router.get(
    "/runs/{run_id}/artifacts",
    response_model=list[ResearchArtifactOut],
)
async def list_research_artifacts(run_id: int) -> list[ResearchArtifactOut]:
    async with session_scope() as session:
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Research artifacts for run {run_id} not found",
            )
        return _artifact_outputs(run_id, snapshot.artifact_manifest)


@router.get("/runs/{run_id}/artifacts/{artifact_name}")
async def download_research_artifact(
    run_id: int,
    artifact_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    async with session_scope() as session:
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Research artifacts for run {run_id} not found",
            )
        path = _resolve_artifact_path(
            settings=settings,
            run_id=run_id,
            artifact_name=artifact_name,
            manifest=snapshot.artifact_manifest,
        )
        return FileResponse(path, filename=path.name)


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

    if body.provider == "ifind" and not settings.ifind_proxy_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IFIND_PROXY_API_KEY is not configured",
        )

    request_json: dict[str, object] = body.model_dump(mode="json")
    request_json["as_of"] = (body.as_of or shanghai_today()).isoformat()
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
        status_url=f"/research/runs/{run.id}",
        result_url=f"/research/runs/{run.id}",
    )


def _to_summary(
    run: PipelineRun,
    snapshot: ResearchSnapshot | None,
    change_set: ResearchChangeSet | None,
) -> ResearchRunSummary:
    request = run.request_json
    ticker = _optional_str(request.get("ticker"))
    industry = _optional_str(request.get("industry"))
    report_year = _optional_int(request.get("report_year"))
    as_of = _optional_date(request.get("as_of"))
    company_name: str | None = None
    business_score: float | None = None
    workflow_label: str | None = None
    needs_agent_review: bool | None = None
    if snapshot is not None:
        ticker = f"{snapshot.symbol}.{snapshot.market}"
        company_name = snapshot.company_name
        industry = snapshot.industry
        report_year = snapshot.report_year
        as_of = snapshot.as_of
        business_score = _optional_float(snapshot.score_json.get("business_score"))
        workflow_label = _optional_str(snapshot.score_json.get("suggestion"))
        review_value = snapshot.score_json.get("needs_agent_review")
        needs_agent_review = review_value if isinstance(review_value, bool) else None
    return ResearchRunSummary(
        run_id=run.id,
        status=run.status,
        stage=run.stage,
        ticker=ticker,
        company_name=company_name,
        industry=industry,
        as_of=as_of,
        report_year=report_year,
        business_score=business_score,
        workflow_label=workflow_label,
        needs_agent_review=needs_agent_review,
        has_meaningful_changes=(
            change_set.has_meaningful_changes if change_set is not None else None
        ),
        meaningful_change_count=(
            change_set.meaningful_change_count if change_set is not None else None
        ),
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
        worker_id=run.worker_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_summary=run.error_summary,
    )


def _to_result(snapshot: ResearchSnapshot) -> ResearchRunResult:
    return ResearchRunResult(
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
        artifacts=_artifact_outputs(snapshot.pipeline_run_id, snapshot.artifact_manifest),
    )


def _to_change_out(
    change_set: ResearchChangeSet,
    *,
    run_id: int,
) -> ResearchChangeSetOut:
    value = ResearchChangeSetOut.model_validate(change_set.diff_json)
    if change_set.notification_path is None:
        return value
    return value.model_copy(
        update={
            "notification_url": f"/research/runs/{run_id}/change-notification"
        }
    )


def _resolve_notification_path(
    *,
    settings: Settings,
    run_id: int,
    raw_path: str,
) -> Path:
    allowed_root = (settings.data_dir / "notifications").resolve()
    path = Path(raw_path).resolve()
    if not path.is_relative_to(allowed_root) or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Research change notification for run {run_id} is unavailable",
        )
    return path


def _artifact_outputs(
    run_id: int,
    manifest: dict[str, object],
) -> list[ResearchArtifactOut]:
    return [
        ResearchArtifactOut(
            name=name,
            download_url=f"/research/runs/{run_id}/artifacts/{name}",
        )
        for name, value in sorted(manifest.items())
        if isinstance(value, str)
    ]


def _resolve_artifact_path(
    *,
    settings: Settings,
    run_id: int,
    artifact_name: str,
    manifest: dict[str, object],
) -> Path:
    raw_path = manifest.get(artifact_name)
    if not isinstance(raw_path, str):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact {artifact_name!r} not found for run {run_id}",
        )
    allowed_root = (settings.data_dir / "research-runs" / str(run_id)).resolve()
    path = Path(raw_path).resolve()
    if not path.is_relative_to(allowed_root) or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact {artifact_name!r} is unavailable for run {run_id}",
        )
    return path


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _optional_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
