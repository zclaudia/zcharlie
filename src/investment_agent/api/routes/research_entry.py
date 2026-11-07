"""Authenticated G0 preflight. No task, grant, receipt or budget is written."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends

from investment_agent.api.auth import get_bootstrap_settings, verify_api_key_or_session
from investment_agent.api.deps import raise_http_for_investor_error
from investment_agent.api.schemas.research_entry import ResearchEntryInput, ResearchEntryResponse
from investment_agent.config import Settings
from investment_agent.investor.foundations import InvestorError
from investment_agent.research_quality.entry import ResearchEntryService
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/investor/research-entry",
    tags=["research-entry"],
    dependencies=[Depends(verify_api_key_or_session)],
)


async def entry_service(
    base: Annotated[Settings, Depends(get_bootstrap_settings)],
) -> AsyncIterator[ResearchEntryService]:
    try:
        async with session_scope() as session:
            yield ResearchEntryService(session, base)
    except InvestorError as exc:
        raise_http_for_investor_error(exc)


@router.post("/preflight", response_model=ResearchEntryResponse)
async def preflight(
    body: ResearchEntryInput,
    svc: Annotated[ResearchEntryService, Depends(entry_service, scope="function")],
) -> ResearchEntryResponse:
    return await svc.preflight(body)
