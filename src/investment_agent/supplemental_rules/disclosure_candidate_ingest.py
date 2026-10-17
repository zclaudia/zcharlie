"""Atomic, lease-fenced persistence for one authorized CNInfo original.

The operation records source material as candidate evidence only.  It does not
parse financial facts or grant a verification status.  All durable writes,
including the proof leaf that binds the original to its source index, belong to
the caller's existing worker transaction.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.disclosure_http import DisclosureError, validate_url
from investment_agent.evidence.disclosure_pages import CNInfoDiscoveredDocument
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.supplemental_rules import ResearchEventRevision
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import RunLease
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import ResearchEvent
from investment_agent.supplemental_rules.disclosure_authorization import (
    assert_attempt_authorized,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    DOCUMENT_FEED,
    HttpHopPermit,
    HttpHopSettlement,
    authorization_scope_hash,
)

OWNER: OwnerId = "local_owner"
DISCLOSURE_SOURCE_FEED = "cninfo_szse_v1"
DISCLOSURE_SOURCE_CONTRACT_VERSION = "1"
DISCLOSURE_CONTEXT = "company_research"
ELIGIBLE_TYPES = {
    "annual_report_full",
    "annual_report_correction",
    "annual_report_supplement",
}


def _index_document_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    """Read only a bounded, explicit ordered identity list from an index receipt."""
    rows = payload.get("documents")
    if isinstance(rows, list):
        values: list[str] = []
        for row in rows:
            value = row.get("native_announcement_id") if isinstance(row, dict) else None
            if not isinstance(value, str):
                break
            values.append(value)
        if len(values) == len(rows) and len(values) <= 20:
            return tuple(values)
    for key in ("ordered_document_ids", "ordered_document_keys", "document_ids"):
        value = payload.get(key)
        if isinstance(value, list) and len(value) <= 20:
            ids: list[str] = []
            for item in value:
                native_id = item.get("native_announcement_id") if isinstance(item, dict) else item
                if not isinstance(native_id, str):
                    break
                ids.append(native_id)
            if len(ids) == len(value):
                return tuple(ids)
    return ()


def _index_document_matches(payload: dict[str, Any], document: CNInfoDiscoveredDocument) -> bool:
    for key in ("ordered_document_keys", "documents"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("native_announcement_id") == document.native_announcement_id:
                return row.get("identity_hash") == document.identity_hash
        return False
    return True


def _receipt_ref(receipt: SupplementalChangeReceiptRow) -> dict[str, Any]:
    return {
        "sequence": receipt.sequence,
        "id": receipt.id,
        "payload_hash": receipt.payload_hash,
    }


def _event_semantics(payload: dict[str, Any]) -> str:
    published_at = payload.get("published_at")
    if isinstance(published_at, str):
        # Pydantic JSON mode emits UTC timestamps with ``Z`` while some older
        # candidate producers used ``+00:00``. Compare the instant, not spelling.
        try:
            parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            published_at = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return canonical_hash({
        key: payload.get(key)
        for key in (
            "event_key", "title", "event_type", "jurisdiction", "affected_products",
            "affected_subjects", "policy_stage", "effective_from", "effective_to", "source_refs",
        )
    } | {"published_at": published_at})


async def assert_run_lease(session: AsyncSession, lease: RunLease) -> None:
    """Public worker-facing lease fence for a disclosure writer transaction."""
    from investment_agent.supplemental_rules.disclosure_authorization import (
        assert_run_lease as _assert,
    )

    await _assert(session, lease)


async def ingest_disclosure_original(
    session: AsyncSession,
    *,
    job: ClaimedJob,
    permit: HttpHopPermit,
    settlement: HttpHopSettlement,
    http_attempt_refs: list[dict[str, Any]],
    http_response_refs: list[dict[str, Any]],
    index_ref: dict[str, Any],
    document: CNInfoDiscoveredDocument,
    body: bytes,
    response_url: str,
    now: datetime,
    settings: Settings,
    poll_id: UUID,
    protocol_profile: str,
    protocol_status: str,
) -> dict[str, Any]:
    """Persist one fetched document and its candidate event atomically.

    ``permit`` identifies the exact fetch hop; ``settlement`` proves that the
    transport result was already durably accounted.  The helper deliberately
    accepts the worker's typed objects rather than a public/user-report DTO.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    now = now.astimezone(UTC)
    identity = permit.identity
    if (
        identity.request_kind != "fetch"
        or permit.state != "sent"
        or settlement.permit_id != permit.permit_id
        or settlement.state != "result"
        or settlement.request_consumed != 1
        or settlement.response_bytes_consumed != len(body)
        or len(body) > identity.response_bytes_limit
        or document.classification not in ELIGIBLE_TYPES
        or document.native_announcement_id == ""
        or not re.fullmatch(r"\d{1,32}", document.native_announcement_id)
    ):
        raise InvestorError("invalid_reference", "原件候选接收身份或结算不匹配")
    if identity.poll_id != str(poll_id):
        raise InvestorError("disclosure_scope_forbidden", "原件与轮询身份不一致")
    if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-4096:]:
        raise DisclosureError("invalid_original_mime_or_signature")
    validate_url(response_url, "original")
    if response_url != identity.url:
        raise InvestorError("disclosure_scope_forbidden", "HTTP最终地址与已授权hop不一致")

    await assert_run_lease(session, job.lease)
    attempt_prefix = f"p4:attempt:{identity.attempt_id}:{identity.hop_ordinal}"
    receipt_repo = SupplementalUpdatesRepository(session)
    reservation_receipt = await receipt_repo.get_change_by_key(
        OWNER, f"{attempt_prefix}:reserved",
    )
    sent_receipt = await receipt_repo.get_change_by_key(
        OWNER, f"{attempt_prefix}:sent",
    )
    settled_receipt = await receipt_repo.get_change_by_key(
        OWNER, f"{attempt_prefix}:result",
    )
    original_lease = (
        reservation_receipt.payload.get("lease")
        if reservation_receipt is not None
        else None
    )
    original_lease_valid = (
        isinstance(original_lease, dict)
        and set(original_lease) == {"run_id", "worker_id", "attempt"}
        and type(original_lease.get("run_id")) is int
        and original_lease["run_id"] > 0
        and isinstance(original_lease.get("worker_id"), str)
        and bool(original_lease["worker_id"])
        and type(original_lease.get("attempt")) is int
        and original_lease["attempt"] > 0
    )
    original_run_id = (
        original_lease.get("run_id") if isinstance(original_lease, dict) else None
    )
    if (
        reservation_receipt is None or sent_receipt is None or settled_receipt is None
        or reservation_receipt.payload.get("identity_hash")
        != canonical_hash(identity.model_dump(mode="json"))
        or not isinstance(reservation_receipt.payload.get("permit"), dict)
        or reservation_receipt.payload["permit"].get("permit_id") != str(permit.permit_id)
        or reservation_receipt.payload["permit"].get("identity")
        != identity.model_dump(mode="json")
        or not original_lease_valid
        or sent_receipt.payload.get("stage") != "sent"
        or sent_receipt.payload.get("permit_id") != str(permit.permit_id)
        or sent_receipt.payload.get("attempt_id") != str(identity.attempt_id)
        or sent_receipt.payload.get("hop_id") != identity.hop_id
        or sent_receipt.payload.get("hop_ordinal") != identity.hop_ordinal
        # A reclaimed run may have sent an unsent reservation under its new
        # lease. Bind the sent record to the original reservation explicitly;
        # current job ownership is fenced independently above before writing.
        or sent_receipt.payload.get(
            "reservation_lease", sent_receipt.payload.get("lease")
        ) != original_lease
        or not isinstance(sent_receipt.payload.get("lease"), dict)
        or set(sent_receipt.payload["lease"]) != {"run_id", "worker_id", "attempt"}
        or sent_receipt.payload["lease"].get("run_id") != job.run_id
        or sent_receipt.payload["lease"].get("run_id") != original_run_id
        or settled_receipt.payload.get("stage") != "result"
        or settled_receipt.payload.get("permit_id") != str(permit.permit_id)
        or settled_receipt.payload.get("attempt_id") != str(identity.attempt_id)
        or settled_receipt.payload.get("hop_id") != identity.hop_id
        or settled_receipt.payload.get("hop_ordinal") != identity.hop_ordinal
        or settled_receipt.payload.get("response_bytes") != len(body)
        or not isinstance(settled_receipt.payload.get("storage_bytes"), int)
        or settled_receipt.payload.get("storage_bytes", 0) < len(body)
        or settled_receipt.payload.get("result_code") != settlement.result_code
        or settled_receipt.payload.get("response_hash") != hashlib.sha256(body).hexdigest()
    ):
        raise InvestorError("invalid_reference", "HTTP发送/结算事实收据缺失或不匹配")
    current = await load_settings(session, base=settings)
    if not (
        current.supplemental_rules_enabled
        and current.supplemental_updates_enabled
        and current.supplemental_disclosure_polling_enabled
        and current.disclosure_originals_enabled
    ):
        raise InvestorError("writes_disabled", "披露来源接收当前关闭")
    grant = await assert_attempt_authorized(session, identity, now=now)
    if not (
        protocol_profile == "live" and protocol_status == "live_validated"
        or protocol_profile == "fixture" and protocol_status == "fixture_validated"
        and session.info.get("allow_disclosure_fixture_proofs", False)
    ):
        raise InvestorError("writes_disabled", "来源协议尚未完成独立验证")
    if document.classification not in grant.doc_type_allowlist:
        raise InvestorError("disclosure_scope_forbidden", "公告类型不在授权范围")

    repo = SupplementalUpdatesRepository(session)
    listing_id = int(identity.listing_ref.id)
    poll_root = await repo.get_change(OWNER, poll_id)
    if (
        poll_root is None
        or poll_root.payload.get("record_type") != "poll_root_v1"
        or identity.poll_id != str(poll_id)
        or str(poll_root.id) != str(poll_id)
        or poll_root.payload.get("authorization_id") != str(grant.authorization_id)
        or poll_root.payload.get("authorization_revision") != grant.revision
        or poll_root.payload.get("authorization_hash") != grant.authorization_hash
        or poll_root.payload.get("contract_hash") != grant.contract_hash
        or poll_root.payload.get("attempt_scope_hash") != identity.scope_hash
        or poll_root.payload.get("listing_ref") != identity.listing_ref.model_dump(mode="json")
        or poll_root.payload.get("window_start") != identity.window_start.isoformat()
        or poll_root.payload.get("window_end") != identity.window_end.isoformat()
    ):
        raise InvestorError("disclosure_scope_forbidden", "轮询根收据与HTTP授权不匹配")

    from investment_agent.supplemental_rules.contracts import CoverageReceiptRef

    frozen_index = CoverageReceiptRef.model_validate(index_ref)
    index = await repo.get_change(OWNER, frozen_index.sequence)
    if (
        index is None
        or str(index.id) != str(frozen_index.id)
        or index.payload_hash != frozen_index.payload_hash
        or index.kind != "source_page"
        or index.listing_id != listing_id
        or index.context != "company_research"
        or index.payload.get("record_type") != "poll_index_v1"
        or index.payload.get("poll_id") != str(poll_id)
        or index.payload.get("authorization_id") != str(grant.authorization_id)
        or index.payload.get("authorization_revision") != grant.revision
        or index.payload.get("authorization_hash") != grant.authorization_hash
        or index.payload.get("contract_hash") != grant.contract_hash
        or index.payload.get("source_scope_hash") != poll_root.payload.get("source_scope_hash")
        or index.payload.get("attempt_scope_hash") != identity.scope_hash
        or index.payload.get("protocol_profile") != protocol_profile
        or index.payload.get("protocol_status") != protocol_status
        or index.payload.get("listing_ref") != identity.listing_ref.model_dump(mode="json")
        or index.payload.get("window_start") != identity.window_start.isoformat()
        or index.payload.get("window_end") != identity.window_end.isoformat()
        or document.native_announcement_id not in _index_document_ids(index.payload)
        or index.payload.get("ordered_document_keys_hash") is not None
        and index.payload.get("ordered_document_keys_hash")
        != canonical_hash(index.payload.get("ordered_document_keys"))
        or not _index_document_matches(index.payload, document)
    ):
        raise InvestorError("disclosure_scope_forbidden", "公告不属于已封存的原始目录页")
    raw_query_refs = index.payload.get("http_attempt_refs")
    if not isinstance(raw_query_refs, list) or not 3 <= len(raw_query_refs) <= 9 or len(raw_query_refs) % 3:
        raise InvestorError("invalid_reference", "目录页缺少完整HTTP阶段收据")
    query_refs = []
    for raw in raw_query_refs:
        if not isinstance(raw, dict):
            raise InvestorError("invalid_reference", "目录页HTTP阶段收据格式错误")
        ref = CoverageReceiptRef.model_validate(raw)
        receipt = await repo.get_change(OWNER, ref.sequence)
        if (
            receipt is None or str(receipt.id) != str(ref.id)
            or receipt.payload_hash != ref.payload_hash
            or receipt.payload.get("stage") not in {"reserved", "sent", "result"}
        ):
            raise InvestorError("invalid_reference", "目录页HTTP阶段收据不匹配")
        query_refs.append(receipt)
    if (
        any([r.payload.get("stage") for r in query_refs[i:i+3]] != ["reserved", "sent", "result"]
            for i in range(0, len(query_refs), 3))
        or any(
            query_refs[i].payload.get("permit", {}).get("permit_id")
            != query_refs[i + 1].payload.get("permit_id")
            or query_refs[i + 1].payload.get("permit_id") != query_refs[i + 2].payload.get("permit_id")
            for i in range(0, len(query_refs), 3)
        )
        or query_refs[0].payload.get("identity_hash") is None
        or not isinstance(query_refs[0].payload.get("permit"), dict)
        or query_refs[0].payload["permit"].get("identity", {}).get("request_kind") != "query"
        or query_refs[0].payload["permit"].get("identity", {}).get("poll_id") != str(poll_id)
        or query_refs[0].payload["permit"].get("identity", {}).get("scope_hash") != identity.scope_hash
        or any(query_refs[i].payload.get("permit", {}).get("identity", {}).get("attempt_id")
               != query_refs[i + 1].payload.get("attempt_id")
               or query_refs[i + 1].payload.get("attempt_id") != query_refs[i + 2].payload.get("attempt_id")
               for i in range(0, len(query_refs), 3))
        or query_refs[-1].payload.get("response_hash") != index.payload.get("response_hash")
        or query_refs[-1].payload.get("response_bytes") != index.payload.get("response_bytes")
    ):
        raise InvestorError("invalid_reference", "目录页原始响应与HTTP结果摘要不匹配")
    for offset in range(0, len(query_refs), 3):
        reserved, sent, result = query_refs[offset:offset + 3]
        frozen = reserved.payload.get("permit", {}).get("identity", {})
        if (
            frozen.get("request_kind") != "query"
            or frozen.get("poll_id") != str(poll_id)
            or frozen.get("scope_hash") != identity.scope_hash
            or frozen.get("authorization_id") != str(grant.authorization_id)
            or frozen.get("authorization_revision") != grant.revision
            or frozen.get("authorization_hash") != grant.authorization_hash
            or frozen.get("listing_ref") != identity.listing_ref.model_dump(mode="json")
            or frozen.get("window_start") != identity.window_start.isoformat().replace("+00:00", "Z")
            or frozen.get("window_end") != identity.window_end.isoformat().replace("+00:00", "Z")
            or sent.payload.get("hop_id") != frozen.get("hop_id")
            or result.payload.get("hop_id") != frozen.get("hop_id")
        ):
            raise InvestorError("invalid_reference", "目录页HTTP收据越出冻结授权范围")

    listing = await session.get(Listing, listing_id)
    ticker = f"{listing.ticker}.SZ" if listing is not None else ""
    if (
        listing is None or listing.market_code != "SZSE"
        or not re.fullmatch(r"\d{6}", listing.ticker)
        or document.ticker not in {listing.ticker, ticker}
        or document.published_at < identity.window_start
        or document.published_at > identity.window_end
    ):
        raise InvestorError("disclosure_scope_forbidden", "公告主体或时间超出轮询范围")

    event_key = f"cninfo:{document.issuer_id}:{document.native_announcement_id}"
    source_scope_key = f"listing:{listing_id}:{DISCLOSURE_CONTEXT}"
    expected_source_scope_hash = canonical_hash({
        "source_id": DISCLOSURE_SOURCE_FEED,
        "contract_version": DISCLOSURE_SOURCE_CONTRACT_VERSION,
        "contract_hash": grant.contract_hash,
        "scope_key": source_scope_key,
        "listing_ref": identity.listing_ref,
    })
    scope_state = await repo.get_source(
        OWNER, DISCLOSURE_SOURCE_FEED, source_scope_key, for_update=True,
    )
    if (
        scope_state is None
        or scope_state.owner_id != OWNER
        or scope_state.feed_id != DISCLOSURE_SOURCE_FEED
        or scope_state.scope_key != source_scope_key
        or scope_state.listing_id != listing_id
        or scope_state.context != DISCLOSURE_CONTEXT
        or scope_state.contract_version != DISCLOSURE_SOURCE_CONTRACT_VERSION
        or scope_state.config_hash != grant.contract_hash
        or scope_state.payload.get("source_scope_hash") != expected_source_scope_hash
        or scope_state.payload.get("source_scope_hash") != poll_root.payload.get("source_scope_hash")
        or scope_state.payload.get("active_poll_id") != str(poll_id)
        or scope_state.payload.get("active_step_run_id") != job.run_id
    ):
        raise InvestorError("disclosure_scope_forbidden", "来源scope当前状态与轮询根不匹配")
    response_hash = hashlib.sha256(body).hexdigest()
    source_url = document.locator
    validate_url(source_url, "original")
    if document.issuer_id != poll_root.payload.get("issuer_id", document.issuer_id):
        raise InvestorError("disclosure_scope_forbidden", "公告发行人与轮询目录不匹配")
    identity_body = {
        "source": "cninfo.v1", "listing_id": listing_id,
        "ticker": ticker, "exchange": "SZSE", "issuer_id": document.issuer_id,
        "disclosure_id": document.native_announcement_id,
        "first_published_at": document.published_at.isoformat(),
        "source_url": source_url,
    }
    item_identity_hash = canonical_hash({
        key: value for key, value in identity_body.items() if key != "source_url"
    })
    item_id = uuid5(NAMESPACE_URL, "p4-cninfo-item:" + item_identity_hash)
    original_id = uuid5(NAMESPACE_URL, f"p4-cninfo-original:{item_identity_hash}:{response_hash}")
    evidence_id = uuid5(NAMESPACE_URL, f"p4-cninfo-evidence:{item_identity_hash}:{response_hash}")
    original_ref = ObjectRef(kind="disclosure_original", id=original_id, revision=1)
    evidence_ref = ObjectRef(kind="evidence", id=evidence_id, revision=1)

    mapping_key = "doc:" + canonical_hash({
        "source": "cninfo.v1", "issuer_id": document.issuer_id,
        "native_announcement_id": document.native_announcement_id,
    })
    mapping_hash = canonical_hash({"feed_id": DOCUMENT_FEED, "schema_version": 1})
    mapping = await repo.ensure_source(
        OWNER, DOCUMENT_FEED, mapping_key, mapping_hash,
        listing_id=None, context=None, contract_version="1",
        payload={"schema_version": 1, "native_announcement_id": document.native_announcement_id},
    )
    if mapping.payload.get("listing_id") not in (None, listing_id):
        raise InvestorError("source_revision_conflict", "原生公告ID已绑定另一上市主体；来源隔离待核")
    prior_hashes = mapping.payload.get("prior_content_hashes", [])
    if (
        not isinstance(prior_hashes, list) or len(prior_hashes) > 32
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in prior_hashes)
    ):
        raise InvestorError("invalid_reference", "公告来源内容历史映射损坏")
    current_content_hash = mapping.payload.get("content_hash")
    if current_content_hash is not None and (
        not isinstance(current_content_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", current_content_hash)
    ):
        raise InvestorError("invalid_reference", "公告来源当前内容摘要损坏")
    if current_content_hash != response_hash and response_hash in prior_hashes:
        raise InvestorError(
            "source_revision_conflict",
            "公告正文摘要回现；隔离来源版本而不复活历史原件",
        )
    prior_original_refs = mapping.payload.get("prior_original_refs", [])
    current_original_ref = mapping.payload.get("original_ref")
    if not isinstance(prior_original_refs, list) or len(prior_original_refs) > 32:
        raise InvestorError("invalid_reference", "公告来源原件历史映射损坏")
    prior_original_refs = list(prior_original_refs)
    if current_content_hash and current_content_hash != response_hash:
        if not isinstance(current_original_ref, dict):
            raise InvestorError("invalid_reference", "当前公告正文缺少原件引用")
        if current_original_ref not in prior_original_refs:
            prior_original_refs.append(current_original_ref)
        if len(prior_original_refs) > 32:
            raise InvestorError("capacity_blocked", "公告原件历史达到保留上限；需人工复核")
    candidate_semantics = {
        "event_key": event_key, "title": document.title,
        "event_type": "company_disclosure", "jurisdiction": "CN",
        "affected_products": [], "affected_subjects": [f"listing:{listing_id}"],
        "policy_stage": "unknown", "published_at": document.published_at.isoformat(),
        "effective_from": None, "effective_to": None,
        "source_refs": [original_ref.model_dump(mode="json"), evidence_ref.model_dump(mode="json")],
    }
    semantic_hash = _event_semantics(candidate_semantics)
    prior_semantic_rows = list((await session.scalars(select(ResearchEventRevision).where(
        ResearchEventRevision.owner_id == OWNER,
        ResearchEventRevision.event_key == event_key,
    ).order_by(ResearchEventRevision.revision))).all())
    if prior_semantic_rows:
        latest_semantic_hash = _event_semantics(prior_semantic_rows[-1].payload)
        if latest_semantic_hash != semantic_hash and any(
            _event_semantics(row.payload) == semantic_hash for row in prior_semantic_rows[:-1]
        ):
            raise InvestorError(
                "source_revision_conflict",
                "公告语义回现；保留所有候选版本并将当前来源标记待核",
            )

    if not 3 <= len(http_attempt_refs) <= 9 or len(http_attempt_refs) % 3:
        raise InvestorError("invalid_reference", "原件缺少完整fetch阶段收据链")
    fetch_receipts = []
    from investment_agent.supplemental_rules.contracts import CoverageReceiptRef
    for raw in http_attempt_refs:
        ref = CoverageReceiptRef.model_validate(raw)
        receipt = await repo.get_change(OWNER, ref.sequence)
        if (receipt is None or str(receipt.id) != str(ref.id)
                or receipt.payload_hash != ref.payload_hash):
            raise InvestorError("invalid_reference", "原件HTTP阶段收据不存在或摘要不匹配")
        fetch_receipts.append(receipt)
    if any([r.payload.get("stage") for r in fetch_receipts[i:i+3]] != ["reserved", "sent", "result"]
           for i in range(0, len(fetch_receipts), 3)):
        raise InvestorError("invalid_reference", "原件HTTP阶段顺序错误")
    previous_hop = -1
    for offset in range(0, len(fetch_receipts), 3):
        reserved, sent, result = fetch_receipts[offset:offset + 3]
        frozen = reserved.payload.get("permit", {}).get("identity", {})
        if (
            frozen.get("request_kind") != "fetch"
            or frozen.get("poll_id") != str(poll_id)
            or frozen.get("scope_hash") != identity.scope_hash
            or frozen.get("authorization_id") != str(grant.authorization_id)
            or frozen.get("authorization_revision") != grant.revision
            or frozen.get("authorization_hash") != grant.authorization_hash
            or frozen.get("listing_ref") != identity.listing_ref.model_dump(mode="json")
            or frozen.get("window_start") != identity.window_start.isoformat().replace("+00:00", "Z")
            or frozen.get("window_end") != identity.window_end.isoformat().replace("+00:00", "Z")
            or frozen.get("logical_request_key") != identity.logical_request_key
            or frozen.get("attempt_id") != str(identity.attempt_id)
            or frozen.get("hop_ordinal", -1) <= previous_hop
            or sent.payload.get("hop_id") != frozen.get("hop_id")
            or result.payload.get("hop_id") != frozen.get("hop_id")
            or sent.payload.get("permit_id") != reserved.payload.get("permit", {}).get("permit_id")
            or result.payload.get("permit_id") != sent.payload.get("permit_id")
        ):
            raise InvestorError("invalid_reference", "原件HTTP收据越出冻结授权范围")
        previous_hop = int(frozen["hop_ordinal"])
    if len(http_response_refs) != len(fetch_receipts) // 3:
        raise InvestorError("invalid_reference", "原件HTTP响应收据数量与hop不匹配")
    response_receipts = []
    for raw in http_response_refs:
        ref = CoverageReceiptRef.model_validate(raw)
        receipt = await repo.get_change(OWNER, ref.sequence)
        if (
            receipt is None or str(receipt.id) != str(ref.id)
            or receipt.payload_hash != ref.payload_hash
            or receipt.payload.get("record_type") != "poll_http_response_v1"
        ):
            raise InvestorError("invalid_reference", "原件HTTP响应收据不存在或摘要不匹配")
        response_receipts.append(receipt)
    for hop_index, response_receipt in enumerate(response_receipts):
        reserved, sent, result = fetch_receipts[hop_index * 3:hop_index * 3 + 3]
        response_payload = response_receipt.payload
        result_ref = {
            "sequence": result.sequence, "id": str(result.id),
            "payload_hash": result.payload_hash,
        }
        frozen_identity = reserved.payload["permit"]["identity"]
        status = response_payload.get("status")
        hop_response_hash = response_payload.get("response_hash")
        response_bytes = response_payload.get("response_bytes")
        raw_blob_hash = response_payload.get("raw_blob_hash")
        raw_blob = (
            await session.get(DisclosureBlob, raw_blob_hash)
            if isinstance(raw_blob_hash, str)
            else None
        )
        if (
            response_payload.get("poll_id") != str(poll_id)
            or response_payload.get("attempt_id") != frozen_identity.get("attempt_id")
            or response_payload.get("hop_ordinal") != frozen_identity.get("hop_ordinal")
            or frozen_identity.get("hop_ordinal") != hop_index
            or frozen_identity.get("attempt_id") != str(identity.attempt_id)
            or response_payload.get("request_kind") != "fetch"
            or response_payload.get("logical_request_key") != frozen_identity.get("logical_request_key")
            or response_payload.get("response_url") != frozen_identity.get("url")
            or result.payload.get("result_code") != f"http_{status}"
            or hop_response_hash != result.payload.get("response_hash")
            or type(response_bytes) is not int
            or response_bytes != result.payload.get("response_bytes")
            or response_payload.get("settlement_ref") != result_ref
            or raw_blob_hash != hop_response_hash
            or not isinstance(raw_blob_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_blob_hash)
            or raw_blob is None
            or hashlib.sha256(raw_blob.body).hexdigest() != raw_blob_hash
            or len(raw_blob.body) != response_bytes
        ):
            raise InvestorError("invalid_reference", "原件HTTP响应收据与结算/请求不匹配")
        if hop_index < len(response_receipts) - 1:
            if status not in {301, 302, 303, 307, 308}:
                raise InvestorError("invalid_reference", "原件HTTP重定向链中间hop状态错误")
            location = response_payload.get("location")
            hop_response_url = response_payload.get("response_url")
            if not isinstance(location, str) or not location or not isinstance(hop_response_url, str):
                raise InvestorError("invalid_reference", "原件HTTP重定向目标缺失")
            target = urljoin(hop_response_url, location)
            try:
                validate_url(target, "original")
            except DisclosureError as exc:
                raise InvestorError("invalid_reference", "原件HTTP重定向目标非法") from exc
            next_identity = fetch_receipts[(hop_index + 1) * 3].payload.get(
                "permit", {}
            ).get("identity", {})
            if next_identity.get("url") != target:
                raise InvestorError("invalid_reference", "原件HTTP重定向未绑定下一hop URL")
        elif (
            status != 200
            or response_hash != hashlib.sha256(body).hexdigest()
            or response_bytes != len(body)
        ):
            raise InvestorError("invalid_reference", "最终HTTP hop未绑定PDF原件字节")
    if (
        str(fetch_receipts[-1].id) != str(settled_receipt.id)
        or fetch_receipts[-1].payload.get("response_hash") != response_hash
        or fetch_receipts[-1].payload.get("response_bytes") != len(body)
        or fetch_receipts[-1].payload.get("permit_id") != str(permit.permit_id)
    ):
        raise InvestorError("invalid_reference", "最终fetch HTTP收据未绑定原件字节")
    # A no-op write obtains SQLite's writer lock before deduplication.  Thus
    # original/evidence/event/leaf uniqueness is decided inside one serialized
    # short transaction after the HTTP body has already been fetched.
    await repo.reserve(OWNER)
    item = await session.scalar(select(DisclosureItem).where(
        DisclosureItem.owner_id == OWNER,
        DisclosureItem.identity_hash == item_identity_hash,
    ))
    metadata = {
        **identity_body,
        "id": str(item_id),
        "title": document.title,
        "discovered_at": now.isoformat(),
        "identity_directory_hash": index.payload.get("response_hash"),
        "correction_lead": document.classification != "annual_report_full",
        "verified_correction_refs": [],
        "verification": "candidate",
        "historical_reconstruction": False,
        "source_type": document.source_type,
        "classification": document.classification,
    }
    if item is None:
        session.add(DisclosureItem(
            id=str(item_id), owner_id=OWNER, listing_id=listing_id,
            identity_hash=item_identity_hash, payload=metadata,
        ))
        await session.flush()
    elif item.id != str(item_id) or item.listing_id != listing_id:
        raise InvestorError("invalid_reference", "公告身份摘要与既有目录项不匹配")

    original = await session.get(DisclosureOriginal, str(original_id))
    original_payload = {
        "id": str(original_id), "item_id": str(item_id), "listing_id": listing_id,
        "source": "cninfo.v1", "disclosure_id": document.native_announcement_id,
        "issuer_id": document.issuer_id, "ticker": ticker, "exchange": "SZSE",
        "source_url": source_url, "resolved_url": response_url,
        "first_published_at": document.published_at.isoformat(),
        "acquired_at": now.isoformat(), "available_from": now.isoformat(),
        "content_hash": response_hash, "mime": "application/pdf", "size": len(body),
        "verification": "candidate", "prior_original_refs": prior_original_refs,
        "verified_correction_refs": [], "historical_reconstruction": False,
        "evidence_ref": evidence_ref.model_dump(mode="json"),
        "classification": document.classification,
    }
    if original is None:
        await session.execute(sqlite_insert(DisclosureBlob).values(
            content_hash=response_hash, body=body,
        ).on_conflict_do_nothing())
        session.add(DisclosureOriginal(
            id=str(original_id), owner_id=OWNER, item_id=str(item_id),
            content_hash=response_hash, payload=original_payload,
        ))
        evidence = EvidenceRecord(
            id=evidence_id, revision=1, created_at=now, actor="disclosure_adapter",
            origin_ref=None, source_ref=original_ref, source_kind="document",
            locator=f"/investor/disclosures/originals/{original_id}/download",
            content_hash=response_hash,
            subject_ref=identity.listing_ref,
            period=None, published_at=document.published_at,
            acquired_at=now, available_from=now, verification="candidate",
            conflict="none", text="Unparsed official original candidate: " + document.title,
        )
        session.add(SavedEvidence(
            id=str(evidence_id), revision=1, owner_id=OWNER,
            payload=evidence.model_dump(mode="json"),
        ))
        await session.flush()
    else:
        if (
            original.owner_id != OWNER or original.item_id != str(item_id)
            or original.content_hash != response_hash
            or original.payload.get("evidence_ref") != evidence_ref.model_dump(mode="json")
        ):
            raise InvestorError("idempotency_conflict", "原件身份UUID已绑定其他内容")

    event_id = uuid5(NAMESPACE_URL, "p4-cninfo-event:" + event_key)
    rules_repo = SupplementalRulesRepository(session)
    prior_row = (await session.execute(select(ResearchEventRevision).where(
        ResearchEventRevision.owner_id == OWNER,
        ResearchEventRevision.event_key == event_key,
    ).order_by(ResearchEventRevision.revision.desc()).limit(1))).scalar_one_or_none()
    previous_ref = None
    event_revision = 1
    if prior_row is not None:
        if prior_row.id != str(event_id):
            raise InvestorError("idempotency_conflict", "披露事件稳定身份冲突")
        previous_ref = ObjectRef(kind="research_event", id=event_id, revision=prior_row.revision)
        event_revision = prior_row.revision + 1
        current_semantics = _event_semantics(prior_row.payload)
        if current_semantics == semantic_hash:
            event_ref = ObjectRef(kind="research_event", id=event_id, revision=prior_row.revision)
            event_created = False
        else:
            event_created = True
    else:
        event_created = True
    if event_created:
        candidate = ResearchEvent(
            id=event_id, revision=event_revision, owner_id=OWNER,
            event_key=event_key, title=document.title, event_type="company_disclosure",
            jurisdiction="CN", affected_products=(),
            affected_subjects=(f"listing:{listing_id}",), policy_stage="unknown",
            published_at=document.published_at, effective_from=None, effective_to=None,
            acquired_at=now, available_from=now,
            source_refs=(original_ref, evidence_ref), verification="candidate",
            conflict=False, previous_ref=previous_ref,
        )
        event_payload = candidate.model_dump(mode="json")
        event_hash = canonical_hash({k: v for k, v in event_payload.items() if k != "content_hash"})
        candidate = candidate.model_copy(update={"content_hash": event_hash})
        checked_payload = candidate.model_dump(mode="json")
        session.add(ResearchEventRevision(
            id=str(event_id), revision=event_revision, owner_id=OWNER,
            previous_ref=previous_ref.model_dump(mode="json") if previous_ref else None,
            event_key=event_key, event_aliases=[], published_at=document.published_at,
            effective_from=None, effective_to=None, acquired_at=now, available_from=now,
            created_at=now, content_hash=event_hash, payload=checked_payload,
        ))
        await session.flush()
        await rules_repo.advance_registry(OWNER, events_delta=1, updated_at=now)
        event_ref = ObjectRef(kind="research_event", id=event_id, revision=event_revision)
        await repo.append_change(
            OWNER,
            change_key=f"p4:cninfo:event:{event_id}:revision:{event_revision}",
            kind="research_event",
            payload={
                "schema_version": 1,
                "source_ref": event_ref.model_dump(mode="json"),
                "change_type": "candidate_disclosure",
                "event_key": event_key,
                "previous_ref": previous_ref.model_dump(mode="json") if previous_ref else None,
                "content_hash": event_hash,
                "events_generation": (await rules_repo.get_registry_head(OWNER)).events_generation,
                "poll_id": str(poll_id),
                "original_ref": original_ref.model_dump(mode="json"),
            },
            acquired_at=now, available_from=now, committed_at=now,
            listing_id=listing_id, context="company_research",
        )

    leaf_key = (
        f"p4:cninfo:document:{poll_id}:{index.sequence}:"
        f"{identity.attempt_id}:{identity.hop_ordinal}"
    )
    leaf_payload = {
        "record_type": "poll_document_v1", "poll_id": str(poll_id),
        "poll_ref": str(poll_root.id),
        "authorization_id": str(grant.authorization_id),
        "authorization_revision": grant.revision,
        "authorization_hash": grant.authorization_hash,
        "contract_hash": grant.contract_hash,
        "source_scope_hash": poll_root.payload.get("source_scope_hash"),
        "attempt_scope_hash": authorization_scope_hash(
            grant.authorization_id, grant.revision, grant.authorization_hash,
            grant.contract_hash, identity.listing_ref,
            identity.window_start, identity.window_end,
        ),
        "protocol_profile": protocol_profile, "protocol_status": protocol_status,
        "listing_ref": identity.listing_ref.model_dump(mode="json"),
        "window_start": identity.window_start.isoformat(),
        "window_end": identity.window_end.isoformat(),
        "index_ref": frozen_index.model_dump(mode="json"),
        "http_attempt_refs": http_attempt_refs,
        "http_response_refs": http_response_refs,
        "classification": document.classification,
        "document_type": document.classification,
        "coverage_disposition": "eligible",
        "native_announcement_id": document.native_announcement_id,
        "identity_hash": document.identity_hash,
        "content_hash": response_hash, "response_bytes": len(body),
        "item_ref": {"kind": "disclosure_item", "id": str(item_id), "revision": 1},
        "original_ref": original_ref.model_dump(mode="json"),
        "evidence_ref": evidence_ref.model_dump(mode="json"),
        "event_ref": event_ref.model_dump(mode="json"),
        "event_created": event_created,
        "semantic_change": bool(event_created or original is None),
    }
    leaf = await repo.append_change(
        OWNER, change_key=leaf_key, kind="source_attempt", payload=leaf_payload,
        listing_id=listing_id, context="company_research",
        acquired_at=now, available_from=now, committed_at=now,
    )
    # Keep a bounded point projection per native announcement in A's source
    # state table. The append-only leaf above remains the authority/history.
    await repo.save_source(
        mapping.model_copy(update={
            "revision": mapping.revision + 1,
            "coverage_state": "unknown",
            "success_sequence": leaf.sequence,
            "success_receipt_hash": leaf.payload_hash,
            "source_refs": [
                original_ref.model_dump(mode="json"),
                evidence_ref.model_dump(mode="json"),
                event_ref.model_dump(mode="json"),
            ],
            "last_attempt_at": now,
            "last_success_at": None,
            "health_limit_seconds": 0,
            "payload": {
                "schema_version": 1,
                "listing_id": listing_id,
                "native_announcement_id": document.native_announcement_id,
                "identity_hash": document.identity_hash,
                "content_hash": response_hash,
                "prior_content_hashes": list(dict.fromkeys(
                    [*prior_hashes, *([current_content_hash] if current_content_hash
                                       and current_content_hash != response_hash else [])]
                )),
                "prior_original_refs": prior_original_refs,
                "classification": document.classification,
                "item_ref": leaf_payload["item_ref"],
                "original_ref": original_ref.model_dump(mode="json"),
                "evidence_ref": evidence_ref.model_dump(mode="json"),
                "event_ref": event_ref.model_dump(mode="json"),
                "leaf_ref": {"sequence": leaf.sequence, "id": str(leaf.id),
                              "payload_hash": leaf.payload_hash},
            },
        }), expected_revision=mapping.revision,
    )
    prior_semantic_generation = scope_state.payload.get("semantic_generation", 0)
    if type(prior_semantic_generation) is not int or prior_semantic_generation < 0:
        raise InvestorError("invalid_reference", "来源scope语义代次损坏")
    if event_created or original is None:
        await repo.save_source(
            scope_state.model_copy(update={
                "revision": scope_state.revision + 1,
                "payload": {
                    **scope_state.payload,
                    "semantic_generation": prior_semantic_generation + 1,
                },
            }), expected_revision=scope_state.revision,
        )
    return {
        "leaf_ref": {"sequence": leaf.sequence, "id": str(leaf.id), "payload_hash": leaf.payload_hash},
        "item_ref": leaf_payload["item_ref"],
        "original_ref": original_ref.model_dump(mode="json"),
        "evidence_ref": evidence_ref.model_dump(mode="json"),
        "event_ref": event_ref.model_dump(mode="json"),
        "event_created": event_created,
        "content_hash": response_hash,
    }
