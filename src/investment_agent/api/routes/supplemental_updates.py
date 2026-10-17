"""Authenticated P4 intake, bounded disclosure permissions and operating views."""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from investment_agent.api.auth import (
    get_bootstrap_settings,
    get_settings,
    verify_api_key_or_session,
)
from investment_agent.api.deps import session_service
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.supplemental_updates import (
    SupplementalChangeReceiptRow as Change,
)
from investment_agent.storage.models.supplemental_updates import (
    SupplementalRefreshStateRow as Refresh,
)
from investment_agent.storage.models.supplemental_updates import (
    SupplementalSourceStateRow as Source,
)
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import Context
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationCommand,
)
from investment_agent.supplemental_rules.disclosure_updates import DisclosurePollRequest
from investment_agent.supplemental_rules.read_snapshot import ensure_read_snapshot
from investment_agent.supplemental_rules.update_intake import (
    ImportPreviewRequest,
    ImportRequest,
    aware,
    import_saved_sources,
    resolve_saved_sources,
)

router = APIRouter(tags=["supplemental-updates"], dependencies=[Depends(verify_api_key_or_session)])
session_dep = session_service(lambda session: session)
Session = Annotated[AsyncSession, Depends(session_dep, scope="function")]
Effective = Annotated[Settings, Depends(get_settings)]
Bootstrap = Annotated[Settings, Depends(get_bootstrap_settings)]
PageLimit = Annotated[int, Query(ge=1, le=100)]


@router.post("/supplemental-updates/sources/cninfo_szse_v1/scopes")
async def source_authorization(
    body: BackgroundDisclosureAuthorizationCommand, session: Session,
) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_authorization import (
        apply_authorization_command,
    )

    result = await apply_authorization_command(session, body, actor="api")
    return result.model_dump(mode="json")


@router.get("/supplemental-updates/authorizations/{authorization_id}")
async def authorization_status(authorization_id: UUID, session: Session) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_views import authorization_view

    await ensure_read_snapshot(session)
    return await authorization_view(session, authorization_id)


@router.post("/supplemental-updates/polls", status_code=202)
async def source_poll(body: DisclosurePollRequest, session: Session, settings: Bootstrap) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_updates import admit_disclosure_poll

    return await admit_disclosure_poll(session, body, settings=settings)


@router.get("/supplemental-updates/polls/{poll_id}")
async def source_poll_status(poll_id: UUID, session: Session) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_updates import disclosure_poll_detail

    await ensure_read_snapshot(session)
    detail = await disclosure_poll_detail(session, poll_id)
    if detail is None:
        raise InvestorError("not_found", "来源轮询不存在")
    return detail


@router.post("/supplemental-updates/imports/preview")
async def preview_import(body: ImportPreviewRequest, session: Session, settings: Effective) -> dict[str, Any]:
    await ensure_read_snapshot(session)
    sources = await resolve_saved_sources(session, body.listing_ref, body.source_refs,
                                          settings=settings, now=datetime.now(UTC))
    state = await SupplementalUpdatesRepository(session).get_source(
        "local_owner", "controlled_local_v1", f"listing:{body.listing_ref.id}:{body.context}")
    return {"manifest_hash": canonical_hash({"listing_ref": body.listing_ref, "context": body.context, "sources": sources}),
            "sources": sources, "expected_source_revision": state.revision if state else 0,
            "coverage": "explicit_saved_refs", "verification": "not_promoted"}


@router.post("/supplemental-updates/imports", status_code=202)
async def controlled_import(body: ImportRequest, session: Session, settings: Bootstrap) -> dict[str, Any]:
    return await import_saved_sources(session, body, settings)


def _encode(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(cursor: str | None, binding: dict[str, Any]) -> int:
    if cursor is None:
        return 0
    try:
        if len(cursor) > 2048:
            raise ValueError
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict) or set(data) != {*binding, "after"}:
            raise ValueError
        after = data.pop("after")
        if type(after) is not int or after < 0:
            raise ValueError
        if data != binding:
            raise InvestorError("revision_conflict", "分页集合已变化，请重新读取")
        return after
    except InvestorError:
        raise
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvestorError("invalid_cursor", "持续更新分页标识无效") from exc


