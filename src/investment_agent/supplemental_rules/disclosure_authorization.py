"""Durable, CAS-backed authorization and accounting for B disclosure HTTP hops.

This module creates no queue, lease, or general budget abstraction.  It stores
the bounded policy, quota projections and immutable phase receipts in A's three
P4 tables.  Callers own each short transaction and must commit a sent marker
before making exactly one HTTP hop.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import DisclosureError, validate_url
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.supplemental_updates import (
    SupplementalChangeReceiptRow,
    SupplementalSourceStateRow,
)
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    AuthorizationCommandResultV2,
    BackgroundDisclosureAuthorizationBodyV2,
    BackgroundDisclosureAuthorizationCommandV2,
    BackgroundDisclosureAuthorizationV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    AUTH_FEED,
    MAX_ACTIVE_GRANTS_PER_OWNER_SOURCE,
    MAX_LISTINGS_PER_GRANT,
    OWNER_DAILY_HTTP_CAP,
    OWNER_DAILY_RESPONSE_BYTES_CAP,
    OWNER_MIN_REQUEST_INTERVAL_SECONDS,
    OWNER_STORAGE_BYTES_CAP,
    QUOTA_FEED,
    AuthorizationCommandResult,
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
    BackgroundDisclosureAuthorizationV1,
    DisclosureAttemptIdentity,
    HttpHopPermit,
    HttpHopSettlement,
    authorization_scope_hash,
)

DisclosureAuthorization = BackgroundDisclosureAuthorizationV1 | BackgroundDisclosureAuthorizationV2
DisclosureAuthorizationCommand = (
    BackgroundDisclosureAuthorizationCommand | BackgroundDisclosureAuthorizationCommandV2
)
DisclosureAuthorizationResult = AuthorizationCommandResult | AuthorizationCommandResultV2

LOCAL_OWNER: OwnerId = "local_owner"
SHANGHAI = ZoneInfo("Asia/Shanghai")
AUTH_CONFIG_HASH = hashlib.sha256(b"p4-cninfo-background-auth-v1").hexdigest()
QUOTA_CONFIG_HASH = hashlib.sha256(b"p4-cninfo-background-quota-v1").hexdigest()
DOCUMENT_CONFIG_HASH = hashlib.sha256(b"p4-cninfo-document-state-v1").hexdigest()
OWNER_POLICY_SCOPE = "source-policy:cninfo_szse_v1"
COOLDOWN_SCOPE = "source-cooldown:cninfo_szse_v1"
STORAGE_SCOPE = "owner-storage:cninfo_szse_v1"
STORAGE_METADATA_RESERVE = 1 * 1024 * 1024


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return current.astimezone(UTC)


def _day(value: datetime) -> str:
    return value.astimezone(SHANGHAI).date().isoformat()


def _owner(value: OwnerId) -> OwnerId:
    if value != LOCAL_OWNER:
        raise InvestorError("not_found", "来源授权不存在")
    return value


async def _begin_write(session: AsyncSession) -> None:
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        active = await connection.run_sync(
            lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False))
        )
        if not active:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")


async def _require_lease(session: AsyncSession, lease: RunLease) -> PipelineRun:
    await _begin_write(session)
    run = (await session.execute(
        select(PipelineRun).where(PipelineRun.id == lease.run_id).with_for_update()
    )).scalar_one_or_none()
    if (
        run is None or run.status != "running" or run.worker_id != lease.worker_id
        or run.attempt_count != lease.attempt
    ):
        raise LeaseLostError(lease.run_id, "disclosure HTTP lease no longer owns run")
    return run


async def assert_run_lease(session: AsyncSession, lease: RunLease) -> None:
    """Public lease fence for internal P4 writers and schedulers."""
    await _require_lease(session, lease)


def _grant_scope(authorization_id: UUID) -> str:
    return f"grant:{authorization_id}"


def _quota_scope(kind: str, identity: str) -> str:
    return f"{kind}:{identity}"


async def _get_state(session: AsyncSession, feed: str, scope: str) -> dict[str, Any] | None:
    state = await SupplementalUpdatesRepository(session).get_source(
        LOCAL_OWNER, feed, scope, for_update=True
    )
    if state is None:
        return None
    if not isinstance(state.payload, dict) or state.payload.get("schema_version") != 1:
        raise InvestorError("invalid_reference", "来源状态投影格式无效")
    return {"state": state, "payload": dict(state.payload)}


async def _ensure_state(
    session: AsyncSession, *, feed: str, scope: str, config_hash: str,
    initial_payload: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    repo = SupplementalUpdatesRepository(session)
    state = await repo.ensure_source(
        LOCAL_OWNER, feed, scope, config_hash,
        payload=initial_payload,
    )
    payload = dict(state.payload)
    if payload.get("schema_version") != 1:
        raise InvestorError("invalid_reference", "来源状态投影格式无效")
    return state, payload


async def _save_state(session: AsyncSession, state: Any, payload: dict[str, Any]) -> Any:
    if getattr(state, "feed_id", None) == QUOTA_FEED:
        expected_revision = state.revision + 1
        if payload.get("revision") != expected_revision:
            raise InvestorError("invalid_reference", "来源配额版本与CAS版本不一致")
        prior_ref = state.payload.get("quota_authority_ref") if isinstance(state.payload, dict) else None
        projection = dict(payload)
        projection.pop("quota_authority_ref", None)
        projection_hash = canonical_hash(projection)
        authority_key = (
            "p4:quota-projection:"
            + canonical_hash({"feed_id": state.feed_id, "scope_key": state.scope_key})
            + f":{expected_revision}"
        )
        receipt = await SupplementalUpdatesRepository(session).append_change(
            LOCAL_OWNER,
            change_key=authority_key,
            kind="source_attempt",
            payload={
                "record_type": "quota_projection_v1",
                "feed_id": state.feed_id,
                "scope_key": state.scope_key,
                "previous_authority_ref": prior_ref,
                "projection_revision": expected_revision,
                "projection_hash": projection_hash,
                "projection": projection,
            },
            acquired_at=datetime.now(UTC),
            available_from=datetime.now(UTC),
            committed_at=datetime.now(UTC),
        )
        payload["quota_authority_ref"] = {
            "sequence": receipt.sequence,
            "id": str(receipt.id),
            "payload_hash": receipt.payload_hash,
        }
    updated = state.model_copy(update={"revision": state.revision + 1, "payload": payload})
    await SupplementalUpdatesRepository(session).save_source(updated, expected_revision=state.revision)
    return updated


async def _validate_quota_projection_authority(
    session: AsyncSession, state: Any, payload: dict[str, Any], *, scope: str,
) -> None:
    payload_revision = payload.get("revision")
    if type(payload_revision) is not int or payload_revision != state.revision:
        raise InvestorError("invalid_reference", "来源配额CAS版本投影不匹配")
    authority_ref = payload.get("quota_authority_ref")
    repo = SupplementalUpdatesRepository(session)
    if authority_ref is None:
        # A fresh zero state is its only valid witness-free representation.
        # Check the first immutable key too, so deleting/resetting the mutable
        # state cannot erase an already-recorded quota history.
        zero_counters = (
            "request_reserved", "request_consumed", "bytes_reserved", "bytes_consumed",
            "storage_reserved", "storage_consumed", "query_reserved", "query_consumed",
            "fetch_reserved", "fetch_consumed",
        )
        first_key = "p4:quota-projection:" + canonical_hash({
            "feed_id": QUOTA_FEED, "scope_key": scope,
        }) + ":1"
        if (
            state.revision != 0 or payload_revision != 0
            or any(type(payload.get(key)) is not int or payload[key] != 0 for key in zero_counters)
            or payload.get("active_hop") is not None
            or payload.get("last_sent_at") is not None
            or await repo.get_change_by_key(LOCAL_OWNER, first_key) is not None
        ):
            raise InvestorError("invalid_reference", "来源配额缺少权威账本收据")
        return
    if not isinstance(authority_ref, dict):
        raise InvestorError("invalid_reference", "来源配额权威收据引用格式错误")
    sequence = authority_ref.get("sequence")
    receipt_id = authority_ref.get("id")
    receipt_hash = authority_ref.get("payload_hash")
    if type(sequence) is not int or sequence < 1 or not isinstance(receipt_id, str) or not isinstance(receipt_hash, str):
        raise InvestorError("invalid_reference", "来源配额权威收据引用字段损坏")
    receipt = await repo.get_change(LOCAL_OWNER, sequence)
    projection = dict(payload)
    projection.pop("quota_authority_ref", None)
    receipt_payload = receipt.payload if receipt is not None else {}
    if (
        receipt is None or str(receipt.id) != receipt_id or receipt.payload_hash != receipt_hash
        or receipt.kind != "source_attempt"
        or receipt_payload.get("record_type") != "quota_projection_v1"
        or receipt_payload.get("feed_id") != QUOTA_FEED
        or receipt_payload.get("scope_key") != scope
        or receipt_payload.get("projection_revision") != state.revision
        or receipt_payload.get("projection_hash") != canonical_hash(projection)
        or receipt_payload.get("projection") != projection
    ):
        raise InvestorError("invalid_reference", "来源配额投影与权威账本不一致")
    previous = receipt_payload.get("previous_authority_ref")
    if state.revision == 1:
        if previous is not None:
            raise InvestorError("invalid_reference", "来源配额首个账本收据存在断链")
    else:
        if not isinstance(previous, dict):
            raise InvestorError("invalid_reference", "来源配额历史账本链缺失")
        previous_sequence = previous.get("sequence")
        previous_id = previous.get("id")
        previous_hash = previous.get("payload_hash")
        if (
            type(previous_sequence) is not int or previous_sequence < 1
            or not isinstance(previous_id, str) or not isinstance(previous_hash, str)
        ):
            raise InvestorError("invalid_reference", "来源配额前序账本引用损坏")
        previous_receipt = await repo.get_change(LOCAL_OWNER, previous_sequence)
        previous_payload = previous_receipt.payload if previous_receipt is not None else {}
        if (
            previous_receipt is None
            or str(previous_receipt.id) != previous_id
            or previous_receipt.payload_hash != previous_hash
            or previous_payload.get("record_type") != "quota_projection_v1"
            or previous_payload.get("feed_id") != QUOTA_FEED
            or previous_payload.get("scope_key") != scope
            or previous_payload.get("projection_revision") != state.revision - 1
        ):
            raise InvestorError("invalid_reference", "来源配额前序账本收据不存在或不匹配")


def _parse_authorization(value: Any) -> DisclosureAuthorization:
    # V1 remains byte-for-byte strict. Old readers reject the V2 wire rather
    # than silently losing campaign/manual-only restrictions.
    model = (BackgroundDisclosureAuthorizationV2
             if isinstance(value, dict) and value.get("schema_version") == 2
             else BackgroundDisclosureAuthorizationV1)
    return model.model_validate(value)


def _authorization_result(grant: DisclosureAuthorization, *, receipt_id: UUID,
                          receipt_sequence: int, replayed: bool = False) -> DisclosureAuthorizationResult:
    if isinstance(grant, BackgroundDisclosureAuthorizationV2):
        return AuthorizationCommandResultV2(authorization=grant, receipt_id=receipt_id,
                                             receipt_sequence=receipt_sequence, replayed=replayed)
    return AuthorizationCommandResult(authorization=grant, receipt_id=receipt_id,
                                      receipt_sequence=receipt_sequence, replayed=replayed)


async def _read_authorization(
    session: AsyncSession, authorization_id: UUID, *, owner_id: OwnerId = LOCAL_OWNER,
) -> tuple[Any, DisclosureAuthorization]:
    _owner(owner_id)
    found = await _get_state(session, AUTH_FEED, _grant_scope(authorization_id))
    if found is None:
        raise InvestorError("not_found", "来源授权不存在")
    payload = found["payload"]
    try:
        grant = _parse_authorization(payload["authorization"])
    except Exception as exc:
        raise InvestorError("invalid_reference", "来源授权投影损坏") from exc
    if grant.authorization_id != authorization_id or grant.owner_id != owner_id:
        raise InvestorError("invalid_reference", "来源授权主体不匹配")
    if canonical_hash(grant.model_dump(mode="json", exclude={"authorization_hash"})) != grant.authorization_hash:
        raise InvestorError("invalid_reference", "来源授权摘要不匹配")
    receipt_sequence = payload.get("authorization_receipt_sequence")
    receipt_id = payload.get("authorization_receipt_id")
    receipt_hash = payload.get("authorization_receipt_hash")
    if type(receipt_sequence) is not int or receipt_sequence < 1 or not isinstance(receipt_id, str) or not isinstance(receipt_hash, str):
        raise InvestorError("invalid_reference", "来源授权缺少权威审计收据")
    receipt = await SupplementalUpdatesRepository(session).get_change(LOCAL_OWNER, receipt_sequence)
    if (
        receipt is None or str(receipt.id) != receipt_id or receipt.payload_hash != receipt_hash
        or receipt.kind != "source_authorization"
        or receipt.payload.get("authorization") != grant.model_dump(mode="json")
    ):
        raise InvestorError("invalid_reference", "来源授权投影与权威审计收据不一致")
    return found["state"], grant


async def read_authorization_snapshot(
    session: AsyncSession, authorization_id: UUID, *, owner_id: OwnerId = LOCAL_OWNER,
) -> DisclosureAuthorization:
    """Pure read of the current snapshot, including revoked/expired grants."""
    _state, grant = await _read_authorization(session, authorization_id, owner_id=owner_id)
    return grant


async def get_current_authorization(
    session: AsyncSession, authorization_id: UUID, *,
    owner_id: OwnerId = LOCAL_OWNER, now: datetime | None = None,
) -> DisclosureAuthorization:
    _state, grant = await _read_authorization(session, authorization_id, owner_id=owner_id)
    current = _now(now)
    if grant.status != "active" or grant.revoked_at is not None:
        raise InvestorError("disclosure_authorization_required", "来源授权已撤销")
    if grant.expires_at <= current:
        raise InvestorError("disclosure_authorization_expired", "来源授权已过期")
    if isinstance(grant, BackgroundDisclosureAuthorizationV2):
        from investment_agent.supplemental_rules.disclosure_campaign import (
            _db_path,
            read_disclosure_campaign,
        )

        if grant.valid_from > current:
            raise InvestorError("disclosure_authorization_required", "来源授权尚未生效")
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        binding = {"authorization_id": str(grant.authorization_id),
                   "revision": grant.revision, "sample_env_id": grant.sample_env_id}
        if (campaign.status != "active" or not campaign.valid_from <= current < campaign.expires_at
            or campaign.sample_env_id != grant.sample_env_id
            or campaign.database_path != _db_path(session) or binding not in campaign.bound_grants):
            raise InvestorError("disclosure_authorization_required", "campaign已暂停或授权环境失效")
    return grant


async def freeze_poll_quota(
    session: AsyncSession,
    *,
    poll_id: str,
    authorization: DisclosureAuthorization,
    listing_ref: ObjectRef,
    window_start: datetime,
    window_end: datetime,
    poll_http_cap: int,
    poll_query_http_cap: int,
    poll_fetch_cap: int,
    poll_response_bytes_cap: int,
    now: datetime | None = None,
) -> str:
    """Persist immutable per-poll scope/caps in the quota feed at admission.

    The API/router must call this in the same transaction as the root poll
    receipt and PipelineRun enqueue. A request cannot self-assert looser caps.
    """
    current = _now(now)
    active = await get_current_authorization(
        session, authorization.authorization_id, owner_id=authorization.owner_id, now=current
    )
    if active != authorization:
        raise InvestorError("disclosure_authorization_expired", "poll提交时授权版本已变化")
    if not poll_id or len(poll_id) > 128:
        raise InvestorError("invalid_reference", "poll ID形状无效")
    if window_start < authorization.history_start or window_end > authorization.history_end:
        raise InvestorError("disclosure_scope_forbidden", "poll窗口超出来源授权")
    if listing_ref not in authorization.listing_refs:
        raise InvestorError("disclosure_scope_forbidden", "证券不在来源授权范围")
    if (poll_http_cap > authorization.per_poll_http
        or poll_query_http_cap > authorization.per_poll_query_http
        or poll_fetch_cap > authorization.per_poll_fetch
        or poll_response_bytes_cap > authorization.per_poll_response_bytes):
        raise InvestorError("invalid_reference", "poll额度超过来源授权")
    scope_hash = authorization_scope_hash(
        authorization.authorization_id, authorization.revision,
        authorization.authorization_hash, authorization.contract_hash,
        listing_ref, window_start, window_end,
    )
    scope = _quota_scope("poll", poll_id)
    state, payload = await _ensure_state(
        session, feed=QUOTA_FEED, scope=scope, config_hash=QUOTA_CONFIG_HASH,
        initial_payload={"schema_version": 1, "day": None, "timezone": "Asia/Shanghai",
                         "request_reserved": 0, "request_consumed": 0,
                         "bytes_reserved": 0, "bytes_consumed": 0,
                         "storage_reserved": 0, "storage_consumed": 0,
                         "query_reserved": 0, "query_consumed": 0,
                         "fetch_reserved": 0, "fetch_consumed": 0,
                         "revision": 0},
    )
    frozen = {
        "poll_id": poll_id,
        "authorization_id": str(authorization.authorization_id),
        "authorization_revision": authorization.revision,
        "authorization_hash": authorization.authorization_hash,
        "contract_hash": authorization.contract_hash,
        "listing_ref": listing_ref.model_dump(mode="json"),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "scope_hash": scope_hash,
        "poll_http_cap": poll_http_cap,
        "poll_query_http_cap": poll_query_http_cap,
        "poll_fetch_cap": poll_fetch_cap,
        "poll_response_bytes_cap": poll_response_bytes_cap,
        "admitted_at": current.isoformat(),
    }
    existing = payload.get("frozen_poll")
    if existing is not None:
        comparable = {k: v for k, v in frozen.items() if k != "admitted_at"}
        prior_comparable = {k: v for k, v in existing.items() if k != "admitted_at"}
        if prior_comparable != comparable:
            raise InvestorError("idempotency_conflict", "poll冻结范围或额度已存在且不匹配")
        return scope_hash
    payload["frozen_poll"] = frozen
    payload["revision"] += 1
    await _save_state(session, state, payload)
    return scope_hash


async def inherit_poll_quota(
    session: AsyncSession,
    *,
    new_poll_id: UUID,
    old_poll_id: UUID,
    replacement_ref: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, int]:
    """Carry conservative parent-poll usage into a proven replacement poll.

    Parent reservations count as used in the child, so a replacement cannot
    reset per-poll HTTP, response-byte, query, or fetch limits. Owner/grant day
    totals are deliberately untouched: the parent reservation already charged
    those ledgers. The source-owned immutable checkpoint snapshot is the only
    permitted transfer authority.
    """
    _now(now)
    if new_poll_id == old_poll_id:
        raise InvestorError("invalid_reference", "替代poll不能继承自身额度")
    repo = SupplementalUpdatesRepository(session)
    new_root = await repo.get_change(LOCAL_OWNER, new_poll_id)
    old_root = await repo.get_change(LOCAL_OWNER, old_poll_id)
    sequence = replacement_ref.get("sequence")
    snapshot_id = replacement_ref.get("id")
    snapshot_hash = replacement_ref.get("payload_hash")
    if (
        set(replacement_ref) != {"sequence", "id", "payload_hash"}
        or type(sequence) is not int or sequence < 1
        or not isinstance(snapshot_id, str) or not isinstance(snapshot_hash, str)
        or new_root is None or new_root.payload.get("record_type") != "poll_root_v1"
        or new_root.payload.get("poll_id") != str(new_poll_id)
        or old_root is None or old_root.payload.get("record_type") != "poll_root_v1"
        or old_root.payload.get("poll_id") != str(old_poll_id)
        or new_root.payload.get("predecessor_poll_ref") != str(old_poll_id)
        or new_root.payload.get("predecessor_snapshot_ref") != replacement_ref
    ):
        raise InvestorError("invalid_reference", "替代poll缺少精确前序快照绑定")
    snapshot = await repo.get_change(LOCAL_OWNER, sequence)
    if (
        snapshot is None or str(snapshot.id) != snapshot_id
        or snapshot.payload_hash != snapshot_hash
        or snapshot.kind != "source_attempt"
        or snapshot.payload.get("record_type") != "poll_checkpoint_snapshot_v1"
        or snapshot.payload.get("poll_id") != str(old_poll_id)
        or snapshot.payload.get("superseded_by") != str(new_poll_id)
        or snapshot.payload.get("old_root_ref") != {
            "sequence": old_root.sequence,
            "id": str(old_root.id),
            "payload_hash": old_root.payload_hash,
        }
        or snapshot.payload.get("listing_ref") != old_root.payload.get("listing_ref")
        or new_root.payload.get("listing_ref") != old_root.payload.get("listing_ref")
        or snapshot.payload.get("window_start") != old_root.payload.get("window_start")
        or snapshot.payload.get("window_end") != old_root.payload.get("window_end")
        or snapshot.payload.get("context") != "company_research"
        or not isinstance(snapshot.payload.get("reason"), str)
        or not snapshot.payload.get("reason", "").strip()
        or snapshot.payload.get("poll_state") in {"queued", "running"}
        or not isinstance(snapshot.payload.get("checkpoint"), dict)
    ):
        raise InvestorError("invalid_reference", "替代poll前序快照不匹配")
    pending_ids = snapshot.payload.get("pending_native_ids")
    if (
        not isinstance(pending_ids, list) or len(pending_ids) > 200
        or any(not isinstance(value, str) or not value.isdigit() or len(value) > 32
               for value in pending_ids)
        or pending_ids != sorted(set(pending_ids))
    ):
        raise InvestorError("invalid_reference", "替代poll待处理公告快照损坏")

    parent_scope = _quota_scope("poll", str(old_poll_id))
    child_scope = _quota_scope("poll", str(new_poll_id))
    _parent_state, parent = await _quota_state(session, parent_scope, day=None)
    child_state, child = await _quota_state(session, child_scope, day=None)
    frozen = child.get("frozen_poll")
    parent_frozen = parent.get("frozen_poll")
    if (
        not isinstance(frozen, dict) or frozen.get("poll_id") != str(new_poll_id)
        or not isinstance(parent_frozen, dict)
        or parent_frozen.get("poll_id") != str(old_poll_id)
        or frozen.get("listing_ref") != new_root.payload.get("listing_ref")
        or parent_frozen.get("listing_ref") != old_root.payload.get("listing_ref")
        or frozen.get("authorization_id") != new_root.payload.get("authorization_id")
        or parent_frozen.get("authorization_id") != old_root.payload.get("authorization_id")
        or frozen.get("window_start") != new_root.payload.get("window_start")
        or frozen.get("window_end") != new_root.payload.get("window_end")
        or parent_frozen.get("window_start") != old_root.payload.get("window_start")
        or parent_frozen.get("window_end") != old_root.payload.get("window_end")
    ):
        raise InvestorError("invalid_reference", "替代poll额度尚未冻结")
    inherited = {
        "request_consumed": parent["request_reserved"] + parent["request_consumed"],
        "bytes_consumed": parent["bytes_reserved"] + parent["bytes_consumed"],
        "storage_consumed": parent["storage_reserved"] + parent["storage_consumed"],
        "query_consumed": parent["query_reserved"] + parent["query_consumed"],
        "fetch_consumed": parent["fetch_reserved"] + parent["fetch_consumed"],
    }
    lineage_hash = canonical_hash({
        "new_poll_id": str(new_poll_id),
        "old_poll_id": str(old_poll_id),
        "replacement_ref": replacement_ref,
        "usage": inherited,
    })
    prior_parent = child.get("inherited_from_poll_id")
    if prior_parent is not None:
        if (
            prior_parent != str(old_poll_id)
            or child.get("inheritance_ref") != replacement_ref
            or child.get("inheritance_hash") != lineage_hash
            or any(child.get(key) != value for key, value in inherited.items())
        ):
            raise InvestorError("idempotency_conflict", "替代poll额度继承已绑定其他前序")
        return inherited
    if any(child.get(key) != 0 for key in (
        "request_reserved", "request_consumed", "bytes_reserved", "bytes_consumed",
        "storage_reserved", "storage_consumed", "query_reserved", "query_consumed",
        "fetch_reserved", "fetch_consumed",
    )):
        raise InvestorError("revision_conflict", "新poll已有HTTP使用，不能再继承")

    # A source-wide active hop must first be recovered by maintenance using
    # the ordinary cancel (unsent) or unknown settlement (sent) ledger path.
    # Replacement admission never silently clears or transfers a live token.
    cooldown_state, cooldown_payload = await _quota_state(
        session, COOLDOWN_SCOPE, day=None,
    )
    active_hop = cooldown_payload.get("active_hop")
    if active_hop is not None:
        raise InvestorError(
            "source_hop_recovery_required",
            "替代poll前须由维护任务恢复来源活动HTTP hop",
        )

    caps = {
        "request_consumed": frozen.get("poll_http_cap"),
        "bytes_consumed": frozen.get("poll_response_bytes_cap"),
        "query_consumed": frozen.get("poll_query_http_cap"),
        "fetch_consumed": frozen.get("poll_fetch_cap"),
    }
    if any(type(caps[key]) is not int or value > caps[key]
           for key, value in inherited.items() if key in caps):
        raise InvestorError("capacity_blocked", "替代poll冻结额度不足以承接前序已用额度")
    payload = dict(child)
    payload.update(inherited)
    payload.update({
        "inherited_from_poll_id": str(old_poll_id),
        "inheritance_ref": replacement_ref,
        "inheritance_hash": lineage_hash,
        "revision": child["revision"] + 1,
    })
    await _save_state(session, child_state, payload)
    return inherited


async def apply_authorization_command(
    session: AsyncSession,
    command: DisclosureAuthorizationCommand,
    *,
    actor: Literal["api", "web"],
    now: datetime | None = None,
) -> DisclosureAuthorizationResult:
    """Apply an authenticated user grant mutation and CAS the owner quota policy.

    Create/amend/revoke and owner policy are one transaction.  The caller must
    commit before returning a successful response.
    """
    current = _now(now)
    await _begin_write(session)
    repo = SupplementalUpdatesRepository(session)
    command_version = "v2" if isinstance(command, BackgroundDisclosureAuthorizationCommandV2) else "v1"
    request_hash = canonical_hash({"command": f"supplemental_updates.source_authorization.{command_version}", "body": command})

    # Root operation replay is exact and precedes current feature/gate checks.
    old = (await session.execute(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == LOCAL_OWNER,
        SupplementalChangeReceiptRow.operation_id == str(command.operation_id),
    ))).scalar_one_or_none()
    if old is not None:
        receipt = await repo.get_change(LOCAL_OWNER, old.id)
        if receipt is None or receipt.request_hash != request_hash:
            raise InvestorError("idempotency_conflict", "授权操作ID已用于其他请求")
        try:
            snapshot = _parse_authorization(receipt.payload["authorization"])
        except Exception as exc:
            raise InvestorError("invalid_reference", "授权操作收据损坏") from exc
        return _authorization_result(
            grant=snapshot, receipt_id=receipt.id,
            receipt_sequence=receipt.sequence, replayed=True,
        )

    policy_state, policy_payload = await _ensure_state(
        session, feed=AUTH_FEED, scope=OWNER_POLICY_SCOPE,
        config_hash=AUTH_CONFIG_HASH,
        initial_payload={"schema_version": 1, "policy_revision": 0,
                         "owner_daily_http": None, "owner_daily_response_bytes": None,
                         "owner_min_request_interval_seconds": None,
                         "hard_http": OWNER_DAILY_HTTP_CAP,
                         "hard_response_bytes": OWNER_DAILY_RESPONSE_BYTES_CAP,
                         "min_request_interval_seconds": OWNER_MIN_REQUEST_INTERVAL_SECONDS},
    )
    policy_revision = int(policy_payload.get("policy_revision", -1))
    policy_http = policy_payload.get("owner_daily_http")
    policy_bytes = policy_payload.get("owner_daily_response_bytes")
    policy_interval = policy_payload.get("owner_min_request_interval_seconds")
    if command.expected_owner_policy_revision != policy_revision:
        raise InvestorError("revision_conflict", "来源总配额策略版本已变化")
    requested_policy = (command.owner_daily_http, command.owner_daily_response_bytes,
                        command.owner_min_request_interval_seconds)
    existing_policy = (policy_http, policy_bytes, policy_interval)
    if policy_revision == 0:
        if command.action != "create" or command.expected_owner_policy_revision != 0:
            raise InvestorError("revision_conflict", "须先由首个授权初始化来源总配额")
        policy_payload.update({"owner_daily_http": requested_policy[0],
                               "owner_daily_response_bytes": requested_policy[1],
                               "owner_min_request_interval_seconds": requested_policy[2],
                               "policy_revision": 1})
    elif requested_policy != existing_policy:
        if not command.update_owner_policy or command.action == "revoke":
            raise InvestorError("idempotency_conflict", "来源总配额变更须显式授权")
        policy_payload.update({"owner_daily_http": requested_policy[0],
                               "owner_daily_response_bytes": requested_policy[1],
                               "owner_min_request_interval_seconds": requested_policy[2],
                               "policy_revision": policy_revision + 1})
    elif command.update_owner_policy:
        raise InvestorError("idempotency_conflict", "来源总配额策略无变化")

    grant_state = await repo.get_source(LOCAL_OWNER, AUTH_FEED, _grant_scope(command.authorization_id), for_update=True)
    if command.action == "create":
        if grant_state is not None and grant_state.payload.get("authorization") is not None:
            raise InvestorError("idempotency_conflict", "来源授权标识已存在")
        if command.expected_revision != 0 or command.body is None:
            raise InvestorError("revision_conflict", "创建授权的版本无效")
        revision = 1
        status = "active"
        body = command.body
    else:
        if grant_state is None or not isinstance(grant_state.payload, dict):
            raise InvestorError("not_found", "来源授权不存在")
        _grant_projection, prior = await _read_authorization(
            session, command.authorization_id
        )
        if prior.revision != command.expected_revision:
            raise InvestorError("revision_conflict", "来源授权版本已变化")
        if prior.authorization_id != command.authorization_id:
            raise InvestorError("invalid_reference", "来源授权标识不匹配")
        if command.action == "amend":
            prior_v2 = isinstance(prior, BackgroundDisclosureAuthorizationV2)
            body_v2 = isinstance(command.body, BackgroundDisclosureAuthorizationBodyV2)
            if prior_v2 != body_v2 or (
                isinstance(prior, BackgroundDisclosureAuthorizationV2)
                and isinstance(command.body, BackgroundDisclosureAuthorizationBodyV2)
                and (prior.campaign_id != command.body.campaign_id
                     or prior.sample_env_id != command.body.sample_env_id)
            ):
                raise InvestorError("disclosure_scope_forbidden", "授权不能改变版本或campaign环境")
            if prior.status != "active" or prior.expires_at <= current or command.body is None:
                raise InvestorError("writes_disabled", "过期或撤销授权不能修订")
            revision, status, body = prior.revision + 1, "active", command.body
        else:
            if prior.status != "active":
                raise InvestorError("writes_disabled", "来源授权已撤销")
            revision, status = prior.revision + 1, "revoked"
            body_model = (BackgroundDisclosureAuthorizationBodyV2
                          if isinstance(prior, BackgroundDisclosureAuthorizationV2)
                          else BackgroundDisclosureAuthorizationBody)
            body = body_model.model_validate(
                prior.model_dump(mode="python", exclude={"authorization_id", "revision", "owner_id", "created_at", "actor", "status", "revoked_at", "revocation_reason", "authorization_hash"})
            )
    if body is None:
        raise InvestorError("invalid_reference", "授权正文缺失")
    if body.contract_hash == "0" * 64:
        raise InvestorError("invalid_reference", "来源合同摘要无效")
    if command.action != "revoke" and isinstance(body, BackgroundDisclosureAuthorizationBodyV2):
        from investment_agent.supplemental_rules.disclosure_campaign import read_disclosure_campaign

        campaign = await read_disclosure_campaign(session, body.campaign_id)
        if (campaign.status != "active" or campaign.sample_env_id != body.sample_env_id
            or body.valid_from < campaign.valid_from or body.expires_at > campaign.expires_at
            or body.valid_from >= body.expires_at or len(body.listing_refs) != 1):
            raise InvestorError("disclosure_scope_forbidden", "V2授权超出campaign环境或绝对期限")
    if command.action != "revoke":
        # The source contract allowlist is an owner/source-wide boundary, not
        # a per-grant loophole. Revisions replace this grant's old scope; other
        # active grants continue to count until revoked or expired.
        active_refs = set(body.listing_refs)
        refs_to_check = {ref.id for ref in body.listing_refs}
        if any(type(ref_id) is not int or ref_id < 1 for ref_id in refs_to_check):
            raise InvestorError("invalid_reference", "来源授权证券ID必须为正整数")
        listings = (await session.execute(select(Listing).where(Listing.id.in_(refs_to_check)))).scalars().all()
        if len(listings) != len(refs_to_check) or any(
            listing.market_code != "SZSE" or len(listing.ticker) != 6 or not listing.ticker.isdigit()
            for listing in listings
        ):
            raise InvestorError("invalid_reference", "来源授权只接受现存深交所六位证券代码")
        grant_rows = (await session.execute(select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.owner_id == LOCAL_OWNER,
            SupplementalSourceStateRow.feed_id == AUTH_FEED,
            SupplementalSourceStateRow.scope_key.like("grant:%"),
            SupplementalSourceStateRow.payload["authorization"]["status"].as_string() == "active",
            func.julianday(SupplementalSourceStateRow.payload["authorization"]["expires_at"].as_string())
            > func.julianday(current.isoformat()),
        ).limit(MAX_ACTIVE_GRANTS_PER_OWNER_SOURCE + 1))).scalars().all()
        if len(grant_rows) > MAX_ACTIVE_GRANTS_PER_OWNER_SOURCE:
            raise InvestorError("capacity_blocked", "owner-source活动grant数量超过硬上限")
        other_active_rows = [
            row for row in grant_rows if row.scope_key != _grant_scope(command.authorization_id)
        ]
        # The proposed create/amend remains active, so count it after excluding
        # the current grant being amended. This rejects the ninth grant at the
        # boundary rather than only rejecting a tenth.
        if len(other_active_rows) >= MAX_ACTIVE_GRANTS_PER_OWNER_SOURCE:
            raise InvestorError("capacity_blocked", "owner-source活动grant数量达到硬上限")
        for row in grant_rows:
            if row.scope_key == _grant_scope(command.authorization_id):
                continue
            try:
                other_id = UUID(row.scope_key.removeprefix("grant:"))
            except ValueError as exc:
                raise InvestorError("invalid_reference", "授权范围投影键损坏") from exc
            other = await read_authorization_snapshot(session, other_id)
            if other.status == "active" and other.revoked_at is None and other.expires_at > current:
                active_refs.update(other.listing_refs)
        if len(active_refs) > MAX_LISTINGS_PER_GRANT:
            raise InvestorError("capacity_blocked", "owner-source活动授权证券并集超过硬上限")
        if (body.grant_daily_http > requested_policy[0]
            or body.grant_daily_response_bytes > requested_policy[1]
            or body.min_http_interval_seconds < requested_policy[2]):
            raise InvestorError("invalid_reference", "grant额度或HTTP间隔不符合owner-source策略")
        if body.expires_at <= current or body.expires_at - current > timedelta(seconds=31 * 24 * 60 * 60):
            raise InvestorError("invalid_reference", "来源授权有效期不符合上限")
        if body.history_end - body.history_start > timedelta(days=366):
            raise InvestorError("invalid_reference", "授权历史窗口超过366日")
    # Contract hash is owned by the page/source adapter and must be a strict digest.
    auth_fields: dict[str, Any] = {
        **body.model_dump(mode="json"), "authorization_id": str(command.authorization_id),
        "revision": revision, "owner_id": LOCAL_OWNER, "created_at": current.isoformat(),
        "actor": actor, "status": status,
        "revoked_at": current.isoformat() if status == "revoked" else None,
        "revocation_reason": command.reason if status == "revoked" else None,
    }
    provisional_fields = {**auth_fields, "authorization_hash": "0" * 64}
    grant_model = (BackgroundDisclosureAuthorizationV2
                   if isinstance(body, BackgroundDisclosureAuthorizationBodyV2)
                   else BackgroundDisclosureAuthorizationV1)
    provisional = grant_model.model_validate(provisional_fields)
    auth_hash = canonical_hash(
        provisional.model_dump(mode="json", exclude={"authorization_hash"})
    )
    grant = provisional.model_copy(update={"authorization_hash": auth_hash})
    payload = {
        "schema_version": 1,
        "authorization": grant.model_dump(mode="json"),
        "authorization_hash": auth_hash,
        "owner_policy_revision": int(policy_payload["policy_revision"]),
    }
    if grant_state is None:
        grant_state, _ = await _ensure_state(
            session, feed=AUTH_FEED, scope=_grant_scope(command.authorization_id),
            config_hash=AUTH_CONFIG_HASH, initial_payload={"schema_version": 1},
        )
    receipt = await repo.append_change(
        LOCAL_OWNER,
        change_key=f"p4:source-auth:{command.operation_id}",
        kind="source_authorization",
        payload={"schema_version": 1, "action": command.action,
                 "authorization_id": str(command.authorization_id),
                 "expected_revision": command.expected_revision,
                 "request_hash": request_hash,
                 "authorization": grant.model_dump(mode="json"),
                 "owner_policy": {"revision": int(policy_payload["policy_revision"]),
                                  "owner_daily_http": requested_policy[0],
                                  "owner_daily_response_bytes": requested_policy[1],
                                  "owner_min_request_interval_seconds": requested_policy[2]}},
        operation_id=command.operation_id,
        request_hash=request_hash,
        acquired_at=current, available_from=current, committed_at=current,
    )
    payload["authorization_receipt_id"] = str(receipt.id)
    payload["authorization_receipt_sequence"] = receipt.sequence
    payload["authorization_receipt_hash"] = receipt.payload_hash
    policy_payload["policy_receipt_id"] = str(receipt.id)
    policy_payload["policy_receipt_sequence"] = receipt.sequence
    policy_payload["policy_receipt_hash"] = receipt.payload_hash
    await _save_state(session, grant_state, payload)
    await _save_state(session, policy_state, policy_payload)
    if command.action != "revoke" and isinstance(grant, BackgroundDisclosureAuthorizationV2):
        from investment_agent.supplemental_rules.disclosure_campaign import (
            bind_disclosure_campaign_grant,
        )

        await bind_disclosure_campaign_grant(
            session, campaign_id=grant.campaign_id, grant_id=grant.authorization_id,
            grant_revision=grant.revision, sample_env_id=grant.sample_env_id, now=current,
        )
    return _authorization_result(
        grant=grant, receipt_id=receipt.id, receipt_sequence=receipt.sequence,
    )


def _settings_open(settings: Settings) -> bool:
    return bool(settings.supplemental_rules_enabled and settings.supplemental_updates_enabled
                and settings.supplemental_disclosure_polling_enabled
                and settings.disclosure_originals_enabled)


async def assert_attempt_authorized(
    session: AsyncSession,
    identity: DisclosureAttemptIdentity,
    *,
    owner_id: OwnerId = LOCAL_OWNER,
    now: datetime | None = None,
) -> DisclosureAuthorization:
    current = _now(now)
    grant = await get_current_authorization(
        session, identity.authorization_id, owner_id=owner_id, now=current
    )
    if (
        grant.revision != identity.authorization_revision
        or grant.authorization_hash != identity.authorization_hash
        or grant.contract_hash != identity.contract_hash
        or identity.listing_ref not in grant.listing_refs
        or identity.window_start < grant.history_start
        or identity.window_end > grant.history_end
        or identity.window_end > grant.expires_at
    ):
        raise InvestorError("disclosure_scope_forbidden", "请求范围不在当前来源授权内")
    expected_scope = authorization_scope_hash(
        grant.authorization_id, grant.revision, grant.authorization_hash,
        grant.contract_hash, identity.listing_ref, identity.window_start,
        identity.window_end,
    )
    if identity.scope_hash != expected_scope:
        raise InvestorError("invalid_reference", "请求scope摘要不匹配")
    if (
        identity.poll_http_cap > grant.per_poll_http
        or identity.poll_query_http_cap > grant.per_poll_query_http
        or identity.poll_fetch_cap > grant.per_poll_fetch
        or identity.poll_response_bytes_cap > grant.per_poll_response_bytes
        or identity.response_bytes_limit > grant.per_poll_response_bytes
    ):
        raise InvestorError("invalid_reference", "请求poll额度超过冻结授权")
    if identity.request_kind == "fetch" and grant.per_poll_fetch < 1:
        raise InvestorError("writes_disabled", "授权禁止原件抓取")
    purpose = {"directory": "directory", "query": "query", "fetch": "original"}[identity.request_kind]
    try:
        validate_url(identity.url, purpose)
    except DisclosureError as exc:
        raise InvestorError("invalid_reference", "来源请求地址不在adapter允许范围") from exc
    if isinstance(grant, BackgroundDisclosureAuthorizationV2):
        from investment_agent.supplemental_rules.disclosure_campaign import assert_campaign_attempt

        action = await assert_campaign_attempt(session, grant=grant, identity=identity, now=current)
        if action.get("kind") == "sample_poll":
            from investment_agent.evidence.disclosure_profiles import require_approved_contract

            try:
                approved = require_approved_contract(
                    grant.contract_hash, now=current, protocol_hash=action.get("profile_hash"),
                )
            except DisclosureError as exc:
                raise InvestorError("disclosure_scope_forbidden", "来源profile当前未批准") from exc
            if purpose not in approved.approval.purpose_scopes:
                raise InvestorError("disclosure_scope_forbidden", "来源profile未批准此请求用途")
    return grant


async def _load_policy(session: AsyncSession) -> tuple[Any, dict[str, Any]]:
    found = await _get_state(session, AUTH_FEED, OWNER_POLICY_SCOPE)
    if found is None:
        raise InvestorError("writes_disabled", "来源总额度策略尚未授权")
    payload = found["payload"]
    receipt_sequence = payload.get("policy_receipt_sequence")
    receipt_id = payload.get("policy_receipt_id")
    receipt_hash = payload.get("policy_receipt_hash")
    if type(receipt_sequence) is not int or receipt_sequence < 1 or not isinstance(receipt_id, str) or not isinstance(receipt_hash, str):
        raise InvestorError("invalid_reference", "来源总额度策略缺少权威审计收据")
    receipt = await SupplementalUpdatesRepository(session).get_change(LOCAL_OWNER, receipt_sequence)
    if receipt is None or str(receipt.id) != receipt_id or receipt.payload_hash != receipt_hash or receipt.kind != "source_authorization":
        raise InvestorError("invalid_reference", "来源总额度策略与审计收据不一致")
    journal_policy = receipt.payload.get("owner_policy")
    if not isinstance(journal_policy, dict) or any(
        journal_policy.get(key) != payload.get(projection)
        for key, projection in (("revision", "policy_revision"), ("owner_daily_http", "owner_daily_http"),
                                ("owner_daily_response_bytes", "owner_daily_response_bytes"),
                                ("owner_min_request_interval_seconds", "owner_min_request_interval_seconds"))
    ):
        raise InvestorError("invalid_reference", "来源总额度策略投影与审计收据不一致")
    if (
        type(payload.get("owner_daily_http")) is not int
        or type(payload.get("owner_daily_response_bytes")) is not int
        or type(payload.get("policy_revision")) is not int
        or payload.get("policy_revision", 0) < 1
        or payload["owner_daily_http"] > OWNER_DAILY_HTTP_CAP
        or payload["owner_daily_response_bytes"] > OWNER_DAILY_RESPONSE_BYTES_CAP
        or type(payload.get("owner_min_request_interval_seconds")) is not int
        or payload["owner_min_request_interval_seconds"] < OWNER_MIN_REQUEST_INTERVAL_SECONDS
    ):
        raise InvestorError("invalid_reference", "来源总额度策略损坏")
    return found["state"], payload


async def _quota_state(session: AsyncSession, scope: str, *, day: str | None = None) -> tuple[Any, dict[str, Any]]:
    state, payload = await _ensure_state(
        session, feed=QUOTA_FEED, scope=scope, config_hash=QUOTA_CONFIG_HASH,
        initial_payload={"schema_version": 1, "day": day, "timezone": "Asia/Shanghai",
                         "request_reserved": 0, "request_consumed": 0,
                         "bytes_reserved": 0, "bytes_consumed": 0,
                         "storage_reserved": 0, "storage_consumed": 0,
                         "query_reserved": 0, "query_consumed": 0,
                         "fetch_reserved": 0, "fetch_consumed": 0,
                         "revision": 0},
    )
    if payload.get("day") != day:
        raise InvestorError("invalid_reference", "来源配额日身份不匹配")
    await _validate_quota_projection_authority(session, state, payload, scope=scope)
    for key in ("request_reserved", "request_consumed", "bytes_reserved", "bytes_consumed",
                "storage_reserved", "storage_consumed", "query_reserved", "query_consumed",
                "fetch_reserved", "fetch_consumed", "revision"):
        if type(payload.get(key)) is not int or payload[key] < 0:
            raise InvestorError("invalid_reference", "来源配额状态损坏")
    return state, payload


async def _check_live_lease(session: AsyncSession, lease: RunLease) -> None:
    await _require_lease(session, lease)


async def reserve_http_hop(
    session: AsyncSession,
    *,
    identity: DisclosureAttemptIdentity,
    response_byte_limit: int,
    lease: RunLease,
    base_settings: Settings,
    now: datetime | None = None,
) -> HttpHopPermit:
    current = _now(now)
    await _check_live_lease(session, lease)
    settings = await load_settings(session, base=base_settings)
    if not _settings_open(settings):
        raise InvestorError("writes_disabled", "披露来源轮询当前关闭")
    if response_byte_limit != identity.response_bytes_limit:
        raise InvestorError("invalid_reference", "HTTP响应上限与冻结attempt不一致")
    grant = await assert_attempt_authorized(session, identity, now=current)
    day = _day(current)
    # Replays may recover an unsent reservation, but a sent identity can never
    # be returned to a caller as permission to transmit again.
    repo = SupplementalUpdatesRepository(session)
    reserve_key = f"p4:attempt:{identity.attempt_id}:{identity.hop_ordinal}:reserved"
    prior = (await session.execute(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == LOCAL_OWNER,
        SupplementalChangeReceiptRow.change_key == reserve_key,
    ))).scalar_one_or_none()
    if prior is not None:
        if prior.payload.get("identity_hash") != canonical_hash(identity.model_dump(mode="json")):
            raise InvestorError("idempotency_conflict", "attempt身份重用")
        phase_keys = (
            f"p4:attempt:{identity.attempt_id}:{identity.hop_ordinal}:sent",
            f"p4:attempt:{identity.attempt_id}:{identity.hop_ordinal}:canceled",
            f"p4:attempt:{identity.attempt_id}:{identity.hop_ordinal}:result",
            f"p4:attempt:{identity.attempt_id}:{identity.hop_ordinal}:unknown",
        )
        phase = (await session.execute(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.owner_id == LOCAL_OWNER,
            SupplementalChangeReceiptRow.change_key.in_(phase_keys),
        ))).scalars().first()
        if phase is not None:
            raise InvestorError("idempotency_conflict", "已发送或终结attempt不可再次获取发送许可")
        permit = HttpHopPermit.model_validate(prior.payload["permit"])
        await _fence_old_lease(session, prior.payload, lease)
        if permit.shanghai_day != day:
            raise InvestorError("revision_conflict", "旧日reservation需先显式迁移")
        cooldown_state, cooldown_payload = await _quota_state(session, COOLDOWN_SCOPE, day=None)
        active = cooldown_payload.get("active_hop")
        if not isinstance(active, dict) or active.get("permit_id") != str(permit.permit_id):
            raise InvestorError("idempotency_conflict", "reservation已失效或不再持有来源并发令牌")
        return permit

    auth_state, _auth = await _read_authorization(session, grant.authorization_id)
    policy_state, policy = await _load_policy(session)
    if grant.grant_daily_http > policy["owner_daily_http"] or grant.grant_daily_response_bytes > policy["owner_daily_response_bytes"]:
        raise InvestorError("writes_disabled", "grant已超过当前owner-source额度")
    cooldown = await _quota_state(session, COOLDOWN_SCOPE, day=None)
    last_request = cooldown[1].get("last_sent_at")
    if cooldown[1].get("active_hop") is not None:
        raise InvestorError("source_cooldown", "来源已有未结算HTTP hop")
    if isinstance(last_request, str):
        try:
            last_at = datetime.fromisoformat(last_request).astimezone(UTC)
        except ValueError as exc:
            raise InvestorError("invalid_reference", "来源cooldown投影损坏") from exc
        allowed_at = last_at + timedelta(seconds=max(
            policy.get("owner_min_request_interval_seconds") or OWNER_MIN_REQUEST_INTERVAL_SECONDS,
            settings_source_interval(grant)
        ))
        if current < allowed_at:
            raise InvestorError("source_cooldown", "来源HTTP最小间隔尚未到")

    owner_daily_state, owner_daily = await _quota_state(session, _quota_scope("owner-day", day), day=day)
    grant_daily_state, grant_daily = await _quota_state(
        session, _quota_scope("grant-day", f"{grant.authorization_id}:{day}"), day=day
    )
    poll_state, poll = await _quota_state(session, _quota_scope("poll", identity.poll_id), day=None)
    frozen_poll = poll.get("frozen_poll")
    expected_frozen = {
        "poll_id": identity.poll_id,
        "authorization_id": str(identity.authorization_id),
        "authorization_revision": identity.authorization_revision,
        "authorization_hash": identity.authorization_hash,
        "contract_hash": identity.contract_hash,
        "listing_ref": identity.listing_ref.model_dump(mode="json"),
        "window_start": identity.window_start.isoformat(),
        "window_end": identity.window_end.isoformat(),
        "scope_hash": identity.scope_hash,
        "poll_http_cap": identity.poll_http_cap,
        "poll_query_http_cap": identity.poll_query_http_cap,
        "poll_fetch_cap": identity.poll_fetch_cap,
        "poll_response_bytes_cap": identity.poll_response_bytes_cap,
    }
    if not isinstance(frozen_poll, dict) or any(
        frozen_poll.get(key) != value for key, value in expected_frozen.items()
    ):
        raise InvestorError("disclosure_scope_forbidden", "HTTP attempt与已接收poll冻结清单不匹配")
    storage_state, storage = await _quota_state(session, STORAGE_SCOPE, day=None)
    grant_storage_state, grant_storage = await _quota_state(
        session, _quota_scope("grant-storage", str(grant.authorization_id)), day=None
    )
    fetch_counter = identity.request_kind == "fetch"
    used_http = owner_daily["request_reserved"] + owner_daily["request_consumed"]
    used_bytes = owner_daily["bytes_reserved"] + owner_daily["bytes_consumed"]
    grant_used_http = grant_daily["request_reserved"] + grant_daily["request_consumed"]
    grant_used_bytes = grant_daily["bytes_reserved"] + grant_daily["bytes_consumed"]
    poll_used_http = poll["request_reserved"] + poll["request_consumed"]
    poll_used_bytes = poll["bytes_reserved"] + poll["bytes_consumed"]
    if used_http + 1 > policy["owner_daily_http"] or used_bytes + response_byte_limit > policy["owner_daily_response_bytes"]:
        raise InvestorError("capacity_blocked", "owner-source日额度已用尽")
    if grant_used_http + 1 > grant.grant_daily_http or grant_used_bytes + response_byte_limit > grant.grant_daily_response_bytes:
        raise InvestorError("capacity_blocked", "grant日额度已用尽")
    if poll_used_http + 1 > identity.poll_http_cap or poll_used_bytes + response_byte_limit > identity.poll_response_bytes_cap:
        raise InvestorError("capacity_blocked", "poll额度已用尽")
    if identity.request_kind == "query":
        count = poll["query_reserved"] + poll["query_consumed"]
        if count + 1 > identity.poll_query_http_cap:
            raise InvestorError("capacity_blocked", "poll query额度已用尽")
    if fetch_counter:
        count = poll["fetch_reserved"] + poll["fetch_consumed"]
        if count + 1 > identity.poll_fetch_cap:
            raise InvestorError("capacity_blocked", "poll fetch额度已用尽")
    storage_bytes = response_byte_limit + STORAGE_METADATA_RESERVE
    storage_used = storage["storage_reserved"] + storage["storage_consumed"]
    grant_storage_used = grant_storage["storage_reserved"] + grant_storage["storage_consumed"]
    if storage_used + storage_bytes > OWNER_STORAGE_BYTES_CAP:
        raise InvestorError("capacity_blocked", "来源逻辑存储额度已用尽")
    if grant_storage_used + storage_bytes > grant.storage_bytes_cap:
        raise InvestorError("capacity_blocked", "grant逻辑存储额度已用尽")

    # The attempt ledger is authoritative.  All projections change in the
    # same SQLite writer transaction as its immutable reservation receipt.
    permit = HttpHopPermit(
        permit_id=identity.attempt_id if identity.hop_ordinal == 0 else UUID(
            canonical_hash({"attempt": str(identity.attempt_id), "hop": identity.hop_ordinal})[:32]
        ),
        owner_id=LOCAL_OWNER, identity=identity, shanghai_day=day,
        reserved_response_bytes=response_byte_limit,
        storage_reserved_bytes=storage_bytes, state="reserved", created_at=current,
    )
    if isinstance(grant, BackgroundDisclosureAuthorizationV2):
        from investment_agent.supplemental_rules.disclosure_campaign import reserve_campaign_hop

        await reserve_campaign_hop(
            session, grant=grant, identity=identity, permit_id=permit.permit_id,
            response_bytes_limit=response_byte_limit, storage_bytes_limit=storage_bytes,
            now=current,
        )
    identity_hash = canonical_hash(identity.model_dump(mode="json"))
    await repo.append_change(
        LOCAL_OWNER, change_key=reserve_key, kind="source_attempt",
        payload={"schema_version": 1, "stage": "reserved", "permit": permit.model_dump(mode="json"),
                 "identity_hash": identity_hash, "identity": identity.model_dump(mode="json"),
                 **({"campaign_id": str(grant.campaign_id)}
                    if isinstance(grant, BackgroundDisclosureAuthorizationV2) else {}),
                 "lease": {"run_id": lease.run_id, "worker_id": lease.worker_id, "attempt": lease.attempt}},
        acquired_at=current, available_from=current, committed_at=current,
    )
    for state, data, req_field, byte_field, _type_field in (
        (owner_daily_state, owner_daily, "request_reserved", "bytes_reserved", "request_kind"),
        (grant_daily_state, grant_daily, "request_reserved", "bytes_reserved", "request_kind"),
        (poll_state, poll, "request_reserved", "bytes_reserved", "request_kind"),
    ):
        data[req_field] += 1
        data[byte_field] += response_byte_limit
        if identity.request_kind == "query":
            data["query_reserved"] += 1
        if fetch_counter:
            data["fetch_reserved"] += 1
        data["revision"] += 1
        await _save_state(session, state, data)
    for state, data in ((storage_state, storage), (grant_storage_state, grant_storage)):
        data["storage_reserved"] += storage_bytes
        data["revision"] += 1
        await _save_state(session, state, data)
    cooldown_state, cooldown_payload = cooldown
    cooldown_payload["active_hop"] = {"permit_id": str(permit.permit_id),
                                       "attempt_id": str(identity.attempt_id),
                                       "hop_ordinal": identity.hop_ordinal}
    cooldown_payload["revision"] += 1
    await _save_state(session, cooldown_state, cooldown_payload)
    return permit


def settings_source_interval(grant: DisclosureAuthorization) -> int:
    # A grant may require stricter request spacing than the owner policy.
    return max(OWNER_MIN_REQUEST_INTERVAL_SECONDS, grant.min_http_interval_seconds)


async def _load_permit_receipts(
    session: AsyncSession, permit: HttpHopPermit,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    reserve_key = f"p4:attempt:{permit.identity.attempt_id}:{permit.identity.hop_ordinal}:reserved"
    sent_key = f"p4:attempt:{permit.identity.attempt_id}:{permit.identity.hop_ordinal}:sent"
    repo = SupplementalUpdatesRepository(session)
    reservation = await repo.get_change_by_key(LOCAL_OWNER, reserve_key)
    sent = await repo.get_change_by_key(LOCAL_OWNER, sent_key)
    reserved_payload = reservation.payload if reservation is not None else None
    reserved_permit = reserved_payload.get("permit") if reserved_payload is not None else None
    supplied = permit.model_dump(mode="json")
    if isinstance(supplied, dict):
        supplied["state"] = "reserved"
    if (
        reservation is None or reserved_payload is None
        or reserved_payload.get("stage") != "reserved"
        or reserved_payload.get("identity_hash") != canonical_hash(
            permit.identity.model_dump(mode="json")
        )
        or reserved_payload.get("identity") != permit.identity.model_dump(mode="json")
        or reserved_permit != supplied
    ):
        raise InvestorError("invalid_reference", "HTTP reservation不存在或不匹配")
    sent_payload = sent.payload if sent is not None else None
    if sent_payload is not None and (
        sent_payload.get("stage") != "sent"
        or sent_payload.get("permit_id") != str(permit.permit_id)
        or sent_payload.get("attempt_id") != str(permit.identity.attempt_id)
        or sent_payload.get("hop_id") != permit.identity.hop_id
        or sent_payload.get("hop_ordinal") != permit.identity.hop_ordinal
        or not isinstance(sent_payload.get("lease"), dict)
        or not isinstance(reserved_payload.get("lease"), dict)
        or sent_payload.get("reservation_lease", sent_payload.get("lease"))
        != reserved_payload.get("lease")
        or set(sent_payload["lease"]) != {"run_id", "worker_id", "attempt"}
        or type(sent_payload["lease"].get("run_id")) is not int
        or sent_payload["lease"]["run_id"] < 1
        or not isinstance(sent_payload["lease"].get("worker_id"), str)
        or not sent_payload["lease"]["worker_id"]
        or type(sent_payload["lease"].get("attempt")) is not int
        or sent_payload["lease"]["attempt"] < 1
        or sent_payload["lease"].get("run_id")
        != reserved_payload.get("lease", {}).get("run_id")
    ):
        raise InvestorError("invalid_reference", "HTTP sent阶段收据与reservation不匹配")
    return reserved_payload, sent_payload


async def mark_http_hop_sent(
    session: AsyncSession,
    *,
    permit: HttpHopPermit,
    lease: RunLease,
    base_settings: Settings,
    now: datetime | None = None,
) -> HttpHopPermit:
    current = _now(now)
    await _require_lease(session, lease)
    settings = await load_settings(session, base=base_settings)
    if not _settings_open(settings):
        raise InvestorError("writes_disabled", "发送前来源开关已关闭")
    grant = await assert_attempt_authorized(session, permit.identity, now=current)
    if current >= grant.expires_at:
        raise InvestorError("disclosure_authorization_expired", "来源授权已过期")
    _policy_state, policy = await _load_policy(session)
    if (grant.grant_daily_http > policy["owner_daily_http"]
        or grant.grant_daily_response_bytes > policy["owner_daily_response_bytes"]):
        raise InvestorError("disclosure_scope_forbidden", "来源总额度策略已收紧")
    reservation, sent = await _load_permit_receipts(session, permit)
    if sent is not None:
        raise InvestorError("idempotency_conflict", "HTTP hop已有sent阶段")
    await _fence_old_lease(session, reservation, lease)
    reservation_lease = reservation.get("lease")
    if (
        not isinstance(reservation_lease, dict)
        or reservation_lease.get("run_id") != lease.run_id
    ):
        raise InvestorError(
            "invalid_reference",
            "HTTP reservation只能由原pipeline run重领后发送",
        )
    if permit.shanghai_day != _day(current):
        # Caller must cancel and reserve against the new Shanghai day first.
        raise InvestorError("revision_conflict", "reservation跨日；必须迁移后再发送")
    cooldown = await _quota_state(session, COOLDOWN_SCOPE, day=None)
    cooldown_state, cooldown_payload = cooldown
    active_hop = cooldown_payload.get("active_hop")
    if not isinstance(active_hop, dict) or active_hop.get("permit_id") != str(permit.permit_id):
        raise InvestorError("source_cooldown", "HTTP发送许可已失效或被其他hop占用")
    last_request = cooldown_payload.get("last_sent_at")
    if isinstance(last_request, str):
        last_at = datetime.fromisoformat(last_request).astimezone(UTC)
        if current < last_at + timedelta(seconds=max(
            grant.min_http_interval_seconds,
            policy["owner_min_request_interval_seconds"],
            OWNER_MIN_REQUEST_INTERVAL_SECONDS,
        )):
            raise InvestorError("source_cooldown", "来源HTTP最小间隔尚未到")
    if isinstance(grant, BackgroundDisclosureAuthorizationV2):
        from investment_agent.supplemental_rules.disclosure_campaign import mark_campaign_hop_sent

        await mark_campaign_hop_sent(session, permit_id=permit.permit_id, now=current)
    sent_key = f"p4:attempt:{permit.identity.attempt_id}:{permit.identity.hop_ordinal}:sent"
    await SupplementalUpdatesRepository(session).append_change(
        LOCAL_OWNER, change_key=sent_key, kind="source_attempt",
        payload={"schema_version": 1, "stage": "sent", "permit_id": str(permit.permit_id),
                 "attempt_id": str(permit.identity.attempt_id), "hop_id": permit.identity.hop_id,
                 "hop_ordinal": permit.identity.hop_ordinal,
                 "sent_at": current.isoformat(), "shanghai_day": permit.shanghai_day,
                 "lease": {"run_id": lease.run_id, "worker_id": lease.worker_id, "attempt": lease.attempt},
                 "reservation_lease": reservation["lease"]},
        acquired_at=current, available_from=current, committed_at=current,
    )
    cooldown_payload["last_sent_at"] = current.isoformat()
    cooldown_payload["last_attempt_id"] = str(permit.identity.attempt_id)
    cooldown_payload["last_hop_id"] = permit.identity.hop_id
    cooldown_payload["revision"] += 1
    await _save_state(session, cooldown_state, cooldown_payload)
    return permit.model_copy(update={"state": "sent"})


async def _fence_old_lease(session: AsyncSession, reservation: dict[str, Any], lease: RunLease) -> None:
    old = reservation.get("lease")
    if not isinstance(old, dict):
        raise InvestorError("invalid_reference", "attempt reservation缺少lease证明")
    if (old.get("run_id"), old.get("worker_id"), old.get("attempt")) == (
        lease.run_id, lease.worker_id, lease.attempt
    ):
        return
    prior = await session.get(PipelineRun, old.get("run_id"))
    if prior is not None and prior.status == "running" and prior.worker_id == old.get("worker_id") and prior.attempt_count == old.get("attempt"):
        raise LeaseLostError(lease.run_id, "旧HTTP attempt仍未被fence")


async def _settle_counters(
    session: AsyncSession, permit: HttpHopPermit, *, response_bytes: int,
    storage_bytes: int, was_sent: bool,
) -> None:
    day = permit.shanghai_day
    owner_state, owner = await _quota_state(session, _quota_scope("owner-day", day), day=day)
    grant_state, grant = await _quota_state(
        session, _quota_scope("grant-day", f"{permit.identity.authorization_id}:{day}"), day=day
    )
    poll_state, poll = await _quota_state(session, _quota_scope("poll", permit.identity.poll_id), day=None)
    storage_state, storage = await _quota_state(session, STORAGE_SCOPE, day=None)
    grant_storage_state, grant_storage = await _quota_state(
        session, _quota_scope("grant-storage", str(permit.identity.authorization_id)), day=None
    )
    charged_req = 1 if was_sent else 0
    charged_bytes = response_bytes if was_sent else 0
    for state, payload in ((owner_state, owner), (grant_state, grant), (poll_state, poll)):
        if payload["request_reserved"] < 1 or payload["bytes_reserved"] < permit.reserved_response_bytes:
            raise InvestorError("invalid_reference", "HTTP配额预留不足，拒绝结算")
        payload["request_reserved"] -= 1
        payload["request_consumed"] += charged_req
        payload["bytes_reserved"] -= permit.reserved_response_bytes
        payload["bytes_consumed"] += charged_bytes
        if permit.identity.request_kind == "query":
            if payload["query_reserved"] < 1:
                raise InvestorError("invalid_reference", "query预留不足")
            payload["query_reserved"] -= 1
            payload["query_consumed"] += charged_req
        if permit.identity.request_kind == "fetch":
            if payload["fetch_reserved"] < 1:
                raise InvestorError("invalid_reference", "fetch预留不足")
            payload["fetch_reserved"] -= 1
            payload["fetch_consumed"] += charged_req
        payload["revision"] += 1
        await _save_state(session, state, payload)
    for state, payload in ((storage_state, storage), (grant_storage_state, grant_storage)):
        if payload["storage_reserved"] < permit.storage_reserved_bytes:
            raise InvestorError("invalid_reference", "逻辑存储预留不足")
        payload["storage_reserved"] -= permit.storage_reserved_bytes
        payload["storage_consumed"] += storage_bytes if was_sent else 0
        payload["revision"] += 1
        await _save_state(session, state, payload)


async def settle_http_hop_result(
    session: AsyncSession,
    *,
    permit: HttpHopPermit,
    lease: RunLease,
    response_bytes: int | None,
    result_code: str,
    response_hash: str | None = None,
    storage_bytes: int = 0,
    now: datetime | None = None,
) -> HttpHopSettlement:
    current = _now(now)
    await _require_lease(session, lease)
    reservation, sent = await _load_permit_receipts(session, permit)
    await _fence_old_lease(session, reservation, lease)
    if sent is None:
        raise InvestorError("invalid_reference", "未sent的HTTP hop不能记result")
    if response_bytes is not None and response_bytes < 0:
        raise ValueError("response_bytes must be non-negative or unknown")
    if response_hash is not None and (
        response_bytes is None or len(response_hash) != 64
        or any(char not in "0123456789abcdef" for char in response_hash)
    ):
        raise ValueError("response_hash must be a lowercase SHA-256 digest for a known response")
    if storage_bytes < 0:
        raise ValueError("storage_bytes must be non-negative")
    if response_bytes is not None and response_bytes > permit.reserved_response_bytes:
        raise InvestorError("capacity_blocked", "响应超过reservation上限；必须按unknown结算")
    if storage_bytes > permit.storage_reserved_bytes - STORAGE_METADATA_RESERVE:
        raise InvestorError("capacity_blocked", "持久化字节超过reservation上限")
    result_key = f"p4:attempt:{permit.identity.attempt_id}:{permit.identity.hop_ordinal}:result"
    unknown_key = f"p4:attempt:{permit.identity.attempt_id}:{permit.identity.hop_ordinal}:unknown"
    result_row = await SupplementalUpdatesRepository(session).get_change_by_key(
        LOCAL_OWNER, result_key,
    )
    unknown_row = await SupplementalUpdatesRepository(session).get_change_by_key(
        LOCAL_OWNER, unknown_key,
    )
    if result_row is not None or unknown_row is not None:
        raise InvestorError("idempotency_conflict", "HTTP hop已结算")
    unknown = response_bytes is None
    consumed_bytes = permit.reserved_response_bytes if response_bytes is None else response_bytes
    # Include bounded persistent attempt-receipt metadata in every settlement,
    # including when the response byte count is unknown.
    stored_bytes = (
        permit.storage_reserved_bytes
        if unknown and reservation.get("campaign_id") is not None
        else STORAGE_METADATA_RESERVE + (0 if unknown else storage_bytes)
    )
    if reservation.get("campaign_id") is not None:
        from investment_agent.supplemental_rules.disclosure_campaign import settle_campaign_hop

        await settle_campaign_hop(
            session, permit_id=permit.permit_id, response_bytes=response_bytes,
            storage_bytes=stored_bytes, result_code=result_code[:64], now=current,
        )
    await _settle_counters(
        session, permit, response_bytes=consumed_bytes,
        storage_bytes=stored_bytes, was_sent=True,
    )
    key = unknown_key if unknown else result_key
    await SupplementalUpdatesRepository(session).append_change(
        LOCAL_OWNER, change_key=key, kind="source_attempt",
        payload={"schema_version": 1, "stage": "unknown" if unknown else "result",
                 "permit_id": str(permit.permit_id), "attempt_id": str(permit.identity.attempt_id),
                 "hop_id": permit.identity.hop_id, "hop_ordinal": permit.identity.hop_ordinal,
                 "result_code": result_code[:64], "response_bytes": consumed_bytes,
                 "response_hash": response_hash,
                 "observed_response_bytes": response_bytes,
                 "storage_bytes": stored_bytes, "settled_at": current.isoformat(),
                 "shanghai_day": permit.shanghai_day},
        acquired_at=current, available_from=current, committed_at=current,
    )
    cooldown_state, cooldown_payload = await _quota_state(session, COOLDOWN_SCOPE, day=None)
    active = cooldown_payload.get("active_hop")
    if not isinstance(active, dict) or active.get("permit_id") != str(permit.permit_id):
        raise InvestorError("invalid_reference", "结算hop不持有来源并发令牌")
    cooldown_payload["active_hop"] = None
    cooldown_payload["revision"] += 1
    await _save_state(session, cooldown_state, cooldown_payload)
    return HttpHopSettlement(
        permit_id=permit.permit_id, state="unknown" if unknown else "result",
        shanghai_day=permit.shanghai_day, request_consumed=1,
        response_bytes_consumed=consumed_bytes, storage_bytes_consumed=stored_bytes,
        result_code=result_code[:64],
    )


async def cancel_unsent_http_hop(
    session: AsyncSession,
    *,
    permit: HttpHopPermit,
    lease: RunLease,
    now: datetime | None = None,
) -> bool:
    current = _now(now)
    await _require_lease(session, lease)
    reservation, sent = await _load_permit_receipts(session, permit)
    await _fence_old_lease(session, reservation, lease)
    if sent is not None:
        raise InvestorError("idempotency_conflict", "已sent的reservation不能退款")
    canceled_key = f"p4:attempt:{permit.identity.attempt_id}:{permit.identity.hop_ordinal}:canceled"
    exists = (await session.execute(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == LOCAL_OWNER,
        SupplementalChangeReceiptRow.change_key == canceled_key,
    ))).scalar_one_or_none()
    if exists is not None:
        return False
    if reservation.get("campaign_id") is not None:
        from investment_agent.supplemental_rules.disclosure_campaign import cancel_campaign_hop

        await cancel_campaign_hop(session, permit_id=permit.permit_id, now=current)
    await _settle_counters(session, permit, response_bytes=0, storage_bytes=0, was_sent=False)
    cooldown_state, cooldown_payload = await _quota_state(session, COOLDOWN_SCOPE, day=None)
    active = cooldown_payload.get("active_hop")
    if not isinstance(active, dict) or active.get("permit_id") != str(permit.permit_id):
        raise InvestorError("invalid_reference", "取消hop不持有来源并发令牌")
    cooldown_payload["active_hop"] = None
    cooldown_payload["revision"] += 1
    await _save_state(session, cooldown_state, cooldown_payload)
    await SupplementalUpdatesRepository(session).append_change(
        LOCAL_OWNER, change_key=canceled_key, kind="source_attempt",
        payload={"schema_version": 1, "stage": "canceled", "permit_id": str(permit.permit_id),
                 "attempt_id": str(permit.identity.attempt_id), "hop_id": permit.identity.hop_id,
                 "hop_ordinal": permit.identity.hop_ordinal, "canceled_at": current.isoformat(),
                 "reason": "fenced_before_sent"},
        acquired_at=current, available_from=current, committed_at=current,
    )
    return True


async def recover_orphan_disclosure_hop(
    session: AsyncSession,
    *,
    lease: RunLease,
    now: datetime | None = None,
    poll_id: str | None = None,
    recovery_operation_id: UUID | None = None,
    sample_env_id: str | None = None,
) -> dict[str, str | int] | None:
    """Settle a source-wide hop left by a fenced disclosure worker.

    Maintenance owns the current lease. A still-live or queued original run is
    left untouched so it can safely resume its reserved request. Once the
    original lease is terminal/fenced, an unsent hop is refunded through the
    ordinary cancellation ledger; a sent hop without a result is charged as
    unknown. This function never performs network, blob, or candidate work.
    """
    current = _now(now)
    await _require_lease(session, lease)
    cooldown_state, cooldown_payload = await _quota_state(
        session, COOLDOWN_SCOPE, day=None,
    )
    active = cooldown_payload.get("active_hop")
    if active is None:
        return None
    if isinstance(active, dict) and set(active) == {"probe_permit_id"}:
        # S0 has a different immutable raw-action identity. A native global
        # maintenance lease must never pretend it owns that reservation.
        return None
    if not isinstance(active, dict) or set(active) != {
        "permit_id", "attempt_id", "hop_ordinal",
    }:
        raise InvestorError("invalid_reference", "来源活动HTTP令牌损坏")
    attempt_id = active.get("attempt_id")
    hop_ordinal = active.get("hop_ordinal")
    if not isinstance(attempt_id, str) or type(hop_ordinal) is not int or hop_ordinal < 0:
        raise InvestorError("invalid_reference", "来源活动HTTP令牌身份损坏")
    repo = SupplementalUpdatesRepository(session)
    reservation = await repo.get_change_by_key(
        LOCAL_OWNER, f"p4:attempt:{attempt_id}:{hop_ordinal}:reserved",
    )
    if reservation is None or reservation.payload.get("stage") != "reserved":
        raise InvestorError("invalid_reference", "来源活动hop缺少精确reservation收据")
    try:
        permit = HttpHopPermit.model_validate(reservation.payload["permit"])
        active_permit_id = UUID(active["permit_id"])
        active_attempt_id = UUID(attempt_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvestorError("invalid_reference", "来源活动hop许可身份损坏") from exc
    if (
        permit.permit_id != active_permit_id
        or permit.identity.attempt_id != active_attempt_id
        or permit.identity.hop_ordinal != hop_ordinal
    ):
        raise InvestorError("invalid_reference", "来源活动令牌与许可收据不匹配")
    if poll_id is not None and permit.identity.poll_id != poll_id:
        return None
    if reservation.payload.get("campaign_id") is not None and poll_id is None:
        # Broad maintenance never takes over a manually approved campaign.
        # Its explicit recovery runner checks the exact root/env and supplies
        # the immutable poll id; no new HTTP authority is implied here.
        return None
    if reservation.payload.get("campaign_id") is not None:
        from investment_agent.supplemental_rules.disclosure_campaign import (
            assert_campaign_recovery_authority,
        )

        if recovery_operation_id is None or sample_env_id is None:
            raise InvestorError("disclosure_scope_forbidden", "manual orphan requires an exact recovery-only ticket")
        await assert_campaign_recovery_authority(
            session, permit_id=permit.permit_id, recovery_operation_id=recovery_operation_id,
            sample_env_id=sample_env_id,
        )
    reservation_payload, sent_payload = await _load_permit_receipts(session, permit)
    terminal = []
    for phase in ("result", "unknown", "canceled"):
        row = await repo.get_change_by_key(
            LOCAL_OWNER, f"p4:attempt:{attempt_id}:{hop_ordinal}:{phase}",
        )
        if row is not None:
            terminal.append(phase)
    if terminal:
        raise InvestorError("invalid_reference", "已终结HTTP hop仍持有来源活动令牌")
    old_lease = reservation_payload.get("lease")
    if not isinstance(old_lease, dict) or set(old_lease) != {
        "run_id", "worker_id", "attempt",
    } or (
        type(old_lease.get("run_id")) is not int or old_lease["run_id"] < 1
        or not isinstance(old_lease.get("worker_id"), str) or not old_lease["worker_id"]
        or type(old_lease.get("attempt")) is not int or old_lease["attempt"] < 1
    ):
        raise InvestorError("invalid_reference", "HTTP reservation缺少原run lease")
    old_run = await session.get(PipelineRun, old_lease["run_id"])
    if old_run is None:
        raise InvestorError("invalid_reference", "HTTP reservation引用的原run不存在")
    # A newer claim of the same run may already be resuming this reservation.
    # Do not race that worker by recovering based only on the stale lease tuple.
    if old_run.status in {"queued", "running"}:
        return None
    if sent_payload is None:
        await cancel_unsent_http_hop(
            session, permit=permit, lease=lease, now=current,
        )
        state = "canceled"
    else:
        await settle_http_hop_result(
            session, permit=permit, lease=lease, response_bytes=None,
            result_code="unknown_after_worker_recovery", now=current,
        )
        state = "unknown"
    return {
        "state": state,
        "poll_id": permit.identity.poll_id,
        "attempt_id": attempt_id,
        "hop_ordinal": hop_ordinal,
    }


async def rollover_reserved_http_hop(
    session: AsyncSession,
    *,
    old_permit: HttpHopPermit,
    next_identity: DisclosureAttemptIdentity,
    lease: RunLease,
    base_settings: Settings,
    now: datetime | None = None,
) -> HttpHopPermit:
    """Atomically release a provably-unsent old-day reservation and reserve anew.

    The caller commits the enclosing transaction once; the next attempt gets a
    fresh attempt UUID/ordinal but remains attached to the same logical request.
    """
    current = _now(now)
    if _day(current) == old_permit.shanghai_day:
        raise InvestorError("invalid_reference", "reservation尚未跨上海自然日")
    if (
        next_identity.logical_request_key != old_permit.identity.logical_request_key
        or next_identity.poll_id != old_permit.identity.poll_id
        or next_identity.attempt_ordinal != old_permit.identity.attempt_ordinal + 1
        or next_identity.hop_ordinal != old_permit.identity.hop_ordinal
        or next_identity.authorization_id != old_permit.identity.authorization_id
    ):
        raise InvestorError("invalid_reference", "跨日重预留必须沿原logical request创建下一attempt")
    await cancel_unsent_http_hop(session, permit=old_permit, lease=lease, now=current)
    return await reserve_http_hop(
        session, identity=next_identity,
        response_byte_limit=next_identity.response_bytes_limit,
        lease=lease, base_settings=base_settings, now=current,
    )

