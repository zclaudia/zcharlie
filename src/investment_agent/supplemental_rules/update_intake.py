"""Controlled local intake: saved inputs only, never source or fact promotion."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import DisclosureError
from investment_agent.evidence.disclosures import DisclosureService
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import Context

LOCAL_CONTRACT: dict[str, Any] = {
    "source_id": "controlled_local_v1", "version": "1",
    "allowed_kinds": ["disclosure_original", "evidence", "research_dataset"],
    "coverage": "explicit_saved_refs", "health_limit_seconds": 86400,
}
LOCAL_CONTRACT_HASH = canonical_hash(LOCAL_CONTRACT)


class ImportPreviewRequest(Contract):
    listing_ref: ObjectRef
    context: Context = "company_research"
    source_refs: Annotated[tuple[ObjectRef, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def exact_inputs(self) -> ImportPreviewRequest:
        if self.listing_ref.kind != "listing" or self.listing_ref.revision != 1 or self.listing_ref.field_path:
            raise ValueError("an exact listing reference is required")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source references must be unique")
        if any(r.kind not in LOCAL_CONTRACT["allowed_kinds"] or r.field_path is not None for r in self.source_refs):
            raise ValueError("unsupported saved-source kind or field path")
        return self


class ImportRequest(ImportPreviewRequest):
    operation_id: UUID
    manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_source_revision: Annotated[int, Field(strict=True, ge=0)] = 0


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else utc_timestamp(value)


async def resolve_saved_sources(
    session: AsyncSession, listing_ref: ObjectRef, refs: tuple[ObjectRef, ...],
    *, settings: Settings, now: datetime, owner_id: OwnerId = "local_owner",
) -> list[dict[str, Any]]:
    """Resolve each exact supported type against the server's saved identity."""
    listing = await session.get(Listing, int(listing_ref.id))
    if listing is None:
        raise InvestorError("not_found", "证券标的不存在")
    sources: list[dict[str, Any]] = []
    for ref in sorted(refs, key=lambda r: (r.kind, str(r.id), r.revision)):
        status: str
        if ref.kind == "evidence":
            result = await EvidenceService(session).get_evidence(owner_id, ref, now, subject_ref=listing_ref)
            if result.data is None:
                raise InvestorError("invalid_reference", "导入证据不存在或在当前窗口不可见")
            evidence = result.data
            acquired, available = evidence.acquired_at, evidence.available_from
            digest = evidence.content_hash
            status = "conflict" if result.status == "conflict" else result.status
            if result.warnings and status == "ok":
                status = "candidate" if all(w.startswith("verification=") for w in result.warnings) else "conflict"
        elif ref.kind == "disclosure_original":
            if ref.revision != 1:
                raise InvestorError("invalid_reference", "原件版本不存在")
            try:
                original, _body = await DisclosureService(session, owner_id).read(UUID(str(ref.id)), now)
            except DisclosureError as exc:
                raise InvestorError("invalid_reference", "已归档原件不可读或完整性检查失败") from exc
            if original.get("listing_id") != listing_ref.id:
                raise InvestorError("invalid_reference", "原件不属于该证券")
            digest = original["content_hash"]
            acquired = datetime.fromisoformat(original["acquired_at"])
            available = datetime.fromisoformat(original["available_from"])
            status = "candidate"
        elif ref.kind == "research_dataset":
            row = await session.get(ResearchDataset, int(ref.id))
            if row is None or row.revision != ref.revision:
                raise InvestorError("invalid_reference", "资料精确版本不存在")
            if ((row.subject_kind == "listing" and row.subject_id != listing_ref.id)
                or (row.subject_kind == "company" and row.subject_id != listing.company_id)
                or row.subject_kind not in {"listing", "company"}):
                raise InvestorError("invalid_reference", "资料主体不匹配")
            acquired = aware(row.captured_at)
            available = max(acquired, aware(row.created_at))
            if available > now:
                raise InvestorError("invalid_reference", "资料尚不可见")
            encoded = json.dumps(row.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
            checksum = hashlib.sha256(encoded)
            if row.raw_ref:
                raw = await asyncio.to_thread(_read_raw, Path(settings.data_dir), row.raw_ref)
                checksum.update(b"\x00")
                checksum.update(raw)
            if checksum.hexdigest() != row.content_hash:
                raise InvestorError("invalid_reference", "资料内容摘要不匹配")
            digest = row.content_hash
            # A dataset without its raw archive is an honest coverage gap.
            status = "candidate" if row.raw_ref else "original_missing"
        else:
            raise InvestorError("invalid_reference", "不支持的已保存来源类型")
        sources.append({"ref": ref.model_dump(mode="json"), "content_hash": digest,
                        "status": status, "acquired_at": aware(acquired).isoformat(),
                        "available_from": aware(available).isoformat()})
    return sources


async def import_saved_sources(
    session: AsyncSession, request: ImportRequest, settings: Settings,
) -> dict[str, Any]:
    repo = SupplementalUpdatesRepository(session)
    await repo.reserve("local_owner")
    request_hash = canonical_hash({"command": "supplemental_updates.import.v1", "body": request})
    old = await session.scalar(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == "local_owner",
        SupplementalChangeReceiptRow.operation_id == str(request.operation_id)))
    if old is not None:
        receipt = await repo.get_change("local_owner", old.id)
        if old.request_hash != request_hash or receipt is None:
            raise InvestorError("idempotency_conflict", "导入操作标识已用于其他请求")
        return {"receipt": receipt.model_dump(mode="json"),
                "state": "unchanged" if receipt.payload.get("purpose") == "unchanged_saved_import" else "persisted",
                "coverage_state": receipt.payload.get("coverage_state", "unknown")}
    current = await load_settings(session, base=settings)
    if not (current.supplemental_rules_enabled and current.supplemental_updates_enabled):
        raise InvestorError("writes_disabled", "持续更新接入当前关闭")
    now = datetime.now(UTC)
    sources = await resolve_saved_sources(session, request.listing_ref, request.source_refs, settings=current, now=now)
    manifest_hash = canonical_hash({"listing_ref": request.listing_ref, "context": request.context, "sources": sources})
    if manifest_hash != request.manifest_hash:
        raise InvestorError("invalid_reference", "导入清单摘要与已归档来源不一致")
    scope_key = f"listing:{request.listing_ref.id}:{request.context}"
    state = await repo.get_source("local_owner", "controlled_local_v1", scope_key)
    if (state.revision if state else 0) != request.expected_source_revision:
        raise InvestorError("revision_conflict", "来源范围状态已变化")
    if (state is not None and state.config_hash == LOCAL_CONTRACT_HASH
        and state.contract_version == "1" and state.payload.get("manifest_hash") == manifest_hash
        and state.coverage_state == "complete" and state.last_success_at is not None
        and (now - aware(state.last_success_at)).total_seconds() < 86400):
        original = await repo.get_change("local_owner", state.success_sequence)
        if (original is None or original.payload_hash != state.success_receipt_hash
            or original.payload.get("contract_hash") != LOCAL_CONTRACT_HASH
            or original.payload.get("contract_version") != "1"
            or original.payload.get("scope_hash") != state.scope_hash):
            raise InvestorError("invalid_reference", "已保存的来源成功证明不完整")
        duplicate = await repo.append_change(
            "local_owner", change_key=f"import:{request.operation_id}", kind="source_attempt",
            payload={"purpose": "unchanged_saved_import", "manifest_hash": manifest_hash,
                     "original_receipt_id": str(original.id), "original_sequence": original.sequence,
                     "coverage": "explicit_saved_refs", "coverage_state": state.coverage_state},
            operation_id=request.operation_id, request_hash=request_hash,
            listing_id=int(request.listing_ref.id), context=request.context)
        return {"receipt": duplicate.model_dump(mode="json"), "state": "unchanged",
                "coverage_state": state.coverage_state}
    if state is None:
        state = await repo.ensure_source("local_owner", "controlled_local_v1", scope_key,
                                        LOCAL_CONTRACT_HASH, listing_id=int(request.listing_ref.id),
                                        context=request.context, payload={})
    complete = all(s["status"] in {"ok", "candidate"} for s in sources)
    scope_hash = canonical_hash({"source_id": "controlled_local_v1", "contract_version": "1",
                                 "contract_hash": LOCAL_CONTRACT_HASH, "scope_key": scope_key,
                                 "listing_ref": request.listing_ref})
    payload = {"source_refs": [r.model_dump(mode="json") for r in request.source_refs],
               "sources": sources, "listing_refs": [request.listing_ref.model_dump(mode="json")],
               "contexts": [request.context], "manifest_hash": manifest_hash,
               "source_id": "controlled_local_v1", "contract_version": "1",
               "contract_hash": LOCAL_CONTRACT_HASH, "scope_hash": scope_hash,
               "origin": "controlled_import", "coverage": "explicit_saved_refs",
               "coverage_state": "complete" if complete else "partial"}
    payload["source_refs_hash"] = canonical_hash(payload["source_refs"])
    receipt = await repo.append_change(
        "local_owner", change_key=f"import:{request.operation_id}", kind="evidence_revision",
        payload=payload, acquired_at=now, available_from=now, committed_at=now,
        operation_id=request.operation_id, request_hash=request_hash,
        listing_id=int(request.listing_ref.id), context=request.context)
    window_start = min(datetime.fromisoformat(s["available_from"]) for s in sources)
    changed = state.model_copy(update={
        "revision": state.revision + 1, "config_hash": LOCAL_CONTRACT_HASH,
        "contract_version": "1", "scope_hash": scope_hash, "window_start": window_start,
        "window_end": now, "complete_watermark": now if complete else None,
        "coverage_state": "complete" if complete else "partial", "success_sequence": receipt.sequence,
        "success_receipt_hash": receipt.payload_hash, "source_refs": payload["source_refs"],
        "last_attempt_at": now, "last_success_at": now if complete else state.last_success_at,
        "health_limit_seconds": 86400, "failure_code": None if complete else "saved_source_incomplete",
        "payload": {"manifest_hash": manifest_hash, "contract": LOCAL_CONTRACT, "scope": payload["listing_refs"],
                    "verification": "not_promoted", "coverage": "explicit_saved_refs"},
    })
    await repo.save_source(changed, expected_revision=state.revision)
    return {"receipt": receipt.model_dump(mode="json"), "state": "persisted",
            "coverage_state": changed.coverage_state}


def _read_raw(data_dir: Path, raw_ref: str) -> bytes:
    root = data_dir.resolve()
    path = (root / raw_ref).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise InvestorError("invalid_reference", "资料原件缺失或路径非法")
    with path.open("rb") as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise InvestorError("source_capacity_exceeded", "本地原件超过16MiB上限")
    return raw