def source_view(row: Source, at: datetime) -> dict[str, Any]:
    last_success = aware(row.last_success_at) if row.last_success_at else None
    stale = last_success is None or row.health_limit_seconds <= 0 or (at - last_success).total_seconds() >= row.health_limit_seconds
    invalid = any(aware(value) > at for value in (
        row.last_success_at, row.window_start, row.window_end, row.complete_watermark,
    ) if value is not None)
    current_status = ("unknown" if invalid else "failed" if row.coverage_state == "failed"
                      else row.coverage_state if last_success is None
                      else "stale" if stale else row.coverage_state)
    checkpoint = row.payload.get("poll_checkpoint", {})
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    pending_predecessor = row.payload.get("pending_predecessor")
    poll_progress = {"stage": checkpoint.get("stage"),
                     "indexed_pages": len(checkpoint["primary_pages"])
                     if isinstance(checkpoint.get("primary_pages"), list) else None,
                     "sealed_pages": len(checkpoint["bridge_refs"])
                     if isinstance(checkpoint.get("bridge_refs"), list) else None,
                     "documents_completed": checkpoint.get("document_count"),
                     "pending_originals": len(checkpoint["pending_documents"])
                     if isinstance(checkpoint.get("pending_documents"), list) else None}
    return {"source_id": row.feed_id, "scope_key": row.scope_key, "revision": row.revision,
            "listing_id": row.listing_id, "context": row.context, "contract_version": row.contract_version,
            "contract_hash": row.config_hash, "scope_hash": row.scope_hash,
            "coverage_state": row.coverage_state, "current_status": current_status,
            "coverage": row.payload.get("coverage", "not_assessed"),
            "window_start": aware(row.window_start) if row.window_start else None,
            "window_end": aware(row.window_end) if row.window_end else None,
            "complete_watermark": aware(row.complete_watermark) if row.complete_watermark else None,
            "last_attempt_at": aware(row.last_attempt_at) if row.last_attempt_at else None,
            "last_success_at": last_success, "failure_code": row.failure_code,
            "retry_at": aware(row.retry_at) if row.retry_at else None, "health_limit_seconds": row.health_limit_seconds,
            "success_sequence": row.success_sequence, "source_refs": row.source_refs,
            **({"index_watermark": row.payload.get("index_watermark"),
                "original_watermark": row.payload.get("original_watermark"),
                "bridge_pending": row.payload.get("bridge_pending"),
                "history_start": row.payload.get("history_start"),
                "authorized_history_end": row.payload.get("authorized_history_end"),
                "history_frontier": row.payload.get("history_frontier"),
                "history_gap": row.payload.get("history_gap"),
                "pending_predecessor": (
                    True if isinstance(pending_predecessor, dict)
                    else False if pending_predecessor is None else None
                ),
                "pending_predecessor_details": (
                    pending_predecessor if isinstance(pending_predecessor, dict) else None
                ),
                "poll_progress": poll_progress,
                "protocol_profile": row.payload.get("protocol_profile", "live"),
                "protocol_status": row.payload.get("protocol_status", "unvalidated"),
                "poll_state": row.payload.get("poll_state"),
                "active_poll_id": row.payload.get("active_poll_id"),
                "active_step_run_id": row.payload.get("active_step_run_id")}
               if row.feed_id == "cninfo_szse_v1" else {})}


async def source_views(session: AsyncSession, rows: list[Source], at: datetime) -> list[dict[str, Any]]:
    from investment_agent.supplemental_rules.disclosure_views import (
        source_authorization_view,
        source_schedule_view,
    )

    views = []
    for row in rows:
        view = source_view(row, at)
        if row.feed_id == "cninfo_szse_v1":
            view.update(await source_authorization_view(
                session, {**row.payload, "contract_hash": row.config_hash}, at=at,
            ))
            view.update(await source_schedule_view(session, row.listing_id))
            if view["grant_status"] != "active" or view["protocol_status"] != "live_validated":
                view["current_status"] = "unknown"
            if (view["grant_status"] != "active"
                    and view.get("schedule_status") not in {"disabled", "not_scheduled"}):
                view["schedule_status"] = "blocked"
                view["schedule_blocked_reason"] = "grant_" + view["grant_status"]
        views.append(view)
    return views


async def scope_review_due(session: AsyncSession, listing_id: int, context: Context,
                           sources: list[Source], saved_due: datetime | None) -> datetime | None:
    """Read saved boundaries even if no timer has run; this does not renew them."""
    due = [aware(saved_due)] if saved_due else []
    for source in sources:
        if source.last_success_at and source.health_limit_seconds > 0:
            due.append(aware(source.last_success_at) + timedelta(seconds=source.health_limit_seconds))
    current = InvestmentAssessment
    later = aliased(InvestmentAssessment)
    latest_sequence = select(func.max(later.application_sequence)).where(
        later.owner_id == current.owner_id, later.listing_id == current.listing_id,
        later.series_key == current.series_key, later.application_mode == "live",
    ).correlate(current).scalar_subquery()
    paths = ("$.review_due_at", "$.valid_until", "$.frozen_pair.rule.definition.check_by")
    minima = (await session.execute(select(*[
        func.min(func.julianday(func.json_extract(current.payload, path))) for path in paths
    ]).where(current.owner_id == "local_owner", current.listing_id == listing_id,
             current.kind == "rule_application", current.application_mode == "live",
             current.application_sequence == latest_sequence,
             current.payload["context"].as_string() == context))).one()
    due.extend(datetime.fromtimestamp((value - 2440587.5) * 86400, UTC)
               for value in minima if value is not None)
    return min(due) if due else None


