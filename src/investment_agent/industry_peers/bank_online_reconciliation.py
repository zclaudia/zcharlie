"""Reconcile online bank ratios with immutable annual-report evidence.

Online supplier responses are candidate evidence.  This module only promotes a
same-value ratio when the existing FY original can be read and revalidated
against its stored bytes.  It never changes an existing snapshot.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.originals import validate_field_original
from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
from investment_agent.storage.models.peer import PeerAnnualSnapshot


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _aware(value).isoformat()


def _same_value(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False


async def _verified_baseline(
    session: AsyncSession, row: PeerAnnualSnapshot, metric: str, captured_at: datetime
) -> bool:
    """Revalidate the baseline proof using validate_field_original's DB readback."""
    probe = PeerAnnualSnapshot(
        id=row.id,
        company_id=row.company_id,
        listing_id=row.listing_id,
        report_period=row.report_period,
        provider=row.provider,
        adapter_version=row.adapter_version,
        captured_at=captured_at,
        content_hash=row.content_hash,
        semantic_hash=row.semantic_hash,
        fields=copy.deepcopy(row.fields),
        verification=copy.deepcopy(row.verification),
    )
    return await validate_field_original(session, probe, metric)


async def _verified_scope_exclusion(
    session: AsyncSession, row: PeerAnnualSnapshot, metric: str, *, captured_at: datetime
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    """Check an exclusion record against the bytes of the stored annual PDF."""
    proof = row.verification.get(metric)
    if not isinstance(proof, dict):
        return False, None, None
    # Imported Ping An exclusions deliberately use status=missing and a boolean
    # scope_exclusion marker; only the embedded immutable audit record names the
    # excluded row and its actual PDF locator.
    if proof.get("scope_exclusion") is not True:
        return False, None, None
    record = proof.get("audit_record")
    if not isinstance(record, dict) or record.get("status") != "scope_mismatch":
        return False, None, None
    try:
        from investment_agent.industry_peers.bank_proofs import (
            BANK_ISSUER_NAMES,
            BANK_RULE,
            validate_bank_scope_exclusion,
        )
        from investment_agent.industry_peers.originals import AnnualOriginal
        from investment_agent.investor.common import ObjectRef

        # Mismatch records have no field proof of their own. A sibling proof
        # supplies only the annual report reference; exclusion remains checked
        # against the actual PDF bytes below.
        source_pair = next(
            (
                (name, candidate)
                for name, candidate in row.verification.items()
                if name != metric and isinstance(candidate, dict) and candidate.get("original_ref")
            ),
            None,
        )
        source_metric, source_proof = source_pair if source_pair else (None, None)
        if (
            not isinstance(source_metric, str)
            or not isinstance(source_proof, dict)
            or source_proof.get("check_rule_version") != BANK_RULE
        ):
            return False, None, None
        ref = ObjectRef.model_validate(source_proof["original_ref"])
        original_row = await session.get(DisclosureOriginal, str(ref.id))
        if (
            ref.kind != "disclosure_original"
            or original_row is None
            or original_row.owner_id != "local_owner"
            or original_row.payload.get("listing_id") != row.listing_id
        ):
            return False, None, None
        if (
            ref.kind != "disclosure_original"
            or ref.revision != 1
            or original_row.payload.get("ticker")
            != source_proof.get("report_identity", {}).get("code")
            or original_row.payload.get("disclosure_id")
            != source_proof.get("report_identity", {}).get("disclosure_id")
            or source_proof.get("report_identity", {}).get("period")
            != row.report_period.isoformat()
        ):
            return False, None, None
        blob = await session.get(DisclosureBlob, original_row.content_hash)
        if blob is None:
            return False, None, None
        if hashlib.sha256(blob.body).hexdigest() != original_row.content_hash:
            return False, None, None
        published = datetime.fromisoformat(original_row.payload["first_published_at"])
        original = AnnualOriginal(
            code=original_row.payload["ticker"],
            period=row.report_period,
            body=blob.body,
            metadata={
                "chosen": {
                    "announcementId": original_row.payload["disclosure_id"],
                    "secName": BANK_ISSUER_NAMES[original_row.payload["ticker"]],
                }
            },
            pdf_hash=original_row.content_hash,
            published_at=published,
        )
        # This re-read checks owner, same listing and issuer ticker, reporting
        # period, filing identity, publication/acquired/available/check times,
        # and the actual SHA-256 of the stored annual-report bytes.
        if not await validate_field_original(session, row, source_metric, cutoff=captured_at):
            return False, None, None
        validate_bank_scope_exclusion(original, metric, record)
        return True, copy.deepcopy(proof), copy.deepcopy(record)
    except (KeyError, TypeError, ValueError, ImportError):
        return False, None, None


def _supplier_matches_exclusion(field: PeerAnnualField, record: dict[str, Any]) -> bool:
    """Require the current supplier's printed value and unit to match exclusion."""
    return (
        field.value is not None
        and field.raw_unit == record.get("raw_unit") == "percent"
        and _same_value(field.raw_value, record.get("raw_value"))
    )


def _supplier_metadata(field: PeerAnnualField, proof: dict[str, Any]) -> dict[str, Any]:
    """Retain all source-version facts when the canonical field uses annual data."""
    return {
        "provider": proof.get("provider")
        or proof.get("verification_source")
        or "eastmoney_bank_annual",
        "source_field": field.source_field,
        "source_version": field.source_version,
        "source_notice_date": proof.get("source_notice_date")
        or (field.disclosed_on.isoformat() if field.disclosed_on else None),
        "source_update_date": proof.get("source_update_date")
        or (field.updated_on.isoformat() if field.updated_on else None),
        "source_response_sha256": proof.get("source_response_sha256") or proof.get("response_hash"),
        "raw_value": field.raw_value,
        "raw_unit": field.raw_unit,
        "parsed_value": str(field.value) if field.value is not None else None,
        "missing_reason": field.missing_reason,
    }


async def reconcile_bank_online_fields(
    session: AsyncSession,
    *,
    listing_id: int,
    company_id: int,
    report_period: date,
    fields: dict[str, PeerAnnualField | dict[str, Any]],
    proofs: dict[str, dict[str, Any]],
    captured_at: datetime,
) -> tuple[dict[str, PeerAnnualField], dict[str, dict[str, Any]], dict[str, Any]]:
    """Return reconciled candidate fields/proofs and an auditable review summary.

    Existing snapshots are append-only.  A changed online value remains a
    candidate and receives a review record.  An unchanged value is verified
    only when its same-listing, same-period annual-report proof passes a fresh
    DB byte readback.  Parent equity can be carried forward from that verified
    annual-report proof even when the online adapter has no equity field.
    """
    captured_at = _aware(captured_at)
    normalized_fields = {
        metric: value
        if isinstance(value, PeerAnnualField)
        else PeerAnnualField.model_validate(value)
        for metric, value in fields.items()
    }
    normalized_proofs = copy.deepcopy(proofs)
    if set(normalized_fields) != set(normalized_proofs):
        raise ValueError("bank online fields and proofs must have identical metric keys")
    for metric, field in normalized_fields.items():
        if field.metric != metric:
            raise ValueError(f"{metric}: field metric key mismatch")
        if field.period.end != report_period:
            raise ValueError(f"{metric}: online field period mismatch")
        if not isinstance(normalized_proofs[metric], dict):
            raise ValueError(f"{metric}: proof must be an object")
    if report_period.month != 12 or report_period.day != 31:
        return (
            normalized_fields,
            normalized_proofs,
            {"status": "review_needed", "reason": "non_fiscal_year_end"},
        )

    result_fields = dict(normalized_fields)
    result_proofs = normalized_proofs
    summary: dict[str, Any] = {"baseline_snapshot_id": None, "metrics": {}, "review_needed": []}
    rows = list(
        (
            await session.scalars(
                select(PeerAnnualSnapshot)
                .where(
                    PeerAnnualSnapshot.company_id == company_id,
                    PeerAnnualSnapshot.report_period == report_period,
                    PeerAnnualSnapshot.listing_id == listing_id,
                    PeerAnnualSnapshot.captured_at <= captured_at,
                )
                .order_by(PeerAnnualSnapshot.captured_at.desc(), PeerAnnualSnapshot.id.desc())
            )
        ).all()
    )
    latest_source = rows[0] if rows else None
    if latest_source is not None:
        summary["latest_source_snapshot_id"] = latest_source.id
        summary["latest_source_provider"] = latest_source.provider
        summary["latest_source_adapter_version"] = latest_source.adapter_version
    if not rows:
        for metric, proof in result_proofs.items():
            proof["reconciliation"] = {
                "status": "review_needed",
                "reason": "no_same_listing_annual_baseline",
            }
            summary["review_needed"].append(metric)
        summary["status"] = "review_needed"
        return result_fields, result_proofs, summary

    for metric, online in list(result_fields.items()):
        # First authenticate a scope exclusion.  Its field is intentionally
        # missing, and the audit marker/value live in the immutable proof.
        exclusion_result: tuple[PeerAnnualSnapshot, dict[str, Any], dict[str, Any]] | None = None
        for candidate in rows:
            candidate_proof = candidate.verification.get(metric)
            if isinstance(candidate_proof, dict) and candidate_proof.get("scope_exclusion") is True:
                result = await _verified_scope_exclusion(
                    session, candidate, metric, captured_at=captured_at
                )
                if result[0] and result[1] is not None and result[2] is not None:
                    exclusion_result = (candidate, result[1], result[2])
                    break
        if exclusion_result is not None:
            exclusion_row, exclusion_proof, exclusion_record = exclusion_result
            if _supplier_matches_exclusion(online, exclusion_record):
                checked = _timestamp(captured_at)
                source_proof = copy.deepcopy(exclusion_proof)
                source_proof["supplier_comparison"] = {
                    **_supplier_metadata(online, result_proofs[metric]),
                    "matched_excluded_value": True,
                    "checked_at": checked,
                }
                source_proof["reconciliation"] = {
                    "status": "scope_exclusion_revalidated",
                    "audit_value": exclusion_record.get("raw_value"),
                    "source_report_period": report_period.isoformat(),
                }
                result_fields[metric] = online.model_copy(
                    update={
                        "value": None,
                        "verification": PeerFieldVerification.CANDIDATE,
                        "missing_reason": "原件已确认本行口径，不参与集团口径比较",
                    }
                )
                result_proofs[metric] = source_proof
                summary["metrics"][metric] = {
                    "status": "scope_excluded",
                    "source_snapshot_id": exclusion_row.id,
                }
                continue
            reason = (
                "supplier_scope_exclusion_value_changed"
                if online.value is not None
                else "supplier_scope_exclusion_value_missing"
            )
            result_proofs[metric]["reconciliation"] = {
                "status": "review_needed",
                "reason": reason,
                "old_value": exclusion_record.get("raw_value"),
                "old_unit": exclusion_record.get("raw_unit"),
                "new_value": online.raw_value,
                "new_unit": online.raw_unit,
            }
            summary["review_needed"].append(metric)
            summary["metrics"][metric] = {"status": "excluded_candidate", "reason": reason}
            continue

        # Search past online-candidate snapshots for a still-valid trusted
        # annual original.  Each candidate is bounded by its own captured time
        # and the new run cutoff; future snapshots never enter this list.
        trusted: tuple[PeerAnnualSnapshot, dict[str, Any], dict[str, Any]] | None = None
        saw_unreadable = False
        for candidate in rows:
            old_field_data = candidate.fields.get(metric)
            old_proof = candidate.verification.get(metric)
            if (
                not isinstance(old_field_data, dict)
                or not isinstance(old_proof, dict)
                or old_field_data.get("verification") != PeerFieldVerification.VERIFIED.value
            ):
                continue
            if await _verified_baseline(session, candidate, metric, captured_at):
                trusted = (candidate, old_field_data, old_proof)
                break
            saw_unreadable = True

        if trusted is None:
            reason = (
                "baseline_original_failed_byte_readback"
                if saw_unreadable
                else "no_verified_same_year_original_baseline"
            )
            result_proofs[metric]["reconciliation"] = {"status": "review_needed", "reason": reason}
            summary["review_needed"].append(metric)
            summary["metrics"][metric] = {"status": "review_needed", "reason": reason}
            continue

        baseline, old_field_data, old_proof = trusted
        summary.setdefault("baseline_snapshot_by_metric", {})[metric] = baseline.id
        if metric == "equity_parent" and online.value is None:
            inherited = PeerAnnualField.model_validate(old_field_data).model_copy(
                update={"acquired_at": captured_at}
            )
            result_fields[metric] = inherited
            proof = copy.deepcopy(old_proof)
            checked = _timestamp(captured_at)
            proof["checked_at"] = checked
            audit = proof.get("audit_record")
            if isinstance(audit, dict):
                audit["checked_at"] = checked
            proof["supplier_comparison"] = {
                **_supplier_metadata(online, result_proofs[metric]),
                "matched": None,
                "checked_at": checked,
            }
            proof["reconciliation"] = {
                "status": "annual_report_equity_reused",
                "source": "same_listing_annual_report",
            }
            result_proofs[metric] = proof
            summary["metrics"][metric] = {
                "status": "annual_report_equity_reused",
                "original_ref": proof.get("original_ref"),
            }
            continue

        old_value = old_field_data.get("value")
        if _same_value(online.value, old_value):
            checked = _timestamp(captured_at)
            proof = copy.deepcopy(old_proof)
            proof["checked_at"] = checked
            audit = proof.get("audit_record")
            if isinstance(audit, dict):
                audit["checked_at"] = checked
            proof["supplier_comparison"] = {
                **_supplier_metadata(online, result_proofs[metric]),
                "matched": True,
                "checked_at": checked,
            }
            result_fields[metric] = PeerAnnualField.model_validate(old_field_data).model_copy(
                update={
                    "acquired_at": captured_at,
                    "verification": PeerFieldVerification.VERIFIED,
                }
            )
            result_proofs[metric] = proof
            summary["metrics"][metric] = {
                "status": "matched_original",
                "original_ref": proof.get("original_ref"),
                "response_hash": proof["supplier_comparison"]["source_response_sha256"],
            }
        else:
            change = {
                "old_value": old_value,
                "new_value": str(online.value) if online.value is not None else None,
            }
            result_proofs[metric]["original_comparison"] = {
                "status": "missing" if online.value is None else "mismatch",
                "original_ref": old_proof.get("original_ref"),
                "pdf_hash": old_proof.get("pdf_hash"),
                "accounting_scope": old_proof.get("accounting_scope"),
                **change,
                "period": report_period.isoformat(),
                "metric": metric,
            }
            result_proofs[metric]["reconciliation"] = {
                "status": "review_needed",
                "reason": "online_value_missing"
                if online.value is None
                else "online_value_changed",
                **change,
            }
            summary["review_needed"].append(metric)
            summary["metrics"][metric] = {
                "status": "missing_review_needed"
                if online.value is None
                else "changed_review_needed",
                **change,
            }

    summary["status"] = "review_needed" if summary["review_needed"] else "reconciled"
    return result_fields, result_proofs, summary
