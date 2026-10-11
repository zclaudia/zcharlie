"""Bounded literal search of owner-scoped immutable saved material.

No acquisition, parsing, vectors, or inferred verification. Coverage is explicit.
"""

import hashlib
import json
from typing import Any

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, OwnerId
from investment_agent.storage.models.disclosure import DisclosureItem, DisclosureOriginal
from investment_agent.storage.models.evidence import SavedEvidence


def literal_fold(value: str) -> str:
    """Match Chinese/Unicode literally, with ASCII letters case insensitive."""
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


class SearchRequest(Contract):
    listing_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    query: str = Field(min_length=1, max_length=120)
    as_of: AwareDatetime
    offset: int = Field(default=0, ge=0, le=200)
    limit: int = Field(default=10, ge=1, le=20)
    field: str | None = Field(default=None, max_length=80)
    period_end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    historical_reconstruction: bool = False

    @model_validator(mode="after")
    def bounded(self) -> "SearchRequest":
        if any(i < 1 for i in self.listing_ids) or len(set(self.listing_ids)) != len(self.listing_ids):
            raise ValueError("unique positive listing IDs required")
        if not self.query.strip():
            raise ValueError("nonblank literal query required")
        return self


async def search_saved(
    session: AsyncSession, request: SearchRequest, *, owner: OwnerId = "local_owner"
) -> dict[str, Any]:
    # Apply identity and all visibility filters BEFORE the bounded material read.
    # Contract saves normalized UTC timestamps; compare fractional seconds
    # exactly as well as whole seconds before material limits.
    from sqlalchemy import func

    p = SavedEvidence.payload
    cutoff = request.as_of.isoformat()
    def at_cutoff(key: str) -> Any:
        # Saved Contract timestamps are UTC; compare six fractional digits
        # exactly. julianday alone rounds to milliseconds and could leak a
        # parser record created microseconds after a fixed message cutoff.
        stamp = p[key].as_string()
        seconds = func.substr(stamp, 1, 19)
        fractional = case((func.substr(stamp, 20, 1) == ".", func.replace(func.substr(stamp, 21), "Z", "")), else_="")
        micros = func.substr(fractional.op("||")("000000"), 1, 6)
        boundary = request.as_of.strftime("%Y-%m-%dT%H:%M:%S")
        return (seconds < boundary) | ((seconds == boundary) & (micros <= f"{request.as_of.microsecond:06d}"))

    predicates = [
        SavedEvidence.owner_id == owner,
        p["subject_ref"]["kind"].as_string() == "listing",
        p["subject_ref"]["id"].as_integer().in_(request.listing_ids),
        *(at_cutoff(k) for k in ("published_at", "acquired_at", "available_from", "created_at")),
    ]
    if request.period_end:
        predicates.append(p["period"]["end"].as_string() == request.period_end)
    if request.field:
        predicates.append(p["source_ref"]["field_path"].as_string() == request.field)
    # Divide the window fairly across requested companies; no busy listing
    # can hide all material for another. Literal match is bound, never FTS/SQL.
    needle = literal_fold(request.query.strip())
    encoded_needle = json.dumps(needle, ensure_ascii=False)[1:-1]
    predicates.append((func.instr(func.lower(p["text"].as_string()), needle) > 0)
                      | (func.instr(func.lower(p["locator"].as_string()), needle) > 0)
                      | (func.instr(func.lower(p["source_ref"]["field_path"].as_string()), needle) > 0)
                      | ((p["source_ref"]["field_path"].as_string() == "parsed_fragments")
                         & (func.instr(func.lower(p["text"].as_string()), encoded_needle) > 0)))
    per_listing = 100 // len(request.listing_ids)
    rows: list[SavedEvidence] = []
    exhausted = []
    for listing_id in request.listing_ids:
        batch = (await session.scalars(select(SavedEvidence).where(
            *predicates, p["subject_ref"]["id"].as_integer() == listing_id
        ).order_by(SavedEvidence.id, SavedEvidence.revision).limit(per_listing + 1))).all()
        if len(batch) > per_listing:
            exhausted.append(listing_id)
        rows.extend(batch[:per_listing])
    matches: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    needle = literal_fold(request.query.strip())
    hit_cap_reached = False
    hit_counts: dict[int, int] = {}
    hit_cap_per_listing = 220 // len(request.listing_ids)
    for row in rows:
        record = EvidenceRecord.model_validate(row.payload)
        ref = ObjectRef(kind="evidence", id=record.id, revision=record.revision)
        view = await EvidenceService(session).get_evidence(
            owner, ref, request.as_of, subject_ref=record.subject_ref, visible_at=request.as_of
        )
        original = None
        material: dict[str, Any] = {}
        fragments = [{"text": record.text or record.locator, "locator": record.locator,
                      "content_hash": record.content_hash, "text_kind": "saved_text" if record.text else "field_locator"}]
        if record.source_ref.kind == "disclosure_original":
            original = await session.get(DisclosureOriginal, str(record.source_ref.id))
            if original is None or original.owner_id != owner:
                continue
            op = original.payload
            from datetime import datetime
            if (op["listing_id"] != record.subject_ref.id
                or max(datetime.fromisoformat(op[k]) for k in
                       ("first_published_at", "acquired_at", "available_from")) > request.as_of):
                continue
            if record.source_ref.field_path == "parsed_fragments":
                if not record.text or hashlib.sha256(record.text.encode()).hexdigest() != record.content_hash:
                    continue
                material = json.loads(record.text)
                if material.get("original_hash") != original.content_hash or op["content_hash"] != original.content_hash:
                    continue
                fragments = material.get("fragments", [])
                if any(hashlib.sha256(f["text"].encode()).hexdigest() != f.get("content_hash") for f in fragments):
                    continue
        if original and not material and record.document_provenance:
            manifests = (await session.scalars(select(SavedEvidence).where(
                SavedEvidence.owner_id == owner,
                p["subject_ref"]["id"].as_integer() == record.subject_ref.id,
                p["source_ref"]["id"].as_string() == str(record.source_ref.id),
                p["source_ref"]["field_path"].as_string() == "parsed_fragments",
                *(at_cutoff(k) for k in ("published_at", "acquired_at", "available_from", "created_at")),
            ).order_by(SavedEvidence.id).limit(3))).all()
            # Require a unique exact saved parser manifest; ambiguous versions
            # do not silently supply neighboring text from a chosen winner.
            exact_manifests = []
            for saved in manifests:
                manifest = EvidenceRecord.model_validate(saved.payload)
                if not manifest.text or hashlib.sha256(manifest.text.encode()).hexdigest() != manifest.content_hash:
                    continue
                candidate = json.loads(manifest.text)
                if candidate.get("original_hash") != original.content_hash or candidate.get("parser_version") != record.document_provenance.parser_version:
                    continue
                if any(hashlib.sha256(f["text"].encode()).hexdigest() != f.get("content_hash") for f in candidate.get("fragments", [])):
                    continue
                if any(f["locator"] == record.locator and f["content_hash"] == record.content_hash for f in candidate.get("fragments", [])):
                    exact_manifests.append((manifest, candidate))
            if len(manifests) < 3 and len(exact_manifests) == 1:
                context_manifest, material = exact_manifests[0]
                fragments = material["fragments"]
            else:
                context_manifest = record
        else:
            context_manifest = record
        item = await session.get(DisclosureItem, original.item_id) if original else None
        title = item.payload["title"] if item and item.owner_id == owner else record.locator
        for index, fragment in enumerate(fragments):
            if record.document_provenance and fragment["locator"] != record.locator:
                continue
            text = fragment.get("text", "")
            metadata_match = not material and (needle in literal_fold(record.locator) or needle in literal_fold(record.source_ref.field_path or ""))
            if needle not in literal_fold(text) and not metadata_match:
                continue
            # Parser fragments include the exact hash; reject corrupted manifests.
            if material and hashlib.sha256(text.encode()).hexdigest() != fragment.get("content_hash"):
                continue
            identity = (str(record.subject_ref.id), original.content_hash if original else record.content_hash, fragment["locator"])
            citation = {"evidence_ref": ref.model_dump(mode="json"),
                        "source_ref": record.source_ref.model_dump(mode="json"),
                        "subject_ref": record.subject_ref.model_dump(mode="json"),
                        "verification": record.verification, "effective_status": view.status,
                        "warnings": list(view.warnings),
                        "correction_refs": [r.model_dump(mode="json") for r in record.correction_refs]}
            if identity in seen:
                duplicate = seen[identity]
                duplicate["source_copy_count"] += 1
                if len(duplicate["source_audit"]) < 10:
                    duplicate["source_audit"].append(citation)
                else:
                    duplicate["source_audit_truncated"] = True
                duplicate["verification_states"] = sorted(set((*duplicate["verification_states"], record.verification)))
                duplicate["correction_refs"] = list({json.dumps(r, sort_keys=True): r for r in (*duplicate["correction_refs"], *citation["correction_refs"])}.values())
                duplicate["warnings"] = sorted(set((*duplicate["warnings"], *view.warnings)))
                if view.status == "conflict":
                    duplicate["effective_status"] = "conflict"
                continue
            listing_key = int(record.subject_ref.id)
            if hit_counts.get(listing_key, 0) >= hit_cap_per_listing:
                hit_cap_reached = True
                continue
            position = literal_fold(text).find(needle)
            excerpt_start = max(0, position - 300)
            hit = {
                **citation, "subject_ref": record.subject_ref.model_dump(mode="json"),
                "title": title,
                "locator": fragment["locator"], "fragment_index": index, "manifest_hash": context_manifest.content_hash if material else None,
                "fragment_hash": fragment["content_hash"],
                "original_hash": original.content_hash if original else None,
                "parser_version": material.get("parser_version", material.get("version")),
                "published_at": record.published_at.isoformat(),
                "acquired_at": record.acquired_at.isoformat(),
                "available_from": record.available_from.isoformat(),
                "verification": record.verification, "effective_status": view.status,
                "value": record.value.model_dump(mode="json") if record.value else None,
                "warnings": list(view.warnings), "correction_refs": [r.model_dump(mode="json") for r in record.correction_refs],
                "period": record.period.model_dump(mode="json") if record.period else None,
                "text_kind": fragment.get("text_kind", "saved_original_fragment"),
                "text": text[excerpt_start:excerpt_start+2000], "text_truncated": len(text) > 2000,
                "excerpt_start": excerpt_start,
                "adjacent_context": [
                    {"locator": f["locator"], "fragment_hash": f["content_hash"], "text": f["text"][:2000],
                     "text_truncated": len(f["text"]) > 2000,
                     "source_ref": record.source_ref.model_dump(mode="json"),
                     "evidence_ref": ObjectRef(kind="evidence", id=context_manifest.id, revision=context_manifest.revision).model_dump(mode="json"), "manifest_hash": context_manifest.content_hash if material else None,
                     "subject_ref": record.subject_ref.model_dump(mode="json"),
                     "as_of": cutoff, "fragment_index": context_index,
                     "original_hash": original.content_hash if original else None}
                    for context_index, f in enumerate(fragments[max(0, index-1):index+2], start=max(0,index-1))
                ],
                "source_audit": [citation], "source_copy_count": 1, "source_audit_truncated": False,
                "verification_states": [record.verification], "distinct_original_count": 1 if original else 0,
                "provenance_group": original.content_hash if original else record.content_hash,
                "relevance": literal_fold(text).count(needle), "discovery_only": True,
            }
            hit_counts[listing_key] = hit_counts.get(listing_key, 0) + 1
            matches.append(hit)
            seen[identity] = hit
    matches.sort(key=lambda h: (-h["relevance"], h["title"], h["locator"]))
    # Equal relevance is interleaved by company as well as fairly scanned;
    # one busy company cannot hide all another company's hits on page one.
    ordinals: dict[tuple[int, int], int] = {}
    for hit in matches:
        key = (hit["subject_ref"]["id"], hit["relevance"])
        hit["company_relevance_position"] = ordinals.get(key, 0)
        ordinals[key] = hit["company_relevance_position"] + 1
    matches.sort(key=lambda h: (-h["relevance"], h["company_relevance_position"], h["subject_ref"]["id"]))
    return {"untrusted_source_data": True, "matches": matches[request.offset:request.offset+request.limit],
            "matched_count_in_window": len(matches), "scanned_records": len(rows),
            "coverage": "bounded_saved_window" if exhausted or hit_cap_reached else "saved_authorized_material",
            "fragment_hit_cap_reached": hit_cap_reached, "fragment_hit_cap": 220, "fragment_hit_cap_per_listing": hit_cap_per_listing,
            "window_exhausted": bool(exhausted) or hit_cap_reached, "exhausted_listing_ids": exhausted,
            "record_cap_per_listing": per_listing, "historical_reconstruction": request.historical_reconstruction,
            "as_of": cutoff, "matching_basis": "literal Unicode, ASCII case insensitive",
            "visibility_basis": "saved-library creation and source availability at cutoff",
            "ranking_basis": "literal relevance, never truth or certification",
            "limitations": ["搜索结果仅是线索；相同原文转载不增加独立支持。",
                             "只搜索已保存且截止时间可见的材料；未解析正文不在结果中。"]}


