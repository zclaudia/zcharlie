"""Import bank annual facts with optional explicit, byte-checked field audits.

Supplier-only imports stay candidate. Audited imports retain original reports,
verify each field separately, and exclude proven standalone-scope ratios.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.hashing import SEMANTIC_HASH_SCHEMA, canonical_json
from investment_agent.industry_peers.originals import (
    load_annual_original,
    store_annual_original,
    store_bank_supplemental_original,
)
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

app = typer.Typer(help="Import offline bank annual facts with optional original-report audits")

PROVIDER = "eastmoney"
ADAPTER_VERSION = "offline-run02-v1"

BANK_CODES = (
    "601398.SH",
    "601939.SH",
    "601288.SH",
    "600036.SH",
    "601166.SH",
    "000001.SZ",
    "600919.SH",
    "601009.SH",
    "002142.SZ",
    "601128.SH",
)
FIELDS = {
    "net_interest_margin": ("NET_INTEREST_MARGIN", "净息差", "ratio"),
    "npl_ratio": ("NONPERLOAN", "不良贷款率", "ratio"),
    "provision_coverage": ("BLDKBBL", "拨备覆盖率", "ratio"),
    "roe_weighted_annual": ("ROEJQ", "加权平均ROE", "roe"),
}
MISSING_EQUITY_REASON = (
    "归母净资产未在银行供应商字段中完成口径映射；不得用 NZBJE 或 TOTAL_EQUITY_PK 推断"
)
GROUP_SCOPE_EXCLUSION_REASON = "原件已确认本行口径，不参与集团口径比较"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def _date(value: Any, where: str) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected ISO date text, got {type(value).__name__}")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValueError(f"{where}: invalid source date {value!r}") from exc


def _number(value: Any, where: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{where}: expected finite JSON number or null")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{where}: expected finite JSON number or null")
    return result


def _period(period: date) -> Period:
    return Period(start=date(period.year, 1, 1), end=period, basis="annual_ratio")


def _snapshot_hash(fields: dict[str, PeerAnnualField], proofs: dict[str, dict[str, Any]]) -> str:
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key
                not in {
                    "source_file",
                    "pdf_path",
                    "metadata_path",
                    "source_manifest_ref",
                    "source_manifest",
                    "path",
                    "acquired_at",
                    "originals_root",
                    "evidence_root",
                }
                and not key.endswith("_path")
                and not key.endswith("_root")
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    payload = {
        "schema": SEMANTIC_HASH_SCHEMA,
        "metric_pack": "bank_annual_v1",
        "fields": {
            key: {k: v for k, v in field.model_dump(mode="json").items() if k != "acquired_at"}
            for key, field in fields.items()
        },
        "verification": stable(proofs),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _aware_iso(value: Any, where: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected timezone-aware ISO datetime")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where}: invalid datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{where}: expected timezone-aware ISO datetime")
    return result.astimezone(UTC)


def _read_bank_manifest(
    audit_path: Path,
    originals_root: Path,
    evidence_root: Path,
    report_period: date,
    acquired_at: datetime,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Read fixed audit/source manifests and verify their batch bindings."""
    audit = _json(audit_path)
    if not isinstance(audit, dict) or audit.get("schema") != "bank-annual-original/1":
        raise ValueError("audit schema must be bank-annual-original/1")
    source_ref = audit.get("source_manifest_ref", audit.get("source_manifest"))
    if not isinstance(source_ref, str) or not source_ref:
        raise ValueError("audit source_manifest_ref is required")
    # The audit root pins the source manifest by a workspace-relative name.
    workspace = Path(__file__).resolve().parents[4]
    source_path = (workspace / source_ref).resolve()
    if not source_path.is_file():
        source_path = (originals_root / source_ref).resolve()
    if not source_path.is_file():
        raise ValueError(f"source manifest is missing: {source_ref}")
    source = _json(source_path)
    if not isinstance(source, dict) or source.get("schema") != "bank-proof-source-manifest/1":
        raise ValueError("source manifest schema must be bank-proof-source-manifest/1")
    expected_period = report_period.isoformat()
    if (
        audit.get("report_period") != expected_period
        or source.get("report_period") != expected_period
    ):
        raise ValueError("audit/source manifest report period mismatch")
    rows = audit.get("records")
    if not isinstance(rows, list) or len(rows) != len(BANK_CODES) * 5:
        raise ValueError("audit must contain exactly five field records for each of ten banks")
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    allowed_metrics = set(FIELDS) | {"equity_parent"}
    allowed_statuses = {
        "verified",
        "candidate",
        "missing",
        "supplier_mismatch",
        "scope_mismatch",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"audit record {index}: expected object")
        code, metric = row.get("code"), row.get("metric")
        if code not in BANK_CODES or metric not in allowed_metrics:
            raise ValueError(f"audit record {index}: unknown bank or metric")
        key = (code, metric)
        if key in by_key:
            raise ValueError(f"audit contains duplicate record for {code}.{metric}")
        if row.get("period", row.get("report_period")) != expected_period:
            raise ValueError(f"{code}.{metric}: audit report period mismatch")
        if row.get("status") not in allowed_statuses:
            raise ValueError(f"{code}.{metric}: invalid audit status")
        if row.get("checked_at") is not None:
            checked = _aware_iso(row["checked_at"], f"{code}.{metric}.checked_at")
            if checked > acquired_at.astimezone(UTC):
                raise ValueError(f"{code}.{metric}: audit check is later than import cutoff")
        elif row.get("status") == "verified":
            raise ValueError(f"{code}.{metric}: verified audit requires checked_at")
        by_key[key] = row
    missing = [
        (code, metric)
        for code in BANK_CODES
        for metric in allowed_metrics
        if (code, metric) not in by_key
    ]
    if missing:
        raise ValueError("audit is incomplete: " + ", ".join(f"{c}.{m}" for c, m in missing))
    sources = source.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(BANK_CODES):
        raise ValueError("source manifest must bind the exact fixed ten-bank roster")
    return by_key, {
        "manifest": source,
        "manifest_path": source_path,
        "originals_root": originals_root.resolve(),
        "evidence_root": evidence_root.resolve(),
    }


