"""Manage bank annual original acquisition and field review rollover."""

# Typer declares command options in parameter defaults by design.
# ruff: noqa: B008
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.cli.runtime import bootstrap_settings
from investment_agent.industry_peers.bank_annual_review import (
    FIELDS,
    OWNER,
    append_review_event,
    build_rollover_snapshot,
    latest_same_year_snapshot,
    list_pending_bank_annual_reviews,
    resolve_bank_annual_review,
    review_id,
)
from investment_agent.industry_peers.hashing import canonical_json
from investment_agent.industry_peers.metric_packs import BANK_PACK, cohort_metric_pack
from investment_agent.storage.database import run_with_engine, session_scope
from investment_agent.storage.models.disclosure import DisclosureOriginal
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.models.reference import Listing

app = typer.Typer(help="Acquire and review bank annual original reports")
reviews_app = typer.Typer(help="Persisted bank field reviews")
app.add_typer(reviews_app, name="reviews")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stamp(value: str | None) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(UTC)


def _check_audit_envelope(audit: Any, *, listing: Listing, report_period: date) -> None:
    if not isinstance(audit, dict) or audit.get("schema") != "bank-annual-review-audit/2":
        raise ValueError("audit schema must be bank-annual-review-audit/2")
    if (
        audit.get("listing_id") != listing.id
        or audit.get("code") != listing.ticker
        or audit.get("report_period") != report_period.isoformat()
    ):
        raise ValueError("audit code/listing/year does not match selected trusted cohort member")
    if not isinstance(audit.get("fields"), dict):
        raise ValueError("audit.fields must be a metric-keyed object")


async def _verified_review_baseline(
    session: AsyncSession, *, listing: Listing, report_period: date, cutoff: datetime, metric: str
) -> PeerAnnualSnapshot | None:
    """Find the latest same-year bank field whose original still validates."""
    from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
    from investment_agent.industry_peers.originals import validate_field_original

    rows = (
        await session.scalars(
            select(PeerAnnualSnapshot)
            .where(
                PeerAnnualSnapshot.listing_id == listing.id,
                PeerAnnualSnapshot.company_id == listing.company_id,
                PeerAnnualSnapshot.report_period == report_period,
                PeerAnnualSnapshot.captured_at <= cutoff,
                PeerAnnualSnapshot.provider == "eastmoney",
                PeerAnnualSnapshot.adapter_version == "bank-annual-v1",
            )
            .order_by(PeerAnnualSnapshot.captured_at.desc(), PeerAnnualSnapshot.id.desc())
        )
    ).all()
    for row in rows:
        raw = row.fields.get(metric)
        proof = row.verification.get(metric)
        if not isinstance(raw, dict) or not isinstance(proof, dict):
            continue
        try:
            field = PeerAnnualField.model_validate(raw)
        except (TypeError, ValueError):
            continue
        if field.verification != PeerFieldVerification.VERIFIED:
            continue
        if await validate_field_original(session, row, metric, cutoff=cutoff):
            return row
    return None


async def _eligible(
    session: AsyncSession, cohort_key: str, listing_id: int, period: date
) -> tuple[Listing, PeerCohortSnapshot]:
    cohorts = (
        await session.scalars(
            select(PeerCohortSnapshot)
            .where(
                PeerCohortSnapshot.cohort_key == cohort_key,
            )
            .order_by(PeerCohortSnapshot.revision.desc())
        )
    ).all()
    cohort = next((row for row in cohorts if cohort_metric_pack(row.payload) == BANK_PACK), None)
    if cohort is None:
        raise ValueError(f"no trusted bank cohort {cohort_key!r} for {period.year}")
    entries = cohort.payload.get("entries", [])
    if not any(isinstance(item, dict) and item.get("listing_id") == listing_id for item in entries):
        raise ValueError("listing is not in the selected trusted bank cohort")
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise ValueError("listing_id does not exist")
    return listing, cohort


@app.command("acquire")
def acquire(
    cohort: str = typer.Option(..., "--cohort"),
    listing_id: int = typer.Option(..., "--listing-id"),
    period: str = typer.Option(..., "--period"),
    acquired_at: str | None = typer.Option(None, "--acquired-at"),
    data_dir: Path = typer.Option(Path("data/bank-annual"), "--data-dir"),
    refresh: bool = typer.Option(False, "--refresh"),
) -> None:
    """Explicitly acquire official original bytes for one cohort member."""
    from investment_agent.industry_peers.bank_annual_sources import acquire_bank_annual_originals

    report_period = date.fromisoformat(period)
    stamp = _stamp(acquired_at)

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            listing, _ = await _eligible(session, cohort, listing_id, report_period)
            return await acquire_bank_annual_originals(
                session,
                listing_id=listing.id,
                report_period=report_period,
                acquired_at=stamp,
                data_dir=data_dir,
                refresh=refresh,
            )

    result = run_with_engine(bootstrap_settings().database_url, main)
    typer.echo(json.dumps(result, ensure_ascii=False, default=str, indent=2))