class ComparisonItem(Contract):
    listing_id: int = Field(ge=1)
    ref: ObjectRef

    @model_validator(mode="after")
    def evidence_only(self) -> "ComparisonItem":
        if self.ref.kind != "evidence" or self.ref.field_path is not None:
            raise ValueError("exact whole saved evidence reference required")
        return self


class SavedComparisonRequest(Contract):
    items: tuple[ComparisonItem, ...] = Field(min_length=2, max_length=10)
    as_of: AwareDatetime


async def compare_saved(session: AsyncSession, request: SavedComparisonRequest) -> dict[str, Any]:
    """No rank: comparable only with exact semantic and effective-state identity."""
    from investment_agent.storage.models.pit_snapshot import FinancialSnapshot

    rows = []
    identities = []
    reasons = []
    for item in request.items:
        subject = ObjectRef(kind="listing", id=item.listing_id, revision=1)
        view = await EvidenceService(session).get_evidence("local_owner", item.ref, request.as_of,
                                                        subject_ref=subject, visible_at=request.as_of)
        record = view.data
        basis: str | None = None
        if record and record.document_provenance:
            basis = record.document_provenance.reporting_scope
        elif record and record.source_ref.kind == "financial_snapshot":
            snapshot = await session.get(FinancialSnapshot, record.source_ref.id)
            raw_scope = snapshot.fields.get("reporting_scope") if snapshot else None
            basis = raw_scope if isinstance(raw_scope, str) else None
            if record.value and record.value.currency and (not snapshot or snapshot.fields.get("currency") != record.value.currency):
                reasons.append("explicit_currency_missing_or_mismatched")
        if not record or view.status != "ok" or record.verification != "verified" or not record.value or not record.period:
            reasons.append("source_not_current_verified_numeric_fact")
        elif not basis or basis == "unknown":
            reasons.append("reporting_scope_missing")
        else:
            identities.append((record.period, record.value.unit, record.value.currency, basis,
                               record.source_ref.field_path))
        rows.append({"ref": item.ref.model_dump(mode="json"), "subject_ref": subject.model_dump(mode="json"),
                     "status": view.status, "warnings": list(view.warnings),
                     "fact": record.model_dump(mode="json", exclude={"text"}) if record else None,
                     "text_excerpt": (record.text or "")[:2000] if record else None,
                     "text_truncated": bool(record and record.text and len(record.text) > 2000),
                     "reporting_scope": basis})
    if len(set(i.listing_id for i in request.items)) != len(request.items):
        reasons.append("distinct_companies_required")
    if len(set(identities)) > 1:
        reasons.append("period_unit_currency_scope_or_metric_mismatch")
    return {"comparable": not reasons, "rows": rows, "not_comparable_reasons": sorted(set(reasons)),
            "ranking": None, "as_of": request.as_of.isoformat(),
            "policy": "相同报告期、单位、币种、指标及合并口径才能比较；不同材料仅并列，不作硬排名。"}
