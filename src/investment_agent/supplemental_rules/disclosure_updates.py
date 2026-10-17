"""Lease-fenced steps for the bounded CNInfo annual-report source adapter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import DisclosureError, SourceResponse, validate_url
from investment_agent.evidence.disclosure_pages import (
    DisclosureBackgroundFetchV1,
    DisclosurePageRequestV1,
    page_url_body,
    parse_page_response,
    validate_pdf,
)
from investment_agent.investor.common import Contract, ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.disclosure import DisclosureBlob
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
    DisclosureAttemptIdentity,
    HttpHopPermit,
    HttpHopSettlement,
)

LOCAL_OWNER: OwnerId = "local_owner"
DISCLOSURE_POLL_KIND = "supplemental_disclosure_poll"
DISCLOSURE_FEED = "cninfo_szse_v1"
PAGE_STEP_SIZE = 20
POLL_MIN_INTERVAL_SECONDS = 6 * 60 * 60
MAX_QUERY_PAGES = 10
P4_PENDING_RUN_CAPACITY = 128
P4_ACTIVE_POLL_SCOPE_CAPACITY = 3
P4_RUN_KINDS = ("supplemental_update_process", DISCLOSURE_POLL_KIND)


@dataclass(frozen=True, slots=True)
class _CampaignDispatchCapability:
    run_id: int
    poll_id: str
    campaign_id: str
    action_id: str
    sample_env_id: str
    allow_network: bool


@dataclass(frozen=True, slots=True)
class _CampaignResumeCapability:
    poll_id: str
    campaign_id: str
    action_id: str
    grant_id: str
    grant_revision: int
    grant_hash: str
    sample_env_id: str
    operation_id: str
    resume_approval_ref: str


def _campaign_resume_capability(
    *,
    poll_id: UUID,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    grant_hash: str,
    sample_env_id: str,
    operation_id: UUID,
    resume_approval_ref: str,
) -> _CampaignResumeCapability:
    if not action_id or not grant_hash or not sample_env_id or not resume_approval_ref:
        raise ValueError("invalid exact campaign resume binding")
    return _CampaignResumeCapability(
        poll_id=str(poll_id), campaign_id=str(campaign_id), action_id=action_id,
        grant_id=str(grant_id), grant_revision=grant_revision, grant_hash=grant_hash,
        sample_env_id=sample_env_id, operation_id=str(operation_id),
        resume_approval_ref=resume_approval_ref,
    )


def _campaign_dispatch_capability(
    *, run_id: int, poll_id: UUID, campaign_id: UUID, action_id: str,
    sample_env_id: str, allow_network: bool,
) -> _CampaignDispatchCapability:
    if type(run_id) is not int or run_id < 1 or not action_id or not sample_env_id:
        raise ValueError("invalid exact campaign dispatch binding")
    return _CampaignDispatchCapability(
        run_id=run_id, poll_id=str(poll_id), campaign_id=str(campaign_id),
        action_id=action_id, sample_env_id=sample_env_id, allow_network=allow_network,
    )


def _receipt_ref(receipt: Any) -> dict[str, Any]:
    return {
        "sequence": receipt.sequence,
        "id": str(receipt.id),
        "payload_hash": receipt.payload_hash,
    }


def _json_wire(value: Any) -> Any:
    """Freeze durable run requests to JSON primitives before SQL persistence."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_wire(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_wire(value.model_dump(mode="json"))
    return value


def _checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("poll_checkpoint")
    if isinstance(value, dict):
        return dict(value)
    return {"stage": "directory", "page_num": 1, "attempt_ordinal": 0}


