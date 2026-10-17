"""Append-only workflow for rechecking bank annual-report originals.

Review decisions live in MethodArtifact so they are included in ordinary DB
backup/restore. Snapshot rows remain immutable and are built from audited
PDF bytes by the v2 verifier.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.hashing import SEMANTIC_HASH_SCHEMA, canonical_json
from investment_agent.industry_peers.metric_packs import BANK_PACK
from investment_agent.storage.models.investor import MethodArtifact
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.models.reference import Listing

EVENT_KIND = "bank_annual_event"
REVIEW_KIND = "bank_annual_review"
EVENT_SCHEMA = "bank-annual-review-event/1"
OWNER = "local_owner"
FIELDS = (
    "roe_weighted_annual",
    "net_interest_margin",
    "npl_ratio",
    "provision_coverage",
    "equity_parent",
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def review_id(
    *,
    listing_id: int,
    report_period: date,
    metric: str,
    old_snapshot_id: str | None,
    candidate_snapshot_id: str | None,
    candidate_source_version: str | None,
) -> str:
    key = {
        "kind": REVIEW_KIND,
        "listing_id": listing_id,
        "period": report_period.isoformat(),
        "metric": metric,
        "old": old_snapshot_id,
        "candidate": candidate_snapshot_id,
        "candidate_source_version": candidate_source_version,
    }
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bank-annual-review/" + _digest(key)))


async def load_bank_annual_review_events(
    session: AsyncSession,
    *,
    listing_id: int | None = None,
    report_period: date | None = None,
    metric: str | None = None,
) -> list[dict[str, Any]]:
    """Read the persisted event journal; callers must validate referenced proofs."""
    rows = (
        await session.scalars(
            select(MethodArtifact)
            .where(
                MethodArtifact.owner_id == OWNER, MethodArtifact.kind.in_((EVENT_KIND, REVIEW_KIND))
            )
            .order_by(MethodArtifact.id)
        )
    ).all()
    result = []
    for row in rows:
        p = row.payload
        if p.get("schema") != EVENT_SCHEMA:
            continue
        if listing_id is not None and p.get("listing_id") != listing_id:
            continue
        if report_period is not None and p.get("report_period") != report_period.isoformat():
            continue
        if metric is not None and p.get("metric") != metric:
            continue
        result.append(copy.deepcopy(p))
    return result


async def list_pending_bank_annual_reviews(
    session: AsyncSession,
    *,
    listing_id: int | None = None,
    report_period: date | None = None,
) -> list[dict[str, Any]]:
    """List review rows unless same-year relation validation proves resolution."""
    from investment_agent.industry_peers.bank_review_states import bank_review_relations
    from investment_agent.industry_peers.contracts import PeerAnnualField

    reviews = [
        row
        for row in await load_bank_annual_review_events(
            session, listing_id=listing_id, report_period=report_period
        )
        if row.get("event_type") == "review"
    ]
    pending: list[dict[str, Any]] = []
    for review in reviews:
        old = await session.get(PeerAnnualSnapshot, review.get("old_snapshot_id"))
        if old is None:
            pending.append(review)
            continue
        metric = str(review.get("metric"))
        rows = (
            await session.scalars(
                select(PeerAnnualSnapshot).where(
                    PeerAnnualSnapshot.listing_id == old.listing_id,
                    PeerAnnualSnapshot.company_id == old.company_id,
                    PeerAnnualSnapshot.report_period == old.report_period,
                )
            )
        ).all()
        entries = []
        for row in rows:
            raw, proof = row.fields.get(metric), row.verification.get(metric)
            if not isinstance(raw, dict) or not isinstance(proof, dict):
                continue
            try:
                field = PeerAnnualField.model_validate(raw)
            except (ValueError, TypeError):
                continue
            entries.append((row, field, proof))
        relations = await bank_review_relations(session, entries, metric, cutoff=None)
        if str(review.get("review_id")) not in relations.resolved_review_ids:
            pending.append(review)
    return pending


async def append_review_event(
    session: AsyncSession, *, event: dict[str, Any], review: bool = False
) -> str:
    """Append a semantically idempotent record and return its artifact id."""
    payload = {"schema": EVENT_SCHEMA, **event}
    semantic = _digest({k: v for k, v in payload.items() if k not in {"created_at", "event_id"}})
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "bank-annual-event/" + semantic))
    prior = await session.get(MethodArtifact, identifier)
    if prior is None:
        payload.update(event_id=identifier, semantic_hash=semantic)
        session.add(
            MethodArtifact(
                id=identifier,
                owner_id=OWNER,
                kind=REVIEW_KIND if review else EVENT_KIND,
                payload=payload,
            )
        )
        await session.flush()
    return identifier


async def latest_same_year_snapshot(
    session: AsyncSession, *, listing_id: int, report_period: date, cutoff: datetime
) -> PeerAnnualSnapshot | None:
    rows = (
        await session.scalars(
            select(PeerAnnualSnapshot)
            .where(
                PeerAnnualSnapshot.listing_id == listing_id,
                PeerAnnualSnapshot.report_period == report_period,
                PeerAnnualSnapshot.captured_at <= cutoff,
            )
            .order_by(PeerAnnualSnapshot.captured_at.desc(), PeerAnnualSnapshot.id.desc())
        )
    ).all()
    return next(
        (r for r in rows if r.provider == "eastmoney" and r.adapter_version.startswith("bank-")),
        None,
    )


async def build_rollover_snapshot(
    session: AsyncSession,
    *,
    listing_id: int,
    company_id: int,
    report_period: date,
    audit: dict[str, Any],
    captured_at: datetime,
    candidate_source_version: str | None = None,
    prior: PeerAnnualSnapshot | None = None,
) -> tuple[PeerAnnualSnapshot, dict[str, Any]]:
    """Build a complete v2 fact set; only fields passing byte validation verify."""
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    if captured_at.astimezone(UTC) > datetime.now(UTC):
        raise ValueError("captured_at cannot be in the future")
    if report_period.month != 12 or report_period.day != 31:
        raise ValueError("bank annual report_period must be December 31")
    from investment_agent.industry_peers.bank_annual_v2 import (
        BANK_ANNUAL_V2_RULE,
        build_bank_annual_v2_field,
        validate_bank_annual_v2,
    )

    fields: dict[str, PeerAnnualField] = {}
    proofs: dict[str, dict[str, Any]] = {}
    audit_fields = audit.get("fields", {}) if isinstance(audit, dict) else {}
    if not isinstance(audit_fields, dict):
        raise ValueError("audit.fields must be an object keyed by metric")
    unknown = set(audit_fields) - set(FIELDS)
    if unknown:
        raise ValueError(
            "audit contains unsupported or duplicate metric keys: " + ", ".join(sorted(unknown))
        )
    for metric in FIELDS:
        entry = audit_fields.get(metric)
        if isinstance(entry, dict):
            try:
                field, proof = await build_bank_annual_v2_field(
                    session,
                    listing_id=listing_id,
                    report_period=report_period,
                    audit={**entry, "metric": metric},
                    captured_at=captured_at,
                )
            except (ValueError, KeyError, TypeError) as exc:
                fields[metric] = PeerAnnualField(
                    metric=metric,
                    value=None,
                    unit="CNY" if metric == "equity_parent" else "fraction",
                    currency="CNY" if metric == "equity_parent" else None,
                    period=_period(report_period, metric),
                    acquired_at=captured_at,
                    verification=PeerFieldVerification.CANDIDATE,
                    missing_reason="audit_rejected:" + str(exc)[:160],
                )
                proofs[metric] = {
                    "check_rule_version": BANK_ANNUAL_V2_RULE,
                    "status": "candidate",
                    "rejection_reason": str(exc)[:240],
                }
                continue
            field = field.model_copy(update={"verification": PeerFieldVerification.CANDIDATE})
            fields[metric], proofs[metric] = field, proof
            continue
        # Carry same-year non-target data only after the trusted legacy checker
        # re-reads its original. No cross-year inheritance is ever attempted.
        if prior is not None and metric in prior.fields:
            old_field = PeerAnnualField.model_validate(prior.fields[metric])
            old_proof = copy.deepcopy(prior.verification.get(metric, {}))
            if old_field.verification == PeerFieldVerification.VERIFIED:
                from investment_agent.industry_peers.originals import validate_field_original

                probe = PeerAnnualSnapshot(
                    id=prior.id,
                    company_id=prior.company_id,
                    listing_id=prior.listing_id,
                    report_period=prior.report_period,
                    provider=prior.provider,
                    adapter_version=prior.adapter_version,
                    captured_at=captured_at,
                    content_hash=prior.content_hash,
                    semantic_hash=prior.semantic_hash,
                    fields=copy.deepcopy(prior.fields),
                    verification=copy.deepcopy(prior.verification),
                )
                if await validate_field_original(session, probe, metric, cutoff=captured_at):
                    fields[metric] = old_field.model_copy(update={"acquired_at": captured_at})
                    proofs[metric] = old_proof
                    continue
            fields[metric] = old_field.model_copy(
                update={"verification": PeerFieldVerification.CANDIDATE, "acquired_at": captured_at}
            )
            proofs[metric] = old_proof
        else:
            fields[metric] = PeerAnnualField(
                metric=metric,
                value=None,
                unit="CNY" if metric == "equity_parent" else "fraction",
                currency="CNY" if metric == "equity_parent" else None,
                period=_period(report_period, metric),
                acquired_at=captured_at,
                verification=PeerFieldVerification.CANDIDATE,
                missing_reason="no_same_year_original_or_audit",
            )
            proofs[metric] = {"check_rule_version": BANK_ANNUAL_V2_RULE, "status": "missing"}
    from investment_agent.providers.eastmoney_bank_annual import BANK_ADAPTER_VERSION

    snapshot = PeerAnnualSnapshot(
        id=str(uuid.uuid4()),
        company_id=company_id,
        listing_id=listing_id,
        report_period=report_period,
        provider="eastmoney",
        adapter_version=BANK_ADAPTER_VERSION,
        captured_at=captured_at,
        content_hash="pending",
        semantic_hash="pending",
        fields={k: v.model_dump(mode="json") for k, v in fields.items()},
        verification=proofs,
    )
    # Verify only after a complete immutable-shape snapshot exists. The audit's
    # asserted status cannot promote a field by itself.
    for metric in FIELDS:
        proof = proofs[metric]
        if (
            proof.get("check_rule_version") == BANK_ANNUAL_V2_RULE
            and proof.get("status") == "verified"
            and await validate_bank_annual_v2(session, snapshot, metric, proof, cutoff=captured_at)
        ):
            checked = PeerAnnualField.model_validate(snapshot.fields[metric]).model_copy(
                update={"verification": PeerFieldVerification.VERIFIED}
            )
            snapshot.fields[metric] = checked.model_dump(mode="json")
        elif (
            proof.get("check_rule_version") == BANK_ANNUAL_V2_RULE
            and proof.get("status") == "verified"
        ):
            current = PeerAnnualField.model_validate(snapshot.fields[metric])
            snapshot.fields[metric] = current.model_copy(
                update={
                    "verification": PeerFieldVerification.CANDIDATE,
                }
            ).model_dump(mode="json")
            proof["status"] = "candidate"
            proof["rejection_reason"] = "v2_readback_failed"

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: stable(v)
                for k, v in value.items()
                if k not in {"acquired_at", "checked_at", "source_file", "pdf_path"}
            }
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value

    sem = _digest(
        {
            "schema": SEMANTIC_HASH_SCHEMA,
            "metric_pack": BANK_PACK,
            "listing_id": listing_id,
            "company_id": company_id,
            "report_period": report_period.isoformat(),
            "provider": "eastmoney",
            "adapter_version": BANK_ADAPTER_VERSION,
            "fields": stable(snapshot.fields),
            "verification": stable(proofs),
        }
    )
    snapshot.id = str(uuid.uuid5(uuid.NAMESPACE_URL, "bank-annual-rollover/" + sem))
    snapshot.content_hash = snapshot.semantic_hash = sem
    return snapshot, proofs


def _period(report_period: date, metric: str) -> Period:
    equity = metric == "equity_parent"
    return Period(
        start=report_period if equity else date(report_period.year, 1, 1),
        end=report_period,
        basis="point_in_time" if equity else "annual_ratio",
    )


async def resolve_bank_annual_review(
    session: AsyncSession,
    *,
    review_id_value: str,
    audit: dict[str, Any],
    decision: str,
    checked_at: datetime,
) -> dict[str, Any]:
    """Resolve only after re-reading and validating the same-year PDF bytes."""
    if decision not in {"accept_original", "reject_supplier"}:
        raise ValueError("decision must be accept_original or reject_supplier")
    if audit.get("schema") != "bank-annual-review-audit/2":
        raise ValueError("audit schema must be bank-annual-review-audit/2")
    events = await load_bank_annual_review_events(session)
    review = next(
        (
            e
            for e in events
            if e.get("event_type") == "review" and e.get("review_id") == review_id_value
        ),
        None,
    )
    if review is None:
        raise ValueError("unknown review_id")
    existing = next(
        (
            e
            for e in events
            if e.get("event_type") == "resolve" and e.get("review_id") == review_id_value
        ),
        None,
    )
    if existing:
        if existing.get("decision") != decision or existing.get("audit_request_hash") != _digest(
            audit
        ):
            raise ValueError("review already resolved with a different decision or audit")
        return {"event_id": existing["event_id"], "replayed": True}
    listing = await session.get(Listing, review["listing_id"])
    if (
        listing is None
        or audit.get("listing_id") != listing.id
        or audit.get("code") != listing.ticker
        or audit.get("report_period") != review.get("report_period")
    ):
        raise ValueError("audit code/listing/year does not match the pending review")
    period = date.fromisoformat(review["report_period"])
    candidate = await session.get(PeerAnnualSnapshot, review["candidate_snapshot_id"])
    old = await session.get(PeerAnnualSnapshot, review["old_snapshot_id"])
    if candidate is None or candidate.content_hash != review.get("candidate_content_hash"):
        raise ValueError("candidate snapshot binding does not match review")
    if old is None or old.content_hash != review.get("old_content_hash"):
        raise ValueError("old snapshot binding does not match review")
    if (
        old.listing_id != listing.id
        or candidate.listing_id != listing.id
        or old.company_id != listing.company_id
        or candidate.company_id != listing.company_id
        or old.report_period != period
        or candidate.report_period != period
    ):
        raise ValueError("review snapshots do not match the same company/listing/year")
    metric = review["metric"]
    audit_field = (audit.get("fields") or {}).get(metric)
    if not isinstance(audit_field, dict):
        raise ValueError("audit must contain the reviewed metric")
    snapshot, proofs = await build_rollover_snapshot(
        session,
        listing_id=listing.id,
        company_id=listing.company_id,
        report_period=period,
        audit={"fields": {metric: audit_field}},
        captured_at=checked_at,
        candidate_source_version=review.get("candidate_source_version"),
        prior=candidate,
    )
    proof = proofs[metric]
    if snapshot.fields[metric].get("verification") != "verified" or not await _v2_valid(
        session, snapshot, metric, proof, checked_at
    ):
        raise ValueError("resolution requires a successfully validated v2 original field")
    old_value = old.fields.get(metric, {}).get("value")
    candidate_value = candidate.fields.get(metric, {}).get("value")
    resolved_value = snapshot.fields.get(metric, {}).get("value")
    if decision == "reject_supplier":
        if candidate_value == old_value or resolved_value != old_value:
            raise ValueError(
                "reject_supplier requires the actual original to confirm the old value"
            )
    else:
        if resolved_value != candidate_value:
            raise ValueError("accept_original requires the original to match the candidate value")
        if not audit_field.get("revision_basis") or not audit_field.get("source_version"):
            raise ValueError(
                "accept_original requires an explicit official revision basis and source_version"
            )
        old_proof = old.verification.get(metric, {})
        old_ref = old_proof.get("original_ref", {}) if isinstance(old_proof, dict) else {}
        new_ref = proof.get("original_ref", {})
        old_identity = old_proof.get("report_identity", {}) if isinstance(old_proof, dict) else {}
        new_identity = proof.get("report_identity", {})
        if (
            new_ref.get("id") == old_ref.get("id")
            or proof.get("pdf_hash") == old_proof.get("pdf_hash")
            or new_identity.get("disclosure_id") == old_identity.get("disclosure_id")
        ):
            raise ValueError(
                "accept_original requires a distinct, formally revised same-year original"
            )
        from investment_agent.storage.models.disclosure import DisclosureOriginal

        previous_original = await session.get(DisclosureOriginal, old_ref.get("id"))
        revised_original = await session.get(DisclosureOriginal, new_ref.get("id"))
        if (
            previous_original is None
            or revised_original is None
            or not any(
                word in revised_original.payload.get("title", "") for word in ("修订", "更正")
            )
            or datetime.fromisoformat(revised_original.payload["first_published_at"])
            < datetime.fromisoformat(previous_original.payload["first_published_at"])
        ):
            raise ValueError("accept_original requires a newer official full-report revision")
    resolution_proof = {
        "review_id": review_id_value,
        "decision": decision,
        "basis": "v2_original_review",
        "old_snapshot_id": old.id,
        "candidate_snapshot_id": candidate.id,
        "resolved_value": resolved_value,
    }
    proof["resolution"] = resolution_proof
    snapshot.verification[metric] = proof
    resolved_hash = _digest(
        {
            "base_semantic_hash": snapshot.semantic_hash,
            "resolution": resolution_proof,
            "proof_hash": _digest(proof),
        }
    )
    snapshot.semantic_hash = snapshot.content_hash = resolved_hash
    snapshot.id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "bank-annual-rollover-resolution/" + resolved_hash)
    )
    if await session.get(PeerAnnualSnapshot, snapshot.id) is None:
        session.add(snapshot)
    await session.flush()
    event_id = await append_review_event(
        session,
        event={
            "event_type": "resolve",
            "review_id": review_id_value,
            "code": review["code"],
            "listing_id": listing.id,
            "company_id": listing.company_id,
            "report_period": period.isoformat(),
            "metric": metric,
            "old_snapshot_id": old.id,
            "candidate_snapshot_id": candidate.id,
            "resulting_snapshot_id": snapshot.id,
            "old_content_hash": old.content_hash,
            "candidate_content_hash": candidate.content_hash,
            "decision": decision,
            "old_value": old_value,
            "candidate_value": candidate_value,
            "resolved_value": resolved_value,
            "period": snapshot.fields[metric].get("period"),
            "accounting_scope": proof.get("accounting_scope"),
            "candidate_source_version": review.get("candidate_source_version"),
            "revision_basis": audit_field.get("revision_basis"),
            "original_refs": [proof["original_ref"]] if proof.get("original_ref") else [],
            "checked_at": proof.get("checked_at"),
            "proof_hashes": {metric: _digest(proof)},
            "resolution_basis": "v2_original_review",
            "audit_request_hash": _digest(audit),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    return {"event_id": event_id, "snapshot_id": snapshot.id, "decision": decision}


async def _v2_valid(
    session: AsyncSession,
    snapshot: PeerAnnualSnapshot,
    metric: str,
    proof: dict[str, Any],
    checked_at: datetime,
) -> bool:
    from investment_agent.industry_peers.bank_annual_v2 import validate_bank_annual_v2

    return await validate_bank_annual_v2(session, snapshot, metric, proof, cutoff=checked_at)
