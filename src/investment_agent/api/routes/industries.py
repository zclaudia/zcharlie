"""Independent read-only API for saved industry peer batches."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.industries import (
    IndustryListOut,
    IndustryOverviewOut,
    IndustrySummaryOut,
)
from investment_agent.config import Settings
from investment_agent.industry_peers.contracts import FEATURE_DISABLED_GAP
from investment_agent.industry_peers.overview import industry_overview, list_saved_industries
from investment_agent.industry_peers.service import PeerTargetLookupError
from investment_agent.storage.database import session_scope

router = APIRouter(prefix="/industries", tags=["industries"], dependencies=[Depends(verify_api_key_or_session)])


@router.get("", response_model=IndustryListOut)
async def industries(settings: Annotated[Settings, Depends(get_settings)]) -> IndustryListOut:
    if not settings.industry_peer_context_enabled:
        return IndustryListOut(enabled=False, gaps=[FEATURE_DISABLED_GAP])
    async with session_scope() as session:
        rows = await list_saved_industries(session, data_dir=settings.data_dir, owner_id="local_owner")
    return IndustryListOut(
        enabled=True,
        industries=[IndustrySummaryOut.model_validate(row) for row in rows],
    )


@router.get("/{cohort_key}", response_model=IndustryOverviewOut)
async def industry_detail(
    cohort_key: Annotated[str, PathParam(min_length=1, max_length=64)],
    settings: Annotated[Settings, Depends(get_settings)],
    batch_run_id: Annotated[str | None, Query(max_length=128)] = None,
    report_period: Annotated[date | None, Query()] = None,
) -> IndustryOverviewOut:
    if not settings.industry_peer_context_enabled:
        return IndustryOverviewOut(
            enabled=False,
            cohort_key=cohort_key,
            gaps=[FEATURE_DISABLED_GAP],
        )
    try:
        async with session_scope() as session:
            payload = await industry_overview(
                session,
                cohort_key=cohort_key,
                data_dir=settings.data_dir,
                report_period=report_period,
                batch_run_id=batch_run_id,
                owner_id="local_owner",
            )
    except PeerTargetLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IndustryOverviewOut(**payload)
