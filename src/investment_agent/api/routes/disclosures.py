"""Authenticated bounded discovery and attachment-only immutable original readback."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import AwareDatetime

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import DisclosureError
from investment_agent.evidence.disclosures import DisclosureService, DiscoveryRequest, FetchRequest
from investment_agent.evidence.documents import OriginalRequest, parse_staged, readback, stage
from investment_agent.evidence.search import SearchRequest, search_saved
from investment_agent.investor.common import Contract
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/investor/disclosures",
    tags=["disclosures"],
    dependencies=[Depends(verify_api_key_or_session)],
)


@router.get("/originals/{original_id}/fragments")
async def fragments(
    original_id: UUID,
    listing_id: Annotated[int, Query(ge=1)],
    as_of: AwareDatetime,
    offset: Annotated[int, Query(ge=0, le=500)] = 0,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    historical_reconstruction: bool = False,
) -> dict[str, Any]:
    request = OriginalRequest(
        listing_id=listing_id, original_id=original_id, as_of=as_of, offset=offset, limit=limit
    )
    try:
        async with session_scope() as session:
            payload, body = await stage(session, request, historical=historical_reconstruction)
        parsed = await parse_staged(body, payload["mime"])
        result = readback(payload, parsed, request)
        result["historical_reconstruction"] = historical_reconstruction
        return result
    except DisclosureError as exc:
        raise HTTPException(404, str(exc)) from exc


class DiscoverBody(Contract):
    request: DiscoveryRequest
    idempotency_key: UUID


class FetchBody(Contract):
    request: FetchRequest
    idempotency_key: UUID


async def execute(body: DiscoverBody | FetchBody, settings: Settings) -> dict[str, Any]:
    from investment_agent.investor.foundations import InvestorError

    try:
        async with session_scope() as session:
            service = DisclosureService(session)
            # Feature shutdown preserves exact immutable receipts and history.
            replay = await service._replay(body.idempotency_key, body.request)
            if replay is not None:
                return replay
            if not settings.disclosure_originals_enabled:
                raise HTTPException(409, "disclosure_originals_disabled")
            return await service.execute(body.idempotency_key, body.request)
    except InvestorError as exc:
        raise HTTPException(409, exc.detail.model_dump(mode="json")) from exc


@router.post("/discover")
async def discover(
    body: DiscoverBody, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    return await execute(body, settings)


@router.post("/fetch")
async def fetch(
    body: FetchBody, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    return await execute(body, settings)


@router.get("")
async def metadata(
    listing_id: Annotated[int, Query(ge=1)],
    as_of: AwareDatetime,
    historical_reconstruction: bool = False,
) -> dict[str, Any]:
    async with session_scope() as session:
        return await DisclosureService(session).metadata(
            listing_id, as_of, historical_reconstruction=historical_reconstruction
        )


@router.get("/originals/{original_id}/download")
async def download(
    original_id: UUID, as_of: AwareDatetime, historical_reconstruction: bool = False
) -> Response:
    try:
        async with session_scope() as session:
            payload, body = await DisclosureService(session).read(
                original_id, as_of, historical_reconstruction=historical_reconstruction
            )
    except DisclosureError as exc:
        raise HTTPException(404, str(exc)) from exc
    extension = "pdf" if payload["mime"] == "application/pdf" else "html"
    return Response(
        body,
        media_type=payload["mime"],
        headers={
            "Content-Disposition": f'attachment; filename="{original_id}.{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
            "ETag": '"' + payload["content_hash"] + '"',
            "X-Historical-Reconstruction": str(historical_reconstruction).lower(),
        },
    )


@router.post("/search")
async def search_sources(body: SearchRequest) -> dict[str, Any]:
    async with session_scope() as session:
        return await search_saved(session, body)