def _load_manifest_original(code: str, period: date, source_info: dict[str, Any]) -> Any:
    """Load a source-manifest PDF and metadata into the shared original contract."""
    from investment_agent.industry_peers.bank_pdf_text import bank_page_text
    from investment_agent.industry_peers.originals import AnnualOriginal, normalized

    source = source_info["manifest"]["sources"][code]
    workspace = Path(__file__).resolve().parents[4]
    allowed_roots = (source_info["originals_root"], source_info["evidence_root"])
    raw_pdf = Path(str(source.get("pdf_path", "")))
    # Manifests normally carry workspace-relative paths. Also support a
    # relocated evidence root by resolving the suffix after its known folder.
    pdf_candidates = [workspace / raw_pdf, *(root / raw_pdf for root in allowed_roots)]
    for marker in ("bank-proof-2026-10-15", "run-02"):
        parts = raw_pdf.parts
        if marker in parts:
            suffix = Path(*parts[parts.index(marker) + 1 :])
            pdf_candidates.extend(root / suffix for root in allowed_roots)
    pdf_path = next(
        (
            candidate.resolve()
            for candidate in pdf_candidates
            if candidate.is_file()
            and any(candidate.resolve().is_relative_to(root) for root in allowed_roots)
        ),
        None,
    )
    if pdf_path is None:
        raise ValueError(f"{code}: original PDF path is missing or outside the supplied roots")
    metadata = source.get("metadata")
    if metadata is None:
        raw_meta = Path(str(source.get("metadata_path", "")))
        meta_candidates = [workspace / raw_meta, *(root / raw_meta for root in allowed_roots)]
        for marker in ("bank-proof-2026-10-15", "run-02"):
            parts = raw_meta.parts
            if marker in parts:
                suffix = Path(*parts[parts.index(marker) + 1 :])
                meta_candidates.extend(root / suffix for root in allowed_roots)
        metadata_path = next(
            (
                candidate.resolve()
                for candidate in meta_candidates
                if candidate.is_file()
                and any(candidate.resolve().is_relative_to(root) for root in allowed_roots)
            ),
            None,
        )
        if metadata_path is None:
            raise ValueError(
                f"{code}: filing metadata path is missing or outside the supplied roots"
            )
        metadata = _json(metadata_path)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("chosen"), dict):
        raise ValueError(f"{code}: official filing metadata is incomplete")
    body = pdf_path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    chosen = metadata["chosen"]
    if not body.startswith(b"%PDF-") or metadata.get("pdf_sha256") != digest:
        raise ValueError(f"{code}: original PDF signature or source hash mismatch")
    title = normalized(str(chosen.get("announcementTitle", "")))
    if not any(t in title for t in (f"{period.year}年年度报告", f"{period.year}年度报告")) or any(
        bad in title for bad in ("摘要", "英文", "取消")
    ):
        raise ValueError(f"{code}: original title does not identify the full annual report")
    if (
        metadata.get("code") != code
        or chosen.get("secCode") != code[:6]
        or not chosen.get("secName")
    ):
        raise ValueError(f"{code}: original filing issuer mismatch")
    url = metadata.get("url")
    expected_url = "https://static.cninfo.com.cn/" + str(chosen.get("adjunctUrl", ""))
    if not isinstance(url, str) or url != expected_url:
        raise ValueError(f"{code}: original official URL is invalid")
    from investment_agent.evidence.disclosure_http import validate_url

    validate_url(url, "original")
    from io import BytesIO

    from pypdf import PdfReader

    total_pages = len(PdfReader(BytesIO(body)).pages)
    front = normalized(bank_page_text(body, 1))
    cover_context = normalized(
        "\n".join(bank_page_text(body, n) for n in range(1, min(8, total_pages) + 1))
    )
    issuer_names = {normalized(str(chosen["secName"]))}
    if code == "600036.SH":
        issuer_names.add("招商银行")
    if code == "600919.SH":
        issuer_names.add("BANKOFJIANGSU")
    if code == "601128.SH":
        issuer_names.add("江苏常熟农村商业银行")
    issuer_confirmed = any(name and name in front for name in issuer_names) or code[:6] in front
    if not issuer_confirmed:
        confirmed = any(
            code[:6] in normalized(bank_page_text(body, n))
            or any(name in normalized(bank_page_text(body, n)) for name in issuer_names)
            for n in range(2, min(8, total_pages) + 1)
        )
        if not confirmed:
            raise ValueError(f"{code}: original cover does not identify the issuer")
        else:
            issuer_confirmed = True
    period_confirmed = str(period.year) in cover_context and "年度报告" in cover_context
    if not period_confirmed:
        raise ValueError(f"{code}: original cover does not identify the annual period")
    stamp = chosen.get("announcementTime")
    if isinstance(stamp, bool) or not isinstance(stamp, int):
        raise ValueError(f"{code}: original publication time is missing")
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    original = AnnualOriginal(
        code, period, body, metadata, digest, _datetime.fromtimestamp(stamp / 1000, _UTC)
    )
    from investment_agent.industry_peers.bank_proofs import validate_bank_report_identity

    validate_bank_report_identity(original)
    return original


