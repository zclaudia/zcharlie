"""Verification request queue (P1-A13): enqueue with dedup / merge, due selection.

Triggers in priority order (p1-plan 2.3): ``promote`` (before a candidate is
promoted / held) > ``conflict`` (key-field conflict between free sources) >
``missing`` (no free source has a key field) > ``exit_review`` (P2) >
``calibration`` (weekly sample; first to wait when the budget is tight).

Dedup: at most one open (``pending`` / ``deferred``) request per
``(listing, trigger, report period)`` - ``report_period=None`` meaning "the
latest annual period" is its own key. A repeated request merges into the open
one: fields are united (``None`` = all key fields absorbs any list), detail
keys are updated, and the request keeps its place in the queue.

A request becomes *due* when it is ``pending``, or ``deferred`` with
``not_before <= now``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.verification import (
    OPEN_STATUSES,
    VERIFICATION_TRIGGERS,
    VerificationRequest,
)
from investment_agent.storage.repositories.pit import to_utc

log = structlog.get_logger(__name__)

TRIGGER_PRIORITY: dict[str, int] = {
    "promote": 50,
    "conflict": 40,
    "missing": 30,
    "exit_review": 20,
    "calibration": 10,
}
assert tuple(TRIGGER_PRIORITY) == VERIFICATION_TRIGGERS


def dedup_key(listing_id: int, trigger: str, report_period: date | None) -> str:
    period = report_period.isoformat() if report_period is not None else "latest"
    return f"{listing_id}:{trigger}:{period}"


def merge_fields(
    current: Sequence[str] | None, extra: Sequence[str] | None
) -> list[str] | None:
    if current is None or extra is None:
        return None
    return sorted(set(current) | set(extra))


def _normalize_fields(fields: Sequence[str] | None) -> list[str] | None:
    if fields is None:
        return None
    if isinstance(fields, str):
        raise TypeError("fields must be a sequence of field names, not a string")
    names = sorted({str(name).strip() for name in fields if str(name).strip()})
    if not names:
        raise ValueError("fields must name at least one field (or be None for all key fields)")
    return names


def _jsonable(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    loaded = json.loads(json.dumps(dict(detail), default=str, ensure_ascii=False))
    return loaded if isinstance(loaded, dict) else {}


async def _open_request(session: AsyncSession, key: str) -> VerificationRequest | None:
    stmt = select(VerificationRequest).where(
        VerificationRequest.dedup_key == key,
        VerificationRequest.status.in_(OPEN_STATUSES),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _merge_into(
    request: VerificationRequest,
    *,
    fields: list[str] | None,
    detail: dict[str, Any],
    ticker: str,
    source_run_id: int | None,
) -> None:
    request.fields = merge_fields(request.fields, fields)
    if detail:
        request.detail = {**(request.detail or {}), **detail}
    request.priority = max(request.priority, TRIGGER_PRIORITY[request.trigger])
    request.ticker = ticker
    if source_run_id is not None and request.source_run_id is None:
        request.source_run_id = source_run_id


async def _enqueue(
    session: AsyncSession,
    *,
    listing_id: int,
    ticker: str,
    trigger: str,
    fields: list[str] | None,
    report_period: date | None,
    detail: dict[str, Any],
    run_id: int | None,
    now: datetime,
) -> bool:
    key = dedup_key(listing_id, trigger, report_period)
    existing = await _open_request(session, key)
    if existing is not None:
        _merge_into(existing, fields=fields, detail=detail, ticker=ticker, source_run_id=run_id)
        await session.flush()
        return False
    try:
        async with session.begin_nested():
            session.add(
                VerificationRequest(
                    listing_id=listing_id,
                    ticker=ticker,
                    trigger=trigger,
                    priority=TRIGGER_PRIORITY[trigger],
                    fields=fields,
                    report_period=report_period,
                    detail=detail,
                    status="pending",
                    dedup_key=key,
                    requested_at=to_utc(now),
                    attempts=0,
                    source_run_id=run_id,
                )
            )
    except IntegrityError:
        # A concurrent enqueue created the open request first: merge into it.
        existing = await _open_request(session, key)
        if existing is None:
            raise
        _merge_into(existing, fields=fields, detail=detail, ticker=ticker, source_run_id=run_id)
        await session.flush()
        return False
    return True


async def enqueue_verification(
    *,
    listing_id: int,
    ticker: str,
    trigger: str,
    fields: Sequence[str] | None = None,
    report_period: date | None = None,
    detail: Mapping[str, Any] | None = None,
    run_id: int | None = None,
    session: AsyncSession | None = None,
    now: datetime | None = None,
) -> bool:
    """Queue an iFinD verification of ``ticker``; ``True`` when a new request was created.

    * ``fields`` - factor-layer field names; ``None`` = every key field of
      ``config/field_sources.yaml`` plus the latest close;
    * ``report_period`` - ``None`` = the latest annual period when the job runs;
    * ``run_id`` - the run asking for it (stored as ``source_run_id``);
    * ``session`` - insert inside the caller's transaction (e.g. the promote
      flow); otherwise a new transaction is opened and committed.

    An open request with the same (listing, trigger, period) absorbs the new
    one (``False``).
    """

    if trigger not in TRIGGER_PRIORITY:
        raise ValueError(f"unknown verification trigger {trigger!r}; one of {VERIFICATION_TRIGGERS}")
    names = _normalize_fields(fields)
    payload = _jsonable(detail)
    stamp = now or datetime.now(UTC)
    kwargs: dict[str, Any] = {
        "listing_id": listing_id,
        "ticker": ticker,
        "trigger": trigger,
        "fields": names,
        "report_period": report_period,
        "detail": payload,
        "run_id": run_id,
        "now": stamp,
    }
    if session is not None:
        created = await _enqueue(session, **kwargs)
    else:
        async with session_scope() as own:
            created = await _enqueue(own, **kwargs)
    log.info(
        "verification.enqueued" if created else "verification.merged",
        listing_id=listing_id,
        ticker=ticker,
        trigger=trigger,
        report_period=report_period.isoformat() if report_period else None,
        fields=names,
    )
    return created


def _due_clause(now: datetime) -> Any:
    return or_(
        VerificationRequest.status == "pending",
        and_(
            VerificationRequest.status == "deferred",
            or_(
                VerificationRequest.not_before.is_(None),
                VerificationRequest.not_before <= to_utc(now),
            ),
        ),
    )


async def due_requests(
    session: AsyncSession, *, now: datetime, limit: int | None = None
) -> list[VerificationRequest]:
    """Due requests by priority (high first), then request time."""

    stmt = (
        select(VerificationRequest)
        .where(_due_clause(now))
        .order_by(
            VerificationRequest.priority.desc(),
            VerificationRequest.requested_at,
            VerificationRequest.id,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars())


async def count_due(session: AsyncSession, *, now: datetime) -> int:
    stmt = select(func.count()).select_from(VerificationRequest).where(_due_clause(now))
    return int((await session.execute(stmt)).scalar_one())


async def release_stale_running(session: AsyncSession, *, now: datetime) -> int:
    """Return ``running`` requests whose run is no longer running to the queue.

    A worker crash leaves them ``running``; they become ``pending`` again
    (or ``superseded`` when another open request took their dedup key).
    """

    stmt = (
        select(VerificationRequest)
        .outerjoin(PipelineRun, PipelineRun.id == VerificationRequest.run_id)
        .where(
            VerificationRequest.status == "running",
            or_(PipelineRun.id.is_(None), PipelineRun.status.not_in(("running", "queued"))),
        )
    )
    released = 0
    for request in (await session.execute(stmt)).scalars().all():
        other = await _open_request(session, request.dedup_key)
        if other is not None:
            other.fields = merge_fields(other.fields, request.fields)
            request.status = "superseded"
            request.result = {"superseded_by": other.id, "reason": "stale running request"}
            request.finished_at = to_utc(now)
        else:
            request.status = "pending"
            request.not_before = None
        released += 1
        await session.flush()
    return released


async def queue_summary(session: AsyncSession) -> dict[str, dict[str, int]]:
    """``{status: {trigger: count}}`` over every request."""

    stmt = select(
        VerificationRequest.status, VerificationRequest.trigger, func.count()
    ).group_by(VerificationRequest.status, VerificationRequest.trigger)
    summary: dict[str, dict[str, int]] = {}
    for status, trigger, count in (await session.execute(stmt)).all():
        summary.setdefault(str(status), {})[str(trigger)] = int(count)
    return summary
