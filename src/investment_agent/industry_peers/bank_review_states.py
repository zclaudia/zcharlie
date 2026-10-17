"""Trusted bank revision-review dependencies over immutable fact versions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers.contracts import PeerAnnualField
from investment_agent.industry_peers.hashing import canonical_json
from investment_agent.storage.models.investor import MethodArtifact
from investment_agent.storage.models.peer import PeerAnnualSnapshot


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _time(value: Any) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        if isinstance(value, datetime):  # SQLite's persisted UTC timestamps.
            return stamp.replace(tzinfo=UTC)
        raise ValueError("review journal times require a timezone")
    return stamp.astimezone(UTC)


def _equal(left: Any, right: Any) -> bool:
    try:
        return (
            left is right
            if left is None or right is None
            else Decimal(str(left)) == Decimal(str(right))
        )
    except (InvalidOperation, ValueError):
        return False


async def record_bank_online_reviews(
    session: AsyncSession,
    snapshot: PeerAnnualSnapshot,
    baseline_by_metric: dict[str, str],
) -> list[str]:
    """Persist each detected discrepancy atomically with the job checkpoint."""
    from investment_agent.industry_peers.bank_annual_review import append_review_event, review_id
    from investment_agent.industry_peers.originals import validate_field_original

    identifiers = []
    for metric, proof in snapshot.verification.items():
        comparison = proof.get("original_comparison") if isinstance(proof, dict) else None
        if not isinstance(comparison, dict) or comparison.get("status") not in {
            "mismatch",
            "missing",
        }:
            continue
        baseline = await session.get(PeerAnnualSnapshot, baseline_by_metric.get(metric, ""))
        if (
            baseline is None
            or baseline.listing_id != snapshot.listing_id
            or baseline.company_id != snapshot.company_id
            or baseline.report_period != snapshot.report_period
            or not await validate_field_original(
                session, baseline, metric, cutoff=_time(snapshot.captured_at)
            )
        ):
            continue
        old_field, candidate = baseline.fields[metric], snapshot.fields[metric]
        rid = review_id(
            listing_id=snapshot.listing_id,
            report_period=snapshot.report_period,
            metric=metric,
            old_snapshot_id=baseline.id,
            candidate_snapshot_id=snapshot.id,
            candidate_source_version=candidate.get("source_version"),
        )
        await append_review_event(
            session,
            review=True,
            event={
                "review_id": rid,
                "event_type": "review",
                "code": baseline.verification[metric]["report_identity"]["code"],
                "listing_id": snapshot.listing_id,
                "company_id": snapshot.company_id,
                "report_period": snapshot.report_period.isoformat(),
                "metric": metric,
                "old_snapshot_id": baseline.id,
                "candidate_snapshot_id": snapshot.id,
                "resulting_snapshot_id": snapshot.id,
                "old_content_hash": baseline.content_hash,
                "candidate_content_hash": snapshot.content_hash,
                "old_value": old_field.get("value"),
                "candidate_value": candidate.get("value"),
                "period": candidate.get("period"),
                "accounting_scope": comparison.get("accounting_scope"),
                "candidate_source_version": candidate.get("source_version"),
                "original_refs": [comparison["original_ref"]],
                "decision": None,
                "checked_at": _time(snapshot.captured_at).isoformat(),
                "proof_hashes": {metric: _hash(proof)},
                "created_at": _time(snapshot.captured_at).isoformat(),
            },
        )
        identifiers.append(rid)
    return identifiers


@dataclass
class BankReviewRelations:
    corrections: dict[str, list[str]] = field(default_factory=dict)
    resolved_ids: set[str] = field(default_factory=set)
    pending_ids: set[str] = field(default_factory=set)
    linked_scopes: dict[str, str] = field(default_factory=dict)
    resolved_review_ids: set[str] = field(default_factory=set)


def _valid_event(row: MethodArtifact) -> bool:
    payload = row.payload
    semantic = _hash(
        {k: v for k, v in payload.items() if k not in {"created_at", "event_id", "semantic_hash"}}
    )
    identifier = str(uuid5(NAMESPACE_URL, "bank-annual-event/" + semantic))
    return (
        payload.get("schema") == "bank-annual-review-event/1"
        and payload.get("event_id") == row.id == identifier
        and payload.get("semantic_hash") == semantic
    )


async def bank_review_relations(
    session: AsyncSession,
    entries: list[tuple[PeerAnnualSnapshot, PeerAnnualField, dict[str, Any]]],
    metric: str,
    cutoff: datetime | None,
) -> BankReviewRelations:
    """A receipt never clears a revision without validating its actual result PDF.

    Pending reviews also cover later same-value copies. Rechecking an old PDF
    through a different command cannot bypass an unresolved supplier revision.
    """
    from investment_agent.industry_peers.originals import validate_field_original

    result = BankReviewRelations()
    by_id = {row.id: (row, value, proof) for row, value, proof in entries}
    if not by_id:
        return result
    artifacts = list(
        (
            await session.scalars(
                select(MethodArtifact).where(
                    MethodArtifact.owner_id == "local_owner",
                    MethodArtifact.kind.in_(("bank_annual_event", "bank_annual_review")),
                )
            )
        ).all()
    )
    events = [
        row.payload
        for row in artifacts
        if _valid_event(row) and row.payload.get("metric") == metric
    ]
    unresolved_by_review: dict[str, set[str]] = {}
    for review in events:
        try:
            if review.get("event_type") != "review" or (
                cutoff and _time(review["checked_at"]) > cutoff
            ):
                continue
            old, old_field, old_proof = by_id[review["old_snapshot_id"]]
            candidate, candidate_field, candidate_proof = by_id[review["candidate_snapshot_id"]]
            if (
                old.listing_id != candidate.listing_id
                or old.company_id != candidate.company_id
                or old.report_period != candidate.report_period
                or old.provider != candidate.provider
                or candidate.provider != "eastmoney"
                or candidate.adapter_version != "bank-annual-v1"
                or review.get("listing_id") != old.listing_id
                or review.get("company_id") != old.company_id
                or review.get("report_period") != old.report_period.isoformat()
                or review.get("old_content_hash") != old.content_hash
                or review.get("candidate_content_hash") != candidate.content_hash
                or review.get("candidate_source_version") != candidate_field.source_version
                or not _equal(review.get("old_value"), old_field.value)
                or not _equal(review.get("candidate_value"), candidate_field.value)
                or _equal(old_field.value, candidate_field.value)
                or (old_field.unit, old_field.currency, old_field.period)
                != (candidate_field.unit, candidate_field.currency, candidate_field.period)
                or not await validate_field_original(session, old, metric, cutoff=cutoff)
            ):
                continue
            scope = old_proof.get("accounting_scope")
            if scope != "group_consolidated":
                continue
            affected = {
                row.id
                for row, value, _proof in entries
                if row.listing_id == old.listing_id
                and row.report_period == old.report_period
                and row.provider == old.provider
                and (value.unit, value.currency, value.period)
                == (old_field.unit, old_field.currency, old_field.period)
                and (
                    _equal(value.value, old_field.value)
                    or _equal(value.value, candidate_field.value)
                )
            }
            review_key = str(review.get("review_id"))
            unresolved_by_review[review_key] = affected
            for identifier in affected:
                result.linked_scopes[identifier] = scope
            for event in events:
                if event.get("event_type") != "resolve" or event.get("review_id") != review.get(
                    "review_id"
                ):
                    continue
                resolved, resolved_field, resolved_proof = by_id[event["resulting_snapshot_id"]]
                stamp = _time(event["checked_at"])
                if (
                    (cutoff and stamp > cutoff)
                    or not _time(candidate.captured_at)
                    <= stamp
                    <= _time(resolved.captured_at)
                    <= _time(event["created_at"])
                    or any(
                        event.get(k) != review.get(k)
                        for k in (
                            "listing_id",
                            "company_id",
                            "report_period",
                            "metric",
                            "old_snapshot_id",
                            "candidate_snapshot_id",
                            "old_content_hash",
                            "candidate_content_hash",
                            "candidate_source_version",
                        )
                    )
                    or resolved.listing_id != old.listing_id
                    or resolved.company_id != old.company_id
                    or resolved.report_period != old.report_period
                    or resolved.provider != old.provider
                    or resolved.adapter_version != "bank-annual-v1"
                    or resolved_proof.get("check_rule_version") != "bank-annual-original/2"
                    or resolved_proof.get("accounting_scope") != scope
                    or event.get("proof_hashes", {}).get(metric) != _hash(resolved_proof)
                    or resolved_proof.get("original_ref") not in event.get("original_refs", [])
                    or not _equal(event.get("resolved_value"), resolved_field.value)
                    or not await validate_field_original(session, resolved, metric, cutoff=cutoff)
                ):
                    continue
                decision = event.get("decision")
                if decision == "reject_supplier":
                    if not _equal(resolved_field.value, old_field.value):
                        continue
                elif decision == "accept_original":
                    if not _equal(resolved_field.value, candidate_field.value) or not event.get(
                        "revision_basis"
                    ):
                        continue
                    from investment_agent.storage.models.disclosure import DisclosureOriginal

                    previous = await session.get(
                        DisclosureOriginal, old_proof.get("original_ref", {}).get("id")
                    )
                    revised = await session.get(
                        DisclosureOriginal, resolved_proof.get("original_ref", {}).get("id")
                    )
                    if (
                        previous is None
                        or revised is None
                        or previous.id == revised.id
                        or previous.content_hash == revised.content_hash
                        or previous.payload.get("disclosure_id")
                        == revised.payload.get("disclosure_id")
                        or not any(
                            word in str(revised.payload.get("title", ""))
                            for word in ("修订", "更正")
                        )
                        or _time(revised.payload["first_published_at"])
                        < _time(previous.payload["first_published_at"])
                    ):
                        continue
                else:
                    continue
                targets = {
                    identifier
                    for identifier in affected
                    if _time(by_id[identifier][0].captured_at) <= stamp
                }
                targets.discard(resolved.id)
                result.resolved_ids.update(targets)
                # Subsequent online revalidation may copy the same checked
                # result into a new snapshot. It remains the same resolution,
                # but a later pending review below can limit that copy again.
                for row, value, proof in entries:
                    if (
                        row.listing_id == resolved.listing_id
                        and row.report_period == resolved.report_period
                        and row.provider == resolved.provider
                        and row.adapter_version == resolved.adapter_version
                        and _time(row.captured_at) >= _time(resolved.captured_at)
                        and _equal(value.value, resolved_field.value)
                        and proof.get("original_ref") == resolved_proof.get("original_ref")
                        and proof.get("pdf_hash") == resolved_proof.get("pdf_hash")
                        and await validate_field_original(session, row, metric, cutoff=cutoff)
                    ):
                        result.corrections.setdefault(row.id, []).extend(sorted(targets))
                unresolved_by_review.pop(review_key, None)
                result.resolved_review_ids.add(review_key)
                break
        except (KeyError, ValueError, TypeError, InvalidOperation):
            # A malformed review journal never restores publication eligibility.
            continue
    for identifiers in unresolved_by_review.values():
        result.pending_ids.update(identifiers)
    return result