def _load_ningbo_final_scope_original(
    period: date, source_info: dict[str, Any], audit_records: list[dict[str, Any]], acquired_at: datetime
) -> tuple[Any, dict[str, Any]]:
    """Load the sole accepted final-scope source from its pinned local bytes."""
    from investment_agent.industry_peers.bank_scope_bridges import ningbo_final_source_identity
    from investment_agent.industry_peers.originals import AnnualOriginal

    if period.isoformat() != "2025-12-31":
        raise ValueError("Ningbo final-scope source only supports FY2025")
    rows = [row for row in audit_records if row.get("supplemental_originals") is not None]
    if len(rows) != 2 or {row.get("metric") for row in rows} != {"npl_ratio", "provision_coverage"}:
        raise ValueError("Ningbo final-scope supplement must be bound to both final ratio fields")
    first = rows[0]["supplemental_originals"]
    if not isinstance(first, list) or len(first) != 1 or not isinstance(first[0], dict):
        raise ValueError("Ningbo final-scope audit source list must contain one original")
    descriptor = first[0]
    if any(row.get("supplemental_originals") != first for row in rows):
        raise ValueError("Ningbo final-scope source identity differs across the two field audits")
    expected = {"source_key": "ningbo_final_scope", **ningbo_final_source_identity()}
    if any(descriptor.get(key) != value for key, value in expected.items()):
        raise ValueError("Ningbo final-scope source identity is not the approved CCXI ChinaMoney report")
    if descriptor.get("available_from") is not None or descriptor.get("original_ref") is not None:
        raise ValueError("Ningbo final-scope preflight descriptor cannot supply a stored reference")
    source_info_row = source_info["manifest"]["sources"].get("002142.SZ", {}).get("supplemental_originals", {}).get("ningbo_final_scope")
    if not isinstance(source_info_row, dict):
        raise ValueError("Ningbo final-scope source is missing from the pinned source manifest")
    for key, value in expected.items():
        if key == "source_key":
            continue
        if source_info_row.get(key) != value:
            raise ValueError(f"Ningbo source manifest {key} does not match the approved source")
    if source_info_row.get("acquired_at") != descriptor.get("acquired_at"):
        raise ValueError("Ningbo source manifest acquisition time differs from the field audit")
    acquired = _aware_iso(descriptor.get("acquired_at"), "Ningbo supplemental acquired_at")
    published = date.fromisoformat(descriptor["published_on"])
    if acquired > acquired_at.astimezone(UTC) or published > acquired.date():
        raise ValueError("Ningbo final-scope source was unavailable at the import cutoff")
    pdf_path = Path(str(source_info_row.get("pdf_path", "")))
    workspace = Path(__file__).resolve().parents[4]
    allowed_roots = (source_info["originals_root"], source_info["evidence_root"])
    candidates = [workspace / pdf_path, *(root / pdf_path for root in allowed_roots)]
    candidate = next(
        (
            path.resolve()
            for path in candidates
            if path.is_file()
            and any(path.resolve().is_relative_to(root) for root in allowed_roots)
        ),
        None,
    )
    if candidate is None:
        raise ValueError("Ningbo final-scope source PDF is missing or outside the supplied originals/evidence roots")
    body = candidate.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if not body.startswith(b"%PDF-") or digest != expected["pdf_sha256"]:
        raise ValueError("Ningbo final-scope source PDF signature or hash mismatch")
    if source_info_row.get("pdf_sha256") != digest:
        raise ValueError("Ningbo final-scope source manifest PDF hash mismatch")
    # AnnualOriginal requires a datetime. Use the actual retrieval time as a
    # conservative availability boundary; the descriptor preserves the
    # source's date-only publication precision without inventing a UTC time.
    # This source has no known publication timezone. Reuse the contract's
    # instant slot as conservative availability, preserving publication only
    # as the actual displayed date/minute and its explicit precision.
    supplemental = AnnualOriginal(
        "002142.SZ", period, body,
        {"source": expected["source_kind"], "published_on": published.isoformat(), "publication_precision": expected["publication_precision"]},
        digest, acquired,
    )
    # The metadata object that is retained is taken from the source manifest,
    # with its path removed before it enters a proof or database row.
    return supplemental, {key: value for key, value in source_info_row.items() if key != "pdf_path"}


