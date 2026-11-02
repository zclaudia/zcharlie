"""Disclosure poll admission, resume, detail, and maintenance enqueue.

The polling handler stays in disclosure_updates.py. Names are re-exported
there so existing imports keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.supplemental_updates import (
    SupplementalChangeReceiptRow,
    SupplementalSourceStateRow,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    SOURCE_CONTRACT_VERSION,
    BackgroundDisclosureAuthorizationV1,
)
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_FEED,
    DISCLOSURE_POLL_KIND,
    LOCAL_OWNER,
    P4_ACTIVE_POLL_SCOPE_CAPACITY,
    P4_PENDING_RUN_CAPACITY,
    POLL_MIN_INTERVAL_SECONDS,
    DisclosurePollRequest,
    _aware_utc,
    _CampaignResumeCapability,
    _checkpoint,
    _checkpoint_attempt_counts,
    _checkpoint_native_ids,
    _json_wire,
    _pending_p4_run_count,
    _poll_settings_open,
    _receipt_ref,
    source_scope_hash,
    source_scope_key,
)


async def admit_disclosure_poll(
    session: AsyncSession,
    request: DisclosurePollRequest,
    *,
    settings: Settings,
    now: datetime | None = None,
    coverage_target_end: datetime | None = None,
) -> dict[str, Any]:
    """Persist a poll command and durable run; route handlers perform no HTTP."""
    from investment_agent.settings.loader import load_settings
    from investment_agent.supplemental_rules.disclosure_authorization import (
        get_current_authorization,
    )

    admitted_at = (now or datetime.now(UTC)).astimezone(UTC)
    target_value = coverage_target_end or request.window_end
    if target_value.tzinfo is None or target_value.utcoffset() is None:
        raise InvestorError("invalid_reference", "连续覆盖目标必须带时区")
    target_end = target_value.astimezone(UTC)

    repo = SupplementalUpdatesRepository(session)
    await repo.reserve(LOCAL_OWNER)
    request_hash = canonical_hash(
        {
            "command": "supplemental_disclosure.poll.v1",
            "path": "/supplemental-updates/polls",
            "body": request.model_dump(mode="json"),
            "coverage_target_end": target_end.isoformat(),
        }
    )
    prior = (
        await session.execute(
            select(SupplementalChangeReceiptRow).where(
                SupplementalChangeReceiptRow.owner_id == LOCAL_OWNER,
                SupplementalChangeReceiptRow.operation_id == str(request.operation_id),
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        if prior.request_hash != request_hash or prior.payload.get("record_type") != "poll_root_v1":
            raise InvestorError("idempotency_conflict", "轮询操作ID已用于不同请求")
        prior_run = await session.scalar(
            select(PipelineRun).where(
                PipelineRun.kind == DISCLOSURE_POLL_KIND,
                PipelineRun.idempotency_key == f"p4:disclosure:{request.operation_id}",
            )
        )
        return {
            "poll_id": prior.id,
            "poll_sequence": prior.sequence,
            "run_id": prior_run.id if prior_run else None,
            "state": prior.payload.get("state", "queued"),
            "replayed": True,
        }
    current = await load_settings(session, base=settings)
    if not (
        current.supplemental_rules_enabled
        and current.supplemental_updates_enabled
        and current.supplemental_disclosure_polling_enabled
        and current.disclosure_originals_enabled
    ):
        raise InvestorError("disclosure_polling_disabled", "来源轮询能力已关闭")
    grant = await get_current_authorization(session, request.authorization_id, now=admitted_at)
    if (
        grant.revision != request.authorization_revision
        or grant.authorization_hash != request.authorization_hash
        or request.listing_ref not in grant.listing_refs
        or request.window_start < grant.history_start
        or request.window_end > grant.history_end
    ):
        raise InvestorError("disclosure_authorization_required", "轮询范围超出当前授权")
    if (
        target_end < request.window_end.astimezone(UTC)
        or target_end > grant.history_end.astimezone(UTC)
        or target_end > admitted_at
    ):
        raise InvestorError("invalid_reference", "连续覆盖目标超出授权或轮询窗口")
    manual_campaign = isinstance(grant, BackgroundDisclosureAuthorizationV2)
    if manual_campaign:
        db_url = make_url(str(cast(Any, session.get_bind()).url))
        database = db_url.database or ""
        if (
            db_url.get_backend_name() != "sqlite"
            or not database
            or database == ":memory:"
            or database.startswith("file:")
            or db_url.query.get("mode") == "memory"
            or db_url.query.get("uri") in {True, "true", "1"}
        ):
            raise InvestorError(
                "disclosure_campaign_environment_mismatch",
                "manual campaign admission requires file-backed SQLite",
            )
    if manual_campaign and (
        not request.campaign_action_id
        or not request.campaign_profile_hash
        or not getattr(grant, "campaign_id", None)
        or not getattr(grant, "sample_env_id", None)
    ):
        raise InvestorError("disclosure_campaign_action_required", "手动授权轮询必须绑定精确campaign动作")
    listing_id = int(request.listing_ref.id)
    listing = await session.get(Listing, listing_id)
    if (
        listing is None
        or listing.market_code != "SZSE"
        or len(listing.ticker) != 6
        or not listing.ticker.isdigit()
    ):
        raise InvestorError("invalid_reference", "仅支持精确的SZSE六位证券代码")
    key = source_scope_key(request.listing_ref)
    scope_hash = source_scope_hash(request.listing_ref, grant.contract_hash)
    if request.window_end - request.window_start > timedelta(days=31):
        raise InvestorError("invalid_reference", "单次轮询窗口不能超过31天")
    if request.window_end > admitted_at:
        raise InvestorError("invalid_reference", "来源轮询窗口不能延伸到未来")
    if await _pending_p4_run_count(session) >= P4_PENDING_RUN_CAPACITY:
        raise InvestorError("source_capacity_exceeded", "持续更新待处理任务已达容量上限")
    active_scope_keys = (
        (
            await session.execute(
                select(SupplementalSourceStateRow.scope_key).where(
                    SupplementalSourceStateRow.owner_id == LOCAL_OWNER,
                    SupplementalSourceStateRow.feed_id == DISCLOSURE_FEED,
                    SupplementalSourceStateRow.payload["poll_state"]
                    .as_string()
                    .in_(("queued", "running", "retry_wait", "capacity_blocked")),
                )
            )
        )
        .scalars()
        .all()
    )
    active_scopes = sum(1 for active_key in active_scope_keys if active_key != key)
    if active_scopes >= P4_ACTIVE_POLL_SCOPE_CAPACITY:
        raise InvestorError("source_capacity_exceeded", "活动披露来源轮询已达容量上限")
    poll_id = uuid4()
    campaign_binding: dict[str, Any] | None = None
    if manual_campaign:
        # Campaign reserve, grant/day/poll quota reserve, root and run all share
        # this caller transaction. No HTTP can occur before the action is
        # durably bound to this exact poll identity.
        from investment_agent.supplemental_rules.disclosure_campaign import (
            authorize_disclosure_campaign_poll,
        )

        if not isinstance(request.campaign_action_id, str) or not isinstance(
            request.campaign_profile_hash, str
        ):
            raise InvestorError(
                "disclosure_campaign_action_required",
                "manual campaign action and profile hash are required",
            )
        bind = cast(Any, session.get_bind())
        db_url = make_url(str(bind.url))
        if db_url.get_backend_name() != "sqlite" or not db_url.database:
            raise InvestorError("disclosure_campaign_environment_mismatch", "campaign只允许绑定文件SQLite环境")
        approved_action = await authorize_disclosure_campaign_poll(
            session,
            grant=cast(BackgroundDisclosureAuthorizationV2, grant),
            poll_id=str(poll_id),
            action_id=request.campaign_action_id,
            listing_ref=request.listing_ref,
            window_start=request.window_start,
            window_end=request.window_end,
            sample_env_id=str(cast(BackgroundDisclosureAuthorizationV2, grant).sample_env_id),
            profile_hash=request.campaign_profile_hash,
            contract_hash=grant.contract_hash,
            now=admitted_at,
        )
        from investment_agent.evidence.disclosure_profiles import (
            SERVER_PROFILE_REGISTRY,
            assert_approved_binding,
        )
        from investment_agent.supplemental_rules.disclosure_campaign_contracts import CampaignAction

        profile = assert_approved_binding(
            SERVER_PROFILE_REGISTRY,
            cast(BackgroundDisclosureAuthorizationV2, grant),
            CampaignAction.model_validate(approved_action),
            now=admitted_at,
            sample_env_id=str(cast(BackgroundDisclosureAuthorizationV2, grant).sample_env_id),
        )
        if request.campaign_profile_hash != profile.profile.protocol.protocol_hash:
            raise InvestorError(
                "disclosure_campaign_profile_mismatch",
                "campaign profile hash must equal the approved protocol hash",
            )
        campaign_binding = {
            **approved_action,
            "campaign_id": str(cast(BackgroundDisclosureAuthorizationV2, grant).campaign_id),
            "sample_env_id": str(cast(BackgroundDisclosureAuthorizationV2, grant).sample_env_id),
            "profile_hash": request.campaign_profile_hash,
        }
    poll_http_cap = grant.per_poll_http
    poll_query_http_cap = grant.per_poll_query_http
    poll_fetch_cap = grant.per_poll_fetch
    poll_response_bytes_cap = grant.per_poll_response_bytes
    attempt_scope_hash = await _freeze_poll_quota(
        session,
        poll_id=str(poll_id),
            grant=cast(BackgroundDisclosureAuthorizationV1, grant),
        request=request,
        poll_http_cap=poll_http_cap,
        poll_query_http_cap=poll_query_http_cap,
        poll_fetch_cap=poll_fetch_cap,
        poll_response_bytes_cap=poll_response_bytes_cap,
        now=admitted_at,
    )
    source = await repo.ensure_source(
        LOCAL_OWNER,
        DISCLOSURE_FEED,
        key,
        grant.contract_hash,
        contract_version=SOURCE_CONTRACT_VERSION,
        listing_id=listing_id,
        context="company_research",
        payload={
            "schema_version": 1,
            "authorization_id": str(grant.authorization_id),
            "authorization_revision": grant.revision,
            "index_watermark": None,
            "original_watermark": None,
            "bridge_pending": 0,
            "history_start": grant.history_start.isoformat(),
            "authorized_history_end": grant.history_end.isoformat(),
            "history_frontier": None,
            "history_gap": True,
            "protocol_profile": "live",
            "protocol_status": "unvalidated",
            "active_poll_id": None,
            "active_step_run_id": None,
            "poll_state": "unvalidated",
            "source_scope_hash": scope_hash,
        },
    )
    if source.contract_version != SOURCE_CONTRACT_VERSION:
        raise InvestorError("revision_conflict", "来源合同版本不匹配")
    if source.revision != request.expected_source_revision:
        raise InvestorError("revision_conflict", "来源scope状态已变化")
    semantic_generation_base = source.payload.get("semantic_generation", 0)
    if type(semantic_generation_base) is not int or semantic_generation_base < 0:
        raise InvestorError("invalid_reference", "来源scope语义代次损坏")
    predecessor_root: Any | None = None
    predecessor_checkpoint: dict[str, Any] | None = None
    predecessor_poll_state: str | None = None
    predecessor_pending_ids: list[str] = []
    active_poll_id = source.payload.get("active_poll_id")
    current_poll_state = source.payload.get("poll_state")
    unresolved_predecessor = source.payload.get("pending_predecessor")
    if active_poll_id is not None and not isinstance(active_poll_id, str):
        raise InvestorError("invalid_reference", "活动轮询引用损坏")
    if unresolved_predecessor is not None and not isinstance(unresolved_predecessor, dict):
        raise InvestorError("invalid_reference", "前序轮询待处理状态损坏")
    requires_replacement = isinstance(active_poll_id, str) and (
        current_poll_state != "completed" or isinstance(unresolved_predecessor, dict)
    )
    if isinstance(unresolved_predecessor, dict) and not isinstance(active_poll_id, str):
        raise InvestorError("invalid_reference", "待处理前序轮询缺少活动poll引用")
    if isinstance(active_poll_id, str) and requires_replacement:
        if request.replace_poll_id is None or str(request.replace_poll_id) != active_poll_id:
            raise InvestorError("source_poll_replacement_required", "未完成轮询必须显式指定替换对象")
        try:
            predecessor_root = await repo.get_change(LOCAL_OWNER, UUID(active_poll_id))
        except ValueError as exc:
            raise InvestorError("invalid_reference", "活动轮询根收据损坏") from exc
        if (
            predecessor_root is None
            or predecessor_root.payload.get("record_type") != "poll_root_v1"
            or predecessor_root.listing_id != listing_id
            or predecessor_root.context != "company_research"
            or predecessor_root.payload.get("listing_ref")
            != request.listing_ref.model_dump(mode="json")
        ):
            raise InvestorError("invalid_reference", "前序轮询与来源scope不匹配")
        predecessor_run_id = source.payload.get("active_step_run_id")
        predecessor_run = (
            await session.get(PipelineRun, predecessor_run_id)
            if type(predecessor_run_id) is int
            else None
        )
        if predecessor_run is not None and predecessor_run.status in {"queued", "running"}:
            raise InvestorError("source_poll_active", "前序轮询step仍在运行，不能替换")
        raw_predecessor_checkpoint = source.payload.get("poll_checkpoint")
        if not isinstance(raw_predecessor_checkpoint, dict):
            raise InvestorError("invalid_reference", "前序活动轮询缺少有效续点")
        predecessor_checkpoint = dict(raw_predecessor_checkpoint)
        predecessor_poll_state = str(current_poll_state or "partial")
        predecessor_pending_ids = _checkpoint_native_ids(predecessor_checkpoint)
    elif isinstance(active_poll_id, str) and not requires_replacement and request.replace_poll_id is not None:
        raise InvestorError("revision_conflict", "指定的替换轮询已不是未完成活动任务")
    elif requires_replacement:
        raise InvestorError("invalid_reference", "活动轮询标记损坏或缺失")
    minimum_poll_interval = max(POLL_MIN_INTERVAL_SECONDS, grant.min_poll_interval_seconds)
    last_poll_started = source.payload.get("last_poll_started_at")
    if isinstance(last_poll_started, str) and admitted_at - datetime.fromisoformat(
        last_poll_started
    ) < timedelta(seconds=minimum_poll_interval):
        raise InvestorError("source_cooldown", "来源最小轮询间隔尚未到期")
    auth_state = await SupplementalUpdatesRepository(session).get_source(
        LOCAL_OWNER, "cninfo_szse_auth_v1", f"grant:{grant.authorization_id}"
    )
    auth_payload = auth_state.payload if auth_state and isinstance(auth_state.payload, dict) else {}
    grant_receipt_ref = {
        "sequence": auth_payload.get("authorization_receipt_sequence"),
        "id": auth_payload.get("authorization_receipt_id"),
        "payload_hash": auth_payload.get("authorization_receipt_hash"),
    }
    if not (
        isinstance(grant_receipt_ref["sequence"], int)
        and isinstance(grant_receipt_ref["id"], str)
        and isinstance(grant_receipt_ref["payload_hash"], str)
    ):
        raise InvestorError("invalid_reference", "来源授权缺少权威收据")
    predecessor_snapshot_ref: dict[str, Any] | None = None
    inherited_attempt_ordinals: dict[str, int] = {}
    pending_before_replacement = source.payload.get("pending_predecessor")
    pending_snapshot: dict[str, Any] | None = None
    pending_snapshot_refs: list[dict[str, Any]] = []
    pending_ids_for_new_poll: list[str] = []
    pending_start_for_new_poll: datetime | None = None
    pending_end_for_new_poll: datetime | None = None
    if pending_before_replacement is not None:
        if not isinstance(pending_before_replacement, dict):
            raise InvestorError("invalid_reference", "前序轮询待处理状态损坏")
        raw_pending_ids = pending_before_replacement.get("pending_native_ids")
        pending_start_for_new_poll = _aware_utc(pending_before_replacement.get("window_start"))
        pending_end_for_new_poll = _aware_utc(pending_before_replacement.get("window_end"))
        raw_snapshot_ref = pending_before_replacement.get("snapshot_ref")
        raw_snapshot_refs = pending_before_replacement.get("snapshot_refs")
        if raw_snapshot_refs is None:
            raw_snapshot_refs = [raw_snapshot_ref]
        if (
            not isinstance(raw_pending_ids, list)
            or any(not isinstance(item, str) or not item for item in raw_pending_ids)
            or pending_start_for_new_poll is None
            or pending_end_for_new_poll is None
            or pending_end_for_new_poll <= pending_start_for_new_poll
            or not isinstance(raw_snapshot_ref, dict)
            or not isinstance(raw_snapshot_refs, list)
            or any(not isinstance(ref, dict) for ref in raw_snapshot_refs)
            or len(raw_snapshot_refs) > 20
        ):
            raise InvestorError("invalid_reference", "前序轮询待处理范围损坏")
        pending_ids_for_new_poll = sorted(set(raw_pending_ids))
        pending_snapshot = raw_snapshot_ref
        pending_snapshot_refs = list(raw_snapshot_refs)
    if predecessor_root is not None and predecessor_checkpoint is not None:
        old_start = _aware_utc(predecessor_root.payload.get("window_start"))
        old_end = _aware_utc(predecessor_root.payload.get("window_end"))
        if old_start is None or old_end is None or old_end <= old_start:
            raise InvestorError("invalid_reference", "前序轮询窗口损坏")
        pending_ids_for_new_poll = sorted(
            set(pending_ids_for_new_poll).union(predecessor_pending_ids)
        )
        if len(pending_ids_for_new_poll) > 200:
            raise InvestorError("capacity_blocked", "前序待处理公告累计超过200项边界")
        if len(pending_snapshot_refs) >= 20:
            raise InvestorError("capacity_blocked", "前序轮询快照链超过20项审计边界")
        pending_start_for_new_poll = min(
            item for item in (pending_start_for_new_poll, old_start) if item is not None
        )
        pending_end_for_new_poll = max(
            item for item in (pending_end_for_new_poll, old_end) if item is not None
        )
        snapshot = await repo.append_change(
            LOCAL_OWNER,
            change_key=f"p4:disclosure-checkpoint-snapshot:{predecessor_root.id}",
            kind="source_attempt",
            parent_sequence=predecessor_root.sequence,
            listing_id=listing_id,
            context="company_research",
            payload={
                "record_type": "poll_checkpoint_snapshot_v1",
                "poll_id": str(predecessor_root.id),
                "old_root_ref": _receipt_ref(predecessor_root),
                "listing_ref": request.listing_ref.model_dump(mode="json"),
                "context": "company_research",
                "window_start": predecessor_root.payload["window_start"],
                "window_end": predecessor_root.payload["window_end"],
                "pending_window_start": pending_start_for_new_poll.isoformat(),
                "pending_window_end": pending_end_for_new_poll.isoformat(),
                "poll_state": predecessor_poll_state,
                "source_revision": source.revision,
                "checkpoint": _json_wire(predecessor_checkpoint),
                "pending_native_ids": pending_ids_for_new_poll,
                "pending_native_ids_hash": canonical_hash(pending_ids_for_new_poll),
                "prior_pending_predecessor": _json_wire(pending_before_replacement),
                "superseded_by": str(poll_id),
                "reason": request.replace_reason,
                "captured_at": admitted_at.isoformat(),
            },
            acquired_at=admitted_at,
            available_from=admitted_at,
            committed_at=admitted_at,
        )
        predecessor_snapshot_ref = _receipt_ref(snapshot)
        pending_snapshot = predecessor_snapshot_ref
        pending_snapshot_refs.append(predecessor_snapshot_ref)
        inherited_attempt_ordinals = _checkpoint_attempt_counts(predecessor_checkpoint)
    elif isinstance(pending_before_replacement, dict):
        pending_snapshot = pending_before_replacement.get("snapshot_ref")
        inherited_attempt_ordinals = _checkpoint_attempt_counts(_checkpoint(source.payload))
    payload = {
        "record_type": "poll_root_v1",
        "operation_id": str(request.operation_id),
        "poll_id": str(poll_id),
        "authorization_id": str(grant.authorization_id),
        "authorization_revision": grant.revision,
        "authorization_hash": grant.authorization_hash,
        "contract_hash": grant.contract_hash,
        "source_scope_hash": scope_hash,
        "attempt_scope_hash": attempt_scope_hash,
        "grant_receipt_ref": grant_receipt_ref,
        "predecessor_poll_ref": str(predecessor_root.id) if predecessor_root is not None else (
            pending_before_replacement.get("poll_id")
            if isinstance(pending_before_replacement, dict)
            else None
        ),
        "predecessor_snapshot_ref": predecessor_snapshot_ref or pending_snapshot,
        "predecessor_snapshot_refs": pending_snapshot_refs,
        "predecessor_pending_native_ids": pending_ids_for_new_poll,
        "predecessor_pending_ids_hash": canonical_hash(pending_ids_for_new_poll),
        "listing_ref": request.listing_ref.model_dump(mode="json"),
        "window_start": request.window_start.isoformat(),
        "window_end": request.window_end.isoformat(),
        "coverage_target_end": target_end.isoformat(),
        # Freeze the semantic counter and the last successful proof at
        # admission. Candidate ingestion may advance the mutable projection
        # before this poll seals; the final proves that bounded increment.
        "semantic_generation_base": semantic_generation_base,
        "semantic_generation_base_final_ref": source.payload.get("final_ref"),
        "protocol_profile": "live",
        "protocol_status": "unvalidated",
        "campaign_binding": campaign_binding,
        "poll_http_cap": poll_http_cap,
        "poll_query_http_cap": poll_query_http_cap,
        "poll_fetch_cap": poll_fetch_cap,
        "poll_response_bytes_cap": poll_response_bytes_cap,
        "state": "blocked_protocol_unvalidated",
        "page_ordinal": 0,
        "documents_pending": [],
        "retry_at": None,
    }
    run_key = f"p4:disclosure:{request.operation_id}"
    run, created = await PipelineRunRepository(session).enqueue(
        kind=DISCLOSURE_POLL_KIND,
        idempotency_key=run_key,
        triggered_by="supplemental_disclosure_poll",
        request_json=_json_wire(
            {
                "record_type": "poll_admission_v1",
                "schema_version": 1,
                "owner_id": LOCAL_OWNER,
                "operation_id": str(request.operation_id),
                "campaign_id": str(cast(BackgroundDisclosureAuthorizationV2, grant).campaign_id)
                if manual_campaign else None,
                "campaign_action_id": request.campaign_action_id if manual_campaign else None,
                "sample_env_id": str(cast(BackgroundDisclosureAuthorizationV2, grant).sample_env_id)
                if manual_campaign else None,
            }
        ),
        # A manual campaign poll is advanced only by a new, explicitly
        # approved exact run. Worker-level retry would bypass that action gate.
        max_attempts=1 if manual_campaign else 3,
    )
    payload["run_id"] = run.id
    receipt = await repo.append_change(
        LOCAL_OWNER,
        change_key=f"p4:disclosure-poll:{request.operation_id}",
        kind="source_attempt",
        operation_id=request.operation_id,
        request_hash=request_hash,
        listing_id=listing_id,
        context="company_research",
        receipt_id=poll_id,
        payload=payload,
    )
    if predecessor_root is not None and predecessor_snapshot_ref is not None:
        from investment_agent.supplemental_rules.disclosure_authorization import (
            inherit_poll_quota,
        )

        await inherit_poll_quota(
            session,
            new_poll_id=receipt.id,
            old_poll_id=predecessor_root.id,
            replacement_ref=predecessor_snapshot_ref,
            now=admitted_at,
        )
    run.request_json = {
        **run.request_json,
        "record_type": "poll_step_v1",
        "poll_sequence": receipt.sequence,
        "poll_id": str(receipt.id),
        "poll_hash": receipt.payload_hash,
        "source_scope_key": key,
        "source_scope_revision": source.revision + 1,
    }
    await repo.save_source(
        source.model_copy(
            update={
                "revision": source.revision + 1,
                "window_start": request.window_start,
                "window_end": request.window_end,
                "scope_hash": scope_hash,
                "contract_version": SOURCE_CONTRACT_VERSION,
                "payload": {
                    **source.payload,
                    "authorization_id": str(grant.authorization_id),
                    "authorization_revision": grant.revision,
                    "grant_receipt_ref": grant_receipt_ref,
                    "source_scope_hash": scope_hash,
                    "active_poll_id": str(receipt.id),
                    "last_poll_started_at": admitted_at.isoformat(),
                    "active_step_run_id": run.id,
                    "poll_state": "queued",
                    "poll_checkpoint": {
                        "stage": "directory",
                        "page_num": 1,
                        "attempt_ordinal": 0,
                        "logical_attempt_ordinals": inherited_attempt_ordinals,
                        "campaign_binding": campaign_binding,
                    },
                    "pending_predecessor": (
                        {
                            "poll_id": str(predecessor_root.id),
                            "snapshot_ref": predecessor_snapshot_ref,
                            "snapshot_refs": pending_snapshot_refs,
                            "window_start": pending_start_for_new_poll.isoformat(),
                            "window_end": pending_end_for_new_poll.isoformat(),
                            "pending_native_ids": pending_ids_for_new_poll,
                        }
                        if (
                            predecessor_root is not None
                            and predecessor_snapshot_ref is not None
                            and pending_start_for_new_poll is not None
                            and pending_end_for_new_poll is not None
                        )
                        else source.payload.get("pending_predecessor")
                    ),
                    "retry_at": None,
                    "protocol_profile": "live",
                    "protocol_status": "unvalidated",
                        "campaign_binding": campaign_binding,
                },
            }
        ),
        expected_revision=source.revision,
    )
    return {
        "poll_id": str(receipt.id),
        "poll_sequence": receipt.sequence,
        "run_id": run.id,
        "state": "queued" if created else "existing",
    }


async def _freeze_poll_quota(
    session: AsyncSession,
    *,
    poll_id: str,
    grant: BackgroundDisclosureAuthorizationV1,
    request: DisclosurePollRequest,
    poll_http_cap: int,
    poll_query_http_cap: int,
    poll_fetch_cap: int,
    poll_response_bytes_cap: int,
    now: datetime,
) -> str:
    from investment_agent.supplemental_rules.disclosure_authorization import freeze_poll_quota

    return await freeze_poll_quota(
        session,
        poll_id=poll_id,
        authorization=grant,
        listing_ref=request.listing_ref,
        window_start=request.window_start,
        window_end=request.window_end,
        poll_http_cap=poll_http_cap,
        poll_query_http_cap=poll_query_http_cap,
        poll_fetch_cap=poll_fetch_cap,
        poll_response_bytes_cap=poll_response_bytes_cap,
        now=now,
    )


async def resume_disclosure_poll(
    session: AsyncSession,
    poll_id: UUID,
    operation_id: UUID,
    expected_revision: int,
    reason: str,
    *,
    settings: Settings,
    now: datetime | None = None,
    _campaign_resume: _CampaignResumeCapability | None = None,
) -> dict[str, Any]:
    """Create a new durable step generation for safe pending source work."""
    if not reason.strip() or len(reason) > 256:
        raise InvestorError("invalid_request", "恢复原因长度无效")
    repo = SupplementalUpdatesRepository(session)
    await repo.reserve(LOCAL_OWNER)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    root = await repo.get_change(LOCAL_OWNER, poll_id)
    if root is None or root.payload.get("record_type") != "poll_root_v1":
        raise InvestorError("not_found", "来源轮询不存在")
    campaign_binding = root.payload.get("campaign_binding")
    if campaign_binding is not None:
        if (
            _campaign_resume is None
            or _campaign_resume.poll_id != str(poll_id)
            or _campaign_resume.campaign_id != campaign_binding.get("campaign_id")
            or _campaign_resume.action_id != campaign_binding.get("action_id")
            or _campaign_resume.grant_id != root.payload.get("authorization_id")
            or _campaign_resume.grant_revision != root.payload.get("authorization_revision")
            or _campaign_resume.grant_hash != root.payload.get("authorization_hash")
            or _campaign_resume.sample_env_id != campaign_binding.get("sample_env_id")
            or _campaign_resume.operation_id != str(operation_id)
        ):
            raise InvestorError(
                "disclosure_campaign_resume_required",
                "manual campaign resume requires its exact internal approval ticket",
            )
        from investment_agent.supplemental_rules.disclosure_campaign import (
            assert_campaign_resume_authority,
        )

        await assert_campaign_resume_authority(
            session,
            campaign_id=UUID(_campaign_resume.campaign_id),
            poll_id=str(poll_id),
            action_id=_campaign_resume.action_id,
            grant_id=UUID(_campaign_resume.grant_id),
            grant_revision=_campaign_resume.grant_revision,
            grant_hash=_campaign_resume.grant_hash,
            sample_env_id=_campaign_resume.sample_env_id,
            operation_id=UUID(_campaign_resume.operation_id),
            resume_approval_ref=_campaign_resume.resume_approval_ref,
            now=current_time,
        )
    elif _campaign_resume is not None:
        raise InvestorError(
            "disclosure_campaign_binding_invalid",
            "campaign resume ticket cannot be used for a non-campaign poll",
        )
    op_hash = canonical_hash(
        {
            "method": "POST",
            "path": f"/supplemental-updates/polls/{poll_id}/resume",
            "body": {
                "operation_id": str(operation_id),
                "expected_revision": expected_revision,
                "reason": reason,
                **({
                    "campaign_resume": {
                        "campaign_id": _campaign_resume.campaign_id,
                        "action_id": _campaign_resume.action_id,
                        "grant_id": _campaign_resume.grant_id,
                        "grant_revision": _campaign_resume.grant_revision,
                        "grant_hash": _campaign_resume.grant_hash,
                        "sample_env_id": _campaign_resume.sample_env_id,
                        "operation_id": _campaign_resume.operation_id,
                        "resume_approval_ref": _campaign_resume.resume_approval_ref,
                    }
                } if _campaign_resume is not None else {}),
            },
        }
    )
    prior = (
        await session.execute(
            select(SupplementalChangeReceiptRow).where(
                SupplementalChangeReceiptRow.owner_id == LOCAL_OWNER,
                SupplementalChangeReceiptRow.operation_id == str(operation_id),
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        if (
            prior.kind != "source_attempt"
            or prior.request_hash != op_hash
            or prior.payload.get("record_type") != "poll_resume_v1"
        ):
            raise InvestorError("idempotency_conflict", "恢复操作ID已用于不同请求")
        run = await PipelineRunRepository(session).by_idempotency_key(
            kind=DISCLOSURE_POLL_KIND,
            idempotency_key=f"p4:disclosure-resume:{operation_id}",
        )
        if run is None:
            raise InvestorError("idempotency_conflict", "恢复收据与运行队列不匹配")
        return {
            "poll_id": str(poll_id),
            "resume_sequence": prior.sequence,
            "run_id": run.id,
            "state": "queued",
            "replayed": True,
        }
    if await _pending_p4_run_count(session) >= P4_PENDING_RUN_CAPACITY:
        raise InvestorError("source_capacity_exceeded", "持续更新待处理任务已达容量上限")
    from investment_agent.settings.loader import load_settings
    from investment_agent.supplemental_rules.disclosure_authorization import (
        get_current_authorization,
    )

    current = await load_settings(session, base=settings)
    if not (
        current.supplemental_rules_enabled
        and current.supplemental_updates_enabled
        and current.supplemental_disclosure_polling_enabled
        and current.disclosure_originals_enabled
    ):
        raise InvestorError("disclosure_polling_disabled", "来源轮询能力已关闭")
    listing_ref = ObjectRef.model_validate(root.payload.get("listing_ref"))
    scope_key = source_scope_key(listing_ref)
    state = await repo.get_source(LOCAL_OWNER, DISCLOSURE_FEED, scope_key, for_update=True)
    if state is None or state.revision != expected_revision:
        raise InvestorError("revision_conflict", "来源scope版本已变化")
    if state.payload.get("active_poll_id") != str(poll_id):
        raise InvestorError("revision_conflict", "该轮询已不再是当前活动任务")
    allowed_states = {"partial", "failed", "retry_wait", "paused"}
    if _campaign_resume is not None:
        allowed_states.update({"manual_recovery_required", "manual_review_required"})
    if state.payload.get("poll_state") not in allowed_states:
        raise InvestorError("invalid_reference", "仅可恢复部分、失败或等待重试的轮询")
    if state.retry_at is not None and state.retry_at.astimezone(UTC) > current_time:
        raise InvestorError("source_cooldown", "来源重试等待尚未到期")
    grant = await get_current_authorization(
        session, UUID(str(root.payload.get("authorization_id"))), now=current_time
    )
    if (
        grant.revision != root.payload.get("authorization_revision")
        or grant.authorization_hash != root.payload.get("authorization_hash")
        or grant.contract_hash != root.payload.get("contract_hash")
        or listing_ref not in grant.listing_refs
        or datetime.fromisoformat(root.payload["window_start"]) < grant.history_start
        or datetime.fromisoformat(root.payload["window_end"]) > grant.history_end
    ):
        raise InvestorError("disclosure_authorization_required", "恢复范围不再受当前授权覆盖")
    if _campaign_resume is not None:
        if (
            not isinstance(grant, BackgroundDisclosureAuthorizationV2)
            or grant.authorization_hash != _campaign_resume.grant_hash
            or grant.revision != _campaign_resume.grant_revision
            or grant.campaign_id != UUID(_campaign_resume.campaign_id)
            or grant.sample_env_id != _campaign_resume.sample_env_id
            or grant.contract_hash != root.payload.get("contract_hash")
        ):
            raise InvestorError(
                "disclosure_campaign_binding_invalid",
                "resume ticket does not match the current grant and original poll",
            )
        from investment_agent.supplemental_rules.disclosure_campaign import (
            assert_campaign_resume_authority,
        )

        await assert_campaign_resume_authority(
            session,
            campaign_id=UUID(_campaign_resume.campaign_id),
            poll_id=str(poll_id),
            action_id=_campaign_resume.action_id,
            grant_id=UUID(_campaign_resume.grant_id),
            grant_revision=_campaign_resume.grant_revision,
            grant_hash=_campaign_resume.grant_hash,
            sample_env_id=_campaign_resume.sample_env_id,
            operation_id=UUID(_campaign_resume.operation_id),
            resume_approval_ref=_campaign_resume.resume_approval_ref,
            now=current_time,
        )
    receipt = await repo.append_change(
        LOCAL_OWNER,
        change_key=f"p4:disclosure-resume:{operation_id}",
        kind="source_attempt",
        operation_id=operation_id,
        request_hash=op_hash,
        parent_sequence=root.sequence,
        payload={
            "record_type": "poll_resume_v1",
            "poll_id": str(poll_id),
            "parent_sequence": root.sequence,
            "expected_revision": expected_revision,
            "reason": reason,
            "state": "queued",
            **({
                "campaign_resume": {
                    "campaign_id": _campaign_resume.campaign_id,
                    "action_id": _campaign_resume.action_id,
                    "grant_id": _campaign_resume.grant_id,
                    "grant_revision": _campaign_resume.grant_revision,
                    "grant_hash": _campaign_resume.grant_hash,
                    "sample_env_id": _campaign_resume.sample_env_id,
                    "operation_id": _campaign_resume.operation_id,
                    "resume_approval_ref": _campaign_resume.resume_approval_ref,
                },
            } if _campaign_resume is not None else {}),
        },
    )
    run, _created = await PipelineRunRepository(session).enqueue(
        kind=DISCLOSURE_POLL_KIND,
        idempotency_key=f"p4:disclosure-resume:{operation_id}",
        triggered_by="supplemental_disclosure_resume",
        request_json=_json_wire(
            {
                "record_type": "poll_step_v1",
                "schema_version": 1,
                "owner_id": LOCAL_OWNER,
                "poll_sequence": root.sequence,
                "poll_id": str(root.id),
                "poll_hash": root.payload_hash,
                "resume_sequence": receipt.sequence,
                "source_scope_key": scope_key,
                "source_scope_revision": state.revision + 1,
                **({
                    "campaign_id": _campaign_resume.campaign_id,
                    "campaign_action_id": _campaign_resume.action_id,
                    "grant_id": _campaign_resume.grant_id,
                    "grant_revision": _campaign_resume.grant_revision,
                    "grant_hash": _campaign_resume.grant_hash,
                    "sample_env_id": _campaign_resume.sample_env_id,
                    "resume_operation_id": _campaign_resume.operation_id,
                    "resume_approval_ref": _campaign_resume.resume_approval_ref,
                    "manual_campaign_resume": True,
                } if _campaign_resume is not None else {}),
            }
        ),
        max_attempts=1 if _campaign_resume is not None else 3,
    )
    await repo.save_source(
        state.model_copy(
            update={
                "revision": state.revision + 1,
                "retry_at": None,
                "payload": {
                    **state.payload,
                    "active_step_run_id": run.id,
                    "poll_state": "queued",
                    "retry_at": None,
                    "resume_sequence": receipt.sequence,
                },
            }
        ),
        expected_revision=state.revision,
    )
    return {
        "poll_id": str(poll_id),
        "resume_sequence": receipt.sequence,
        "run_id": run.id,
        "state": "queued",
        "replayed": False,
    }


async def disclosure_poll_detail(
    session: AsyncSession,
    poll_id: UUID,
    *,
    owner_id: OwnerId = LOCAL_OWNER,
) -> dict[str, Any] | None:
    receipt = await SupplementalUpdatesRepository(session).get_change(owner_id, poll_id)
    if receipt is None or receipt.payload.get("record_type") != "poll_root_v1":
        return None
    detail = {
        "poll_id": str(receipt.id),
        "sequence": receipt.sequence,
        "payload_hash": receipt.payload_hash,
        **receipt.payload,
    }
    snapshot = await SupplementalUpdatesRepository(session).get_change_by_key(
        owner_id,
        f"p4:disclosure-checkpoint-snapshot:{receipt.id}",
    )
    if (
        snapshot is not None
        and snapshot.payload.get("record_type") == "poll_checkpoint_snapshot_v1"
        and snapshot.payload.get("poll_id") == str(receipt.id)
    ):
        detail.update(
            {
                "archived": True,
                "archived_state": snapshot.payload.get("poll_state"),
                "archived_checkpoint": snapshot.payload.get("checkpoint"),
                "archived_window_start": snapshot.payload.get("window_start"),
                "archived_window_end": snapshot.payload.get("window_end"),
                "superseded_by": snapshot.payload.get("superseded_by"),
                "snapshot_ref": _receipt_ref(snapshot),
            }
        )
    else:
        detail["archived"] = False
    return detail


async def maintain_disclosure_polls(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """Queue one leased maintenance quantum; never mutates poll state inline."""
    from investment_agent.settings.loader import load_settings

    # Serialize distinct wake buckets before checking the shared A+B queue
    # cap; otherwise concurrent wakes can both observe slot 127 and enqueue.
    await SupplementalUpdatesRepository(session).reserve(LOCAL_OWNER)
    current = await load_settings(session, base=settings)
    if not (
        current.supplemental_rules_enabled
        and current.supplemental_updates_enabled
        and current.supplemental_disclosure_polling_enabled
        and current.disclosure_originals_enabled
    ):
        return 0
    active_maintenance = await session.scalar(
        select(PipelineRun.id)
        .where(
            PipelineRun.kind == DISCLOSURE_POLL_KIND,
            PipelineRun.status.in_(("queued", "running")),
            PipelineRun.request_json["record_type"].as_string() == "poll_maintenance_v1",
        )
        .limit(1)
    )
    if active_maintenance is not None:
        return 0
    if await _pending_p4_run_count(session) >= P4_PENDING_RUN_CAPACITY:
        return 0
    current_at = (now or datetime.now(UTC)).astimezone(UTC)
    bucket = int(current_at.timestamp()) // 5
    maintenance_id = uuid5(NAMESPACE_URL, f"p4:disclosure-maintenance:{bucket}")
    _run, created = await PipelineRunRepository(session).enqueue(
        kind=DISCLOSURE_POLL_KIND,
        idempotency_key=f"p4:disclosure-maintenance:{bucket}",
        triggered_by="supplemental_disclosure_maintenance",
        request_json=_json_wire(
            {
                "record_type": "poll_maintenance_v1",
                "schema_version": 1,
                "owner_id": LOCAL_OWNER,
                "operation_id": str(maintenance_id),
            }
        ),
        max_attempts=3,
    )
    return 1 if created else 0


async def _enqueue_active_disclosure_poll_steps(
    session: AsyncSession,
    *,
    settings: Settings,
    job: ClaimedJob,
    now: datetime,
) -> int:
    """Recover accepted poll checkpoints under the maintenance run lease."""
    from investment_agent.settings.loader import load_settings
    from investment_agent.supplemental_rules.disclosure_authorization import (
        get_current_authorization,
    )
    from investment_agent.supplemental_rules.disclosure_candidate_ingest import (
        assert_run_lease,
    )

    await assert_run_lease(session, job.lease)
    current = await load_settings(session, base=settings)
    if not _poll_settings_open(current):
        return 0
    repo = SupplementalUpdatesRepository(session)
    await repo.reserve(LOCAL_OWNER)
    states = (
        (
            await session.execute(
                select(SupplementalSourceStateRow)
                .where(
                    SupplementalSourceStateRow.owner_id == LOCAL_OWNER,
                    SupplementalSourceStateRow.feed_id == DISCLOSURE_FEED,
                    SupplementalSourceStateRow.payload["poll_state"]
                    .as_string()
                    .in_(("queued", "retry_wait", "running", "capacity_blocked")),
                    # Ordinary maintenance never enumerates or resumes a
                    # manual campaign poll. Its checkpoint belongs exclusively
                    # to the exact campaign runner/recovery call.
                    SupplementalSourceStateRow.payload["campaign_binding"]["campaign_id"].as_string().is_(None),
                )
                .order_by(
                    SupplementalSourceStateRow.last_attempt_at.asc().nullsfirst(),
                    SupplementalSourceStateRow.scope_key,
                )
                .limit(P4_ACTIVE_POLL_SCOPE_CAPACITY)
            )
        )
        .scalars()
        .all()
    )
    queued = 0
    pending_runs = await _pending_p4_run_count(session, exclude_run_id=job.run_id)
    for row in states:
        if queued >= P4_ACTIVE_POLL_SCOPE_CAPACITY or pending_runs >= P4_PENDING_RUN_CAPACITY:
            break
        state = await repo.get_source(LOCAL_OWNER, row.feed_id, row.scope_key, for_update=True)
        if state is None:
            continue
        payload = state.payload
        poll_id = payload.get("active_poll_id")
        if not isinstance(poll_id, str):
            continue
        retry_at = payload.get("retry_at")
        if isinstance(retry_at, str) and datetime.fromisoformat(retry_at) > now:
            continue
        run_id = payload.get("active_step_run_id")
        if isinstance(run_id, int):
            previous = await session.get(PipelineRun, run_id)
            if previous is not None and previous.status in {"queued", "running"}:
                continue
        try:
            root = await repo.get_change(LOCAL_OWNER, poll_id)
            if root is None or root.payload.get("record_type") != "poll_root_v1":
                continue
            grant = await get_current_authorization(
                session,
                UUID(str(root.payload.get("authorization_id"))),
                owner_id=LOCAL_OWNER,
                now=now,
            )
        except (InvestorError, ValueError):
            continue
        if (
            grant.revision != root.payload.get("authorization_revision")
            or grant.authorization_hash != root.payload.get("authorization_hash")
            or grant.contract_hash != root.payload.get("contract_hash")
        ):
            continue
        run, created = await PipelineRunRepository(session).enqueue(
            kind=DISCLOSURE_POLL_KIND,
            idempotency_key=f"p4:disclosure-maintain:{poll_id}:{state.revision}",
            triggered_by="supplemental_disclosure_maintenance",
            listing_id=state.listing_id,
            request_json=_json_wire(
                {
                    "record_type": "poll_step_v1",
                    "schema_version": 1,
                    "owner_id": LOCAL_OWNER,
                    "poll_sequence": root.sequence,
                    "poll_id": str(root.id),
                    "poll_hash": root.payload_hash,
                    "source_scope_key": state.scope_key,
                    "source_scope_revision": state.revision + 1,
                }
            ),
            max_attempts=3,
        )
        if not created:
            continue
        await repo.save_source(
            state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "payload": {
                        **payload,
                        "active_step_run_id": run.id,
                        "poll_state": "queued",
                        "retry_at": None,
                    },
                }
            ),
            expected_revision=state.revision,
        )
        queued += 1
        pending_runs += 1
    return queued