def _aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _checkpoint_native_ids(checkpoint: dict[str, Any]) -> list[str]:
    ids: set[str] = set()

    def collect_keys(value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            raise InvestorError("invalid_reference", "前序轮询公告键清单损坏")
        for row in value:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("native_announcement_id"), str)
                or not row["native_announcement_id"]
                or not isinstance(row.get("identity_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["identity_hash"])
            ):
                raise InvestorError("invalid_reference", "前序轮询公告键项损坏")
            ids.add(row["native_announcement_id"])

    collect_keys(checkpoint.get("ordered_document_keys"))
    pages = checkpoint.get("primary_pages", [])
    if not isinstance(pages, list):
        raise InvestorError("invalid_reference", "前序轮询页续点损坏")
    for page in pages:
        if isinstance(page, dict):
            collect_keys(page.get("ordered_document_keys"))
        else:
            raise InvestorError("invalid_reference", "前序轮询页续点损坏")
    current = checkpoint.get("current_primary_page")
    if isinstance(current, dict):
        collect_keys(current.get("ordered_document_keys"))
    elif current is not None:
        raise InvestorError("invalid_reference", "前序轮询当前页续点损坏")
    pending = checkpoint.get("pending_documents", [])
    if not isinstance(pending, list):
        raise InvestorError("invalid_reference", "前序轮询待处理公告续点损坏")
    for row in pending:
        if not isinstance(row, dict) or not isinstance(row.get("native_announcement_id"), str):
            raise InvestorError("invalid_reference", "前序轮询待处理公告项损坏")
        ids.add(row["native_announcement_id"])
    if len(ids) > 200:
        raise InvestorError("capacity_blocked", "待处理前序公告超过200项快照边界")
    return sorted(ids)


def _checkpoint_document_refs(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the bounded, deduplicated document leaves already committed."""
    refs: dict[tuple[int, str], dict[str, Any]] = {}

    def collect(value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            raise InvestorError("invalid_reference", "轮询文档叶引用清单损坏")
        for raw in value:
            if not isinstance(raw, dict):
                raise InvestorError("invalid_reference", "轮询文档叶引用损坏")
            sequence, receipt_id, payload_hash = (
                raw.get("sequence"), raw.get("id"), raw.get("payload_hash")
            )
            if (type(sequence) is not int or sequence < 1
                    or not isinstance(receipt_id, str) or not receipt_id
                    or not isinstance(payload_hash, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", payload_hash)):
                raise InvestorError("invalid_reference", "轮询文档叶引用字段损坏")
            key = (sequence, receipt_id)
            ref = {"sequence": sequence, "id": receipt_id, "payload_hash": payload_hash}
            if key in refs and refs[key] != ref:
                raise InvestorError("invalid_reference", "重复轮询文档叶引用不一致")
            refs[key] = ref

    collect(checkpoint.get("document_refs"))
    collect(checkpoint.get("page_document_refs"))
    current_page = checkpoint.get("current_primary_page")
    if current_page is not None:
        if not isinstance(current_page, dict):
            raise InvestorError("invalid_reference", "当前主索引页续点损坏")
        collect(current_page.get("document_refs"))
    raw_pages = checkpoint.get("primary_pages", [])
    if not isinstance(raw_pages, list):
        raise InvestorError("invalid_reference", "轮询主索引页续点损坏")
    for page in raw_pages:
        if not isinstance(page, dict):
            raise InvestorError("invalid_reference", "轮询主索引页续点损坏")
        collect(page.get("document_refs"))
    if len(refs) > 200:
        raise InvestorError("capacity_blocked", "轮询文档叶超过200项有界审计边界")
    return [refs[key] for key in sorted(refs)]


def _checkpoint_attempt_counts(checkpoint: dict[str, Any]) -> dict[str, int]:
    raw = checkpoint.get("logical_attempt_ordinals")
    if raw is not None and not isinstance(raw, dict):
        raise InvestorError("invalid_reference", "前序轮询尝试次数续点损坏")
    counts: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not key or type(value) is not int or not 0 <= value <= 3:
                raise InvestorError("invalid_reference", "前序轮询尝试次数续点损坏")
            counts[key] = value
    logical_key = checkpoint.get("logical_request_key")
    ordinal = checkpoint.get("attempt_ordinal")
    if logical_key is not None and not isinstance(logical_key, str):
        raise InvestorError("invalid_reference", "前序轮询逻辑请求续点损坏")
    if logical_key is not None and (type(ordinal) is not int or not 0 <= ordinal <= 3):
        raise InvestorError("invalid_reference", "前序轮询尝试次数续点损坏")
    if isinstance(logical_key, str):
        assert type(ordinal) is int
        counts[logical_key] = max(counts.get(logical_key, 0), ordinal)
    return counts


def _merge_pending_predecessor(
    existing: Any,
    *,
    predecessor_root: Any | None,
    snapshot_ref: dict[str, Any] | None,
    pending_native_ids: list[str],
) -> dict[str, Any] | None:
    prior_start: datetime | None = None
    prior_end: datetime | None = None
    prior_ids: set[str] = set()
    prior_refs: list[dict[str, Any]] = []
    if existing is not None:
        if not isinstance(existing, dict):
            raise InvestorError("invalid_reference", "前序轮询待处理状态损坏")
        prior_start = _aware_utc(existing.get("window_start"))
        prior_end = _aware_utc(existing.get("window_end"))
        raw_ids = existing.get("pending_native_ids")
        raw_refs = existing.get("snapshot_refs")
        if raw_refs is None and existing.get("snapshot_ref") is not None:
            raw_refs = [existing["snapshot_ref"]]
        if (
            prior_start is None
            or prior_end is None
            or prior_end <= prior_start
            or not isinstance(raw_ids, list)
            or any(not isinstance(item, str) or not item for item in raw_ids)
            or not isinstance(raw_refs, list)
        ):
            raise InvestorError("invalid_reference", "前序轮询待处理范围损坏")
        prior_ids.update(raw_ids)
        for ref in raw_refs:
            if not isinstance(ref, dict):
                raise InvestorError("invalid_reference", "前序轮询快照引用损坏")
            prior_refs.append(ref)
    if predecessor_root is None:
        return existing if isinstance(existing, dict) else None
    current_start = _aware_utc(predecessor_root.payload.get("window_start"))
    current_end = _aware_utc(predecessor_root.payload.get("window_end"))
    if current_start is None or current_end is None or current_end <= current_start:
        raise InvestorError("invalid_reference", "前序轮询窗口损坏")
    ids = prior_ids.union(pending_native_ids)
    if len(ids) > 200:
        raise InvestorError("capacity_blocked", "前序待处理公告累计超过200项边界")
    refs = list(prior_refs)
    if snapshot_ref is None:
        raise InvestorError("invalid_reference", "前序轮询快照引用缺失")
    refs.append(snapshot_ref)
    return {
        "poll_id": str(predecessor_root.id),
        "snapshot_ref": snapshot_ref,
        "snapshot_refs": refs,
        "window_start": min(
            item for item in (prior_start, current_start) if item is not None
        ).isoformat(),
        "window_end": max(item for item in (prior_end, current_end) if item is not None).isoformat(),
        "pending_native_ids": sorted(ids),
    }


async def _store_blob(session: AsyncSession, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    existing = await session.get(DisclosureBlob, digest)
    if existing is None:
        session.add(DisclosureBlob(content_hash=digest, body=body))
        await session.flush()
    elif existing.body != body:
        raise InvestorError("revision_conflict", "原始字节摘要冲突")
    return digest


async def _http_attempt_refs(
    repo: SupplementalUpdatesRepository, attempt_id: UUID, hop_count: int
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for hop_ordinal in range(hop_count + 1):
        for phase in ("reserved", "sent", "result"):
            receipt = await repo.get_change_by_key(
                LOCAL_OWNER,
                f"p4:attempt:{attempt_id}:{hop_ordinal}:{phase}",
            )
            if receipt is None:
                raise InvestorError("invalid_reference", "HTTP尝试收据链不完整")
            refs.append(_receipt_ref(receipt))
    if not 3 <= len(refs) <= 9:
        raise InvestorError("invalid_reference", "HTTP尝试收据超出边界")
    return refs


async def _http_response_refs(
    repo: SupplementalUpdatesRepository, attempt_id: UUID, hop_count: int
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for hop_ordinal in range(hop_count + 1):
        receipt = await repo.get_change_by_key(
            LOCAL_OWNER,
            f"p4:disclosure-http-response:{attempt_id}:{hop_ordinal}",
        )
        if receipt is None:
            raise InvestorError("invalid_reference", "HTTP原始响应收据链不完整")
        refs.append(_receipt_ref(receipt))
    if not 1 <= len(refs) <= 3:
        raise InvestorError("invalid_reference", "HTTP原始响应收据超出hop边界")
    return refs


async def _pending_p4_run_count(session: AsyncSession, *, exclude_run_id: int | None = None) -> int:
    stmt = select(func.count(PipelineRun.id)).where(
        PipelineRun.kind.in_(P4_RUN_KINDS),
        PipelineRun.status.in_(("queued", "running")),
    )
    if exclude_run_id is not None:
        stmt = stmt.where(PipelineRun.id != exclude_run_id)
    return int((await session.execute(stmt)).scalar_one())


async def _handoff_poll_step(
    session: AsyncSession,
    *,
    job: ClaimedJob,
    root: Any,
    source_state: Any,
    checkpoint: dict[str, Any],
) -> int:
    """Persist one bounded checkpoint and enqueue exactly one leased successor."""
    repo = SupplementalUpdatesRepository(session)
    next_revision = source_state.revision + 1
    if await _pending_p4_run_count(session, exclude_run_id=job.run_id) >= P4_PENDING_RUN_CAPACITY:
        await repo.save_source(
            source_state.model_copy(
                update={
                    "revision": next_revision,
                    "payload": {
                        **source_state.payload,
                        "poll_checkpoint": _json_wire(checkpoint),
                        "active_step_run_id": None,
                        "poll_state": "capacity_blocked",
                        "retry_at": None,
                    },
                }
            ),
            expected_revision=source_state.revision,
        )
        await PipelineRunRepository(session).mark_success(
            job.run_id,
            {"poll_state": "capacity_blocked", "poll_complete": False},
            lease=job.lease,
        )
        return job.run_id
    next_key = canonical_hash({"poll_id": str(root.id), "checkpoint": checkpoint})[:32]
    run, created = await PipelineRunRepository(session).enqueue(
        kind=DISCLOSURE_POLL_KIND,
        idempotency_key=f"p4:disclosure-step:{root.id}:{next_key}",
        triggered_by="supplemental_disclosure_poll_step",
        listing_id=source_state.listing_id,
        request_json=_json_wire(
            {
                "record_type": "poll_step_v1",
                "schema_version": 1,
                "owner_id": LOCAL_OWNER,
                "poll_sequence": root.sequence,
                "poll_id": str(root.id),
                "poll_hash": root.payload_hash,
                "source_scope_key": source_state.scope_key,
                "source_scope_revision": next_revision,
                "campaign_id": (root.payload.get("campaign_binding") or {}).get("campaign_id"),
                "campaign_action_id": (root.payload.get("campaign_binding") or {}).get("action_id"),
                "sample_env_id": (root.payload.get("campaign_binding") or {}).get("sample_env_id"),
            }
        ),
        max_attempts=1 if root.payload.get("campaign_binding") else 3,
    )
    if not created:
        raise InvestorError("revision_conflict", "下一轮询步骤已存在")
    await repo.save_source(
        source_state.model_copy(
            update={
                "revision": next_revision,
                "payload": {
                    **source_state.payload,
                    "poll_checkpoint": checkpoint,
                    "active_step_run_id": run.id,
                    "poll_state": "running",
                    "retry_at": None,
                },
                "last_attempt_at": datetime.now(UTC),
            }
        ),
        expected_revision=source_state.revision,
    )
    await PipelineRunRepository(session).mark_success(
        job.run_id,
        {"poll_state": "handoff", "poll_complete": False},
        lease=job.lease,
    )
    return run.id


def _poll_settings_open(settings: Settings) -> bool:
    return bool(
        settings.supplemental_rules_enabled
        and settings.supplemental_updates_enabled
        and settings.supplemental_disclosure_polling_enabled
        and settings.disclosure_originals_enabled
    )


def _make_page_request(
    root: dict[str, Any],
    listing: Listing,
    protocol: Any,
    page_num: int,
) -> DisclosurePageRequestV1:
    return DisclosurePageRequestV1(
        listing_id=listing.id,
        authorization_id=UUID(root["authorization_id"]),
        authorization_revision=root["authorization_revision"],
        authorization_hash=root["authorization_hash"],
        listing_ref=ObjectRef.model_validate(root["listing_ref"]),
        contract_hash=root["contract_hash"],
        source_scope_hash=root["source_scope_hash"],
        attempt_scope_hash=root["attempt_scope_hash"],
        window_start=datetime.fromisoformat(root["window_start"]),
        window_end=datetime.fromisoformat(root["window_end"]),
        page_num=page_num,
        page_size=PAGE_STEP_SIZE,
        protocol_hash=protocol.protocol_hash,
    )


def _poll_common_payload(
    root: dict[str, Any],
    *,
    profile: str,
    protocol_status: str,
) -> dict[str, Any]:
    return {
        "poll_id": root["poll_id"],
        "poll_ref": root["poll_id"],
        "authorization_id": root["authorization_id"],
        "authorization_revision": root["authorization_revision"],
        "authorization_hash": root["authorization_hash"],
        "contract_hash": root["contract_hash"],
        "source_scope_hash": root["source_scope_hash"],
        "attempt_scope_hash": root["attempt_scope_hash"],
        "grant_receipt_ref": root["grant_receipt_ref"],
        "listing_ref": root["listing_ref"],
        "window_start": root["window_start"],
        "window_end": root["window_end"],
        "protocol_profile": profile,
        "protocol_status": protocol_status,
    }


async def _append_filtered_leaf(
    repo: SupplementalUpdatesRepository,
    root: dict[str, Any],
    document: Any,
    *,
    index_ref: dict[str, Any],
    profile: str,
    protocol_status: str,
    parent_sequence: int,
    listing_id: int,
    now: datetime,
) -> dict[str, Any]:
    payload = _poll_common_payload(root, profile=profile, protocol_status=protocol_status)
    payload.update(
        {
            "record_type": "poll_document_v1",
            "index_ref": index_ref,
            "classification": document.classification,
            "document_type": document.classification,
            "coverage_disposition": "filtered",
            "native_announcement_id": document.native_announcement_id,
            "identity_hash": document.identity_hash,
            "content_hash": None,
            "semantic_change": False,
        }
    )
    receipt = await repo.append_change(
        LOCAL_OWNER,
        change_key=f"p4:cninfo:filtered:{root['poll_id']}:{index_ref['sequence']}:{document.native_announcement_id}",
        kind="source_attempt",
        parent_sequence=parent_sequence,
        listing_id=listing_id,
        context="company_research",
        payload=payload,
        acquired_at=now,
        available_from=now,
        committed_at=now,
    )
    return _receipt_ref(receipt)


def _error_code(error: BaseException) -> str:
    if isinstance(error, DisclosureError) and error.args:
        raw = str(error.args[0])
        if raw and len(raw) <= 64 and raw.replace("_", "").replace("-", "").isalnum():
            return raw
    return "disclosure_transport_failure"


def _retry_after_at(headers: Any, now: datetime, attempt_ordinal: int) -> datetime:
    value = headers.get("retry-after") if headers is not None else None
    if isinstance(value, str):
        try:
            seconds = int(value.strip())
            if seconds >= 0:
                return now + timedelta(seconds=seconds)
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is not None:
                return max(now + timedelta(seconds=1), parsed.astimezone(UTC))
        except (TypeError, ValueError, OverflowError):
            pass
    delay = min(86_400, 60 * (2 ** min(max(attempt_ordinal - 1, 0), 10)))
    delay += int(hashlib.sha256(str(attempt_ordinal).encode()).hexdigest()[:2], 16) % 31
    return now + timedelta(seconds=delay)


class DisclosurePollRequest(Contract):
    """Explicit bounded poll request; parsing it does not grant source authority."""

    operation_id: UUID
    authorization_id: UUID
    authorization_revision: int = Field(strict=True, ge=1)
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    listing_ref: ObjectRef
    window_start: AwareDatetime
    window_end: AwareDatetime
    expected_source_revision: int = Field(strict=True, ge=0)
    # Present only for a V2 manual-only campaign grant. These are identifiers,
    # not authority: campaign admission resolves them against its immutable
    # sidecar and checks the current DB/environment/profile binding.
    campaign_action_id: str | None = Field(default=None, min_length=1, max_length=128)
    campaign_profile_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replace_poll_id: UUID | None = None
    replace_reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def exact_request(self) -> DisclosurePollRequest:
        if (
            self.listing_ref.kind != "listing"
            or self.listing_ref.revision != 1
            or self.listing_ref.field_path
        ):
            raise ValueError("poll requires exact listing revision 1")
        if self.window_end <= self.window_start:
            raise ValueError("poll window must be positive")
        if (self.replace_poll_id is None) != (self.replace_reason is None):
            raise ValueError("replacement poll id and reason must be supplied together")
        if self.replace_reason is not None and not self.replace_reason.strip():
            raise ValueError("replacement reason must not be blank")
        return self


def source_scope_key(listing_ref: ObjectRef) -> str:
    return f"listing:{listing_ref.id}:company_research"


def source_scope_hash(listing_ref: ObjectRef, contract_hash: str) -> str:
    return canonical_hash(
        {
            "source_id": DISCLOSURE_FEED,
            "contract_version": SOURCE_CONTRACT_VERSION,
            "contract_hash": contract_hash,
            "scope_key": source_scope_key(listing_ref),
            "listing_ref": listing_ref,
        }
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


class DisclosurePollingHandler(BaseJobHandler):
    kind = DISCLOSURE_POLL_KIND

    def __init__(
        self,
        *,
        settings: Settings,
    ) -> None:
        from investment_agent.evidence.disclosure_pages import DisclosurePageService

        self.settings = settings
        self._test_service: Any = DisclosurePageService()
        self._testing = False
        self._clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._campaign_dispatch_capability: _CampaignDispatchCapability | None = None

    def _install_campaign_dispatch_capability(
        self, capability: _CampaignDispatchCapability,
    ) -> None:
        """Install a single-run selector used only by the exact campaign entrypoint."""
        self._campaign_dispatch_capability = capability

    @classmethod
    def for_tests(
        cls,
        *,
        settings: Settings,
        page_service: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> DisclosurePollingHandler:
        handler = cls(settings=settings)
        handler._test_service = page_service
        handler._testing = True
        handler._clock = clock or page_service._clock
        return handler

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        return await ctx.run_with_heartbeat(job.lease, lambda: self._execute_step(job))

    def _enable_fixture_proofs(self, session: AsyncSession) -> None:
        """Enable fixture proofs only inside the explicit test handler seam."""
        if self._testing:
            session.info["allow_disclosure_fixture_proofs"] = True

    async def _execute_maintenance_quantum(self, job: ClaimedJob, now: datetime) -> JobOutcome:
        from investment_agent.supplemental_rules.disclosure_authorization import (
            assert_run_lease,
            recover_orphan_disclosure_hop,
        )
        from investment_agent.supplemental_rules.disclosure_schedule import (
            schedule_due_disclosure_polls,
        )

        async with session_scope() as session:
            await assert_run_lease(session, job.lease)
            orphan_hop = await recover_orphan_disclosure_hop(
                session,
                lease=job.lease,
                now=now,
            )
            if orphan_hop is not None:
                try:
                    recovered_poll_id = UUID(str(orphan_hop["poll_id"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise InvestorError(
                        "invalid_reference", "orphan hop恢复结果缺少poll身份",
                    ) from exc
                repo = SupplementalUpdatesRepository(session)
                recovered_root = await repo.get_change(LOCAL_OWNER, recovered_poll_id)
                if (
                    recovered_root is None
                    or recovered_root.payload.get("record_type") != "poll_root_v1"
                    or recovered_root.payload.get("poll_id") != str(recovered_poll_id)
                ):
                    raise InvestorError("invalid_reference", "orphan hop引用的poll root无效")
                recovered_listing = ObjectRef.model_validate(
                    recovered_root.payload.get("listing_ref")
                )
                recovered_source = await repo.get_source(
                    LOCAL_OWNER, DISCLOSURE_FEED, source_scope_key(recovered_listing),
                    for_update=True,
                )
                if (
                    recovered_source is None
                    or recovered_source.payload.get("active_poll_id") != str(recovered_poll_id)
                ):
                    raise InvestorError("revision_conflict", "orphan hop已不属于当前活动poll")
                checkpoint = recovered_source.payload.get("poll_checkpoint")
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("attempt_id") != orphan_hop.get("attempt_id")
                    or checkpoint.get("redirect_count", 0) != orphan_hop.get("hop_ordinal")
                ):
                    raise InvestorError("revision_conflict", "orphan hop与poll checkpoint不匹配")
                # Drop only the exact fenced attempt/hop that maintenance just
                # settled. Keep logical_attempt_ordinals so an unknown send
                # still consumes its bounded retry slot; an unsent reservation
                # also cannot resurrect its terminal attempt identity.
                next_checkpoint = {
                    **checkpoint,
                    "attempt_id": None,
                    "redirect_count": 0,
                    "current_url": None,
                }
                next_payload = {
                    **recovered_source.payload,
                    "poll_checkpoint": _json_wire(next_checkpoint),
                }
                await repo.save_source(
                    recovered_source.model_copy(update={
                        "revision": recovered_source.revision + 1,
                        "payload": next_payload,
                    }),
                    expected_revision=recovered_source.revision,
                )
            recovered = await _enqueue_active_disclosure_poll_steps(
                session,
                settings=self.settings,
                job=job,
                now=now,
            )
            scheduled = await schedule_due_disclosure_polls(
                session,
                settings=self.settings,
                job=job,
                now=now,
            )
            await PipelineRunRepository(session).mark_success(
                job.run_id,
                {
                    "poll_state": "maintenance_complete",
                    "poll_complete": False,
                    "orphan_hop": orphan_hop,
                    "resumed_steps": recovered,
                    "scheduled_polls": scheduled,
                },
                lease=job.lease,
            )
        return "completed"

    async def _execute_campaign_recovery(self, job: ClaimedJob, now: datetime) -> JobOutcome:
        """Settle one explicitly approved orphan hop without dispatching a successor."""
        from investment_agent.supplemental_rules.disclosure_authorization import (
            read_authorization_snapshot,
            recover_orphan_disclosure_hop,
        )
        from investment_agent.supplemental_rules.disclosure_campaign import (
            authorize_campaign_recovery,
        )
        from investment_agent.supplemental_rules.disclosure_candidate_ingest import (
            assert_run_lease,
        )

        request = job.request_json
        try:
            poll_id = UUID(str(request.get("poll_id")))
            campaign_id = UUID(str(request.get("campaign_id")))
            grant_id = UUID(str(request.get("grant_id")))
            raw_grant_revision = request.get("grant_revision")
            if type(raw_grant_revision) is not int or raw_grant_revision < 1:
                raise ValueError("invalid grant revision")
            grant_revision = raw_grant_revision
            action_id = str(request.get("campaign_action_id"))
            sample_env_id = str(request.get("sample_env_id"))
        except (TypeError, ValueError) as exc:
            raise InvestorError("invalid_reference", "精确campaign恢复请求损坏") from exc
        recovery_approval_ref = request.get("recovery_approval_ref")
        operation_id = request.get("operation_id")
        if not isinstance(recovery_approval_ref, str) or not recovery_approval_ref:
            raise InvestorError("invalid_reference", "campaign recovery approval reference is missing")
        try:
            recovery_operation_id = UUID(str(operation_id))
        except (TypeError, ValueError) as exc:
            raise InvestorError("invalid_reference", "campaign recovery operation id is invalid") from exc
        capability = self._campaign_dispatch_capability
        if not (
            capability is not None
            and capability.run_id == job.run_id
            and capability.poll_id == str(poll_id)
            and capability.campaign_id == str(campaign_id)
            and capability.action_id == action_id
            and capability.sample_env_id == sample_env_id
            and capability.allow_network is False
        ):
            raise InvestorError(
                "disclosure_campaign_exact_dispatch_required",
                "campaign recovery requires its exact recovery-only dispatch capability",
            )
        self._campaign_dispatch_capability = None
        async with session_scope() as session:
            await assert_run_lease(session, job.lease)
            decision = await authorize_campaign_recovery(
                session,
                campaign_id=campaign_id,
                poll_id=str(poll_id),
                action_id=action_id,
                grant_id=grant_id,
                grant_revision=grant_revision,
                sample_env_id=sample_env_id,
                recovery_approval_ref=recovery_approval_ref,
                operation_id=recovery_operation_id,
                now=now,
            )
            if decision.get("recovery_only") is not True or decision.get("allow_network") is not False:
                raise InvestorError("disclosure_campaign_recovery_forbidden", "campaign未批准精确recovery-only")
            repo = SupplementalUpdatesRepository(session)
            poll_sequence = request.get("poll_sequence")
            if type(poll_sequence) is not int or poll_sequence < 1:
                raise InvestorError("invalid_reference", "精确poll root序号无效")
            root = await repo.get_change(LOCAL_OWNER, poll_sequence)
            if (
                root is None
                or root.payload_hash != request.get("poll_hash")
                or root.payload.get("record_type") != "poll_root_v1"
                or str(root.id) != str(poll_id)
                or root.payload.get("campaign_binding", {}).get("action_id") != action_id
                or root.payload.get("campaign_binding", {}).get("campaign_id") != str(campaign_id)
                or root.payload.get("campaign_binding", {}).get("sample_env_id") != sample_env_id
            ):
                raise InvestorError("disclosure_campaign_binding_invalid", "recovery与精确poll root不匹配")
            grant = await read_authorization_snapshot(session, grant_id)
            if (
                not isinstance(grant, BackgroundDisclosureAuthorizationV2)
                or grant.revision != grant_revision
                or grant.campaign_id != campaign_id
            ):
                raise InvestorError("disclosure_campaign_binding_invalid", "recovery grant revision不匹配")
            listing_ref = ObjectRef.model_validate(root.payload.get("listing_ref"))
            source = await repo.get_source(
                LOCAL_OWNER, DISCLOSURE_FEED, source_scope_key(listing_ref), for_update=True,
            )
            if source is None or source.payload.get("active_poll_id") != str(poll_id):
                raise InvestorError("revision_conflict", "recovery poll不是该scope的活动轮询")
            checkpoint = source.payload.get("poll_checkpoint")
            if not isinstance(checkpoint, dict):
                raise InvestorError("invalid_reference", "campaign poll缺少精确续点")
            orphan = await recover_orphan_disclosure_hop(
                session,
                lease=job.lease,
                now=now,
                poll_id=str(poll_id),
                recovery_operation_id=recovery_operation_id,
                sample_env_id=sample_env_id,
            )
            if orphan is None:
                await PipelineRunRepository(session).mark_success(
                    job.run_id,
                    {"poll_state": source.payload.get("poll_state"), "poll_complete": False,
                     "recovery_poll_id": str(poll_id), "orphan_hop": None},
                    lease=job.lease,
                )
                return "completed"
            if (
                str(orphan.get("poll_id")) != str(poll_id)
                or checkpoint.get("attempt_id") != orphan.get("attempt_id")
                or checkpoint.get("redirect_count", 0) != orphan.get("hop_ordinal")
            ):
                raise InvestorError("revision_conflict", "orphan hop与精确poll checkpoint不匹配")
            checkpoint = {**checkpoint, "attempt_id": None, "redirect_count": 0, "current_url": None}
            source = source.model_copy(update={
                "revision": source.revision + 1,
                "payload": {**source.payload, "poll_checkpoint": _json_wire(checkpoint)},
            })
            await repo.save_source(source, expected_revision=source.revision - 1)
            recovery_payload = {
                **source.payload,
                "poll_state": "manual_recovery_required",
                "active_step_run_id": None,
                "retry_at": None,
                "poll_checkpoint": _json_wire(checkpoint),
            }
            await repo.save_source(
                source.model_copy(update={"revision": source.revision + 1, "payload": recovery_payload}),
                expected_revision=source.revision,
            )
            await PipelineRunRepository(session).mark_success(
                job.run_id,
                {"poll_state": "manual_recovery_required", "poll_complete": False,
                 "recovery_poll_id": str(poll_id), "orphan_hop": orphan},
                lease=job.lease,
            )
        return "completed"

    async def _execute_step(self, job: ClaimedJob) -> JobOutcome:
        from investment_agent.settings.loader import load_settings
        from investment_agent.supplemental_rules.disclosure_authorization import (
            get_current_authorization,
            mark_http_hop_sent,
            reserve_http_hop,
            settle_http_hop_result,
        )
        from investment_agent.supplemental_rules.disclosure_candidate_ingest import (
            assert_run_lease,
        )

        now = self._clock().astimezone(UTC)
        if job.request_json.get("record_type") == "poll_maintenance_v1":
            return await self._execute_maintenance_quantum(job, now)
        if job.request_json.get("record_type") == "poll_recovery_v1":
            return await self._execute_campaign_recovery(job, now)
        async with session_scope() as session:
            self._enable_fixture_proofs(session)
            await assert_run_lease(session, job.lease)
            repo = SupplementalUpdatesRepository(session)
            poll_sequence = job.request_json.get("poll_sequence")
            if type(poll_sequence) is not int or poll_sequence < 1:
                await PipelineRunRepository(session).mark_partial(
                    job.run_id,
                    "disclosure_poll_receipt_mismatch",
                    lease=job.lease,
                )
                return "partial"
            root = await repo.get_change(LOCAL_OWNER, poll_sequence)
            if (
                root is None
                or root.payload_hash != job.request_json.get("poll_hash")
                or root.payload.get("record_type") != "poll_root_v1"
                or str(root.id) != str(job.request_json.get("poll_id"))
            ):
                await PipelineRunRepository(session).mark_partial(
                    job.run_id,
                    "disclosure_poll_receipt_mismatch",
                    lease=job.lease,
                )
                return "partial"
            listing_ref = ObjectRef.model_validate(root.payload["listing_ref"])
            listing_id = int(listing_ref.id)
            state = await repo.get_source(
                LOCAL_OWNER,
                DISCLOSURE_FEED,
                source_scope_key(listing_ref),
                for_update=True,
            )
            if (
                state is None
                or state.revision != job.request_json.get("source_scope_revision")
                or state.payload.get("active_poll_id") != str(root.id)
                or state.payload.get("active_step_run_id") != job.run_id
            ):
                await PipelineRunRepository(session).mark_partial(
                    job.run_id,
                    "disclosure_source_revision_conflict",
                    lease=job.lease,
                )
                return "partial"
            if state.retry_at is not None and state.retry_at.astimezone(UTC) > now:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "retry_wait",
                    "source_cooldown",
                    retry_at=state.retry_at,
                    checkpoint=_checkpoint(state.payload),
                )
                return "partial"
            current = await load_settings(session, base=self.settings)
            if not _poll_settings_open(current):
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "paused",
                    "disclosure_polling_disabled",
                    checkpoint=_checkpoint(state.payload),
                )
                return "partial"
            if self._test_service is None:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "blocked_protocol_unvalidated",
                    "disclosure_source_protocol_unvalidated",
                )
                return "partial"
            testing = self._testing and bool(getattr(self._test_service, "_testing", False))
            protocol = getattr(self._test_service, "protocol", None) if testing else None
            if testing and protocol is None:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "blocked_protocol_unvalidated",
                    "disclosure_source_protocol_unvalidated",
                )
                return "partial"
            profile = "fixture" if testing else "live"
            protocol_status = "fixture_validated" if testing else "live_validated"
            try:
                grant = await get_current_authorization(
                    session,
                    UUID(str(root.payload["authorization_id"])),
                    now=now,
                )
            except InvestorError as exc:
                expired = exc.detail.code == "disclosure_authorization_expired"
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "blocked_expired" if expired else "blocked_authorization",
                    exc.detail.code,
                    checkpoint=_checkpoint(state.payload),
                )
                return "partial"
            if (
                grant.revision != root.payload.get("authorization_revision")
                or grant.authorization_hash != root.payload.get("authorization_hash")
                or grant.contract_hash != root.payload.get("contract_hash")
                or listing_ref not in grant.listing_refs
            ):
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "authorization_changed",
                    "disclosure_authorization_required",
                )
                return "partial"
            campaign_binding = root.payload.get("campaign_binding")
            if isinstance(grant, BackgroundDisclosureAuthorizationV2):
                capability = self._campaign_dispatch_capability
                request = job.request_json
                is_recovery = request.get("record_type") == "poll_recovery_v1"
                expected_capability = (
                    capability is not None
                    and capability.run_id == job.run_id
                    and capability.poll_id == str(root.id)
                    and capability.campaign_id == str(grant.campaign_id)
                    and capability.action_id == str((campaign_binding or {}).get("action_id"))
                    and capability.sample_env_id == str(grant.sample_env_id)
                    and capability.allow_network is (not is_recovery)
                )
                if not expected_capability:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "manual_dispatch_required",
                        "disclosure_campaign_exact_dispatch_required",
                        checkpoint=_checkpoint(state.payload),
                    )
                    self._campaign_dispatch_capability = None
                    return "partial"
                # A capability authorizes only this exact run; a later queued
                # successor needs a fresh explicit dispatch call.
                self._campaign_dispatch_capability = None
            if isinstance(grant, BackgroundDisclosureAuthorizationV2):
                try:
                    from investment_agent.supplemental_rules.disclosure_campaign import (
                        assert_campaign_action,
                    )

                    if not isinstance(campaign_binding, dict):
                        raise InvestorError(
                            "disclosure_campaign_binding_invalid",
                            "手动轮询缺少不可变campaign绑定",
                        )
                    db_url = make_url(str(cast(Any, session.get_bind()).url))
                    if db_url.get_backend_name() != "sqlite" or not db_url.database:
                        raise InvestorError(
                            "disclosure_campaign_environment_mismatch",
                            "campaign只允许文件SQLite环境",
                        )
                    binding = await assert_campaign_action(
                        session,
                        campaign_id=UUID(str(grant.campaign_id)),
                        action_id=str(campaign_binding.get("action_id")),
                        grant_id=grant.authorization_id,
                        grant_revision=grant.revision,
                        poll_id=str(root.id),
                        sample_env_id=str(grant.sample_env_id),
                        now=now,
                    )
                    if (
                        job.request_json.get("campaign_id") != str(grant.campaign_id)
                        or job.request_json.get("campaign_action_id") != str(binding.get("action_id"))
                        or job.request_json.get("sample_env_id") != str(grant.sample_env_id)
                        or campaign_binding.get("action_id") != binding.get("action_id")
                    ):
                        raise InvestorError(
                            "disclosure_campaign_binding_invalid",
                            "poll root、step与campaign动作绑定不一致",
                        )
                    from investment_agent.evidence.disclosure_profiles import (
                        SERVER_PROFILE_REGISTRY,
                        assert_approved_binding,
                    )
                    from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
                        CampaignAction,
                    )

                    approved = assert_approved_binding(
                        SERVER_PROFILE_REGISTRY,
                        grant,
                        CampaignAction.model_validate(binding),
                        now=now,
                        sample_env_id=str(grant.sample_env_id),
                    )
                    if approved.profile.contract_hash != grant.contract_hash:
                        raise InvestorError(
                            "disclosure_campaign_profile_mismatch",
                            "grant contract does not match current approved profile",
                        )
                    if campaign_binding.get("profile_hash") != approved.profile.protocol.protocol_hash:
                        raise InvestorError(
                            "disclosure_campaign_profile_mismatch",
                            "campaign action must bind the approved protocol hash",
                        )
                    protocol = approved.profile.protocol
                except (ValueError, KeyError) as exc:
                    raise InvestorError(
                        "disclosure_campaign_binding_invalid",
                        "手动轮询campaign绑定字段无效",
                    ) from exc
            elif not testing:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "blocked_authorization",
                    "disclosure_manual_v2_required",
                    checkpoint=_checkpoint(state.payload),
                )
                return "partial"
            if protocol is None:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "blocked_protocol_unvalidated",
                    "disclosure_source_protocol_unvalidated",
                    checkpoint=_checkpoint(state.payload),
                )
                return "partial"
            listing = await session.get(Listing, listing_id)
            if listing is None or listing.market_code != "SZSE" or len(listing.ticker) != 6:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "invalid_listing",
                    "disclosure_listing_invalid",
                )
                return "partial"
            raw_checkpoint = state.payload.get("poll_checkpoint")
            if not isinstance(raw_checkpoint, dict):
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "invalid_poll_checkpoint",
                    "invalid_reference",
                )
                return "partial"
            checkpoint = dict(raw_checkpoint)
            if checkpoint.get("campaign_binding") != campaign_binding:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "campaign_binding_invalid",
                    "disclosure_campaign_binding_invalid",
                    checkpoint=checkpoint,
                )
                return "partial"
            source_revision = state.revision
            checkpoint.setdefault("page_num", 1)
            checkpoint.setdefault("attempt_ordinal", 0)
            checkpoint.setdefault("previous_index_ref", None)
            checkpoint.setdefault("previous_verify_index_ref", None)
            checkpoint.setdefault("previous_bridge_ref", None)
            checkpoint.setdefault("bridge_refs", [])
            checkpoint.setdefault("document_refs", [])
            checkpoint.setdefault("primary_pages", [])
            checkpoint.setdefault("eligible_count", 0)
            checkpoint.setdefault("filtered_count", 0)
            checkpoint.setdefault("document_count", 0)
            checkpoint.setdefault("page_count", 0)
            stage = checkpoint.get("stage", "directory")
            if stage in {"seal_page", "finalize"}:
                await self._finish_local_stage(
                    session,
                    job,
                    root,
                    state,
                    checkpoint,
                    grant,
                    protocol_profile=profile,
                    protocol_status=protocol_status,
                    listing_ref=listing_ref,
                    now=now,
                )
                return "completed"
            if stage not in {"directory", "query", "fetch", "verify_query"}:
                await self._stop_poll(
                    session, job, state, "invalid_poll_checkpoint", "invalid_reference"
                )
                return "partial"
            existing_attempt_id = checkpoint.get("attempt_id")
            attempt_id = UUID(str(existing_attempt_id)) if existing_attempt_id else uuid4()
            redirect_count = int(checkpoint.get("redirect_count", 0))
            purpose: Literal["directory", "query", "original"]
            request_kind: Literal["directory", "query", "fetch"]
            if stage == "directory":
                url, purpose, body, request_kind, response_limit = (
                    "https://www.cninfo.com.cn/new/data/szse_stock.json",
                    "directory",
                    None,
                    "directory",
                    min(8 * 1024 * 1024, grant.per_poll_response_bytes),
                )
                logical_key = "directory"
            elif stage in {"query", "verify_query"}:
                if int(checkpoint["page_num"]) > MAX_QUERY_PAGES:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "window_split_required",
                        "query_page_cap_reached",
                    )
                    return "partial"
                page_request = _make_page_request(
                    root.payload, listing, protocol, int(checkpoint["page_num"])
                )
                url, purpose, body = page_url_body(
                    page_request,
                    issuer_id=str(checkpoint.get("issuer_id", "")),
                    ticker=listing.ticker,
                    protocol=protocol,
                )
                request_kind, response_limit = (
                    "query",
                    min(page_request.max_bytes, grant.per_poll_response_bytes),
                )
                logical_key = (
                    f"page:{checkpoint['page_num']}"
                    if stage == "query"
                    else f"verify-page:{checkpoint['page_num']}"
                )
            else:
                fetches_done = int(checkpoint.get("eligible_count", 0))
                if fetches_done >= grant.per_poll_fetch:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "window_split_required",
                        "fetch_budget_exhausted",
                    )
                    return "partial"
                pending = checkpoint.get("pending_documents")
                if not isinstance(pending, list) or not pending:
                    await self._stop_poll(
                        session, job, state, "empty_fetch_checkpoint", "invalid_reference"
                    )
                    return "partial"
                from investment_agent.evidence.disclosure_pages import CNInfoDiscoveredDocument

                document = CNInfoDiscoveredDocument.model_validate(pending[0])
                url = document.locator
                DisclosureBackgroundFetchV1(
                    listing_id=listing.id,
                    authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision,
                    authorization_hash=grant.authorization_hash,
                    listing_ref=listing_ref,
                    window_start=datetime.fromisoformat(root.payload["window_start"]),
                    window_end=datetime.fromisoformat(root.payload["window_end"]),
                    contract_hash=grant.contract_hash,
                    source_scope_hash=root.payload["source_scope_hash"],
                    attempt_scope_hash=root.payload["attempt_scope_hash"],
                    native_announcement_id=document.native_announcement_id,
                    locator=document.locator,
                    protocol_hash=protocol.protocol_hash,
                )
                validate_url(url, "original")
                purpose, body, request_kind = "original", None, "fetch"
                response_limit = min(16 * 1024 * 1024, grant.per_poll_response_bytes)
                logical_key = f"document:{document.native_announcement_id}"
            attempt_ordinals = _checkpoint_attempt_counts(checkpoint)
            attempt_ordinal = int(checkpoint.get("attempt_ordinal", 0))
            if existing_attempt_id is None:
                attempt_ordinal = attempt_ordinals.get(logical_key, 0) + 1
            if attempt_ordinal > 3:
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "retry_exhausted",
                    "disclosure_attempt_limit",
                    checkpoint={**checkpoint, "attempt_id": None},
                )
                return "partial"
            checkpoint["logical_request_key"] = logical_key
            attempt_ordinals[logical_key] = max(
                attempt_ordinals.get(logical_key, 0), attempt_ordinal
            )
            checkpoint["logical_attempt_ordinals"] = attempt_ordinals
            if isinstance(checkpoint.get("current_url"), str):
                url = checkpoint["current_url"]
            request_hash = canonical_hash(
                {
                    "url": url,
                    "purpose": purpose,
                    "body_sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
                }
            )
            identity = DisclosureAttemptIdentity(
                poll_id=str(root.id),
                logical_request_key=logical_key,
                attempt_id=attempt_id,
                attempt_ordinal=attempt_ordinal,
                hop_ordinal=redirect_count,
                hop_id=f"{root.id}:{logical_key}:{attempt_id}:{redirect_count}",
                request_kind=request_kind,
                request_hash=request_hash,
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                contract_hash=grant.contract_hash,
                scope_hash=str(root.payload["attempt_scope_hash"]),
                listing_ref=listing_ref,
                window_start=datetime.fromisoformat(root.payload["window_start"]),
                window_end=datetime.fromisoformat(root.payload["window_end"]),
                poll_http_cap=int(root.payload["poll_http_cap"]),
                poll_query_http_cap=int(root.payload["poll_query_http_cap"]),
                poll_fetch_cap=int(root.payload["poll_fetch_cap"]),
                poll_response_bytes_cap=int(root.payload["poll_response_bytes_cap"]),
                url=url,
                response_bytes_limit=response_limit,
            )
            replayed_response: SourceResponse | None = None
            replayed_permit: HttpHopPermit | None = None
            replayed_settlement: HttpHopSettlement | None = None
            if existing_attempt_id is not None:
                response_receipt = await repo.get_change_by_key(
                    LOCAL_OWNER,
                    f"p4:disclosure-http-response:{attempt_id}:{redirect_count}",
                )
                discarded_receipt = await repo.get_change_by_key(
                    LOCAL_OWNER,
                    f"p4:disclosure-http-discard:{attempt_id}:{redirect_count}",
                )
                if response_receipt is None and discarded_receipt is not None:
                    discarded_state = str(
                        discarded_receipt.payload.get("poll_state", "blocked_authorization")
                    )
                    discarded_reason = str(
                        discarded_receipt.payload.get(
                            "reason", "disclosure_authorization_required"
                        )
                    )
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        discarded_state,
                        discarded_reason,
                        checkpoint={**checkpoint, "attempt_id": None},
                    )
                    return "partial"
                if response_receipt is not None:
                    reservation_receipt = await repo.get_change_by_key(
                        LOCAL_OWNER,
                        f"p4:attempt:{attempt_id}:{redirect_count}:reserved",
                    )
                    result_receipt = await repo.get_change_by_key(
                        LOCAL_OWNER,
                        f"p4:attempt:{attempt_id}:{redirect_count}:result",
                    )
                    blob = await session.get(
                        DisclosureBlob,
                        str(response_receipt.payload.get("raw_blob_hash", "")),
                    )
                    receipt_payload = response_receipt.payload
                    if (
                        reservation_receipt is None
                        or result_receipt is None
                        or blob is None
                        or hashlib.sha256(blob.body).hexdigest()
                        != receipt_payload.get("response_hash")
                        or result_receipt.payload.get("response_hash")
                        != receipt_payload.get("response_hash")
                        or result_receipt.payload.get("response_bytes")
                        != receipt_payload.get("response_bytes")
                    ):
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "settled_response_replay_invalid",
                            "invalid_reference",
                        )
                        return "partial"
                    replayed_permit = HttpHopPermit.model_validate(
                        reservation_receipt.payload["permit"]
                    ).model_copy(update={"state": "sent"})
                    replayed_settlement = HttpHopSettlement(
                        permit_id=replayed_permit.permit_id,
                        state="result",
                        shanghai_day=replayed_permit.shanghai_day,
                        request_consumed=1,
                        response_bytes_consumed=int(result_receipt.payload["response_bytes"]),
                        storage_bytes_consumed=int(result_receipt.payload["storage_bytes"]),
                        result_code=str(result_receipt.payload["result_code"]),
                    )
                    headers: dict[str, str] = {}
                    if isinstance(receipt_payload.get("content_type"), str):
                        headers["content-type"] = receipt_payload["content_type"]
                    if isinstance(receipt_payload.get("location"), str):
                        headers["location"] = receipt_payload["location"]
                    replayed_response = SourceResponse(
                        status=int(receipt_payload["status"]),
                        headers=headers,
                        body=blob.body,
                        url=str(receipt_payload["response_url"]),
                    )
                else:
                    sent_receipt = await repo.get_change_by_key(
                        LOCAL_OWNER,
                        f"p4:attempt:{attempt_id}:{redirect_count}:sent",
                    )
                    result_receipt = await repo.get_change_by_key(
                        LOCAL_OWNER,
                        f"p4:attempt:{attempt_id}:{redirect_count}:result",
                    )
                    unknown_receipt = await repo.get_change_by_key(
                        LOCAL_OWNER,
                        f"p4:attempt:{attempt_id}:{redirect_count}:unknown",
                    )
                    if (
                        sent_receipt is not None
                        and result_receipt is None
                        and unknown_receipt is None
                    ):
                        reservation_receipt = await repo.get_change_by_key(
                            LOCAL_OWNER,
                            f"p4:attempt:{attempt_id}:{redirect_count}:reserved",
                        )
                        if reservation_receipt is None:
                            await self._stop_poll(
                                session,
                                job,
                                state,
                                "sent_reservation_missing",
                                "invalid_reference",
                            )
                            return "partial"
                        uncertain_permit = HttpHopPermit.model_validate(
                            reservation_receipt.payload["permit"]
                        ).model_copy(update={"state": "sent"})
                        await settle_http_hop_result(
                            session,
                            permit=uncertain_permit,
                            lease=job.lease,
                            response_bytes=None,
                            result_code="unknown_after_worker_recovery",
                            now=now,
                        )
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "retry_wait",
                            "unknown_response_after_worker_recovery",
                            retry_at=_retry_after_at({}, now, attempt_ordinal),
                            checkpoint={
                                **checkpoint,
                                "attempt_id": None,
                                "redirect_count": 0,
                                "current_url": None,
                                "attempt_ordinal": attempt_ordinal,
                            },
                        )
                        return "partial"
            if existing_attempt_id is None:
                next_revision = state.revision + 1
                checkpoint.update(
                    {
                        "attempt_id": str(attempt_id),
                        "attempt_ordinal": attempt_ordinal,
                        "current_url": url,
                    }
                )
                await repo.save_source(
                    state.model_copy(
                        update={
                            "revision": next_revision,
                            "payload": {
                                **state.payload,
                                "poll_checkpoint": _json_wire(checkpoint),
                            },
                        }
                    ),
                    expected_revision=state.revision,
                )
                current_run = await session.get(PipelineRun, job.run_id)
                if current_run is None:
                    raise InvestorError("revision_conflict", "轮询run已丢失")
                current_run.request_json = _json_wire(
                    {
                        **current_run.request_json,
                        "source_scope_revision": next_revision,
                    }
                )
                job.request_json["source_scope_revision"] = next_revision
                source_revision = next_revision

        if replayed_response is not None:
            response = replayed_response
            assert replayed_permit is not None and replayed_settlement is not None
            sent_permit = replayed_permit
            settlement = replayed_settlement
        else:
            try:
                async with session_scope() as session:
                    self._enable_fixture_proofs(session)
                    permit = await reserve_http_hop(
                        session,
                        identity=identity,
                        response_byte_limit=identity.response_bytes_limit,
                        lease=job.lease,
                        base_settings=self.settings,
                        now=now,
                    )
            except InvestorError as exc:
                # A frozen per-poll query cap means the requested window needs
                # a smaller child window; owner/grant/day caps remain visible
                # capacity blocks and must not be mislabeled as a split.
                if (
                    request_kind == "query"
                    and exc.detail.code == "capacity_blocked"
                    and exc.detail.message == "poll query额度已用尽"
                ):
                    async with session_scope() as session:
                        self._enable_fixture_proofs(session)
                        refreshed_state = await SupplementalUpdatesRepository(session).get_source(
                            LOCAL_OWNER,
                            DISCLOSURE_FEED,
                            str(job.request_json["source_scope_key"]),
                            for_update=True,
                        )
                        if refreshed_state is not None:
                            await self._stop_poll(
                                session,
                                job,
                                refreshed_state,
                                "window_split_required",
                                "query_budget_exhausted",
                                checkpoint={**checkpoint, "attempt_id": None},
                            )
                    return "partial"
                raise
            async with session_scope() as session:
                self._enable_fixture_proofs(session)
                sent_permit = await mark_http_hop_sent(
                    session,
                    permit=permit,
                    lease=job.lease,
                    base_settings=self.settings,
                    now=now,
                )
            try:
                if not testing:
                    from investment_agent.evidence.disclosure_profiles import (
                        SERVER_PROFILE_REGISTRY,
                    )

                    SERVER_PROFILE_REGISTRY.require_approved_contract(
                        grant.contract_hash,
                        now=self._clock(),
                        protocol_hash=protocol.protocol_hash,
                    )
                response = await self._test_service.request_one_hop(
                    url,
                    purpose,
                    body=body,
                    max_bytes=identity.response_bytes_limit,
                )
            except Exception as exc:
                async with session_scope() as session:
                    self._enable_fixture_proofs(session)
                    await settle_http_hop_result(
                        session,
                        permit=sent_permit,
                        lease=job.lease,
                        response_bytes=None,
                        result_code=_error_code(exc),
                        now=now,
                    )
                    state = await SupplementalUpdatesRepository(session).get_source(
                        LOCAL_OWNER,
                        DISCLOSURE_FEED,
                        source_scope_key(listing_ref),
                        for_update=True,
                    )
                    if state is not None:
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "retry_wait",
                            _error_code(exc),
                            retry_at=_retry_after_at({}, now, attempt_ordinal),
                            checkpoint={
                                **_checkpoint(state.payload),
                                "attempt_id": None,
                                "redirect_count": 0,
                                "current_url": None,
                                "attempt_ordinal": attempt_ordinal,
                            },
                        )
                return "partial"
        async with session_scope() as session:
            self._enable_fixture_proofs(session)
            await assert_run_lease(session, job.lease)
            current_after_response = await load_settings(session, base=self.settings)
            access_state: str | None = None
            access_reason: str | None = None
            if not _poll_settings_open(current_after_response):
                access_state = "paused"
                access_reason = "disclosure_polling_disabled"
            else:
                try:
                    current_grant = await get_current_authorization(
                        session,
                        grant.authorization_id,
                        now=now,
                    )
                except InvestorError as exc:
                    access_state = (
                        "blocked_expired"
                        if exc.detail.code == "disclosure_authorization_expired"
                        else "blocked_authorization"
                    )
                    access_reason = exc.detail.code
                else:
                    if (
                        current_grant.revision != grant.revision
                        or current_grant.authorization_hash != grant.authorization_hash
                        or current_grant.contract_hash != grant.contract_hash
                        or listing_ref not in current_grant.listing_refs
                    ):
                        access_state = "blocked_authorization"
                        access_reason = "disclosure_authorization_required"
                if access_state is None and not testing:
                    try:
                        from investment_agent.evidence.disclosure_profiles import (
                            SERVER_PROFILE_REGISTRY,
                        )

                        SERVER_PROFILE_REGISTRY.require_approved_contract(
                            grant.contract_hash,
                            now=self._clock(),
                            protocol_hash=protocol.protocol_hash,
                        )
                    except DisclosureError as exc:
                        access_state = "blocked_profile"
                        access_reason = str(exc)
                if access_state is None and isinstance(grant, BackgroundDisclosureAuthorizationV2):
                    try:
                        from investment_agent.supplemental_rules.disclosure_campaign import (
                            assert_campaign_action,
                        )

                        await assert_campaign_action(
                            session,
                            campaign_id=grant.campaign_id,
                            action_id=str((campaign_binding or {}).get("action_id")),
                            grant_id=grant.authorization_id,
                            grant_revision=grant.revision,
                            poll_id=str(root.id),
                            sample_env_id=grant.sample_env_id,
                            now=self._clock(),
                        )
                    except InvestorError as exc:
                        access_state = "blocked_authorization"
                        access_reason = exc.detail.code

            if replayed_response is None:
                response_hash = hashlib.sha256(response.body).hexdigest()
                settlement = await settle_http_hop_result(
                    session,
                    permit=sent_permit,
                    lease=job.lease,
                    response_bytes=len(response.body),
                    result_code=f"http_{response.status}",
                    storage_bytes=len(response.body) if access_state is None else 0,
                    response_hash=response_hash,
                    now=now,
                )
                settled_result_receipt = await SupplementalUpdatesRepository(
                    session
                ).get_change_by_key(
                    LOCAL_OWNER,
                    f"p4:attempt:{attempt_id}:{redirect_count}:result",
                )
                if settled_result_receipt is None:
                    raise InvestorError("invalid_reference", "HTTP结果收据缺失")
                if access_state is None:
                    response_blob_hash = await _store_blob(session, response.body)
                    response_receipt = await SupplementalUpdatesRepository(
                        session
                    ).append_change(
                        LOCAL_OWNER,
                        change_key=f"p4:disclosure-http-response:{attempt_id}:{redirect_count}",
                        kind="source_attempt",
                        parent_sequence=root.sequence,
                        listing_id=listing_id,
                        context="company_research",
                        payload={
                            "record_type": "poll_http_response_v1",
                            "poll_id": str(job.request_json["poll_id"]),
                            "attempt_id": str(attempt_id),
                            "hop_ordinal": redirect_count,
                            "request_kind": request_kind,
                            "logical_request_key": logical_key,
                            "status": response.status,
                            "response_url": response.url,
                            "content_type": response.headers.get("content-type"),
                            "location": response.headers.get("location"),
                            "raw_blob_hash": response_blob_hash,
                            "response_hash": response_hash,
                            "response_bytes": len(response.body),
                            "settlement_ref": _receipt_ref(settled_result_receipt),
                        },
                        acquired_at=now,
                        available_from=now,
                        committed_at=now,
                    )
                else:
                    # Keep an audit of the settled response without retaining a
                    # replayable body once the consent or feature gate is gone.
                    await SupplementalUpdatesRepository(session).append_change(
                        LOCAL_OWNER,
                        change_key=f"p4:disclosure-http-discard:{attempt_id}:{redirect_count}",
                        kind="source_attempt",
                        parent_sequence=root.sequence,
                        listing_id=listing_id,
                        context="company_research",
                        payload={
                            "record_type": "poll_http_discard_v1",
                            "poll_id": str(job.request_json["poll_id"]),
                            "attempt_id": str(attempt_id),
                            "hop_ordinal": redirect_count,
                            "request_kind": request_kind,
                            "logical_request_key": logical_key,
                            "status": response.status,
                            "response_hash": response_hash,
                            "response_bytes": len(response.body),
                            "settlement_ref": _receipt_ref(settled_result_receipt),
                            "poll_state": access_state,
                            "reason": access_reason,
                        },
                        acquired_at=now,
                        available_from=now,
                        committed_at=now,
                    )
                # Settlement, consent decision, and body-retention choice share
                # one short transaction before parsing or candidate ingestion.
                await session.commit()
            else:
                response_receipt = await SupplementalUpdatesRepository(session).get_change_by_key(
                    LOCAL_OWNER,
                    f"p4:disclosure-http-response:{attempt_id}:{redirect_count}",
                )
                if response_receipt is None:
                    raise InvestorError("invalid_reference", "HTTP原始响应收据缺失")
            if access_state is not None:
                refreshed_state = await SupplementalUpdatesRepository(session).get_source(
                    LOCAL_OWNER,
                    DISCLOSURE_FEED,
                    source_scope_key(listing_ref),
                    for_update=True,
                )
                if refreshed_state is not None:
                    await self._stop_poll(
                        session,
                        job,
                        refreshed_state,
                        access_state,
                        access_reason or "disclosure_authorization_required",
                        checkpoint={**checkpoint, "attempt_id": None},
                    )
                return "partial"
            repo = SupplementalUpdatesRepository(session)
            http_refs = await _http_attempt_refs(repo, attempt_id, redirect_count)
            http_response_refs = await _http_response_refs(repo, attempt_id, redirect_count)
            root = await repo.get_change(LOCAL_OWNER, UUID(str(job.request_json["poll_id"])))
            state = await repo.get_source(
                LOCAL_OWNER,
                DISCLOSURE_FEED,
                str(job.request_json["source_scope_key"]),
                for_update=True,
            )
            if (
                root is None
                or state is None
                or state.revision != source_revision
                or state.payload.get("active_step_run_id") != job.run_id
            ):
                await PipelineRunRepository(session).mark_partial(
                    job.run_id,
                    "disclosure_source_revision_conflict",
                    lease=job.lease,
                )
                return "partial"
            if response.status in {301, 302, 303, 307, 308}:
                if body is not None and response.status not in {307, 308}:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "redirect_limit_or_method_change",
                        "redirect_limit_or_method_change",
                    )
                    return "partial"
                try:
                    target = self._test_service.next_redirect_url(
                        response,
                        purpose,
                        redirect_count=redirect_count,
                        body_present=body is not None,
                    )
                except DisclosureError as exc:
                    await self._stop_poll(session, job, state, str(exc), str(exc))
                    return "partial"
                if target is None:
                    await self._stop_poll(
                        session, job, state, "invalid_redirect", "invalid_redirect"
                    )
                    return "partial"
                redirect = await repo.append_change(
                    LOCAL_OWNER,
                    change_key=f"p4:disclosure-redirect:{attempt_id}:{redirect_count}",
                    kind="source_attempt",
                    listing_id=listing_id,
                    context="company_research",
                    payload={
                        "record_type": "poll_redirect_v1",
                        "poll_id": str(root.id),
                        "attempt_id": str(attempt_id),
                        "hop_ordinal": redirect_count,
                        "status": response.status,
                        "from_url": url,
                        "to_url": target,
                    },
                    acquired_at=now,
                    available_from=now,
                    committed_at=now,
                )
                checkpoint.update(
                    {
                        "attempt_id": str(attempt_id),
                        "attempt_ordinal": attempt_ordinal,
                        "redirect_count": redirect_count + 1,
                        "current_url": target,
                        "stage": stage,
                        "last_redirect_sequence": redirect.sequence,
                    }
                )
                await _handoff_poll_step(
                    session,
                    job=job,
                    root=root,
                    source_state=state,
                    checkpoint=checkpoint,
                )
                return "completed"
            if response.status != 200:
                retry_at = (
                    _retry_after_at(response.headers, now, attempt_ordinal)
                    if response.status == 429 or response.status >= 500
                    else None
                )
                if retry_at is not None and now + timedelta(seconds=1) < grant.expires_at:
                    if retry_at > grant.expires_at:
                        retry_at = None
                        retry_state = "blocked_expired"
                    else:
                        retry_state = "retry_wait"
                else:
                    retry_state = (
                        "blocked_expired" if retry_at is not None else f"http_{response.status}"
                    )
                await self._stop_poll(
                    session,
                    job,
                    state,
                    retry_state,
                    f"http_{response.status}",
                    retry_at=retry_at,
                    checkpoint={
                        **checkpoint,
                        "attempt_id": None,
                        "redirect_count": 0,
                        "current_url": None,
                        "attempt_ordinal": attempt_ordinal,
                    },
                )
                return "partial"
            if stage == "directory":
                try:
                    issuer = self._test_service.issuer_from_directory(response, listing.ticker)
                except DisclosureError as exc:
                    await self._stop_poll(session, job, state, str(exc), str(exc))
                    return "partial"
                directory_hash = await _store_blob(session, response.body)
                directory_receipt = await repo.append_change(
                    LOCAL_OWNER,
                    change_key=f"p4:disclosure-directory:{root.id}",
                    kind="source_attempt",
                    parent_sequence=root.sequence,
                    listing_id=listing_id,
                    context="company_research",
                    payload={
                        "record_type": "poll_directory_v1",
                        "poll_id": str(root.id),
                        "authorization_id": str(grant.authorization_id),
                        "authorization_revision": grant.revision,
                        "authorization_hash": grant.authorization_hash,
                        "contract_hash": grant.contract_hash,
                        "source_scope_hash": root.payload["source_scope_hash"],
                        "attempt_scope_hash": root.payload["attempt_scope_hash"],
                        "listing_ref": listing_ref.model_dump(mode="json"),
                        "context": "company_research",
                        "window_start": root.payload["window_start"],
                        "window_end": root.payload["window_end"],
                        "issuer_id": issuer,
                        "directory_hash": directory_hash,
                        "raw_blob_hash": directory_hash,
                        "response_bytes": len(response.body),
                        "response_hash": hashlib.sha256(response.body).hexdigest(),
                        "http_attempt_refs": http_refs,
                        "http_response_refs": http_response_refs,
                    },
                    acquired_at=now,
                    available_from=now,
                    committed_at=now,
                )
                checkpoint.update(
                    {
                        "stage": "query",
                        "issuer_id": issuer,
                        "directory_hash": directory_hash,
                        "directory_ref": _receipt_ref(directory_receipt),
                        "acquired_at": now.isoformat(),
                        "redirect_count": 0,
                        "attempt_id": None,
                        "current_url": None,
                        "attempt_ordinal": attempt_ordinal,
                    }
                )
            elif stage in {"query", "verify_query"}:
                page_request = _make_page_request(
                    root.payload,
                    listing,
                    protocol,
                    int(checkpoint["page_num"]),
                )
                try:
                    parsed_page = parse_page_response(
                        page_request,
                        response,
                        issuer_id=str(checkpoint["issuer_id"]),
                        ticker=listing.ticker,
                        protocol=protocol,
                        now=now,
                    )
                except DisclosureError as exc:
                    await self._stop_poll(session, job, state, str(exc), str(exc))
                    return "partial"
                raw_hash = await _store_blob(session, response.body)
                ordered_keys = [
                    {
                        "native_announcement_id": doc.native_announcement_id,
                        "identity_hash": doc.identity_hash,
                    }
                    for doc in parsed_page.documents
                ]
                page_payload = _poll_common_payload(
                    root.payload,
                    profile=profile,
                    protocol_status=protocol_status,
                )
                page_payload.update(
                    {
                        "record_type": "poll_index_v1",
                        "query_pass": "primary" if stage == "query" else "verification",
                        "page_num": parsed_page.page_num,
                        "has_more": parsed_page.has_more,
                        "total": parsed_page.total,
                        "raw_blob_hash": raw_hash,
                        "response_hash": parsed_page.response_hash,
                        "response_bytes": parsed_page.response_bytes,
                        "http_attempt_refs": http_refs,
                        "http_response_refs": http_response_refs,
                        "previous_index_ref": (
                            checkpoint.get("previous_index_ref")
                            if stage == "query"
                            else checkpoint.get("previous_verify_index_ref")
                        ),
                        "ordered_document_keys": ordered_keys,
                        "ordered_document_keys_hash": canonical_hash(ordered_keys),
                        "identity_directory_hash": checkpoint.get("directory_hash"),
                        "directory_ref": checkpoint.get("directory_ref"),
                        "issuer_id": checkpoint["issuer_id"],
                    }
                )
                index = await repo.append_change(
                    LOCAL_OWNER,
                    change_key=(
                        f"p4:disclosure-index:{root.id}:{parsed_page.page_num}"
                        if stage == "query"
                        else f"p4:disclosure-index:{root.id}:{parsed_page.page_num}:verify"
                    ),
                    kind="source_page",
                    parent_sequence=root.sequence,
                    listing_id=listing_id,
                    context="company_research",
                    payload=page_payload,
                    acquired_at=parsed_page.acquired_at,
                    available_from=parsed_page.acquired_at,
                    committed_at=now,
                )
                index_ref = _receipt_ref(index)
                unknown = [doc for doc in parsed_page.documents if doc.classification == "unknown"]
                if unknown:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "unknown_source_document_type",
                        "unknown_source_document_type",
                    )
                    return "partial"
                observed_rows = (
                    sum(
                        int(page.get("returned_count", 0))
                        for page in checkpoint.get("primary_pages", [])
                        if isinstance(page, dict)
                    )
                    + parsed_page.returned_count
                )
                if observed_rows > 200:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "window_split_required",
                        "poll_document_manifest_limit",
                    )
                    return "partial"
                if stage == "verify_query":
                    primary_pages = checkpoint.get("primary_pages")
                    page_num = int(checkpoint["page_num"])
                    if not isinstance(primary_pages, list) or not 1 <= page_num <= len(
                        primary_pages
                    ):
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "primary_page_checkpoint_missing",
                            "invalid_reference",
                        )
                        return "partial"
                    primary_page = primary_pages[page_num - 1]
                    if (
                        ordered_keys != primary_page.get("ordered_document_keys")
                        or parsed_page.has_more != primary_page.get("has_more")
                        or parsed_page.total != primary_page.get("total")
                        or parsed_page.returned_count != primary_page.get("returned_count")
                    ):
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "source_page_changed_between_passes",
                            "source_page_changed_between_passes",
                        )
                        return "partial"
                    checkpoint.update(
                        {
                            "stage": "seal_page",
                            "verification_index_ref": index_ref,
                            "verification_http_attempt_refs": http_refs,
                            "verification_http_response_refs": http_response_refs,
                            "verification_response_hash": parsed_page.response_hash,
                            "verification_ordered_document_keys_hash": canonical_hash(ordered_keys),
                            "verification_raw_blob_hash": raw_hash,
                            "redirect_count": 0,
                            "attempt_id": None,
                            "current_url": None,
                            "attempt_ordinal": attempt_ordinal,
                        }
                    )
                    await _handoff_poll_step(
                        session,
                        job=job,
                        root=root,
                        source_state=state,
                        checkpoint=checkpoint,
                    )
                    return "completed"
                filtered_refs = []
                pending_docs = []
                for doc in parsed_page.documents:
                    if (
                        doc.classification == "excluded"
                        or doc.classification not in grant.doc_type_allowlist
                    ):
                        leaf = await _append_filtered_leaf(
                            repo,
                            root.payload,
                            doc,
                            index_ref=index_ref,
                            profile=profile,
                            protocol_status=protocol_status,
                            parent_sequence=index.sequence,
                            listing_id=listing_id,
                            now=now,
                        )
                        filtered_refs.append(leaf)
                    else:
                        pending_docs.append(doc.model_dump(mode="json"))
                order_keys = [
                    [doc.published_raw_millis, doc.stable_tie_break_value]
                    for doc in parsed_page.documents
                ]
                primary_pages = checkpoint.get("primary_pages", [])
                seen_ids = {
                    key.get("native_announcement_id")
                    for page in primary_pages
                    for key in page.get("ordered_document_keys", [])
                    if isinstance(page, dict) and isinstance(key, dict)
                }
                page_ids = {key["native_announcement_id"] for key in ordered_keys}
                if seen_ids.intersection(page_ids):
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "duplicate_native_identity_across_pages",
                        "invalid_reference",
                    )
                    return "partial"
                prior_order = next(
                    (
                        page.get("order_keys", [])[-1]
                        for page in reversed(primary_pages)
                        if isinstance(page, dict) and page.get("order_keys")
                    ),
                    None,
                )
                if prior_order is not None and order_keys:
                    if protocol.query_sort_type == "desc":
                        correctly_ordered = prior_order > order_keys[0]
                    else:
                        correctly_ordered = prior_order < order_keys[0]
                    if not correctly_ordered:
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "source_order_changed_across_pages",
                            "invalid_reference",
                        )
                        return "partial"
                primary_page = {
                    "page_num": parsed_page.page_num,
                    "index_ref": index_ref,
                    "response_hash": parsed_page.response_hash,
                    "raw_blob_hash": raw_hash,
                    "ordered_document_keys": ordered_keys,
                    "order_keys": order_keys,
                    "has_more": parsed_page.has_more,
                    "total": parsed_page.total,
                    "returned_count": parsed_page.returned_count,
                    "document_refs": filtered_refs,
                    "eligible_count": 0,
                    "filtered_count": len(filtered_refs),
                    "acquired_at": parsed_page.acquired_at.isoformat(),
                }
                totals = {page.get("total") for page in primary_pages if isinstance(page, dict)}
                if parsed_page.total is not None and totals and totals != {parsed_page.total}:
                    await self._stop_poll(
                        session,
                        job,
                        state,
                        "source_total_changed_between_pages",
                        "invalid_reference",
                    )
                    return "partial"
                checkpoint.update(
                    {
                        "stage": "fetch"
                        if pending_docs
                        else ("query" if parsed_page.has_more else "verify_query"),
                        "index_ref": index_ref,
                        "response_hash": parsed_page.response_hash,
                        "raw_blob_hash": raw_hash,
                        "ordered_document_keys": ordered_keys,
                        "pending_documents": pending_docs,
                        "page_document_refs": filtered_refs,
                        "page_eligible_count": 0,
                        "page_filtered_count": len(filtered_refs),
                        "page_has_more": parsed_page.has_more,
                        "page_total": parsed_page.total,
                        "page_acquired_at": parsed_page.acquired_at.isoformat(),
                        "current_primary_page": primary_page if pending_docs else None,
                        "redirect_count": 0,
                        "attempt_id": None,
                        "current_url": None,
                        "attempt_ordinal": attempt_ordinal,
                    }
                )
                if not pending_docs:
                    primary_pages.append(primary_page)
                    checkpoint["primary_pages"] = primary_pages
                    checkpoint["previous_index_ref"] = index_ref
                    if parsed_page.has_more:
                        checkpoint["page_num"] = parsed_page.page_num + 1
                    else:
                        if (
                            parsed_page.total is not None
                            and sum(int(page.get("returned_count", 0)) for page in primary_pages)
                            != parsed_page.total
                        ):
                            await self._stop_poll(
                                session, job, state, "source_total_incomplete", "invalid_reference"
                            )
                            return "partial"
                        checkpoint["page_num"] = 1
                        first_page = primary_pages[0]
                        checkpoint.update(
                            {
                                "index_ref": first_page["index_ref"],
                                "response_hash": first_page["response_hash"],
                                "raw_blob_hash": first_page["raw_blob_hash"],
                                "ordered_document_keys": first_page["ordered_document_keys"],
                                "page_document_refs": first_page["document_refs"],
                                "page_eligible_count": first_page["eligible_count"],
                                "page_filtered_count": first_page["filtered_count"],
                                "page_has_more": first_page["has_more"],
                                "page_total": first_page["total"],
                                "page_acquired_at": first_page["acquired_at"],
                            }
                        )
            else:
                from investment_agent.evidence.disclosure_pages import CNInfoDiscoveredDocument
                from investment_agent.supplemental_rules.disclosure_candidate_ingest import (
                    ingest_disclosure_original,
                )

                pending = checkpoint.get("pending_documents")
                if not isinstance(pending, list) or not pending:
                    await self._stop_poll(
                        session, job, state, "empty_fetch_checkpoint", "invalid_reference"
                    )
                    return "partial"
                document = CNInfoDiscoveredDocument.model_validate(pending[0])
                try:
                    async with session.begin_nested():
                        validate_pdf(response.body, response.headers.get("content-type", ""))
                        result = await ingest_disclosure_original(
                            session,
                            job=job,
                            permit=sent_permit,
                            settlement=settlement,
                            index_ref=checkpoint["index_ref"],
                            http_attempt_refs=http_refs,
                            http_response_refs=http_response_refs,
                            document=document,
                            body=response.body,
                            response_url=response.url,
                            now=now,
                            settings=self.settings,
                            poll_id=UUID(str(root.id)),
                            protocol_profile=profile,
                            protocol_status=protocol_status,
                        )
                except (DisclosureError, InvestorError) as exc:
                    await self._stop_poll(session, job, state, str(exc), str(exc))
                    return "partial"
                state = await repo.get_source(
                    LOCAL_OWNER,
                    DISCLOSURE_FEED,
                    source_scope_key(listing_ref),
                    for_update=True,
                )
                if state is None:
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id,
                        "disclosure_source_state_missing_after_ingest",
                        lease=job.lease,
                    )
                    return "partial"
                pending.pop(0)
                doc_ref = result["leaf_ref"]
                checkpoint["page_document_refs"] = [
                    *checkpoint.get("page_document_refs", []),
                    doc_ref,
                ]
                checkpoint["page_eligible_count"] = (
                    int(checkpoint.get("page_eligible_count", 0)) + 1
                )
                checkpoint["stage"] = (
                    "fetch"
                    if pending
                    else ("query" if checkpoint.get("page_has_more") else "verify_query")
                )
                checkpoint["pending_documents"] = pending
                current_primary = checkpoint.get("current_primary_page")
                if not pending:
                    if not isinstance(current_primary, dict):
                        await self._stop_poll(
                            session,
                            job,
                            state,
                            "primary_page_checkpoint_missing",
                            "invalid_reference",
                        )
                        return "partial"
                    current_primary["document_refs"] = list(
                        checkpoint.get("page_document_refs", [])
                    )
                    current_primary["eligible_count"] = int(
                        checkpoint.get("page_eligible_count", 0)
                    )
                    current_primary["filtered_count"] = int(
                        checkpoint.get("page_filtered_count", 0)
                    )
                    primary_pages = list(checkpoint.get("primary_pages", []))
                    primary_pages.append(current_primary)
                    checkpoint["primary_pages"] = primary_pages
                    checkpoint["previous_index_ref"] = checkpoint.get("index_ref")
                    if checkpoint.get("page_has_more"):
                        checkpoint["page_num"] = int(checkpoint["page_num"]) + 1
                    else:
                        page_total = checkpoint.get("page_total")
                        if (
                            page_total is not None
                            and sum(int(page.get("returned_count", 0)) for page in primary_pages)
                            != page_total
                        ):
                            await self._stop_poll(
                                session, job, state, "source_total_incomplete", "invalid_reference"
                            )
                            return "partial"
                        checkpoint["page_num"] = 1
                        first_page = primary_pages[0]
                        checkpoint.update(
                            {
                                "index_ref": first_page["index_ref"],
                                "response_hash": first_page["response_hash"],
                                "raw_blob_hash": first_page["raw_blob_hash"],
                                "ordered_document_keys": first_page["ordered_document_keys"],
                                "page_document_refs": first_page["document_refs"],
                                "page_eligible_count": first_page["eligible_count"],
                                "page_filtered_count": first_page["filtered_count"],
                                "page_has_more": first_page["has_more"],
                                "page_total": first_page["total"],
                                "page_acquired_at": first_page["acquired_at"],
                            }
                        )
                    checkpoint["current_primary_page"] = None
                checkpoint["redirect_count"] = 0
                checkpoint["attempt_id"] = None
                checkpoint["current_url"] = None
                checkpoint["attempt_ordinal"] = attempt_ordinal
            await _handoff_poll_step(
                session,
                job=job,
                root=root,
                source_state=state,
                checkpoint=checkpoint,
            )
            return "completed"

    async def _stop_poll(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        state: Any,
        poll_state: str,
        reason: str,
        *,
        retry_at: datetime | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        repo = SupplementalUpdatesRepository(session)
        payload = {
            **state.payload,
            "poll_state": poll_state,
            "failure_code": reason[:64],
            "retry_at": retry_at.isoformat() if retry_at else None,
        }
        if checkpoint is not None:
            payload["poll_checkpoint"] = _json_wire(checkpoint)
        if self._testing:
            payload["protocol_profile"] = "fixture"
            payload["protocol_status"] = "fixture_validated"
        await repo.save_source(
            state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "payload": payload,
                    "coverage_state": "partial",
                    "failure_code": reason[:64],
                    "retry_at": retry_at,
                }
            ),
            expected_revision=state.revision,
        )
        await PipelineRunRepository(session).mark_partial(
            job.run_id,
            reason[:200],
            {"poll_state": poll_state, "poll_complete": False},
            lease=job.lease,
        )

    async def _finish_local_stage(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        root: Any,
        state: Any,
        checkpoint: dict[str, Any],
        grant: BackgroundDisclosureAuthorizationV1 | BackgroundDisclosureAuthorizationV2,
        *,
        protocol_profile: str,
        protocol_status: str,
        listing_ref: ObjectRef,
        now: datetime,
    ) -> None:
        repo = SupplementalUpdatesRepository(session)
        listing_id = int(listing_ref.id)
        if checkpoint.get("stage") == "seal_page":
            index_ref = checkpoint["index_ref"]
            document_refs = checkpoint.get("page_document_refs", [])
            index = await repo.get_change(LOCAL_OWNER, index_ref["sequence"])
            if index is None or _receipt_ref(index) != index_ref:
                await self._stop_poll(
                    session, job, state, "index_receipt_mismatch", "invalid_reference"
                )
                return
            bridge_refs = checkpoint.get("bridge_refs", [])
            previous_page_ref = bridge_refs[-1] if bridge_refs else None
            bridge_payload = _poll_common_payload(
                root.payload,
                profile=protocol_profile,
                protocol_status=protocol_status,
            )
            bridge_payload.update(
                {
                    "record_type": "poll_page_v1",
                    "page_num": checkpoint["page_num"],
                    "has_more": bool(checkpoint.get("page_has_more")),
                    "previous_page_ref": previous_page_ref,
                    "index_ref": index_ref,
                    "verification_index_ref": checkpoint.get("verification_index_ref"),
                    "verification_http_attempt_refs": checkpoint.get(
                        "verification_http_attempt_refs"
                    ),
                    "verification_http_response_refs": checkpoint.get(
                        "verification_http_response_refs"
                    ),
                    "verification_response_hash": checkpoint.get("verification_response_hash"),
                    "verification_ordered_document_keys_hash": checkpoint.get(
                        "verification_ordered_document_keys_hash"
                    ),
                    "directory_ref": checkpoint.get("directory_ref"),
                    "document_refs": document_refs,
                    "document_count": len(document_refs),
                    "eligible_count": int(checkpoint.get("page_eligible_count", 0)),
                    "filtered_count": int(checkpoint.get("page_filtered_count", 0)),
                    "ordered_document_keys_hash": canonical_hash(
                        checkpoint.get("ordered_document_keys", [])
                    ),
                    "response_hash": checkpoint["response_hash"],
                    "seal_status": "complete",
                }
            )
            bridge_payload["seal_hash"] = canonical_hash(
                {
                    "index_ref": index_ref,
                    "document_refs": document_refs,
                    "ordered_document_keys_hash": bridge_payload["ordered_document_keys_hash"],
                    "response_hash": checkpoint["response_hash"],
                    "verification_ordered_document_keys_hash": checkpoint.get(
                        "verification_ordered_document_keys_hash"
                    ),
                    "verification_response_hash": checkpoint.get("verification_response_hash"),
                    "verification_index_ref": checkpoint.get("verification_index_ref"),
                }
            )
            bridge = await repo.append_change(
                LOCAL_OWNER,
                change_key=f"p4:disclosure-page:{root.id}:{checkpoint['page_num']}",
                kind="source_page",
                parent_sequence=root.sequence,
                listing_id=listing_id,
                context="company_research",
                payload=bridge_payload,
                acquired_at=datetime.fromisoformat(checkpoint["page_acquired_at"]),
                available_from=datetime.fromisoformat(checkpoint["page_acquired_at"]),
                committed_at=now,
            )
            bridge_refs.append(_receipt_ref(bridge))
            all_docs = [*checkpoint.get("document_refs", []), *document_refs]
            next_checkpoint = {
                **checkpoint,
                "bridge_refs": bridge_refs,
                "document_refs": all_docs,
                "document_count": len(all_docs),
                "eligible_count": int(checkpoint.get("eligible_count", 0))
                + int(checkpoint.get("page_eligible_count", 0)),
                "filtered_count": int(checkpoint.get("filtered_count", 0))
                + int(checkpoint.get("page_filtered_count", 0)),
                "page_count": int(checkpoint.get("page_count", 0)) + 1,
                "previous_index_ref": index_ref,
                "previous_verify_index_ref": checkpoint.get("verification_index_ref"),
                "previous_bridge_ref": _receipt_ref(bridge),
                "pending_documents": [],
                "page_document_refs": [],
                "index_ref": None,
                "verification_index_ref": None,
                "verification_http_attempt_refs": None,
                "verification_http_response_refs": None,
                "verification_response_hash": None,
                "verification_ordered_document_keys_hash": None,
                "stage": "verify_query",
                "redirect_count": 0,
                "attempt_id": None,
            }
            primary_pages = next_checkpoint.get("primary_pages", [])
            if int(checkpoint["page_num"]) < len(primary_pages):
                next_checkpoint["page_num"] = int(checkpoint["page_num"]) + 1
                next_page = primary_pages[next_checkpoint["page_num"] - 1]
                next_checkpoint.update(
                    {
                        "index_ref": next_page["index_ref"],
                        "response_hash": next_page["response_hash"],
                        "raw_blob_hash": next_page["raw_blob_hash"],
                        "ordered_document_keys": next_page["ordered_document_keys"],
                        "page_document_refs": next_page["document_refs"],
                        "page_eligible_count": next_page["eligible_count"],
                        "page_filtered_count": next_page["filtered_count"],
                        "page_has_more": next_page["has_more"],
                        "page_total": next_page["total"],
                        "page_acquired_at": next_page["acquired_at"],
                    }
                )
                await _handoff_poll_step(
                    session,
                    job=job,
                    root=root,
                    source_state=state,
                    checkpoint=next_checkpoint,
                )
            else:
                next_checkpoint["stage"] = "finalize"
                await _handoff_poll_step(
                    session,
                    job=job,
                    root=root,
                    source_state=state,
                    checkpoint=next_checkpoint,
                )
            return

        bridge_refs = checkpoint.get("bridge_refs", [])
        document_refs = checkpoint.get("document_refs", [])
        if not bridge_refs or len(bridge_refs) > MAX_QUERY_PAGES or len(document_refs) > 200:
            await self._stop_poll(
                session, job, state, "poll_manifest_bounds", "poll_manifest_bounds"
            )
            return
        acquired_at = datetime.fromisoformat(checkpoint["acquired_at"])
        listing = await session.get(Listing, listing_id)
        window_start = datetime.fromisoformat(root.payload["window_start"])
        window_end = datetime.fromisoformat(root.payload["window_end"])
        if listing is None:
            await self._stop_poll(
                session, job, state, "listing_missing", "disclosure_listing_invalid"
            )
            return
        page_chain_root = canonical_hash(bridge_refs)
        document_manifest_hash = canonical_hash(document_refs)
        history_start = grant.history_start.astimezone(UTC)
        history_basis_hash = canonical_hash(
            {
                "source_scope_hash": root.payload["source_scope_hash"],
                "issuer_id": checkpoint["issuer_id"],
                "history_start": history_start.isoformat(),
            }
        )
        prior_basis_hash = state.payload.get("history_basis_hash")
        prior_epoch = state.payload.get("history_epoch")
        raw_prior_ordinal = state.payload.get("epoch_ordinal", 0)
        raw_prior_semantic_generation = state.payload.get("semantic_generation", 0)
        if (
            type(raw_prior_ordinal) is not int
            or raw_prior_ordinal < 0
            or type(raw_prior_semantic_generation) is not int
            or raw_prior_semantic_generation < 0
        ):
            await self._stop_poll(
                session,
                job,
                state,
                "previous_final_invalid",
                "previous_final_missing_or_invalid",
            )
            return
        prior_ordinal = raw_prior_ordinal
        semantic_generation_base = root.payload.get("semantic_generation_base")
        base_final_ref = root.payload.get("semantic_generation_base_final_ref")
        if type(semantic_generation_base) is not int or semantic_generation_base < 0:
            await self._stop_poll(
                session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
            )
            return
        base_final_generation = 0
        base_final_sequence = 0
        if base_final_ref is not None:
            if not isinstance(base_final_ref, dict):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            try:
                base_sequence = base_final_ref.get("sequence")
                base_id = UUID(str(base_final_ref.get("id")))
                base_hash = base_final_ref.get("payload_hash")
                if (type(base_sequence) is not int or base_sequence < 1
                        or not isinstance(base_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", base_hash)):
                    raise ValueError("invalid final reference")
            except (TypeError, ValueError):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            base_final_row = await repo.get_change(LOCAL_OWNER, base_sequence)
            if (
                base_final_row is None or base_final_row.id != base_id
                or base_final_row.payload_hash != base_hash
                or base_final_row.kind != "source_page"
                or base_final_row.listing_id != listing_id
                or base_final_row.context != "company_research"
                or base_final_row.payload.get("record_type") != "poll_final_v1"
                or base_final_row.payload.get("proof_type") != "poll_window_v1"
                or base_final_row.payload.get("source_id") != DISCLOSURE_FEED
                or type(base_final_row.payload.get("semantic_generation")) is not int
            ):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            base_final_generation = base_final_row.payload["semantic_generation"]
            base_final_sequence = base_final_row.sequence

        # A replacement can inherit semantic changes committed by a prior
        # incomplete poll. Count only exact leaf receipts frozen in its
        # bounded checkpoint snapshots, and only those after the last final.
        inherited_semantic_delta = 0
        raw_snapshot_refs = root.payload.get("predecessor_snapshot_refs", [])
        if not isinstance(raw_snapshot_refs, list) or len(raw_snapshot_refs) > 20:
            await self._stop_poll(
                session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
            )
            return
        inherited_leaf_refs: dict[tuple[int, str], dict[str, Any]] = {}
        for snapshot_ref in raw_snapshot_refs:
            if not isinstance(snapshot_ref, dict):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            try:
                snapshot_sequence = snapshot_ref.get("sequence")
                snapshot_id = UUID(str(snapshot_ref.get("id")))
                snapshot_hash = snapshot_ref.get("payload_hash")
                if (type(snapshot_sequence) is not int or snapshot_sequence < 1
                        or not isinstance(snapshot_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)):
                    raise ValueError("invalid snapshot reference")
            except (TypeError, ValueError):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            snapshot_row = await repo.get_change(LOCAL_OWNER, snapshot_sequence)
            checkpoint_snapshot = (
                snapshot_row.payload.get("checkpoint") if snapshot_row is not None else None
            )
            old_poll_id = snapshot_row.payload.get("poll_id") if snapshot_row is not None else None
            if (
                snapshot_row is None or snapshot_row.id != snapshot_id
                or snapshot_row.payload_hash != snapshot_hash
                or snapshot_row.kind != "source_attempt"
                or snapshot_row.listing_id != listing_id
                or snapshot_row.context != "company_research"
                or snapshot_row.payload.get("record_type") != "poll_checkpoint_snapshot_v1"
                or not isinstance(old_poll_id, str)
                or not isinstance(checkpoint_snapshot, dict)
            ):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            try:
                inherited_refs = _checkpoint_document_refs(checkpoint_snapshot)
            except InvestorError:
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            for inherited_ref in inherited_refs:
                key = (inherited_ref["sequence"], inherited_ref["id"])
                if key in inherited_leaf_refs and inherited_leaf_refs[key] != inherited_ref:
                    await self._stop_poll(
                        session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                    )
                    return
                inherited_leaf_refs[key] = inherited_ref
        if len(inherited_leaf_refs) > 200:
            await self._stop_poll(
                session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
            )
            return
        for inherited_ref in inherited_leaf_refs.values():
            if inherited_ref["sequence"] <= base_final_sequence:
                continue
            inherited_leaf = await repo.get_change(LOCAL_OWNER, inherited_ref["sequence"])
            if (
                inherited_leaf is None
                or str(inherited_leaf.id) != inherited_ref["id"]
                or inherited_leaf.payload_hash != inherited_ref["payload_hash"]
                or inherited_leaf.kind != "source_attempt"
                or inherited_leaf.listing_id != listing_id
                or inherited_leaf.context != "company_research"
                or inherited_leaf.payload.get("record_type") != "poll_document_v1"
                or type(inherited_leaf.payload.get("semantic_change")) is not bool
            ):
                await self._stop_poll(
                    session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
                )
                return
            if inherited_leaf.payload.get("semantic_change"):
                inherited_semantic_delta += 1
        if semantic_generation_base != base_final_generation + inherited_semantic_delta:
            await self._stop_poll(
                session, job, state, "previous_final_invalid", "previous_final_missing_or_invalid"
            )
            return
        same_history_basis = prior_basis_hash == history_basis_hash
        previous_final_ref: dict[str, Any] | None = None
        previous_final_payload: dict[str, Any] | None = None
        prior_watermark: datetime | None = None
        if same_history_basis:
            prior_sequence = state.success_sequence
            prior_receipt_hash = state.success_receipt_hash
            if (
                type(prior_sequence) is not int
                or prior_sequence < 1
                or not isinstance(prior_receipt_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", prior_receipt_hash)
            ):
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "previous_final_missing",
                    "previous_final_missing_or_invalid",
                )
                return
            previous_final = await repo.get_change(LOCAL_OWNER, prior_sequence)
            previous_final_payload = (
                previous_final.payload if previous_final is not None else None
            )
            if not isinstance(previous_final_payload, dict):
                previous_final_payload = None
            prior_index = (
                _aware_utc(previous_final_payload.get("continuous_index_watermark"))
                if previous_final_payload is not None
                else None
            )
            prior_original = (
                _aware_utc(previous_final_payload.get("continuous_original_watermark"))
                if previous_final_payload is not None
                else None
            )
            prior_complete = (
                _aware_utc(previous_final_payload.get("continuous_complete_watermark"))
                if previous_final_payload is not None
                else None
            )
            projected_index = _aware_utc(state.payload.get("continuous_index_watermark"))
            projected_original = _aware_utc(state.payload.get("continuous_original_watermark"))
            projected_complete = _aware_utc(state.payload.get("continuous_complete_watermark"))
            prior_coverage_anchor = canonical_hash(
                {
                    "source_scope_hash": root.payload["source_scope_hash"],
                    "history_epoch": prior_epoch,
                    "history_start": history_start.isoformat(),
                }
            ) if isinstance(prior_epoch, str) else None
            if (
                previous_final is None
                or previous_final.kind != "source_page"
                or previous_final.sequence != prior_sequence
                or previous_final.payload_hash != prior_receipt_hash
                or previous_final.committed_at >= now
                or previous_final.listing_id != listing_id
                or previous_final.context != "company_research"
                or previous_final_payload is None
                or previous_final_payload.get("record_type") != "poll_final_v1"
                or previous_final_payload.get("source_id") != DISCLOSURE_FEED
                or previous_final_payload.get("contract_version") != SOURCE_CONTRACT_VERSION
                or previous_final_payload.get("contract_hash") != grant.contract_hash
                or previous_final_payload.get("source_scope_hash") != root.payload["source_scope_hash"]
                or previous_final_payload.get("issuer_id") != checkpoint.get("issuer_id")
                or _aware_utc(previous_final_payload.get("history_start")) != history_start
                or previous_final_payload.get("history_basis_hash") != history_basis_hash
                or previous_final_payload.get("history_epoch") != prior_epoch
                or previous_final_payload.get("epoch_ordinal") != prior_ordinal
                or previous_final_payload.get("semantic_generation")
                != base_final_generation
                or _receipt_ref(previous_final) != base_final_ref
                or previous_final_payload.get("coverage_anchor") != prior_coverage_anchor
                or state.payload.get("coverage_anchor") != prior_coverage_anchor
                or state.complete_watermark != prior_complete
                or (prior_index, prior_original, prior_complete)
                != (projected_index, projected_original, projected_complete)
                or (prior_index is None) != (prior_original is None)
                or (prior_original is None) != (prior_complete is None)
            ):
                await self._stop_poll(
                    session,
                    job,
                    state,
                    "previous_final_invalid",
                    "previous_final_missing_or_invalid",
                )
                return
            previous_final_ref = _receipt_ref(previous_final)
            prior_watermark = prior_complete
            history_epoch = str(prior_epoch)
            epoch_ordinal = max(1, prior_ordinal)
        else:
            history_epoch = str(uuid4())
            epoch_ordinal = max(0, prior_ordinal) + 1
        coverage_anchor = canonical_hash(
            {
                "source_scope_hash": root.payload["source_scope_hash"],
                "history_epoch": history_epoch,
                "history_start": history_start.isoformat(),
            }
        )
        coverage_target_end = datetime.fromisoformat(
            root.payload.get("coverage_target_end", root.payload["window_end"])
        ).astimezone(UTC)
        contiguous = (prior_watermark is None and window_start <= history_start) or (
            prior_watermark is not None and window_start <= prior_watermark
        )
        candidate_watermark = (
            (max(prior_watermark, window_end) if prior_watermark is not None else window_end)
            if contiguous
            else prior_watermark
        )
        pending_predecessor = state.payload.get("pending_predecessor")
        predecessor_satisfied = not isinstance(pending_predecessor, dict)
        if isinstance(pending_predecessor, dict):
            prior_start = _aware_utc(pending_predecessor.get("window_start"))
            prior_end = _aware_utc(pending_predecessor.get("window_end"))
            required_ids = pending_predecessor.get("pending_native_ids", [])
            observed_ids: set[str] = set()
            for page in checkpoint.get("primary_pages", []):
                if not isinstance(page, dict):
                    continue
                for key in page.get("ordered_document_keys", []):
                    if isinstance(key, dict) and isinstance(
                        key.get("native_announcement_id"), str
                    ):
                        observed_ids.add(key["native_announcement_id"])
            predecessor_satisfied = bool(
                prior_start is not None
                and prior_end is not None
                and window_start <= prior_start
                and window_end >= prior_end
                and isinstance(required_ids, list)
                and all(isinstance(item, str) and item in observed_ids for item in required_ids)
            )
        next_watermark = candidate_watermark if predecessor_satisfied else prior_watermark
        coverage_complete = (
            predecessor_satisfied
            and contiguous
            and next_watermark is not None
            and next_watermark >= coverage_target_end
        )
        semantic_generation = max(0, int(state.payload.get("semantic_generation", 0)))
        current_semantic_delta = 0
        current_leaf_ids: set[tuple[int, str]] = set()
        try:
            current_leaf_refs = _checkpoint_document_refs(checkpoint)
        except InvestorError:
            current_leaf_refs = []
            current_leaf_ids = set()
            semantic_generation = -1
        for current_ref in current_leaf_refs:
            key = (current_ref["sequence"], current_ref["id"])
            if key in current_leaf_ids:
                semantic_generation = -1
                break
            current_leaf_ids.add(key)
            current_leaf = await repo.get_change(LOCAL_OWNER, current_ref["sequence"])
            if (
                current_leaf is None or str(current_leaf.id) != current_ref["id"]
                or current_leaf.payload_hash != current_ref["payload_hash"]
                or current_leaf.kind != "source_attempt"
                or current_leaf.listing_id != listing_id
                or current_leaf.context != "company_research"
                or current_leaf.payload.get("record_type") != "poll_document_v1"
                or current_leaf.payload.get("poll_id") != str(root.id)
                or type(current_leaf.payload.get("semantic_change")) is not bool
            ):
                semantic_generation = -1
                break
            if current_leaf.payload.get("semantic_change"):
                current_semantic_delta += 1
        if (semantic_generation < 0
                or semantic_generation_base + current_semantic_delta != semantic_generation):
            await self._stop_poll(
                session, job, state, "semantic_generation_mismatch", "source_scope_generation_invalid"
            )
            return
        payload = {
            "record_type": "poll_final_v1",
            "proof_type": "poll_window_v1",
            "source_id": DISCLOSURE_FEED,
            "protocol_profile": protocol_profile,
            "protocol_status": protocol_status,
            "contract_version": SOURCE_CONTRACT_VERSION,
            "contract_hash": grant.contract_hash,
            "source_scope_hash": root.payload["source_scope_hash"],
            "attempt_scope_hash": root.payload["attempt_scope_hash"],
            "grant_ref": str(grant.authorization_id),
            "grant_hash": grant.authorization_hash,
            "grant_receipt_ref": root.payload["grant_receipt_ref"],
            "poll_ref": str(root.id),
            "poll_id": str(root.id),
            "directory_ref": checkpoint.get("directory_ref"),
            "authorization_id": str(grant.authorization_id),
            "authorization_revision": grant.revision,
            "authorization_hash": grant.authorization_hash,
            "listing_ref": listing_ref.model_dump(mode="json"),
            "context": "company_research",
            "publication_window_start": window_start.isoformat(),
            "publication_window_end": window_end.isoformat(),
            "history_start": history_start.isoformat(),
            "index_watermark": window_end.isoformat(),
            "original_watermark": window_end.isoformat(),
            "complete_watermark": window_end.isoformat(),
            "continuous_index_watermark": (
                next_watermark.isoformat()
                if next_watermark is not None
                else None
            ),
            "continuous_original_watermark": (
                next_watermark.isoformat()
                if next_watermark is not None
                else None
            ),
            "continuous_complete_watermark": (
                next_watermark.isoformat() if next_watermark is not None else None
            ),
            "page_count": len(bridge_refs),
            "document_count": len(document_refs),
            "eligible_count": int(checkpoint.get("eligible_count", 0)),
            "filtered_count": int(checkpoint.get("filtered_count", 0)),
            "child_receipts": bridge_refs,
            "page_chain_root": page_chain_root,
            "document_manifest_hash": document_manifest_hash,
            "observation_at": now.isoformat(),
            "acquired_at": acquired_at.isoformat(),
            "available_from": acquired_at.isoformat(),
            "committed_at": now.isoformat(),
            "last_success_at": now.isoformat(),
            "health_limit_seconds": grant.max_staleness_seconds,
            "history_epoch": history_epoch,
            "epoch_ordinal": epoch_ordinal,
            "semantic_generation": semantic_generation,
            "coverage_anchor": coverage_anchor,
            "issuer_id": checkpoint["issuer_id"],
            "history_basis_hash": history_basis_hash,
            "previous_final_ref": previous_final_ref,
            "predecessor_snapshot_ref": root.payload.get("predecessor_snapshot_ref"),
            "predecessor_snapshot_refs": root.payload.get("predecessor_snapshot_refs", []),
            "predecessor_resolution": (
                "none"
                if not isinstance(pending_predecessor, dict)
                else "resolved"
                if predecessor_satisfied
                else "pending"
            ),
            "predecessor_pending_ids_hash": canonical_hash(
                sorted(set(pending_predecessor.get("pending_native_ids", [])))
                if isinstance(pending_predecessor, dict)
                else []
            ),
        }
        final = await repo.append_change(
            LOCAL_OWNER,
            change_key=f"p4:disclosure-final:{root.id}",
            kind="source_page",
            parent_sequence=root.sequence,
            listing_id=listing_id,
            context="company_research",
            payload=payload,
            acquired_at=acquired_at,
            available_from=acquired_at,
            committed_at=now,
        )
        campaign_binding = root.payload.get("campaign_binding")
        if isinstance(campaign_binding, dict):
            from investment_agent.supplemental_rules.disclosure_campaign import (
                complete_campaign_poll,
            )

            await complete_campaign_poll(
                session,
                campaign_id=UUID(str(campaign_binding.get("campaign_id"))),
                poll_id=str(root.id),
                action_id=str(campaign_binding.get("action_id")),
                now=now,
            )
        await repo.save_source(
            state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "config_hash": grant.contract_hash,
                    "contract_version": SOURCE_CONTRACT_VERSION,
                    "scope_hash": root.payload["source_scope_hash"],
                    "window_start": window_start,
                    "window_end": window_end,
                    "complete_watermark": next_watermark,
                    "coverage_state": "complete" if coverage_complete else "partial",
                    "success_sequence": final.sequence,
                    "success_receipt_hash": final.payload_hash,
                    "source_refs": bridge_refs,
                    "last_attempt_at": now,
                    "last_success_at": now,
                    "failure_code": None,
                    "retry_at": None,
                    "health_limit_seconds": grant.max_staleness_seconds,
                    "payload": {
                        **state.payload,
                        "active_poll_id": str(root.id),
                        "active_step_run_id": job.run_id,
                        "poll_state": "completed",
                        "poll_checkpoint": checkpoint,
                        "authorization_id": str(grant.authorization_id),
                        "authorization_revision": grant.revision,
                        "index_watermark": (
                            window_end.isoformat()
                            if contiguous
                            else state.payload.get("index_watermark")
                            if same_history_basis
                            else None
                        ),
                        "original_watermark": (
                            window_end.isoformat()
                            if contiguous
                            else state.payload.get("original_watermark")
                            if same_history_basis
                            else None
                        ),
                        "continuous_index_watermark": (
                            window_end.isoformat()
                            if contiguous
                            else state.payload.get("index_watermark")
                            if same_history_basis
                            else None
                        ),
                        "continuous_original_watermark": (
                            window_end.isoformat()
                            if contiguous
                            else state.payload.get("original_watermark")
                            if same_history_basis
                            else None
                        ),
                        "continuous_complete_watermark": (
                            next_watermark.isoformat() if next_watermark is not None else None
                        ),
                        "bridge_pending": 0,
                        "history_start": history_start.isoformat(),
                        "authorized_history_end": grant.history_end.astimezone(UTC).isoformat(),
                        "history_frontier": next_watermark.isoformat() if next_watermark else None,
                        "history_gap": not coverage_complete,
                        "pending_predecessor": (
                            None if predecessor_satisfied else pending_predecessor
                        ),
                        "coverage_target_end": coverage_target_end.isoformat(),
                        "issuer_id": checkpoint["issuer_id"],
                        "protocol_profile": protocol_profile,
                        "protocol_status": protocol_status,
                        "history_epoch": history_epoch,
                        "history_basis_hash": history_basis_hash,
                        "epoch_ordinal": epoch_ordinal,
                        "semantic_generation": semantic_generation,
                        "coverage_anchor": coverage_anchor,
                        "final_ref": _receipt_ref(final),
                        "latest_final_sequence": final.sequence,
                        "latest_final_id": str(final.id),
                        "latest_final_hash": final.payload_hash,
                    },
                }
            ),
            expected_revision=state.revision,
        )
        await PipelineRunRepository(session).mark_success(
            job.run_id,
            {"poll_state": "completed", "poll_complete": True, "final_sequence": final.sequence},
            lease=job.lease,
        )