@router.get("/supplemental-updates/sources")
async def sources(session: Session, settings: Effective, cursor: str | None = None,
                  limit: PageLimit = 50) -> dict[str, Any]:
    await ensure_read_snapshot(session)
    scope = (Source.owner_id == "local_owner") & Source.listing_id.is_not(None) & Source.context.is_not(None)
    count, revision_sum = (await session.execute(select(func.count(), func.coalesce(func.sum(Source.revision), 0)).where(scope))).one()
    binding = {"v": 1, "owner": "local_owner", "query": "sources", "count": count, "revision_sum": revision_sum}
    after = _decode(cursor, binding)
    rows = list(await session.scalars(select(Source).where(scope, Source.id > after)
                    .order_by(Source.id).limit(limit + 1)))
    at = datetime.now(UTC)
    return {"enabled": settings.supplemental_updates_enabled, "sources": await source_views(session, rows[:limit], at),
            "next_cursor": _encode({**binding, "after": rows[limit - 1].id}) if len(rows) > limit else None,
            "observed_at": at, "coverage": "declared_scopes_only", "declared_source_count": count,
            "online_polling": "bounded_cninfo_protocol_unvalidated"}


@router.get("/supplemental-updates/scopes/{listing_id}")
async def scope_status(listing_id: int, session: Session, settings: Effective,
                       context: Context = "company_research") -> dict[str, Any]:
    await ensure_read_snapshot(session)
    if await session.get(Listing, listing_id) is None:
        raise InvestorError("not_found", "证券标的不存在")
    row = await session.scalar(select(Refresh).where(Refresh.owner_id == "local_owner", Refresh.listing_id == listing_id, Refresh.context == context))
    source_rows = list(await session.scalars(select(Source).where(Source.owner_id == "local_owner", Source.listing_id == listing_id, Source.context == context).order_by(Source.feed_id)))
    confirmed = row.confirmed_sequence if row else 0
    pending = (await session.execute(select(func.count(), func.min(Change.committed_at)).where(
        Change.owner_id == "local_owner", Change.listing_id == listing_id, Change.context == context,
        Change.kind == "scope_member", Change.sequence > confirmed))).one()
    at = datetime.now(UTC)
    next_due = await scope_review_due(session, listing_id, context, source_rows,
                                      row.next_due_at if row else None)
    active = await session.get(PipelineRun, row.active_run_id) if row and row.active_run_id else None
    latest_outcome_row = await session.scalar(select(Change).where(
        Change.owner_id == "local_owner", Change.listing_id == listing_id,
        Change.context == context, Change.kind == "scope_outcome").order_by(Change.sequence.desc()).limit(1))
    outcome = (await SupplementalUpdatesRepository(session).get_change("local_owner", latest_outcome_row.sequence)
               if latest_outcome_row is not None else None)
    from investment_agent.storage.models.investor import InvestorCase
    from investment_agent.storage.models.monitoring import ReviewCycle
    if context in {"company_research", "holding_review"}:
        case = await session.scalar(select(InvestorCase).where(
            InvestorCase.owner_id == "local_owner", InvestorCase.listing_id == listing_id))
        recommendation = case.payload.get("current_recommendation_ref") if case else None
        if recommendation:
            from investment_agent.investor.publication import PublicationService
            view = await PublicationService(session).get_recommendation(ObjectRef.model_validate(recommendation), at=at)
            if view.status not in {"withdrawn", "superseded"}:
                boundaries = [view.effective_next_review_at, view.effective_valid_until]
                next_due = min([*boundaries, next_due] if next_due else boundaries)
    cycle = await session.scalar(select(ReviewCycle).join(InvestorCase, InvestorCase.id == ReviewCycle.case_id).where(
        InvestorCase.owner_id == "local_owner", InvestorCase.listing_id == listing_id,
        ReviewCycle.payload["supplemental_update_batch_id"].as_string().is_not(None)).order_by(
        ReviewCycle.payload["created_at"].as_string().desc(), ReviewCycle.id).limit(1))
    return {"listing_id": listing_id, "context": context, "enabled": settings.supplemental_updates_enabled,
            "state": "blocked" if row and row.blocked_reason else (active.status if active else ("checked" if row and row.last_success else "not_assessed")),
            "revision": row.revision if row else 0, "confirmed_sequence": confirmed,
            "requested_sequence": row.requested_sequence if row else 0, "pending_count": pending[0],
            "oldest_pending_at": pending[1], "oldest_pending_seconds": (at - aware(pending[1])).total_seconds() if pending[1] else None,
            "next_due_at": next_due,
            "overdue": bool(next_due and next_due <= at),
            "last_success_at": row.last_success if row else None,
            "blocked_reason": row.blocked_reason if row else None,
            "research_state": cycle.state if cycle else (outcome.payload.get("state") if outcome else "not_assessed"),
            "research_pending_reason": cycle.payload.get("pending_reason") if cycle else (outcome.payload.get("reason") if outcome else None),
            "research_task_id": cycle.task_id if cycle else None, "active_run_id": active.id if active else None,
            "progress": {k: active.metadata_json[k] for k in ("supplemental_update_state", "scope_count", "scope_offset", "continuation_enqueued", "processed_count", "failed_count") if k in active.metadata_json} if active else None,
            "attempt_count": active.attempt_count if active else None, "heartbeat_at": active.heartbeat_at if active else None,
            "sources": await source_views(session, source_rows, at),
            "declared_source_count": len(source_rows),
            "coverage": "declared_scopes_only" if source_rows else "not_assessed",
            "notice": "没有采集到不表示没有风险；本地输入覆盖不代表全网覆盖。", "observed_at": at}


