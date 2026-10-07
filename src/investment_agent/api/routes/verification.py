"""Read-only iFinD verification request listing (P1-A13 status for the P1-E1 page).

``GET /verification/requests`` - newest first, filtered by ``listing_id``,
``ticker`` and / or ``status``. Auth: ``X-API-Key`` or the web session.
Nothing here enqueues or changes a request; the queue is driven by the
promote flow, the collectors and the scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.verification import (
    VerificationRequestOut,
    VerificationRequestPageOut,
    VerificationResultSummary,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.verification import VerificationRequest

router = APIRouter(
    prefix="/verification",
    tags=["verification"],
    dependencies=[Depends(verify_api_key_or_session)],
)

_STATUS_PATTERN = r"^(pending|deferred|running|done|failed|superseded)$"
_TICKER_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and not isinstance(value, dict | list) else None


def result_summary(raw: dict[str, Any] | None) -> VerificationResultSummary | None:
    """The outcome keys of ``verification_request.result`` (no raw bodies)."""

    if not raw:
        return None
    conflicts = _str_list(raw.get("conflicts"))
    raw_fields = raw.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    superseded = raw.get("superseded_by")
    return VerificationResultSummary(
        report_period=_optional_str(raw.get("report_period")),
        verified=_str_list(raw.get("verified")),
        conflicts=conflicts,
        unavailable=_str_list(raw.get("unavailable")),
        filled=_str_list(raw.get("filled")),
        conflict_fields={
            name: dict(fields[name])
            for name in conflicts
            if isinstance(fields.get(name), dict)
        },
        note=_optional_str(raw.get("note")),
        error=_optional_str(raw.get("error")),
        deferred=_optional_str(raw.get("deferred")),
        superseded_by=superseded if isinstance(superseded, int) else None,
    )


def _out(row: VerificationRequest) -> VerificationRequestOut:
    requested = _utc(row.requested_at)
    assert requested is not None
    return VerificationRequestOut(
        id=row.id,
        listing_id=row.listing_id,
        ticker=row.ticker,
        trigger=row.trigger,
        status=row.status,
        priority=row.priority,
        fields=list(row.fields) if row.fields is not None else None,
        report_period=row.report_period,
        requested_at=requested,
        not_before=_utc(row.not_before),
        finished_at=_utc(row.finished_at),
        attempts=row.attempts,
        run_id=row.run_id,
        result_summary=result_summary(row.result),
    )


@router.get("/requests", response_model=VerificationRequestPageOut)
async def list_verification_requests(
    listing_id: Annotated[int | None, Query(ge=1)] = None,
    ticker: Annotated[str | None, Query(pattern=_TICKER_PATTERN)] = None,
    request_status: Annotated[str | None, Query(alias="status", pattern=_STATUS_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VerificationRequestPageOut:
    conditions: list[Any] = []
    if listing_id is not None:
        conditions.append(VerificationRequest.listing_id == listing_id)
    if ticker is not None:
        conditions.append(VerificationRequest.ticker == ticker)
    if request_status is not None:
        conditions.append(VerificationRequest.status == request_status)
    async with session_scope() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(VerificationRequest).where(*conditions)
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(VerificationRequest)
                    .where(*conditions)
                    .order_by(VerificationRequest.requested_at.desc(), VerificationRequest.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items = [_out(row) for row in rows]
    return VerificationRequestPageOut(items=items, total=int(total), limit=limit)
