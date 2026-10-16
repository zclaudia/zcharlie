"""Freeze exact, strictly-cutoff source inputs for P2 evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Market
from investment_agent.supplemental_rules.contracts import FrozenSubject
from investment_agent.supplemental_rules.event_sources import (
    load_candidate_event_facts,
    load_research_event,
)
from investment_agent.supplemental_rules.sources import (
    MAX_SOURCE_ROWS,
    build_strict_snapshot_view,
    content_hash,
    factor_snapshot,
    read_classifications,
    read_listing,
    read_snapshot_rows,
)


async def _read_evidence(
    session: AsyncSession,
    owner_id: OwnerId,
    listing_ref: ObjectRef,
    as_of: datetime,
    *,
    evidence_refs: Sequence[ObjectRef],
    visible_at: datetime,
    limit: int,
    strict_source_subjects: bool,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Read back immutable evidence rows through EvidenceService visibility rules."""
    service = EvidenceService(session)
    invalid = [ref for ref in evidence_refs if ref.kind != "evidence"]
    if invalid:
        raise InvestorError("invalid_reference", "仅 evidence 引用可作为保存证据来源")
    if any(ref.revision is None or ref.field_path is not None for ref in evidence_refs):
        raise InvestorError("invalid_reference", "保存证据来源必须是精确版本引用")
    refs: dict[tuple[str, int], ObjectRef] = {
        (str(ref.id), ref.revision): ref for ref in evidence_refs
    }
    rows = await session.execute(
        select(SavedEvidence)
        .where(SavedEvidence.owner_id == str(owner_id))
        .order_by(SavedEvidence.id, SavedEvidence.revision)
        .limit(limit + 1)
    )
    saved_rows = list(rows.scalars().all())
    diagnostics: list[str] = []
    if len(saved_rows) > limit:
        diagnostics.append("saved_evidence_scan_truncated:count_at_least_limit_plus_one")
        saved_rows = saved_rows[:limit]
    for row in saved_rows:
        try:
            record = EvidenceRecord.model_validate(row.payload)
        except Exception as exc:
            # Corrupt persisted data is an infrastructure/data-integrity error,
            # not an ordinary evidence gap that could be called unknown.
            raise ValueError(f"saved evidence {row.id}@{row.revision} is invalid") from exc
        if record.subject_ref == listing_ref:
            refs.setdefault((row.id, row.revision), ObjectRef(
                kind="evidence", id=UUID(row.id), revision=row.revision
            ))
    frozen: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    for key, ref in sorted(refs.items()):
        if not strict_source_subjects:
            selected_row = await session.get(SavedEvidence, key)
            if selected_row is not None and selected_row.owner_id == owner_id:
                record = EvidenceRecord.model_validate(selected_row.payload)
                if str(record.id) != selected_row.id or record.revision != selected_row.revision:
                    raise ValueError("saved evidence identity disagrees with its storage row")
                if record.subject_ref != listing_ref:
                    diagnostics.append(
                        f"requested_evidence_subject_mismatch:{ref.id}@{ref.revision}"
                    )
                    continue
        result = await service.get_evidence(
            owner_id, ref, as_of, subject_ref=listing_ref, visible_at=visible_at
        )
        if result.data is None:
            if key in {(str(r.id), r.revision) for r in evidence_refs}:
                diagnostics.append(f"requested_evidence_not_visible:{ref.id}@{ref.revision}")
            continue
        record = result.data
        payload = record.model_dump(mode="json")
        frozen.append(payload)
        manifest.append({
            "kind": "saved_evidence",
            "ref": ref.model_dump(mode="json"),
            "content_hash": record.content_hash,
            "created_at": record.created_at.isoformat(),
            "available_from": record.available_from.isoformat(),
            "date_precision": record.date_precision.value if record.date_precision else "unrecorded",
            "verification": record.verification,
            "conflict": record.conflict,
            "read_status": result.status,
            "read_warnings": list(result.warnings),
        })
    return tuple(frozen), tuple(manifest), tuple(diagnostics)