@router.get("/supplemental-updates/receipts/{receipt_id}")
async def receipt_detail(receipt_id: UUID, session: Session) -> dict[str, Any]:
    await ensure_read_snapshot(session)
    receipt = await SupplementalUpdatesRepository(session).get_change("local_owner", receipt_id)
    if receipt is None:
        raise InvestorError("not_found", "变化收据不存在")
    return receipt.model_dump(mode="json")


@router.get("/supplemental-updates/batches/{run_id}")
async def batch_detail(run_id: int, session: Session, cursor: str | None = None,
                       limit: PageLimit = 50) -> dict[str, Any]:
    await ensure_read_snapshot(session)
    run = await session.get(PipelineRun, run_id)
    association = await session.scalar(select(Change).where(Change.owner_id == "local_owner", Change.origin_run_id == run_id).limit(1))
    if run is None or (run.kind != "supplemental_update_process" and association is None) or run.request_json.get("owner_id") != "local_owner":
        raise InvestorError("not_found", "持续更新批次不存在")
    related = Change.origin_run_id == run_id
    if run.kind == "supplemental_update_process":
        related = or_(related, Change.payload["coordinator_run_id"].as_integer() == run_id)
    max_sequence = await session.scalar(select(func.max(Change.sequence)).where(
        Change.owner_id == "local_owner", related)) or 0
    binding = {"v": 1, "owner": "local_owner", "query": "batch", "run_id": run_id,
               "manifest_hash": canonical_hash(run.request_json), "max_sequence": max_sequence}
    after = _decode(cursor, binding)
    # Entries are immutable journal records, not arbitrary model/task payload.
    entries = list(await session.scalars(select(Change).where(Change.owner_id == "local_owner", related, Change.sequence > after, Change.sequence <= max_sequence).order_by(Change.sequence).limit(limit + 1)))
    repo = SupplementalUpdatesRepository(session)
    verified = [await repo.get_change("local_owner", e.sequence) for e in entries[:limit]]
    return {"run_id": run_id, "status": run.status, "stage": run.stage, "attempt_count": run.attempt_count,
            "available_at": run.available_at, "heartbeat_at": run.heartbeat_at, "error_code": run.metadata_json.get("error_code"),
            "entries": [e.model_dump(mode="json") for e in verified if e],
            "next_cursor": _encode({**binding, "after": entries[limit - 1].sequence}) if len(entries) > limit else None}


class ResumeRequest(Contract):
    operation_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class PollResumeRequest(Contract):
    operation_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    reason: Annotated[str, Field(min_length=1, max_length=256)]


@router.post("/supplemental-updates/polls/{poll_id}/resume", status_code=202)
async def resume_poll(
    poll_id: UUID, body: PollResumeRequest, session: Session, settings: Bootstrap,
) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_updates import resume_disclosure_poll

    return await resume_disclosure_poll(session, poll_id, body.operation_id,
                                        body.expected_revision, body.reason, settings=settings)


@router.post("/supplemental-updates/batches/{run_id}/resume", status_code=202)
async def resume_batch(run_id: int, body: ResumeRequest, session: Session, settings: Bootstrap) -> dict[str, Any]:
    from investment_agent.supplemental_rules.updates import resume
    return await resume(session, run_id=run_id, expected_revision=body.expected_revision,
                        reason=body.reason, operation_id=body.operation_id, settings=settings)
