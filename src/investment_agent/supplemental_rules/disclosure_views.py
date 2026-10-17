"""Read-only operating projections for bounded disclosure authorization."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import AUTH_FEED, QUOTA_FEED


def _remaining(payload: dict[str, Any] | None, cap: int, kind: str) -> int | None:
    # Missing projections are not evidence of zero consumption. Reads never
    # create a quota row or reconcile the attempt ledger.
    if payload is None:
        return None
    reserved, consumed = payload.get(f"{kind}_reserved"), payload.get(f"{kind}_consumed")
    if type(reserved) is not int or type(consumed) is not int or min(reserved, consumed) < 0:
        return None
    return max(0, cap - reserved - consumed)


async def authorization_view(
    session: AsyncSession, authorization_id: UUID, *, at: datetime | None = None,
) -> dict[str, Any]:
    """Include revoked/expired grants without granting or renewing authority."""
    from investment_agent.supplemental_rules.disclosure_authorization import (
        OWNER_POLICY_SCOPE,
        read_authorization_snapshot,
    )

    at = at or datetime.now(UTC)
    grant = await read_authorization_snapshot(session, authorization_id)
    status = ("revoked" if grant.status != "active" or grant.revoked_at is not None
              else "expired" if grant.expires_at <= at else "active")
    campaign_view: dict[str, Any] = {}
    if isinstance(grant, BackgroundDisclosureAuthorizationV2):
        from investment_agent.supplemental_rules.disclosure_campaign import read_disclosure_campaign

        try:
            campaign = await read_disclosure_campaign(session, grant.campaign_id)
            campaign_view = {
                "campaign_id": str(campaign.campaign_id), "manual_only": True,
                "campaign_status": campaign.status,
                "campaign_remaining_http": max(0, campaign.http_cap - campaign.http_consumed - campaign.http_reserved),
                "campaign_remaining_bytes": max(0, campaign.response_bytes_cap - campaign.response_bytes_consumed - campaign.response_bytes_reserved),
                "campaign_remaining_storage": max(0, campaign.storage_bytes_cap - campaign.storage_bytes_consumed - campaign.storage_bytes_reserved),
                "campaign_actions": [a.model_dump(mode="json") for a in campaign.actions],
            }
            if (campaign.status != "active" or not campaign.valid_from <= at < campaign.expires_at
                or grant.valid_from > at):
                status = "unknown"
        except (InvestorError, ValueError):
            status = "unknown"
            campaign_view = {"campaign_status": "unknown", "campaign_remaining_http": None,
                             "campaign_remaining_bytes": None, "campaign_remaining_storage": None}
    repo = SupplementalUpdatesRepository(session)
    policy = await repo.get_source("local_owner", AUTH_FEED, OWNER_POLICY_SCOPE)
    day = at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    owner_day = await repo.get_source("local_owner", QUOTA_FEED, f"owner-day:{day}")
    grant_day = await repo.get_source("local_owner", QUOTA_FEED, f"grant-day:{authorization_id}:{day}")
    owner_payload = owner_day.payload if owner_day and owner_day.payload.get("day") == day else None
    grant_payload = grant_day.payload if grant_day and grant_day.payload.get("day") == day else None
    owner_http = policy.payload.get("owner_daily_http") if policy else None
    owner_bytes = policy.payload.get("owner_daily_response_bytes") if policy else None
    owner_remaining_http = (_remaining(owner_payload, owner_http, "request")
                            if type(owner_http) is int and owner_http >= 0 else None)
    owner_remaining_bytes = (_remaining(owner_payload, owner_bytes, "bytes")
                             if type(owner_bytes) is int and owner_bytes >= 0 else None)
    grant_remaining_http = _remaining(grant_payload, grant.grant_daily_http, "request")
    grant_remaining_bytes = _remaining(grant_payload, grant.grant_daily_response_bytes, "bytes")
    return {**campaign_view, "authorization": grant.model_dump(mode="json"), "status": status,
            "owner_policy_revision": policy.payload.get("policy_revision") if policy else None,
            "owner_policy": {"owner_daily_http": owner_http,
                             "owner_daily_response_bytes": owner_bytes,
                             "owner_min_request_interval_seconds": policy.payload.get(
                                 "owner_min_request_interval_seconds") if policy else None},
            "shanghai_day": day, "owner_remaining_http": owner_remaining_http,
            "owner_remaining_bytes": owner_remaining_bytes,
            "grant_remaining_http": grant_remaining_http,
            "grant_remaining_bytes": grant_remaining_bytes,
            "remaining_http": min(owner_remaining_http, grant_remaining_http)
            if owner_remaining_http is not None and grant_remaining_http is not None else None,
            "remaining_bytes": min(owner_remaining_bytes, grant_remaining_bytes)
            if owner_remaining_bytes is not None and grant_remaining_bytes is not None else None,
            "quota_notice": "未决请求仍计额度；额度未知不表示尚未使用。", "observed_at": at}


async def source_authorization_view(
    session: AsyncSession, payload: dict[str, Any], *, at: datetime,
) -> dict[str, Any]:
    authorization_id = payload.get("authorization_id")
    if authorization_id is None:
        return {"grant_status": "unknown", "remaining_http": None, "remaining_bytes": None}
    try:
        view = await authorization_view(session, UUID(str(authorization_id)), at=at)
    except (InvestorError, ValueError):
        return {"grant_status": "unknown", "remaining_http": None, "remaining_bytes": None}
    approval = {}
    if payload.get("protocol_profile") == "live":
        from investment_agent.evidence.disclosure_profiles import approved_contract

        approved = approved_contract(str(payload.get("contract_hash", "")), now=at)
        approval = {"profile_approval_status": "approved" if approved else "unapproved",
                    "protocol_status": payload.get("protocol_status") if approved else "unvalidated"}
    return {**approval, "grant_status": view["status"], "authorization_id": authorization_id,
            "authorization_expires_at": view["authorization"]["expires_at"],
            "remaining_http": view["remaining_http"], "remaining_bytes": view["remaining_bytes"],
            "owner_remaining_http": view["owner_remaining_http"],
            "owner_remaining_bytes": view["owner_remaining_bytes"]}


async def source_schedule_view(session: AsyncSession, listing_id: int | None) -> dict[str, Any]:
    from investment_agent.settings.loader import load_settings
    from investment_agent.supplemental_rules.disclosure_schedule import (
        SCHEDULE_FEED,
        _read_schedule,
    )

    if listing_id is None:
        return {"schedule_status": "unknown"}
    row = await SupplementalUpdatesRepository(session).get_source(
        "local_owner", SCHEDULE_FEED, f"listing:{listing_id}")
    if row is None:
        return {"schedule_status": "not_scheduled"}
    try:
        state = await _read_schedule(session, row)
    except (InvestorError, ValueError, TypeError, AttributeError):
        return {"schedule_status": "unknown", "schedule_blocked_reason": "schedule_receipt_invalid"}
    current = await load_settings(session)
    enabled = bool(current.supplemental_rules_enabled and current.supplemental_updates_enabled
                   and current.supplemental_disclosure_polling_enabled and current.disclosure_originals_enabled)
    return {"schedule_status": "disabled" if not enabled else "blocked" if state.blocked_reason else "scheduled",
            "schedule_next_lane": state.next_lane, "schedule_active_lane": state.active_lane,
            "backfill_after": state.backfill_after, "recent_checked_end": state.recent_end,
            "next_poll_at": state.next_poll_at, "deep_sweep_next_at": state.deep_next_at,
            "schedule_blocked_reason": state.blocked_reason}


async def disclosure_metrics(session: AsyncSession) -> dict[str, Any]:
    """Bounded local operations; never equates engineering progress to S0."""
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow

    row = SupplementalSourceStateRow
    where = (row.owner_id == "local_owner", row.feed_id == "cninfo_szse_v1",
             row.listing_id.is_not(None), row.context.is_not(None))
    count = await session.scalar(select(func.count()).select_from(row).where(*where)) or 0
    rows = list(await session.scalars(select(row).where(*where).order_by(row.id).limit(21)))
    at = datetime.now(UTC)
    scopes = []
    for source in rows[:20]:
        scopes.append({"listing_id": source.listing_id, "context": source.context,
                       "scope_key": source.scope_key, "coverage_state": source.coverage_state,
                       "last_attempt_at": source.last_attempt_at, "last_success_at": source.last_success_at,
                       "failure_code": source.failure_code, "retry_at": source.retry_at,
                       "index_watermark": source.payload.get("index_watermark"),
                       "original_watermark": source.payload.get("original_watermark"),
                       "complete_watermark": source.complete_watermark,
                       "authorized_history_end": source.payload.get("authorized_history_end"),
                       "history_frontier": source.payload.get("history_frontier"),
                       "history_gap": source.payload.get("history_gap"),
                       "bridge_pending": source.payload.get("bridge_pending"),
                       "poll_state": source.payload.get("poll_state"),
                       "active_poll_id": source.payload.get("active_poll_id"),
                       "protocol_status": source.payload.get("protocol_status", "unvalidated"),
                       **await source_schedule_view(session, source.listing_id),
                       **await source_authorization_view(
                           session, {**source.payload, "contract_hash": source.config_hash}, at=at,
                       )})
    queue_rows = (await session.execute(select(PipelineRun.status, func.count(PipelineRun.id)).where(
        PipelineRun.kind == "supplemental_disclosure_poll").group_by(PipelineRun.status))).all()
    return {"source_id": "cninfo_szse_v1", "declared_scope_count": count,
            "queue_counts": {str(status): int(size) for status, size in queue_rows},
            "scopes": scopes, "truncated": len(rows) > 20,
            "source_acceptance": "not_assessed_stage_b_engineering",
            "notice": "有限窗口检查不代表全网覆盖；没有采集到不表示没有风险。"}