async def freeze_subject(
    session: AsyncSession,
    listing_ref: ObjectRef,
    as_of: datetime,
    *,
    owner_id: OwnerId,
    factor_names: Sequence[str] | None = None,
    evidence_refs: Sequence[ObjectRef] = (),
    visible_at: datetime | None = None,
    max_source_rows: int = MAX_SOURCE_ROWS,
    strict_source_subjects: bool = True,
) -> FrozenSubject:
    """Read and freeze a listing's source rows at one aware datetime.

    The function is read-only: it does not ensure identities, fetch sources,
    mutate snapshots, or make LLM calls. Unsupported historical precision and
    source coverage remain explicit diagnostics in the returned DTO.
    """
    if (listing_ref.kind != "listing" or listing_ref.revision != 1
            or listing_ref.field_path is not None):
        raise InvestorError("invalid_reference", "冻结输入须使用精确证券标的引用")
    cutoff = utc_timestamp(as_of)
    visible = utc_timestamp(visible_at or cutoff)
    if visible > cutoff:
        raise ValueError("visible_at cannot be later than the source cutoff")
    listing_id = int(listing_ref.id)
    source_refs = tuple(evidence_refs)
    listing, company = await read_listing(session, listing_id)
    market = await session.get(Market, listing.market_code)
    if (listing.market_code not in {"SSE", "SZSE", "BSE"} or market is None
            or market.currency != "CNY" or len(listing.ticker) != 6
            or not listing.ticker.isdigit()):
        raise InvestorError("unsupported_subject", "当前补充事项仅支持上交所、深交所及北交所人民币A股")
    classifications, class_manifest, class_diags = await read_classifications(
        session, listing_id, cutoff, limit=max_source_rows
    )
    markets, financial, dividends, overdue, row_manifest, row_diags = await read_snapshot_rows(
        session, listing_id, cutoff, limit=max_source_rows
    )
    view = build_strict_snapshot_view(
        listing.ticker, cutoff, classifications, markets, financial, dividends, overdue
    )
    factor_values, factor_metadata = factor_snapshot(
        view, factor_names, source_diagnostics=(*class_diags, *row_diags)
    )
    evidence, evidence_manifest, evidence_diags = await _read_evidence(
        session, owner_id, listing_ref, cutoff,
        evidence_refs=tuple(ref for ref in source_refs if ref.kind == "evidence"),
        visible_at=visible, limit=max_source_rows,
        strict_source_subjects=strict_source_subjects,
    )
    candidate_events, event_diags = await load_candidate_event_facts(
        session, listing_id, cutoff, limit=max_source_rows
    )

    # User-selected source references are validated independently of the
    # generic evidence reader. Exact snapshots must be among the rows frozen
    # above; exact event revisions must be owner scoped and visible at cutoff.
    row_refs = {
        (kind, str(item.get("row_id")), 1)
        for item in row_manifest
        for kind in (item.get("kind"),)
    }
    source_diags: list[str] = []
    for ref in source_refs:
        if ref.revision is None or ref.field_path is not None:
            raise InvestorError("invalid_reference", "来源引用必须是精确版本")
        if ref.kind in {"market_snapshot", "financial_snapshot"}:
            if (ref.kind, str(ref.id), ref.revision) not in row_refs:
                source_diags.append(f"requested_source_unavailable:{ref.kind}:{ref.id}@{ref.revision}")
        elif ref.kind == "listing":
            if ref != listing_ref:
                if strict_source_subjects:
                    raise InvestorError("invalid_reference", "来源证券标的与评估主体不匹配")
                source_diags.append(f"requested_source_subject_mismatch:listing:{ref.id}@{ref.revision}")
        elif ref.kind == "research_event":
            event = await load_research_event(session, owner_id, ref, cutoff, visible_at=visible)
            if event is None:
                source_diags.append(f"requested_source_unavailable:research_event:{ref.id}@{ref.revision}")
        elif ref.kind != "evidence":
            source_diags.append(f"unsupported_source_reference:{ref.kind}:{ref.id}@{ref.revision}")

    classification_rows = [item["content"] for item in class_manifest]
    identity_rows = [
        {
            "kind": "listing_identity",
            "row_id": listing.id,
            "content": {
                "id": listing.id, "company_id": listing.company_id,
                "market_code": listing.market_code, "ticker": listing.ticker,
                "is_primary": listing.is_primary,
            },
            "content_hash": content_hash({
                "id": listing.id, "company_id": listing.company_id,
                "market_code": listing.market_code, "ticker": listing.ticker,
                "is_primary": listing.is_primary,
            }),
            "created_at": listing.created_at.isoformat(),
            "updated_at": listing.updated_at.isoformat(),
            "time_precision": "timestamp",
        },
        {
            "kind": "company_identity",
            "row_id": company.id,
            "content": {"id": company.id, "canonical_name": company.canonical_name,
                        "sector_code": company.sector_code},
            "content_hash": content_hash({"id": company.id,
                "canonical_name": company.canonical_name, "sector_code": company.sector_code}),
            "created_at": company.created_at.isoformat(),
            "updated_at": company.updated_at.isoformat(),
            "time_precision": "timestamp",
        },
    ]
    identity_diags = tuple(
        f"{kind}_row_mutated_after_cutoff"
        for kind, row in (("listing_identity", listing), ("company_identity", company))
        if row.updated_at.replace(tzinfo=row.updated_at.tzinfo or cutoff.tzinfo) > cutoff
    )
    identity_available = not identity_diags
    selector_inputs = {
        "listing": {
            "listing_id": listing.id,
            "ticker": listing.ticker,
            "market_code": listing.market_code,
            "company_id": listing.company_id,
            "is_primary": listing.is_primary,
        },
        "company": {
            "company_id": company.id,
            "canonical_name": company.canonical_name,
            "sector_code": company.sector_code,
        },
        "identity_available": identity_available,
        "classifications": classification_rows,
        "factor_metadata": factor_metadata,
        "saved_candidate_events": list(candidate_events),
    }
    manifest = [*identity_rows, *class_manifest, *row_manifest, *evidence_manifest,
                *({"kind": "saved_candidate_event", "ref": item["ref"],
                   "content_hash": item["content_hash"], "captured_at": item["captured_at"],
                   "published_at": item["published_at"], "date_precision": item["date_precision"],
                   "verification": item["verification"], "conflict": item["conflict"],
                   "semantic_role": item["semantic_role"]} for item in candidate_events)]
    missing_diags = tuple(
        message for condition, message in (
            (not classifications, "classification_input_unavailable"),
            (not markets, "market_snapshot_input_unavailable"),
            (not financial, "financial_snapshot_input_unavailable"),
        ) if condition
    )
    diagnostics = (*identity_diags, *missing_diags, *class_diags, *row_diags,
                   *evidence_diags, *event_diags, *source_diags)
    completeness: Literal["ready", "partial", "unknown"] = "partial" if diagnostics else (
        "ready" if manifest else "partial"
    )
    return FrozenSubject(
        listing_ref=listing_ref,
        company_id=company.id,
        ticker=f"{listing.ticker}.{ {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}[listing.market_code] }",
        name=company.canonical_name,
        as_of=cutoff,
        input_status=completeness,
        selector_inputs=selector_inputs,
        factor_values=factor_values,
        evidence=evidence,
        source_manifest=tuple(manifest),
        diagnostics=tuple(diagnostics),
    )
