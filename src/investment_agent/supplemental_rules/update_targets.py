"""Resolve immutable update receipts to a frozen, bounded target snapshot."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset, ResearchSourceBinding
from investment_agent.storage.models.supplemental_rules import ResearchEventRevision
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import Context

TARGET_CONTEXTS: tuple[Context, ...] = (
    "company_research", "candidate", "account_fit", "holding_review",
)
MAX_TARGET_LISTINGS = 100
MAX_FROZEN_TARGET_LISTINGS = 2_000
_SOURCE_KINDS = {"evidence_revision", "material_revision"}


async def freeze_target_snapshot(
    session: AsyncSession,
    receipt: Any,
    *,
    high_water_id: int | None = None,
    after_listing_id: int | None = None,
) -> dict[str, Any]:
    """Freeze one bounded ID segment for replay, independently of page size.

    Each segment retains up to 2,000 IDs and may be dispatched in 100-ID
    pages. If more targets exist, the snapshot carries a continuation cursor;
    callers freeze the next segment with the same high-water fence.
    """
    if high_water_id is None:
        high_water_id = await session.scalar(select(func.max(Listing.id))) or 0
    if high_water_id < 0:
        raise ValueError("high_water_id must be nonnegative")
    if after_listing_id is not None and (
        after_listing_id < 0 or after_listing_id > high_water_id
    ):
        raise ValueError("after_listing_id must be within the high-water fence")
    listing_ids, contexts, complete = await _resolve_targets(
        session, receipt, high_water_id=high_water_id,
        after_listing_id=after_listing_id,
    )
    retained = listing_ids[:MAX_FROZEN_TARGET_LISTINGS]
    truncated = len(listing_ids) > MAX_FROZEN_TARGET_LISTINGS
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "source_sequence": int(receipt.sequence),
        "source_id": str(receipt.id),
        "source_hash": str(receipt.payload_hash),
        "high_water_id": high_water_id,
        "after_listing_id": after_listing_id,
        "listing_ids": retained,
        "overflow_listing_id": (
            listing_ids[MAX_FROZEN_TARGET_LISTINGS]
            if truncated else None
        ),
        "next_after_listing_id": retained[-1] if truncated and retained else None,
        "contexts": list(contexts),
        "complete": complete and not truncated,
        "truncated": truncated,
        "no_target_reason": (
            "draft_rule_revision" if complete and receipt.kind == "rule_revision"
            else "non_active_rule_revision_or_no_local_targets"
            if complete and receipt.kind == "rule_lifecycle" and not listing_ids
            else None
        ),
    }
    snapshot["snapshot_hash"] = canonical_hash(snapshot)
    return snapshot


async def target_scopes(
    session: AsyncSession,
    receipt: Any,
    *,
    offset: int = 0,
    high_water_id: int | None = None,
    target_snapshot: dict[str, Any] | None = None,
) -> tuple[list[tuple[int, Context]], bool]:
    """Return a page from a persisted target snapshot, preserving its cap flag."""
    if offset < 0:
        raise ValueError("target offset must be nonnegative")
    snapshot = target_snapshot
    if snapshot is None:
        snapshot = await freeze_target_snapshot(
            session, receipt, high_water_id=high_water_id,
        )
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("source_sequence") != int(receipt.sequence)
        or snapshot.get("source_id") != str(receipt.id)
        or snapshot.get("source_hash") != str(receipt.payload_hash)
        or not isinstance(snapshot.get("high_water_id"), int)
        or not (snapshot.get("after_listing_id") is None
                or isinstance(snapshot.get("after_listing_id"), int))
        or not (snapshot.get("next_after_listing_id") is None
                or isinstance(snapshot.get("next_after_listing_id"), int))
        or not isinstance(snapshot.get("listing_ids"), list)
        or not isinstance(snapshot.get("contexts"), list)
        or not isinstance(snapshot.get("complete"), bool)
    ):
        return [], False
    hashed_snapshot = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    if snapshot.get("snapshot_hash") != canonical_hash(hashed_snapshot):
        return [], False
    listing_ids = snapshot["listing_ids"]
    contexts = snapshot["contexts"]
    no_target_reason = snapshot.get("no_target_reason")
    if no_target_reason is not None and not listing_ids:
        valid_reasons = {
            "draft_rule_revision": receipt.kind == "rule_revision",
            "non_active_rule_revision_or_no_local_targets": receipt.kind == "rule_lifecycle",
        }
        return ([], True) if valid_reasons.get(no_target_reason, False) else ([], False)
    if (
        any(not isinstance(value, int) or value < 1 for value in listing_ids)
        or listing_ids != sorted(set(listing_ids))
        or any(value not in TARGET_CONTEXTS for value in contexts)
        or len(contexts) != len(set(contexts))
        or not contexts
        or (snapshot.get("overflow_listing_id") is not None
            and (not isinstance(snapshot.get("overflow_listing_id"), int)
                 or not listing_ids
                 or snapshot["overflow_listing_id"] <= listing_ids[-1]
                 or snapshot.get("next_after_listing_id") != listing_ids[-1]))
        or (snapshot.get("next_after_listing_id") is not None
            and snapshot.get("overflow_listing_id") is None)
        or (snapshot.get("after_listing_id") is not None
            and listing_ids and listing_ids[0] <= snapshot["after_listing_id"])
        or len(listing_ids) > MAX_FROZEN_TARGET_LISTINGS
    ):
        return [], False
    page_ids = listing_ids[offset:offset + MAX_TARGET_LISTINGS]
    scopes = [(listing_id, cast(Context, context))
              for listing_id in page_ids for context in contexts]
    complete = snapshot["complete"] and offset + MAX_TARGET_LISTINGS >= len(listing_ids)
    return scopes, complete


async def _resolve_targets(
    session: AsyncSession,
    receipt: Any,
    *,
    high_water_id: int,
    after_listing_id: int | None,
) -> tuple[list[int], tuple[Context, ...], bool]:
    payload = receipt.payload
    if receipt.kind in _SOURCE_KINDS:
        imported = payload.get("origin") == "controlled_import"
        scope_listing_id: int | None = None
        if imported:
            raw_listing_refs = payload.get("listing_refs")
            raw_contexts = payload.get("contexts")
            if (
                receipt.listing_id is None or receipt.context not in TARGET_CONTEXTS
                or not isinstance(payload.get("sources"), list)
                or not payload.get("sources")
                or not isinstance(raw_listing_refs, list) or len(raw_listing_refs) != 1
                or not isinstance(raw_listing_refs[0], dict)
                or raw_listing_refs[0].get("kind") != "listing"
                or raw_listing_refs[0].get("id") != receipt.listing_id
                or raw_listing_refs[0].get("revision") != 1
                or raw_contexts != [receipt.context]
            ):
                return [], (), False
            scope_listing_id = receipt.listing_id
            try:
                manifest = canonical_hash({
                    "listing_ref": ObjectRef.model_validate(raw_listing_refs[0]),
                    "context": receipt.context,
                    "sources": payload.get("sources"),
                })
            except Exception:
                return [], (), False
            if manifest != payload.get("manifest_hash"):
                return [], (), False
        resolved = await _source_targets(
            session, receipt, high_water_id=high_water_id,
            scope_listing_id=scope_listing_id,
            after_listing_id=after_listing_id,
        )
        if resolved is None:
            return [], (), False
        listing_ids, complete = resolved
        raw_contexts = payload.get("contexts")
        if raw_contexts is not None:
            if (
                not isinstance(raw_contexts, list)
                or not raw_contexts
                or any(value not in TARGET_CONTEXTS for value in raw_contexts)
            ):
                return [], (), False
            contexts = tuple(dict.fromkeys(cast(Context, value) for value in raw_contexts))
        else:
            contexts = TARGET_CONTEXTS
        if imported:
            if receipt.listing_id not in listing_ids:
                return [], (), False
            listing_ids = [receipt.listing_id]
            contexts = (cast(Context, receipt.context),)
        return listing_ids[:MAX_FROZEN_TARGET_LISTINGS + 1], contexts, complete

    if receipt.kind in {"rule_revision", "rule_lifecycle"}:
        source = _source_ref(payload, "supplemental_rule")
        if source is None or not isinstance(source.id, UUID):
            return [], (), False
        if receipt.kind == "rule_revision":
            # Rule creation/revision writes are drafts. They cannot trigger
            # evaluation until a separate lifecycle approval changes the
            # authoritative head.
            return [], (), True
        rule = await SupplementalRulesRepository(session).get_rule_revision(
            receipt.owner_id, source,
        )
        if rule is None:
            return [], (), False
        repository = SupplementalRulesRepository(session)
        head = await repository.get_rule_head(receipt.owner_id, source.id)
        if (
            head is None or head.active_revision != source.revision
            or head.active_state not in {"active", "suspended", "retired"}
        ):
            return [], (), True
        query = select(Listing.id).where(Listing.id <= high_water_id)
        if after_listing_id is not None:
            query = query.where(Listing.id > after_listing_id)
        selector = rule.selector
        if selector.kind == "listing":
            query = query.where(Listing.id == int(selector.listing_ref.id))
        elif selector.kind == "company":
            query = query.where(Listing.company_id == selector.company_id)
        ids = list((await session.scalars(
            query.order_by(Listing.id).limit(MAX_FROZEN_TARGET_LISTINGS + 1)
        )).all())
        # Category selectors use the listing universe as a bounded candidate
        # set; the admitted P2 evaluator performs exact category matching.
        complete = len(ids) <= MAX_FROZEN_TARGET_LISTINGS
        return [int(value) for value in ids], tuple(sorted(rule.contexts)), complete

    if receipt.kind == "research_event":
        source = _source_ref(payload, "research_event")
        if source is None:
            return [], (), False
        event = await SupplementalRulesRepository(session).get_event_revision(
            receipt.owner_id, source,
        )
        if event is None:
            return [], (), False
        event_listing_ids: set[int] = set()
        for raw_subject in event.affected_subjects:
            subject = str(raw_subject)
            if subject.startswith("listing:") and subject.removeprefix("listing:").isdigit():
                value = int(subject.removeprefix("listing:"))
                if value <= high_water_id and (after_listing_id is None or value > after_listing_id):
                    exists = await session.scalar(select(Listing.id).where(Listing.id == value))
                    if exists is not None:
                        event_listing_ids.add(value)
            elif subject.startswith("company:") and subject.removeprefix("company:").isdigit():
                company_result = await session.scalars(
                    select(Listing.id).where(
                        Listing.company_id == int(subject.removeprefix("company:")),
                        Listing.id <= high_water_id,
                        *(() if after_listing_id is None else (Listing.id > after_listing_id,)),
                    ).order_by(Listing.id).limit(MAX_FROZEN_TARGET_LISTINGS + 1)
                )
                event_listing_ids.update(int(value) for value in company_result.all())
        ordered = sorted(event_listing_ids)
        return (ordered[:MAX_FROZEN_TARGET_LISTINGS + 1], TARGET_CONTEXTS,
                len(ordered) <= MAX_FROZEN_TARGET_LISTINGS)

    if receipt.kind == "due_boundary":
        if (receipt.listing_id is None) != (receipt.context is None):
            return [], (), False
        due_resolution = await _resolve_due_source(
            session, receipt, high_water_id=high_water_id,
            after_listing_id=after_listing_id,
        )
        if due_resolution is None:
            return [], (), False
        listing_ids, contexts = due_resolution
        if receipt.listing_id is not None:
            if receipt.context not in TARGET_CONTEXTS or receipt.listing_id not in listing_ids:
                return [], (), False
            listing_ids, contexts = [receipt.listing_id], (cast(Context, receipt.context),)
        return listing_ids, contexts, len(listing_ids) <= MAX_FROZEN_TARGET_LISTINGS
    if receipt.kind == "bootstrap":
        listing_id = receipt.listing_id
        raw_contexts = payload.get("contexts")
        source_ref = payload.get("source_ref")
        if (
            payload.get("source_kind") != "industry_classification"
            or not isinstance(listing_id, int) or listing_id > high_water_id
            or (after_listing_id is not None and listing_id <= after_listing_id)
            or not isinstance(source_ref, dict)
            or source_ref != {"kind": "listing", "id": listing_id, "revision": 1}
            or not isinstance(raw_contexts, list) or not raw_contexts
            or any(context not in TARGET_CONTEXTS for context in raw_contexts)
        ):
            return [], (), False
        exists = await session.scalar(select(Listing.id).where(Listing.id == listing_id))
        if exists is None:
            return [], (), False
        contexts = tuple(dict.fromkeys(cast(Context, context) for context in raw_contexts))
        return [listing_id], contexts, True
    return [], (), False


async def _resolve_due_source(
    session: AsyncSession, receipt: Any, *, high_water_id: int,
    after_listing_id: int | None,
) -> tuple[list[int], tuple[Context, ...]] | None:
    """Resolve a source-boundary receipt to all affected local listings.

    Root boundary receipts have no member fields. Their frozen target snapshot
    is based only on the exact referenced immutable revision and explicit due
    boundary, with the same listing high-water fence used by ordinary feeds.
    """
    payload = receipt.payload
    source = payload.get("source_ref")
    boundary = payload.get("boundary")
    if not isinstance(source, dict) or not isinstance(boundary, str):
        return None
    source_id, revision, kind = source.get("id"), source.get("revision"), source.get("kind")
    if not isinstance(revision, int) or revision < 1:
        return None
    due_at = _due_instant(payload.get("due_at"))
    if due_at is None or _due_instant(receipt.available_from) != due_at:
        return None
    query = select(Listing.id).where(Listing.id <= high_water_id)
    if after_listing_id is not None:
        query = query.where(Listing.id > after_listing_id)
    if kind == "supplemental_rule" and isinstance(source_id, str):
        ref = _source_ref({"source_ref": source}, "supplemental_rule")
        if ref is None or not isinstance(ref.id, UUID):
            return None
        repo = SupplementalRulesRepository(session)
        rule = await repo.get_rule_revision(receipt.owner_id, ref)
        head = await repo.get_rule_head(receipt.owner_id, ref.id)
        if (rule is None or head is None or head.active_revision != revision
                or head.active_state != "active"
                or boundary not in {"rule_review_due", "rule_valid_until"}):
            return None
        exact_due = rule.review_due_at if boundary == "rule_review_due" else rule.valid_until
        if exact_due is None or _due_instant(exact_due) != due_at:
            return None
        selector = rule.selector
        if selector.kind == "listing":
            query = query.where(Listing.id == int(selector.listing_ref.id))
        elif selector.kind == "company":
            query = query.where(Listing.company_id == selector.company_id)
        listing_ids = [int(value) for value in (await session.scalars(
            query.order_by(Listing.id).limit(MAX_FROZEN_TARGET_LISTINGS + 1)
        )).all()]
        return listing_ids, tuple(sorted(rule.contexts))
    if kind == "research_event" and isinstance(source_id, str):
        if boundary not in {"event_effective_from", "event_effective_to"}:
            return None
        try:
            event_row = await session.get(ResearchEventRevision, (source_id, revision))
        except (TypeError, ValueError):
            return None
        if event_row is None or event_row.owner_id != receipt.owner_id:
            return None
        exact_due = (event_row.effective_from if boundary == "event_effective_from"
                     else event_row.effective_to)
        if exact_due is None or _due_instant(exact_due) != due_at:
            return None
        affected = event_row.payload.get("affected_subjects", [])
        if not isinstance(affected, list):
            return None
        ids: set[int] = set()
        for subject in affected:
            if not isinstance(subject, str):
                return None
            if subject.startswith("listing:") and subject.removeprefix("listing:").isdigit():
                listing_id = int(subject.removeprefix("listing:"))
                exists = await session.scalar(select(Listing.id).where(
                    Listing.id == listing_id, Listing.id <= high_water_id,
                    *(() if after_listing_id is None else (Listing.id > after_listing_id,)),
                ))
                if exists is not None:
                    ids.add(listing_id)
            elif subject.startswith("company:") and subject.removeprefix("company:").isdigit():
                company_ids = await session.scalars(select(Listing.id).where(
                    Listing.company_id == int(subject.removeprefix("company:")),
                    Listing.id <= high_water_id,
                    *(() if after_listing_id is None else (Listing.id > after_listing_id,)),
                ).order_by(Listing.id).limit(MAX_FROZEN_TARGET_LISTINGS + 1))
                ids.update(int(value) for value in company_ids.all())
            else:
                return None
        return sorted(ids)[:MAX_FROZEN_TARGET_LISTINGS + 1], TARGET_CONTEXTS
    if kind == "research_source_binding" and isinstance(source_id, int):
        if boundary != "material_binding_valid_to":
            return None
        binding = await session.get(ResearchSourceBinding, source_id)
        if binding is None or binding.valid_to is None or _due_instant(binding.valid_to) != due_at:
            return None
        if binding.subject_kind == "listing":
            query = query.where(Listing.id == binding.subject_id)
        elif binding.subject_kind == "company":
            query = query.where(Listing.company_id == binding.subject_id)
        else:
            return None
        listing_ids = [int(value) for value in (await session.scalars(
            query.order_by(Listing.id).limit(MAX_FROZEN_TARGET_LISTINGS + 1)
        )).all()]
        return listing_ids, TARGET_CONTEXTS
    if kind == "rule_application" and isinstance(source_id, str):
        if boundary not in {"application_review_due", "application_valid_until",
                            "research_requirement_check_by"}:
            return None
        try:
            application = await session.get(InvestmentAssessment, (source_id, revision))
        except (TypeError, ValueError):
            return None
        if (application is None or application.owner_id != receipt.owner_id
                or application.kind != "rule_application" or application.listing_id is None):
            return None
        saved_context = application.payload.get("context")
        frozen_pair = application.payload.get("frozen_pair")
        if not isinstance(saved_context, str) and isinstance(frozen_pair, dict):
            saved_context = frozen_pair.get("context")
        if saved_context not in TARGET_CONTEXTS:
            return None
        if boundary == "research_requirement_check_by":
            frozen_rule = frozen_pair.get("rule") if isinstance(frozen_pair, dict) else None
            definition = frozen_rule.get("definition") if isinstance(frozen_rule, dict) else None
            raw_due = definition.get("check_by") if isinstance(definition, dict) else None
        else:
            field = "review_due_at" if boundary == "application_review_due" else "valid_until"
            raw_due = application.payload.get(field)
        if _due_instant(raw_due) != due_at:
            return None
        query = query.where(Listing.id == application.listing_id)
        return [int(value) for value in (await session.scalars(query)).all()], (cast(Context, saved_context),)
    return None


def _due_instant(value: Any) -> datetime | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return cast(datetime, value.astimezone(UTC))


async def _source_targets(
    session: AsyncSession, receipt: Any, *, high_water_id: int,
    scope_listing_id: int | None = None,
    after_listing_id: int | None = None,
) -> tuple[list[int], bool] | None:
    payload = receipt.payload
    refs = payload.get("source_refs")
    if refs is None:
        raw = payload.get("source_ref")
        refs = [raw] if isinstance(raw, dict) else None
    if refs is None or not isinstance(refs, list) or not refs:
        return None
    expected_hashes: dict[tuple[str, str, int], str] = {}
    raw_sources = payload.get("sources")
    if raw_sources is not None:
        if not isinstance(raw_sources, list):
            return None
        for source in raw_sources:
            if not isinstance(source, dict) or not isinstance(source.get("ref"), dict):
                return None
            try:
                expected_ref = ObjectRef.model_validate(source["ref"])
            except Exception:
                return None
            digest = source.get("content_hash")
            # Evidence DTOs predate P4 and accept arbitrary nonempty hashes.
            # Integrity is established by exact equality with the authoritative
            # saved source row below; do not tighten the old DTO contract here.
            if not isinstance(digest, str) or not digest:
                return None
            expected_hashes[(expected_ref.kind, str(expected_ref.id), expected_ref.revision)] = digest
        if len(expected_hashes) != len(raw_sources):
            return None
    resolved_refs: set[tuple[str, str, int]] = set()
    listing_ids: set[int] = set()
    complete = True
    for raw in refs:
        if not isinstance(raw, dict):
            return None
        try:
            ref = ObjectRef.model_validate(raw)
        except Exception:
            return None
        if ref.field_path is not None:
            return None
        ref_identity = (ref.kind, str(ref.id), ref.revision)
        if raw_sources is not None and ref_identity not in expected_hashes:
            return None
        resolved_refs.add(ref_identity)
        expected_digest = expected_hashes.get(ref_identity, payload.get("content_hash"))
        if ref.kind == "evidence":
            row = await session.get(SavedEvidence, (str(ref.id), ref.revision))
            if row is None or row.owner_id != receipt.owner_id:
                return None
            try:
                evidence = EvidenceRecord.model_validate(row.payload)
            except Exception:
                return None
            if (
                evidence.subject_ref.kind != "listing"
                or evidence.subject_ref.revision != 1
                or evidence.subject_ref.field_path is not None
                or evidence.content_hash != expected_digest
            ):
                return None
            evidence_listing_id = int(evidence.subject_ref.id)
            if scope_listing_id is not None and evidence_listing_id != scope_listing_id:
                return None
            listing_ids.add(evidence_listing_id)
        elif ref.kind == "research_dataset":
            row = cast(Any, await session.get(ResearchDataset, int(ref.id)))
            if row is None or row.revision != ref.revision:
                return None
            if row.content_hash != expected_digest:
                return None
            if row.subject_kind == "listing":
                if scope_listing_id is not None and row.subject_id != scope_listing_id:
                    return None
                listing_ids.add(row.subject_id)
            elif row.subject_kind == "company":
                company_query = select(Listing.id).where(
                    Listing.company_id == row.subject_id,
                    Listing.id <= high_water_id,
                )
                if after_listing_id is not None:
                    company_query = company_query.where(Listing.id > after_listing_id)
                if scope_listing_id is not None:
                    company_query = company_query.where(Listing.id == scope_listing_id)
                company_result = await session.scalars(
                    company_query.order_by(Listing.id).limit(MAX_FROZEN_TARGET_LISTINGS + 1)
                )
                company_ids = [int(value) for value in company_result.all()]
                complete = complete and len(company_ids) <= MAX_FROZEN_TARGET_LISTINGS
                listing_ids.update(company_ids)
            else:
                return None
        elif ref.kind == "disclosure_original":
            if ref.revision != 1:
                return None
            row = cast(Any, await session.get(DisclosureOriginal, str(ref.id)))
            if row is None or row.owner_id != receipt.owner_id:
                return None
            item = await session.get(DisclosureItem, row.item_id)
            blob = await session.get(DisclosureBlob, row.content_hash)
            if (
                item is None or item.owner_id != receipt.owner_id
                or blob is None
                or hashlib.sha256(blob.body).hexdigest() != row.content_hash
                or row.payload.get("listing_id") != item.listing_id
                or row.payload.get("content_hash") != row.content_hash
                or row.content_hash != expected_digest
            ):
                return None
            if scope_listing_id is not None and item.listing_id != scope_listing_id:
                return None
            listing_ids.add(item.listing_id)
        else:
            return None
    if raw_sources is not None and resolved_refs != set(expected_hashes):
        return None
    ordered = sorted(value for value in listing_ids if value <= high_water_id)
    if after_listing_id is not None:
        ordered = [value for value in ordered if value > after_listing_id]
    existing_result = await session.scalars(select(Listing.id).where(Listing.id.in_(ordered)))
    existing = sorted(int(value) for value in existing_result.all())
    return (
        existing[:MAX_FROZEN_TARGET_LISTINGS + 1],
        complete and len(existing) == len(ordered) and len(existing) <= MAX_FROZEN_TARGET_LISTINGS,
    )


def _source_ref(payload: dict[str, Any], kind: str) -> ObjectRef | None:
    raw = payload.get("source_ref")
    if not isinstance(raw, dict) or raw.get("kind") != kind:
        return None
    try:
        return ObjectRef.model_validate(raw)
    except Exception:
        return None
