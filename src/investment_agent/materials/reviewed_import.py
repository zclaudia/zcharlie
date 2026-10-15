"""Import a fully reviewed annual evidence package into immutable materials.

The manifest remains an evidence index: every import first re-runs the offline
package verifier against the original bytes. Validated issuer facts are stored
as canonical fact payloads, while derived reconciliations remain separate
material sources with their complete calculation inputs in the review envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import ControlledIdentity
from investment_agent.materials.annual_validation import verify_annual_package
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    DatasetDraft,
    DatasetKind,
    FactPeriod,
    FactRecord,
    PeriodBasis,
    SubjectKind,
    Verification,
    fact_from_payload,
    fact_to_payload,
    validate_draft,
)
from investment_agent.materials.equity_reconciliation import evaluate_equity_reconciliation
from investment_agent.materials.store import MaterialStore, SavedDataset
from investment_agent.storage.models.reference import Listing

SOURCE_REVIEWED_ANNUAL = "issuer.reviewed_annual.v1"
SOURCE_EQUITY_RECONCILIATION = "material.equity_reconciliation"
ADAPTER_VERSION = "1"
REVIEWED_RAW_ROOT = PurePosixPath("reviewed-annual/sources")


@dataclass(frozen=True)
class ReviewedCompanyImport:
    """Import outcome for one controlled issuer."""

    company_key: str
    status: str
    imported: bool
    saved: tuple[SavedDataset, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewedAnnualImportReport:
    """Package hash and per-company offline import outcomes."""

    manifest_sha256: str
    companies: tuple[ReviewedCompanyImport, ...]
    problems: tuple[str, ...] = ()

    @property
    def saved(self) -> tuple[SavedDataset, ...]:
        return tuple(item for company in self.companies for item in company.saved)


async def import_reviewed_annual(
    session: AsyncSession,
    *,
    manifest_path: Path,
    source_root: Path,
    data_dir: Path,
    identities: Sequence[ControlledIdentity],
    company_ids: Mapping[str, int],
    imported_at: datetime | None = None,
) -> ReviewedAnnualImportReport:
    """Verify and import complete companies from a reviewed annual package.

    Verification always uses the validator's default all-controlled scope and
    manifest date. Partial companies are returned with their issues and write
    no datasets. The caller owns the surrounding transaction; nested
    savepoints protect each complete company without committing the session.
    """

    manifest_path, source_root, data_dir = map(Path, (manifest_path, source_root, data_dir))
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ReviewedAnnualImportReport(
            manifest_sha256="",
            companies=(),
            problems=(f"cannot read annual manifest: {type(exc).__name__}: {exc}",),
        )

    # No subset/as-of override: package validation must cover every controlled
    # identity using the manifest's checked_on date.
    verification = verify_annual_package(manifest_path, source_root)
    verification_rows = {
        row["company_key"]: row
        for row in verification.get("companies", [])
        if isinstance(row, Mapping) and isinstance(row.get("company_key"), str)
    }
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("companies"), list):
        return ReviewedAnnualImportReport(
            manifest_sha256,
            (),
            ("annual manifest companies must be a list",),
        )
    entries = {
        row["company_key"]: row
        for row in manifest["companies"]
        if isinstance(row, Mapping) and isinstance(row.get("company_key"), str)
    }
    identity_by_key = {identity.company_key: identity for identity in identities}
    captured_at = _aware_utc(imported_at or datetime.now(UTC))
    package_problems = [str(item) for item in verification.get("issues", [])]
    checked_on: date | None = None
    as_of = verification.get("as_of")
    if isinstance(as_of, str):
        with suppress(ValueError):
            checked_on = date.fromisoformat(as_of)
    if checked_on is None and not package_problems:
        package_problems.append("all-controlled verification returned no valid as_of date")
    if verification.get("scope") != "all_controlled":
        package_problems.append("annual package was not verified in the all-controlled scope")
    if checked_on is not None and captured_at.date() < checked_on:
        package_problems.append(
            f"import timestamp {captured_at.isoformat()} precedes checked_on {checked_on.isoformat()}"
        )
    store = MaterialStore(session, data_dir=data_dir)
    results: list[ReviewedCompanyImport] = []

    for company_key, identity in identity_by_key.items():
        validation = verification_rows.get(company_key)
        issues = tuple(package_problems) + tuple(
            str(item) for item in (validation or {}).get("issues", [])
        )
        status = str((validation or {}).get("status", "partial"))
        if validation is None:
            issues += ("company is absent from default all-controlled verification scope",)
            status = "partial"
        if (
            package_problems
            or verification.get("scope") != "all_controlled"
            or status != "complete"
        ):
            results.append(ReviewedCompanyImport(company_key, "partial", False, problems=issues))
            continue
        assert validation is not None
        assert checked_on is not None

        entry = entries.get(company_key)
        subject_id = company_ids.get(company_key)
        if entry is None or not isinstance(subject_id, int) or isinstance(subject_id, bool) or subject_id <= 0:
            results.append(
                ReviewedCompanyImport(
                    company_key,
                    "partial",
                    False,
                    problems=("complete validation has no manifest entry or positive company id",),
                )
            )
            continue
        try:
            if not await _company_id_matches_identity(session, subject_id, identity):
                raise ValueError(
                    "company id does not have a listing matching the controlled market and ticker"
                )
            drafts, document_copies = _company_drafts(
                entry=entry,
                validation=validation,
                identity=identity,
                subject_id=subject_id,
                manifest_sha256=manifest_sha256,
                checked_on=checked_on,
                source_root=source_root,
                captured_at=captured_at,
            )
            draft_problems = tuple(problem for draft in drafts for problem in validate_draft(draft))
            if draft_problems:
                raise ValueError("; ".join(draft_problems))
            _copy_verified_documents(data_dir, document_copies)
            async with session.begin_nested():
                saved = tuple([await store.save(draft) for draft in drafts])
            results.append(ReviewedCompanyImport(company_key, "complete", True, saved=saved))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            results.append(
                ReviewedCompanyImport(
                    company_key,
                    "partial",
                    False,
                    problems=(f"import preparation failed: {type(exc).__name__}: {exc}",),
                )
            )

    return ReviewedAnnualImportReport(manifest_sha256, tuple(results), tuple(package_problems))


def _company_drafts(
    *,
    entry: Mapping[str, Any],
    validation: Mapping[str, Any],
    identity: ControlledIdentity,
    subject_id: int,
    manifest_sha256: str,
    checked_on: date,
    source_root: Path,
    captured_at: datetime,
) -> tuple[tuple[DatasetDraft, ...], tuple[tuple[Path, Path, str], ...]]:
    latest = entry.get("latest_annual")
    if not isinstance(latest, Mapping):
        raise ValueError("latest_annual missing after complete verification")
    latest_start = _date(latest.get("period_start"), "latest_annual.period_start")
    latest_end = _date(latest.get("period_end"), "latest_annual.period_end")
    latest_published = _optional_date(latest.get("published_on"))
    if entry.get("report_currency") != identity.report_currency:
        raise ValueError("manifest report currency differs from controlled identity")

    doc_rows = entry.get("documents")
    if not isinstance(doc_rows, list):
        raise ValueError("documents must be a list after complete verification")
    document_payloads: list[dict[str, Any]] = []
    document_copies: list[tuple[Path, Path, str]] = []
    doc_id_to_ref: dict[str, str] = {}
    for doc in doc_rows:
        if not isinstance(doc, Mapping):
            raise ValueError("document entry is malformed")
        doc_id, rel, digest = doc.get("id"), doc.get("path"), doc.get("sha256")
        if not isinstance(doc_id, str) or not isinstance(rel, str) or not isinstance(digest, str):
            raise ValueError("document id/path/sha256 is malformed")
        source_path = _safe_source_path(source_root, rel)
        content = source_path.read_bytes()
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != digest:
            raise ValueError(f"document {doc_id} changed after package verification")
        target_rel = REVIEWED_RAW_ROOT / digest[:2] / f"{digest}.blob"
        target = Path(target_rel.as_posix())
        doc_id_to_ref[doc_id] = target_rel.as_posix()
        doc_payload = {
            key: doc.get(key)
            for key in (
                "id", "url", "sha256", "bytes", "captured_at", "http_status",
                "media_type", "published_on", "role",
            )
        }
        # Preserve the manifest's relative locator for audit while keeping all
        # application reads pointed at the copied, durable raw_ref.
        doc_payload["manifest_path"] = rel
        doc_payload["raw_ref"] = target_rel.as_posix()
        document_payloads.append(doc_payload)
        document_copies.append((source_path, target, digest))

    fact_entries, evidence_index = _collect_fact_entries(entry)
    serialized_facts: list[dict[str, Any]] = []
    uniqueness: dict[tuple[str, date], tuple[str, FactRecord]] = {}
    equivalent_facts: dict[str, list[dict[str, Any]]] = {}
    for raw_fact, evidence in fact_entries:
        fact = fact_from_payload(raw_fact)
        canonical = fact_to_payload(fact)
        digest = _json_hash(canonical)
        key = _fact_period_identity(fact)
        previous = uniqueness.get(key)
        if previous is None:
            uniqueness[key] = (digest, fact)
            serialized_facts.append(canonical)
            evidence_index.setdefault(digest, []).extend(
                _evidence_reference(row, doc_id_to_ref) for row in evidence
            )
        elif previous[0] == digest:
            evidence_index.setdefault(digest, []).extend(
                _evidence_reference(row, doc_id_to_ref) for row in evidence
            )
        else:
            selected_digest, selected_fact = previous
            if not _equivalent_annual_copy(selected_fact, fact):
                raise ValueError(
                    f"ambiguous duplicate {fact.metric} fact at {fact.period.end.isoformat()}"
                )
            equivalent_facts.setdefault(selected_digest, []).append(
                {
                    "fact_sha256": digest,
                    "fact": canonical,
                    "references": [
                        _evidence_reference(row, doc_id_to_ref) for row in evidence
                    ],
                }
            )

    # Keep evidence stable and deduplicated when the latest-year fields also
    # appear in fiscal_years, or a copied opening fact equals prior closing.
    fact_evidence = []
    for digest in sorted(evidence_index):
        references = _dedupe_json(evidence_index[digest])
        item = {
            "fact_sha256": digest,
            "hash_kind": "fact_payload_sha256",
            "references": references,
        }
        variants = _dedupe_json(equivalent_facts.get(digest, []))
        if variants:
            item["equivalent_facts"] = variants
        fact_evidence.append(item)

    review = {
        "schema_version": 1,
        "company_key": identity.company_key,
        "manifest_sha256": manifest_sha256,
        "checked_on": checked_on.isoformat(),
        "status": "complete",
        "latest_period_end": latest_end.isoformat(),
        "latest_period_start": latest_start.isoformat(),
        "latest_published_on": None if latest_published is None else latest_published.isoformat(),
        "issues": list(validation.get("issues", [])),
        "documents": document_payloads,
        "fact_evidence": fact_evidence,
        "superseded_facts": _raw_entries(entry.get("superseded_facts", [])),
        "equity_reconciliations": _raw_entries(entry.get("equity_reconciliations", [])),
        "equity_scope_reviews": _raw_entries(entry.get("equity_scope_reviews", [])),
        "conflict_resolutions": _raw_entries(entry.get("conflicts", [])),
    }
    evidence_index_bytes = _canonical_json_bytes(
        {
            "artifact_kind": "reviewed-annual-evidence-index",
            "description": "Local index of verified captured issuer documents; not an upstream response.",
            "company_key": identity.company_key,
            "manifest_sha256": manifest_sha256,
            "documents": document_payloads,
        }
    )
    annual_draft = DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=subject_id,
        source=SOURCE_REVIEWED_ANNUAL,
        schema_version=SCHEMA_VERSION,
        adapter_version=ADAPTER_VERSION,
        captured_at=captured_at,
        source_fetched_at=None,
        payload={"facts": serialized_facts, "review": review},
        period=FactPeriod(
            latest_start,
            latest_end,
            PeriodBasis.ANNUAL,
            label=str(latest.get("label") or f"FY{latest_end.year}"),
        ),
        raw_bytes=evidence_index_bytes,
        raw_media_type="application/json",
        raw_source_url=None,
    )
    # Derived facts are kept in the same reviewed package dataset, but their
    # provenance source remains distinct and they receive calculation evidence
    # instead of a fabricated direct-source excerpt.
    reconciliations = entry.get("equity_reconciliations", [])
    if reconciliations:
        years = entry.get("fiscal_years", [])
        for record in reconciliations:
            if not isinstance(record, Mapping):
                raise ValueError("equity reconciliation record is malformed")
            evaluated = evaluate_equity_reconciliation(record)
            ref = evaluated["calculation_ref"]
            derived_original = _find_reconciliation_opening_fact(years, ref)
            derived_record = fact_from_payload(derived_original)
            derived_record = replace(
                derived_record,
                provenance=replace(
                    derived_record.provenance,
                    source=SOURCE_EQUITY_RECONCILIATION,
                ),
                verification=Verification.VERIFIED,
            )
            output = evaluated["output"]
            if (
                derived_record.value.raw_value != output["value"]
                or derived_record.period.end.isoformat() != output["period_end"]
                or derived_record.metric != output["metric"]
            ):
                raise ValueError(f"{ref} derived fact disagrees with validated calculation")
            derived_payload = fact_to_payload(derived_record)
            derived_hash = _json_hash(derived_payload)
            serialized_facts.append(derived_payload)
            fact_evidence.append(
                {
                    "fact_sha256": derived_hash,
                    "hash_kind": "fact_payload_sha256",
                    "references": [
                        {
                            "kind": "derived_calculation",
                            "calculation_ref": ref,
                            "calculation_input_hash": derived_record.provenance.original_hash,
                        }
                    ],
                }
            )

    # Ensure facts and evidence retain a deterministic order regardless of
    # whether the same fact was encountered through latest and fiscal-year paths.
    review["fact_evidence"] = fact_evidence
    annual_draft = replace(annual_draft, payload={"facts": serialized_facts, "review": review})
    return (annual_draft,), tuple(document_copies)


def _collect_fact_entries(
    entry: Mapping[str, Any],
) -> tuple[list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]], dict[str, list[dict[str, Any]]]]:
    collected: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []

    def collect_field(field: object, *, where: str) -> None:
        if not isinstance(field, Mapping) or not isinstance(field.get("fact"), Mapping):
            raise ValueError(f"{where} fact entry is malformed after complete verification")
        evidence = field.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError(f"{where} evidence is malformed after complete verification")
        collected.append((field["fact"], [evidence]))

    fields = entry.get("fields")
    years = entry.get("fiscal_years")
    if not isinstance(fields, list) or not isinstance(years, list):
        raise ValueError("fields and fiscal_years must be lists after complete verification")
    for index, field in enumerate(fields):
        collect_field(field, where=f"fields[{index}]")
    for year_index, year in enumerate(years):
        if not isinstance(year, Mapping) or not isinstance(year.get("facts"), list):
            raise ValueError(f"fiscal_years[{year_index}] is malformed")
        for fact_index, field in enumerate(year["facts"]):
            collect_field(field, where=f"fiscal_years[{year_index}].facts[{fact_index}]")
        beginning = year.get("beginning_equity_fact")
        if isinstance(beginning, Mapping) and beginning.get("calculation_ref"):
            # A validated calculation output is reconstructed below with its
            # calculation source and reference; it is not an issuer disclosure.
            continue
        collect_field(beginning, where=f"fiscal_years[{year_index}].beginning_equity_fact")
    return collected, {}


def _find_reconciliation_opening_fact(years: object, calculation_ref: str) -> Mapping[str, Any]:
    if not isinstance(years, list):
        raise ValueError("fiscal_years missing for derived equity reconciliation")
    for year in years:
        if not isinstance(year, Mapping):
            continue
        beginning = year.get("beginning_equity_fact")
        if isinstance(beginning, Mapping) and beginning.get("calculation_ref") == calculation_ref:
            fact = beginning.get("fact")
            if isinstance(fact, Mapping):
                return fact
    raise ValueError(f"validated calculation {calculation_ref} has no selected opening fact")


def _evidence_reference(row: Mapping[str, Any], doc_id_to_ref: Mapping[str, str]) -> dict[str, Any]:
    doc_id = row.get("document_id")
    if not isinstance(doc_id, str) or doc_id not in doc_id_to_ref:
        raise ValueError("fact evidence references an uncopied source document")
    return {
        "document_id": doc_id,
        "raw_ref": doc_id_to_ref[doc_id],
        "locator": row.get("locator"),
        "excerpt": row.get("excerpt"),
        "source_label": row.get("source_label"),
    }


def _fact_period_identity(fact: FactRecord) -> tuple[str, date]:
    return fact.metric, fact.period.end


def _equivalent_annual_copy(left: FactRecord, right: FactRecord) -> bool:
    """Recognize a repeated closing/opening balance without accepting aliases."""

    return (
        left.period.start == right.period.start
        and left.period.end == right.period.end
        and left.period.basis == right.period.basis
        and left.period.days == right.period.days
        and left.semantics.metric == right.semantics.metric
        and left.semantics.accounting_standard == right.semantics.accounting_standard
        and left.semantics.reporting_scope == right.semantics.reporting_scope
        and left.semantics.equity_scope == right.semantics.equity_scope
        and left.semantics.per_share_basis == right.semantics.per_share_basis
        and left.value == right.value
        and left.provenance.source == right.provenance.source
        and left.verification == right.verification
        and left.conflict == right.conflict
        and left.applicability == right.applicability
        and left.conflict_note == right.conflict_note
    )


def _safe_source_path(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not posix.parts or ".." in posix.parts or "\\" in relative:
        raise ValueError(f"unsafe source document path: {relative!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / Path(*posix.parts)).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"source document path escapes source_root: {relative!r}")
    return resolved


def _copy_verified_documents(
    data_dir: Path, copies: Sequence[tuple[Path, Path, str]]
) -> None:
    root = data_dir.resolve()
    for source, relative_target, expected_digest in copies:
        target = (root / relative_target).resolve()
        if not target.is_relative_to(root):
            raise ValueError("reviewed source target escapes data_dir")
        if target.stem != expected_digest or len(expected_digest) != 64:
            raise ValueError("reviewed source target does not match its expected SHA-256")
        if target.exists():
            existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing_digest != expected_digest:
                raise ValueError(f"existing immutable reviewed source has wrong hash: {target}")
            continue
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ValueError(f"source changed after verification; refusing to copy: {source}")
        _write_atomically(target, content)


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _raw_entries(value: object) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [item for item in value]


def _dedupe_json(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[bytes] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        encoded = _canonical_json_bytes(row)
        if encoded not in seen:
            seen.add(encoded)
            result.append(row)
    return result


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _date(value: object, where: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{where} must be an ISO date") from exc


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("latest_annual.published_on must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("latest_annual.published_on must be an ISO date or null") from exc


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")
    return value.astimezone(UTC)


async def _company_id_matches_identity(
    session: AsyncSession, subject_id: int, identity: ControlledIdentity
) -> bool:
    result = await session.execute(
        select(Listing.id).where(
            Listing.company_id == subject_id,
            Listing.market_code == identity.market_code,
            Listing.ticker == identity.ticker,
        )
    )
    return result.scalar_one_or_none() is not None