@app.command("prepare")
def prepare(
    cohort: str = typer.Option(..., "--cohort"),
    listing_id: int = typer.Option(..., "--listing-id"),
    period: str = typer.Option(..., "--period"),
    output: Path = typer.Option(..., "--output"),
) -> None:
    """Write a fillable per-field audit template and report original gaps."""
    report_period = date.fromisoformat(period)

    async def main() -> tuple[dict[str, Any], list[str], list[str]]:
        async with session_scope() as session:
            listing, _ = await _eligible(session, cohort, listing_id, report_period)
            rows = (
                await session.scalars(
                    select(PeerAnnualSnapshot)
                    .where(
                        PeerAnnualSnapshot.listing_id == listing.id,
                        PeerAnnualSnapshot.report_period == report_period,
                    )
                    .order_by(PeerAnnualSnapshot.captured_at.desc())
                )
            ).all()
            originals = await session.scalars(
                select(DisclosureOriginal).where(
                    DisclosureOriginal.owner_id == OWNER,
                )
            )
            refs = [
                {
                    "kind": "disclosure_original",
                    "id": r.id,
                    "revision": 1,
                    "content_hash": r.content_hash,
                    "title": r.payload.get("title"),
                    "acquired_at": r.payload.get("acquired_at"),
                    "source_url": r.payload.get("source_url"),
                    "disclosure_id": r.payload.get("disclosure_id"),
                }
                for r in originals
                if r.payload.get("listing_id") == listing.id
                and str(report_period.year) in str(r.payload.get("title", ""))
            ]
            fields = {
                metric: {
                    "metric": metric,
                    "check_rule_version": "bank-annual-original/2",
                    "status": "candidate",
                    "period": period,
                    "report_identity": {
                        "code": listing.ticker,
                        "period": period,
                        "disclosure_id": refs[0]["disclosure_id"] if refs else None,
                    },
                    "original_ref": (
                        {"kind": refs[0]["kind"], "id": refs[0]["id"], "revision": 1}
                        if refs
                        else None
                    ),
                    "pdf_hash": refs[0]["content_hash"] if refs else None,
                    "source_url": refs[0]["source_url"] if refs else None,
                    "pdf_page": None,
                    "table_title": None,
                    "period_header": None,
                    "current_column": None,
                    "header": None,
                    "raw_value": None,
                    "raw_unit": None,
                    "parsed_value": None,
                    "accounting_scope": None,
                    "locator_excerpt": None,
                    "locator_excerpt_sha256": None,
                    "definition_or_footnote": None,
                    "scope_context": None,
                    "checked_at": None,
                    "acquired_at": refs[0]["acquired_at"] if refs else None,
                    "available_original_refs": refs,
                    "missing_reason": "audit_required",
                }
                for metric in FIELDS
            }
            template = {
                "schema": "bank-annual-review-audit/2",
                "listing_id": listing.id,
                "code": listing.ticker,
                "report_period": period,
                "fields": fields,
            }
            gaps = [m for m in FIELDS if not any(m in row.verification for row in rows)]
            return template, gaps, [r.id for r in rows]

    template, gaps, snapshots = run_with_engine(bootstrap_settings().database_url, main)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(
        json.dumps(
            {"template": str(output), "gaps": gaps, "snapshots": snapshots}, ensure_ascii=False
        )
    )