def _attach_audit_proofs(
    plans: list[dict[str, Any]],
    *,
    audit_path: Path,
    originals_root: Path,
    evidence_root: Path,
    report_period: date,
    acquired_at: datetime,
) -> dict[str, Any]:
    """Validate the full bank audit and bind verified fields to actual PDFs."""
    from investment_agent.industry_peers.bank_proofs import (
        BANK_RULE,
        checked_bank_locator,
        validate_bank_scope_exclusion,
    )

    records, source_info = _read_bank_manifest(
        audit_path, originals_root, evidence_root, report_period, acquired_at
    )
    originals: dict[str, Any] = {}
    supplemental_originals: dict[str, Any] = {}
    ningbo_candidates = [
        records[("002142.SZ", metric)]
        for metric in ("npl_ratio", "provision_coverage")
        if records[("002142.SZ", metric)].get("scope_bridge", {}).get("method")
        == "ningbo-fy2025-final-ratio-ccxi-scope-v1"
    ]
    ningbo_verified = [row for row in ningbo_candidates if row.get("status") == "verified"]
    if ningbo_verified and len(ningbo_verified) != 2:
        raise ValueError("Ningbo final-scope original must verify both NPL and coverage fields together")
    if len(ningbo_verified) == 2:
        ningbo_rows = ningbo_verified
        ningbo_original, _descriptor = _load_ningbo_final_scope_original(
            report_period, source_info, ningbo_rows, acquired_at
        )
        supplemental_originals["ningbo_final_scope"] = ningbo_original
    plan_by_code = {plan["code"]: plan for plan in plans}
    # Read and hash every referenced original before any database operation.
    for code in BANK_CODES:
        source_original = _load_manifest_original(code, report_period, source_info)
        originals[code] = source_original
        for metric in set(FIELDS) | {"equity_parent"}:
            record = records[(code, metric)]
            chosen = source_original.metadata["chosen"]
            if (
                record.get("pdf_sha256") != source_original.pdf_hash
                or record.get("disclosure_id") != chosen.get("announcementId")
                or record.get("title") != chosen.get("announcementTitle")
                or record.get("url") != source_original.metadata.get("url")
            ):
                raise ValueError(
                    f"{code}.{metric}: audit filing identity/hash does not match the original"
                )
            if record["status"] == "scope_mismatch":
                try:
                    validate_bank_scope_exclusion(source_original, metric, record)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(
                        f"{code}.{metric}: original scope exclusion check failed: {exc}"
                    ) from exc
            published_at = _aware_iso(record.get("published_at"), f"{code}.{metric}.published_at")
            audit_acquired = _aware_iso(record.get("acquired_at"), f"{code}.{metric}.acquired_at")
            if published_at != source_original.published_at:
                raise ValueError(
                    f"{code}.{metric}: audit publication time does not match official metadata"
                )
            checked_at = (
                _aware_iso(record["checked_at"], f"{code}.{metric}.checked_at")
                if record.get("checked_at")
                else None
            )
            if not published_at <= audit_acquired <= acquired_at.astimezone(UTC) or (
                checked_at is not None
                and not audit_acquired <= checked_at <= acquired_at.astimezone(UTC)
            ):
                raise ValueError(
                    f"{code}.{metric}: audit/original times are outside the import boundary"
                )

    for code, plan in plan_by_code.items():
        original = originals[code]
        for metric in set(FIELDS) | {"equity_parent"}:
            record = records[(code, metric)]
            status = record["status"]
            existing_proof = plan["proofs"][metric]
            supplier_match: bool | None = None
            if status == "scope_mismatch":
                if metric not in FIELDS:
                    raise ValueError(f"{code}.{metric}: scope mismatch is only valid for ratios")
                # A verified standalone value is real source data, but cannot
                # enter a group-consolidated peer comparison. Preserve the
                # supplier facts and full original audit while making the
                # normalized group field explicitly missing.
                current = plan["fields"][metric]
                plan["fields"][metric] = current.model_copy(
                    update={
                        "value": None,
                        "verification": PeerFieldVerification.CANDIDATE,
                        "missing_reason": GROUP_SCOPE_EXCLUSION_REASON,
                    }
                )
                existing_proof.update(
                    {
                        **record,
                        "status": "missing",
                        "verification_source": "bank-annual-original/1",
                        "scope_exclusion": True,
                        "note": GROUP_SCOPE_EXCLUSION_REASON,
                        "original_review": {
                            "status": "scope_mismatch",
                            "accounting_scope": record.get("accounting_scope"),
                            "scope_context": record.get("scope_context"),
                        },
                        "audit_record": record,
                    }
                )
                continue
            if metric == "equity_parent":
                raw_value = record.get("raw_value")
                parsed_value = record.get("parsed_value")
                raw_unit = record.get("raw_unit")
                parsed_unit = record.get("parsed_unit")
                if (status == "verified" and parsed_unit != "CNY") or (
                    parsed_unit is not None and parsed_unit != "CNY"
                ):
                    raise ValueError(f"{code}.equity_parent: parsed unit must be CNY")
            else:
                raw_value = record.get("raw_value")
                parsed_value = record.get("parsed_value")
                raw_unit = record.get("raw_unit")
                parsed_unit = record.get("parsed_unit")
                supplier_comparison = record.get("supplier_comparison")
                current = plan["fields"][metric]
                supplier_raw = Decimal(current.raw_value) if current.raw_value is not None else None
                # The comparison block is display provenance only. Recompute
                # its result from the already validated supplier input.
                if status == "verified" and (raw_unit != "percent" or parsed_unit != "fraction"):
                    raise ValueError(f"{code}.{metric}: ratio audit unit mismatch")
                if (
                    raw_unit is not None
                    and raw_unit != "percent"
                    or parsed_unit is not None
                    and parsed_unit != "fraction"
                ):
                    raise ValueError(f"{code}.{metric}: ratio audit unit mismatch")
                supplier_value = supplier_raw.scaleb(-2) if supplier_raw is not None else None
                if supplier_value is None or parsed_value is None:
                    match: bool | None = None
                else:
                    match = Decimal(str(parsed_value)) == supplier_value
                supplier_match = match
                if isinstance(supplier_comparison, dict):
                    expected_status = (
                        ("match" if match else "mismatch")
                        if parsed_value is not None and supplier_raw is not None
                        else None
                    )
                    if (
                        supplier_comparison.get("source_field") != current.source_field
                        or supplier_comparison.get("raw_value") != current.raw_value
                        or supplier_comparison.get("raw_unit") != current.raw_unit
                        or supplier_comparison.get("converted_value")
                        != (str(supplier_value) if supplier_value is not None else None)
                        or (
                            expected_status is not None
                            and (
                                supplier_comparison.get("status") != expected_status
                                or (
                                    "match" in supplier_comparison
                                    and supplier_comparison.get("match") is not match
                                )
                            )
                        )
                    ):
                        raise ValueError(
                            f"{code}.{metric}: supplier comparison flag disagrees with source facts"
                        )
                if status == "supplier_mismatch" and match is not False:
                    raise ValueError(
                        f"{code}.{metric}: supplier_mismatch status conflicts with available values"
                    )
                if match is False:
                    existing_proof["supplier_comparison"] = {
                        "status": "mismatch" if supplier_raw is not None else "unavailable",
                        "supplier_value": str(supplier_value)
                        if supplier_value is not None
                        else None,
                        "pdf_value": str(parsed_value),
                    }
                elif match is True:
                    existing_proof["supplier_comparison"] = {
                        "status": "match",
                        "supplier_value": str(supplier_value),
                        "pdf_value": str(parsed_value),
                    }
            if status != "verified":
                existing_proof["status"] = (
                    "supplier_mismatch" if supplier_match is False else status
                )
                existing_proof["audit_record"] = record
                existing_proof["original_review"] = {
                    "status": status,
                    "note": record.get("definition_or_footnote"),
                }
                for review_key in ("candidate_reason", "resolution_plan"):
                    if record.get(review_key):
                        existing_proof["original_review"][review_key] = record[review_key]
                continue
            if raw_value is None or parsed_value is None or raw_unit is None:
                raise ValueError(
                    f"{code}.{metric}: verified audit record has incomplete value/unit"
                )
            if metric == "equity_parent":
                # equity is an actual consolidated parent-attributable total;
                # it is not inferred from any supplier equity field.
                value = Decimal(str(parsed_value))
                plan["fields"][metric] = PeerAnnualField(
                    metric=metric,
                    value=value,
                    unit="CNY",
                    currency="CNY",
                    period=Period(start=report_period, end=report_period, basis="point_in_time"),
                    source_field="annual_report_equity_parent",
                    raw_value=str(raw_value),
                    raw_unit=str(raw_unit),
                    acquired_at=acquired_at,
                    disclosed_on=original.published_at.date(),
                    updated_on=original.published_at.date(),
                    source_version=original.published_at.date().isoformat(),
                    verification=PeerFieldVerification.CANDIDATE,
                )
            else:
                value = Decimal(str(parsed_value))
                current = plan["fields"][metric]
                if current.value != value:
                    raise ValueError(
                        f"{code}.{metric}: verified PDF value does not equal the supplier value"
                    )
            report_identity = {
                "code": code,
                "period": report_period.isoformat(),
                "disclosure_id": original.metadata["chosen"]["announcementId"],
            }
            proof = {
                **record,
                "status": "verified" if supplier_match is not False else "supplier_mismatch",
                "verification_source": "bank-annual-original/1",
                "original_ref": None,  # filled after original is stored
                "pdf_hash": original.pdf_hash,
                "report_identity": report_identity,
                "period": report_period.isoformat(),
                "check_rule_version": BANK_RULE,
                "checked_at": record["checked_at"],
                "audit_record": record,
            }
            try:
                checked = checked_bank_locator(
                    original, metric, proof, supplemental_originals=supplemental_originals
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{code}.{metric}: original field check failed: {exc}") from exc
            proof.update(checked)
            if Decimal(str(proof["parsed_value"])) != plan["fields"][metric].value:
                raise ValueError(f"{code}.{metric}: parsed PDF value differs from imported fact")
            if supplier_match is False:
                proof["supplier_comparison"] = existing_proof["supplier_comparison"]
                proof["note"] = (
                    "annual report value differs from the supplier value; retained as candidate"
                )
            else:
                plan["fields"][metric] = plan["fields"][metric].model_copy(
                    update={"verification": PeerFieldVerification.VERIFIED, "missing_reason": None}
                )
            plan["proofs"][metric] = proof
    return {
        "records": records,
        "originals": originals,
        "supplemental_originals": supplemental_originals,
    }


def load_bank_plan(
    evidence_root: Path,
    report_period: date,
    acquired_at: datetime,
) -> list[dict[str, Any]]:
    """Validate fixed bank roster and one exact-period supplier row per issuer."""
    if (report_period.month, report_period.day) != (12, 31):
        raise ValueError("report_period must be an annual Dec 31 date")
    if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
        raise ValueError("acquired_at must be timezone-aware")
    sample = _json(evidence_root / "sample.json")
    if sample.get("bank") != list(BANK_CODES):
        raise ValueError("sample bank roster differs from the fixed run-02 10-bank roster")
    plans = []
    expected_day = report_period.isoformat()
    for code in BANK_CODES:
        source_path = evidence_root / "raw/financial" / f"{code}_main.json"
        source_bytes = source_path.read_bytes()
        payload = _json(source_path)
        rows = payload.get("result", {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"{code}: supplier response must contain result.data rows")
        matched = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{code}: supplier row must be an object")
            if row.get("SECUCODE") != code:
                raise ValueError(
                    f"{code}: response contains foreign issuer {row.get('SECUCODE')!r}"
                )
            row_date = row.get("REPORT_DATE")
            if isinstance(row_date, str) and row_date[:10] == expected_day:
                matched.append(row)
        if len(matched) != 1:
            raise ValueError(
                f"{code}: expected exactly one {expected_day} row, found {len(matched)}"
            )
        row = matched[0]
        if row.get("REPORT_TYPE") != "年报":
            raise ValueError(f"{code}: {expected_day} source row is not labelled 年报")
        fields: dict[str, PeerAnnualField] = {}
        proofs: dict[str, dict[str, Any]] = {}
        notice_date = _date(row.get("NOTICE_DATE"), f"{code} NOTICE_DATE")
        update_date = _date(row.get("UPDATE_DATE"), f"{code} UPDATE_DATE")
        import_day = acquired_at.astimezone(UTC).date()
        if notice_date is not None and notice_date > import_day:
            raise ValueError(f"{code}: NOTICE_DATE is later than the import cutoff")
        if update_date is not None and update_date > import_day:
            raise ValueError(f"{code}: UPDATE_DATE is later than the import cutoff")
        for metric, (source_field, label, _kind) in FIELDS.items():
            if source_field not in row:
                raise ValueError(f"{code}: required supplier field {source_field} is absent")
            raw = _number(row[source_field], f"{code}.{source_field}")
            value = raw.scaleb(-2) if raw is not None else None
            fields[metric] = PeerAnnualField(
                metric=metric,
                value=value,
                unit="fraction",
                currency=None,
                period=_period(report_period),
                source_field=source_field,
                raw_value=str(raw) if raw is not None else None,
                raw_unit="percent",
                acquired_at=acquired_at,
                disclosed_on=notice_date,
                updated_on=update_date,
                source_version=update_date.isoformat() if update_date else None,
                verification=PeerFieldVerification.CANDIDATE,
                missing_reason=f"supplier value is null for {source_field}"
                if raw is None
                else None,
            )
            proofs[metric] = {
                "status": "candidate",
                "verification_source": "eastmoney_supplier_response",
                "source_file": f"raw/financial/{code}_main.json",
                "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "source_field": source_field,
                "source_label": label,
                "raw_value": str(raw) if raw is not None else None,
                "raw_unit": "percent",
                "parsed_value": str(value) if value is not None else None,
                "notice_date": notice_date.isoformat() if notice_date else None,
                "update_date": update_date.isoformat() if update_date else None,
                "report_period": expected_day,
                "original_ref": None,
                "pdf_hash": None,
                "pdf_page": None,
                "locator_excerpt": None,
                "note": "supplier value only; no verified field-level annual-report locator",
            }
        fields["equity_parent"] = PeerAnnualField(
            metric="equity_parent",
            value=None,
            unit="CNY",
            currency="CNY",
            period=Period(start=report_period, end=report_period, basis="point_in_time"),
            source_field=None,
            acquired_at=acquired_at,
            verification=PeerFieldVerification.CANDIDATE,
            missing_reason=MISSING_EQUITY_REASON,
        )
        proofs["equity_parent"] = {
            "status": "candidate",
            "verification_source": "not_mapped",
            "source_file": f"raw/financial/{code}_main.json",
            "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "note": MISSING_EQUITY_REASON,
        }
        plans.append(
            {
                "code": code,
                "fields": fields,
                "proofs": proofs,
                "source_hash": hashlib.sha256(source_bytes).hexdigest(),
            }
        )
    return plans


async def import_bank_annual_snapshots(
    session: AsyncSession,
    *,
    evidence_root: Path,
    report_period: date,
    acquired_at: datetime,
    provider: str = PROVIDER,
    adapter_version: str = ADAPTER_VERSION,
    audit_path: Path | None = None,
    originals_root: Path | None = None,
) -> dict[str, Any]:
    """Append bank facts, optionally promoting only independently checked fields.

    The legacy call (no audit inputs) remains supplier-only. Verification is
    deliberately opt-in because local originals and their field audit are a
    separate evidence set from the supplier capture.
    """
    if (audit_path is None) != (originals_root is None):
        raise ValueError("audit_path and originals_root must be supplied together")
    plans = load_bank_plan(evidence_root, report_period, acquired_at)
    audit_bundle: dict[str, Any] | None = None
    if audit_path is not None and originals_root is not None:
        # Complete every audit, path, hash, time, unit, supplier and PDF check
        # before identity resolution or any write to the disclosure/snapshot store.
        audit_bundle = _attach_audit_proofs(
            plans,
            audit_path=audit_path,
            originals_root=originals_root,
            evidence_root=evidence_root,
            report_period=report_period,
            acquired_at=acquired_at,
        )
    from investment_agent.cli.commands.peer_annual import resolve_identities

    identities = await resolve_identities(session, [p["code"] for p in plans])
    originals: dict[str, Any] = dict(audit_bundle["originals"]) if audit_bundle else {}
    supplemental_originals: dict[str, Any] = dict(audit_bundle.get("supplemental_originals", {})) if audit_bundle else {}
    original_reviews: dict[str, dict[str, Any]] = {}
    if audit_bundle:
        for code, original in originals.items():
            if original is None:
                original_reviews[code] = {
                    "status": "not_registered",
                    "reason_code": "pdf_cover_text_unconfirmed",
                    "reason": "PDF text did not confirm issuer and annual period; all fields remain candidate",
                }
    for plan in plans if audit_bundle is None else []:
        try:
            original = load_annual_original(evidence_root, plan["code"], report_period)
            if original.published_at > acquired_at:
                original_reviews[plan["code"]] = {
                    "status": "not_registered",
                    "reason_code": "published_after_import_cutoff",
                    "reason": "official publication is after the import cutoff",
                }
                continue
            originals[plan["code"]] = original
            original_reviews[plan["code"]] = {"status": "validated_for_retention", "reason": None}
        except FileNotFoundError as exc:
            original_reviews[plan["code"]] = {
                "status": "not_registered",
                "reason_code": "missing_local_original",
                "reason": f"local original is missing: {exc.filename or exc}",
            }
            continue
        except (ValueError, KeyError, TypeError, ImportError) as exc:
            # A broken local original cannot promote any field; facts remain candidates.
            original_reviews[plan["code"]] = {
                "status": "not_registered",
                "reason_code": "original_validation_failed",
                "reason": f"original validation failed: {exc}",
            }
            continue
    repo = PeerAnnualSnapshotRepository(session)
    inserted = skipped = 0
    hashes: dict[str, str] = {}
    for plan in plans:
        company_id, listing_id = identities[plan["code"]]
        if originals.get(plan["code"]) is not None:
            original = originals[plan["code"]]
            stored = await store_annual_original(session, original, listing_id, acquired_at)
            original_reviews[plan["code"]] = {
                "status": "retained_after_field_audit"
                if audit_bundle
                else "retained_as_candidate_original",
                "reason": None,
                "original_ref": {"kind": "disclosure_original", "id": stored.id, "revision": 1},
                "pdf_hash": original.pdf_hash,
            }
            for _metric, proof in plan["proofs"].items():
                available = {
                    "original_ref": {"kind": "disclosure_original", "id": stored.id, "revision": 1},
                    "pdf_hash": original.pdf_hash,
                    "report_identity": {"code": plan["code"], "period": report_period.isoformat()},
                    "note": "original retained; see per-field audit_record for the verification result",
                }
                if proof.get("status") == "verified":
                    proof["original_ref"] = available["original_ref"]
                else:
                    proof["available_original"] = available
        else:
            for proof in plan["proofs"].values():
                review = original_reviews[plan["code"]]
                proof["original_review"] = {
                    "status": review["status"],
                    "reason_code": review.get("reason_code", "published_after_import_cutoff"),
                }
        if plan["code"] == "002142.SZ" and "ningbo_final_scope" in supplemental_originals:
            supplemental = supplemental_originals["ningbo_final_scope"]
            source_descriptor = next(
                (
                    item
                    for metric in ("npl_ratio", "provision_coverage")
                    for item in plan["proofs"][metric].get("supplemental_originals", [])
                    if item.get("source_key") == "ningbo_final_scope"
                ),
                None,
            )
            if source_descriptor is None:
                raise ValueError("Ningbo final-scope proof is missing its supplemental descriptor")
            stored_supplement = await store_bank_supplemental_original(
                session,
                code=plan["code"],
                body=supplemental.body,
                source=source_descriptor,
                listing_id=listing_id,
                acquired_at=supplemental.published_at,
            )
            supplemental_ref = {"kind": "disclosure_original", "id": stored_supplement.id, "revision": 1}
            for metric in ("npl_ratio", "provision_coverage"):
                proof = plan["proofs"][metric]
                for descriptor in proof.get("supplemental_originals", []):
                    if descriptor.get("source_key") == "ningbo_final_scope":
                        descriptor["original_ref"] = supplemental_ref
                        descriptor["available_from"] = stored_supplement.payload["available_from"]
                        descriptor["acquired_at"] = stored_supplement.payload["acquired_at"]
        semantic_hash = _snapshot_hash(plan["fields"], plan["proofs"])
        hashes[plan["code"]] = semantic_hash
        if await repo.by_unique_key(
            company_id=company_id,
            report_period=report_period,
            provider=provider,
            adapter_version=adapter_version,
            semantic_hash=semantic_hash,
        ):
            skipped += 1
            continue
        await repo.add(
            PeerAnnualSnapshot(
                id=str(uuid.uuid4()),
                company_id=company_id,
                listing_id=listing_id,
                report_period=report_period,
                provider=provider,
                adapter_version=adapter_version,
                captured_at=acquired_at,
                content_hash=semantic_hash,
                semantic_hash=semantic_hash,
                fields={k: f.model_dump(mode="json") for k, f in plan["fields"].items()},
                verification=plan["proofs"],
            )
        )
        inserted += 1
    company_states: dict[str, Any] = {}
    for plan in plans:
        by_state: dict[str, int] = {"verified": 0, "candidate": 0, "missing": 0}
        reasons: dict[str, str] = {}
        for metric, field in plan["fields"].items():
            proof_status = plan["proofs"].get(metric, {}).get("status")
            state = (
                "missing"
                if proof_status == "missing" or field.value is None
                else field.verification.value
            )
            by_state[state] = by_state.get(state, 0) + 1
            if field.missing_reason:
                reasons[metric] = field.missing_reason
            elif proof_status in {"candidate", "missing", "supplier_mismatch"}:
                reasons[metric] = str(
                    plan["proofs"][metric].get("definition_or_footnote")
                    or plan["proofs"][metric].get("original_review", {}).get("note")
                    or plan["proofs"][metric].get("note")
                    or "field proof was not verified"
                )
        company_states[plan["code"]] = {**by_state, "reasons": reasons}
    total_verified = sum(item["verified"] for item in company_states.values())
    return {
        "period": report_period.isoformat(),
        "companies": len(plans),
        "fields_per_company": 5,
        "verified_fields": total_verified,
        "inserted": inserted,
        "skipped_existing": skipped,
        "semantic_hashes": hashes,
        "originals": original_reviews,
        "field_states": company_states,
    }


async def import_bank_annual_source_snapshots(
    session: AsyncSession,
    *,
    cohort_key: str,
    evidence_root: Path,
    report_period: date,
    acquired_at: datetime,
    audit_path: Path | None,
    originals_root: Path | None,
) -> dict[str, Any]:
    """Import an audited bank year into the cohort's fixed bank-annual source scope."""
    if audit_path is None or originals_root is None:
        raise ValueError(
            "--bank-annual-source requires both --audit-path and --originals-root"
        )

    from sqlalchemy import select

    from investment_agent.industry_peers.metric_packs import (
        BANK_PACK,
        cohort_metric_pack,
        cohort_read_scope,
    )
    from investment_agent.providers.eastmoney_bank_annual import BANK_ADAPTER_VERSION
    from investment_agent.storage.models.peer import PeerCohortSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    cohort = await session.scalar(
        select(PeerCohortSnapshot)
        .where(PeerCohortSnapshot.cohort_key == cohort_key)
        .order_by(PeerCohortSnapshot.revision.desc(), PeerCohortSnapshot.id.desc())
        .limit(1)
    )
    if cohort is None:
        raise ValueError(f"bank cohort {cohort_key!r} has no saved roster")
    if cohort_metric_pack(cohort.payload) != BANK_PACK:
        raise ValueError("selected cohort must use the bank annual metric pack")
    try:
        read_scope = cohort_read_scope(cohort.payload)
    except ValueError as exc:
        raise ValueError("selected cohort must use the bank annual read scope") from exc
    if read_scope != ("eastmoney", BANK_ADAPTER_VERSION):
        raise ValueError("selected cohort must use the bank annual read scope")

    entries = cohort.payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("selected bank cohort has no valid membership entries")
    member_listing_ids = {
        entry.get("listing_id") for entry in entries if isinstance(entry, dict)
    }
    listing_repo = ListingRepository(session)
    for code in BANK_CODES:
        listing = await listing_repo.get_a_share_listing(code)
        if listing is None or listing.id not in member_listing_ids:
            raise ValueError(f"{code}: not a saved member of the selected bank cohort")

    return await import_bank_annual_snapshots(
        session,
        evidence_root=evidence_root,
        report_period=report_period,
        acquired_at=acquired_at,
        provider=PROVIDER,
        adapter_version=BANK_ADAPTER_VERSION,
        audit_path=audit_path,
        originals_root=originals_root,
    )


@app.command("import-bank")
def import_bank(
    evidence_root: Path = typer.Option(..., "--evidence-root", exists=True, file_okay=False),  # noqa: B008
    report_period: str = typer.Option(..., "--report-period"),
    bank_annual_source: bool = typer.Option(
        False,
        "--bank-annual-source",
        help="Import into the selected cohort's bank-annual source scope (requires audited originals)",
    ),
    cohort_key: str = typer.Option("bank_run02_sample", "--cohort-key"),
    audit_path: Path | None = typer.Option(None, "--audit-path", exists=True, dir_okay=False),  # noqa: B008
    originals_root: Path | None = typer.Option(  # noqa: B008
        None, "--originals-root", exists=True, file_okay=False
    ),
) -> None:
    """Import the fixed 10-bank run-02 annual source snapshot without HTTP."""
    if bank_annual_source and (audit_path is None or originals_root is None):
        typer.echo(
            "--bank-annual-source requires both --audit-path and --originals-root",
            err=True,
        )
        raise typer.Exit(1)
    try:
        period = date.fromisoformat(report_period)
        if (period.month, period.day) != (12, 31):
            raise ValueError("period must end on Dec 31")
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--report-period") from exc
    from investment_agent.cli.runtime import bootstrap_settings
    from investment_agent.storage.database import run_with_engine, session_scope

    settings = bootstrap_settings()
    acquired = datetime.now(UTC)

    async def run() -> dict[str, Any]:
        async with session_scope() as session:
            if bank_annual_source:
                return await import_bank_annual_source_snapshots(
                    session,
                    cohort_key=cohort_key,
                    evidence_root=evidence_root,
                    report_period=period,
                    acquired_at=acquired,
                    audit_path=audit_path,
                    originals_root=originals_root,
                )
            return await import_bank_annual_snapshots(
                session,
                evidence_root=evidence_root,
                report_period=period,
                acquired_at=acquired,
                audit_path=audit_path,
                originals_root=originals_root,
            )

    try:
        typer.echo(
            json.dumps(run_with_engine(settings.database_url, run), ensure_ascii=False, indent=2)
        )
    except (ValueError, OSError) as exc:
        typer.echo(f"bank annual import failed: {exc}", err=True)
        raise typer.Exit(1) from exc