def _hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@app.command("verify")
def verify(
    cohort: str = typer.Option(..., "--cohort"),
    listing_id: int = typer.Option(..., "--listing-id"),
    period: str = typer.Option(..., "--period"),
    audit: Path = typer.Option(..., "--audit"),
    captured_at: str | None = typer.Option(None, "--captured-at"),
) -> None:
    """Build an append-only bank-annual-v2 snapshot from a local audit."""
    report_period, stamp, data = date.fromisoformat(period), _stamp(captured_at), _json(audit)

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            listing, _ = await _eligible(session, cohort, listing_id, report_period)
            _check_audit_envelope(data, listing=listing, report_period=report_period)
            prior = await latest_same_year_snapshot(
                session, listing_id=listing.id, report_period=report_period, cutoff=stamp
            )
            snap, proofs = await build_rollover_snapshot(
                session,
                listing_id=listing.id,
                company_id=listing.company_id,
                report_period=report_period,
                audit=data,
                captured_at=stamp,
                prior=prior,
            )
            existing = await session.get(PeerAnnualSnapshot, snap.id)
            if existing is None:
                session.add(snap)
            await session.flush()
            reviews = []
            for metric in FIELDS:
                new_field = snap.fields[metric]
                baseline = await _verified_review_baseline(
                    session,
                    listing=listing,
                    report_period=report_period,
                    cutoff=stamp,
                    metric=metric,
                )
                baseline_field = baseline.fields.get(metric, {}) if baseline is not None else {}
                changed = baseline is not None and baseline_field.get("value") != new_field.get(
                    "value"
                )
                if baseline is not None and changed:
                    rid = review_id(
                        listing_id=listing.id,
                        report_period=report_period,
                        metric=metric,
                        old_snapshot_id=baseline.id,
                        candidate_snapshot_id=snap.id,
                        candidate_source_version=new_field.get("source_version"),
                    )
                    await append_review_event(
                        session,
                        review=True,
                        event={
                            "review_id": rid,
                            "event_type": "review",
                            "code": listing.ticker,
                            "listing_id": listing.id,
                            "company_id": listing.company_id,
                            "report_period": period,
                            "metric": metric,
                            "old_snapshot_id": baseline.id,
                            "candidate_snapshot_id": snap.id,
                            "resulting_snapshot_id": snap.id,
                            "old_content_hash": baseline.content_hash,
                            "candidate_content_hash": snap.content_hash,
                            "decision": None,
                            "old_value": baseline_field.get("value"),
                            "candidate_value": new_field.get("value"),
                            "period": new_field.get("period"),
                            "accounting_scope": proofs[metric].get("accounting_scope"),
                            "candidate_source_version": new_field.get("source_version"),
                            "original_refs": [proofs[metric].get("original_ref")]
                            if proofs[metric].get("original_ref")
                            else [],
                            "checked_at": proofs[metric].get("checked_at"),
                            "proof_hashes": {metric: _hash(proofs[metric])},
                            "created_at": _stamp(None).isoformat(),
                        },
                    )
                    reviews.append(rid)
            return {
                "snapshot_id": snap.id,
                "semantic_hash": snap.semantic_hash,
                "reviews": reviews,
                "fields": {
                    metric: {
                        "verification": snap.fields[metric].get("verification"),
                        "value": snap.fields[metric].get("value"),
                        "reason": proofs[metric].get("rejection_reason")
                        or snap.fields[metric].get("missing_reason"),
                    }
                    for metric in FIELDS
                },
            }

    result = run_with_engine(bootstrap_settings().database_url, main)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@reviews_app.command("list")
def list_reviews(
    listing_id: int | None = typer.Option(None, "--listing-id"),
    period: str | None = typer.Option(None, "--period"),
) -> None:
    """List persisted, unresolved field reviews."""
    report_period = date.fromisoformat(period) if period else None

    async def main() -> list[dict[str, Any]]:
        async with session_scope() as session:
            return await list_pending_bank_annual_reviews(
                session, listing_id=listing_id, report_period=report_period
            )

    typer.echo(
        json.dumps(
            run_with_engine(bootstrap_settings().database_url, main), ensure_ascii=False, indent=2
        )
    )


@app.command("resolve")
def resolve(
    review_id_arg: str = typer.Argument(..., metavar="REVIEW_ID"),
    audit: Path = typer.Option(..., "--audit"),
    decision: str = typer.Option(..., "--decision"),
    checked_at: str | None = typer.Option(None, "--checked-at"),
) -> None:
    """Resolve one pending discrepancy after a real v2 original check."""
    if decision not in {"accept_original", "reject_supplier"}:
        raise typer.BadParameter("expected accept_original or reject_supplier")
    audit_data, stamp = _json(audit), _stamp(checked_at)

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            return await resolve_bank_annual_review(
                session,
                review_id_value=review_id_arg,
                audit=audit_data,
                decision=decision,
                checked_at=stamp,
            )

    typer.echo(
        json.dumps(
            run_with_engine(bootstrap_settings().database_url, main), ensure_ascii=False, indent=2
        )
    )
