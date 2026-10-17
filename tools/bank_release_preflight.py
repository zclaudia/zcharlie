#!/usr/bin/env python3
"""Offline readiness check for the reviewed two-year bank evidence package.

This checks package completeness and original-file integrity. It does not
re-run field-level PDF semantic or database acceptance checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BANKS = {
    "000001.SZ",
    "601398.SH",
    "601939.SH",
    "601288.SH",
    "600036.SH",
    "601166.SH",
    "600919.SH",
    "601009.SH",
    "002142.SZ",
    "601128.SH",
}
METRICS = {
    "roe_weighted_annual",
    "net_interest_margin",
    "npl_ratio",
    "provision_coverage",
    "equity_parent",
}
PERIODS = {"2024-12-31", "2025-12-31"}
EXPECTED = {"verified": 95, "candidate": 1, "scope_mismatch": 4}
BUNDLES = (
    "tests/fixtures/bank_fy2024/icbc.json",
    "tests/fixtures/bank_fy2024/large.json",
    "tests/fixtures/bank_fy2024/regional.json",
    "tests/fixtures/bank_fy2024/scope.json",
)
MATRIX = "tests/fixtures/bank_fy2024/two-year-matrix.json"
FY25_AUDIT = "tests/fixtures/bank_proofs_v2/audit-manifest.json"
FY25_SUPPLIER_ROOT = "research/industry-data-validation/evidence/run-02"
BANK_ORDER = (
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


class CheckError(Exception):
    pass


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckError(f"cannot read valid JSON {path}: {exc}") from exc


def _safe_rel(root: Path, raw: str, label: str) -> Path:
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        raise CheckError(f"unsafe {label} path: {raw}")
    candidate = root / p
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise CheckError(f"{label} path escapes configured root: {raw}")
    return candidate


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CheckError(f"missing or non-regular {label}: {path}")


def _metadata_ok(
    meta: dict[str, Any], *, code: str, period: str, digest: str, source: dict[str, Any], label: str
) -> None:
    metadata_code = (
        meta.get("code") or meta.get("listing_code") or (meta.get("chosen") or {}).get("secCode")
    )
    if metadata_code not in {code, code.split(".")[0]}:
        raise CheckError(f"{label}: metadata code mismatch (expected {code})")
    if any(
        meta.get(key) != digest for key in ("pdf_sha256", "sha256") if meta.get(key) is not None
    ):
        raise CheckError(f"{label}: metadata PDF hash mismatch")
    if not (meta.get("pdf_sha256") or meta.get("sha256")):
        raise CheckError(f"{label}: metadata PDF hash missing")
    chosen = meta.get("chosen") or {}
    if chosen.get("secCode") and chosen["secCode"] != code.split(".")[0]:
        raise CheckError(f"{label}: selected filing code mismatch")
    expected_id = source.get("disclosure_id")
    if any(
        value != expected_id
        for value in (meta.get("disclosure_id"), chosen.get("announcementId"))
        if value is not None
    ):
        raise CheckError(f"{label}: metadata disclosure identity mismatch")
    if not chosen.get("announcementId"):
        raise CheckError(f"{label}: metadata disclosure identity missing")
    supplied_titles = [meta.get("title"), chosen.get("announcementTitle"), chosen.get("shortTitle")]
    if not any(supplied_titles) or any(
        title != source.get("title") for title in supplied_titles if title
    ):
        raise CheckError(f"{label}: metadata title differs from source manifest")
    if period[:4] not in str(source.get("title")):
        raise CheckError(f"{label}: metadata title does not identify {period[:4]}")
    metadata_url = meta.get("url")
    expected_url = source.get("url")
    adjunct = chosen.get("adjunctUrl")
    if metadata_url and metadata_url != expected_url:
        raise CheckError(f"{label}: metadata URL differs from source manifest")
    if not (adjunct and urlsplit(expected_url or "").path.endswith(str(adjunct))):
        raise CheckError(f"{label}: selected filing URL differs from source manifest")


def _assert_slot_binding(
    matrix_row: dict[str, Any],
    audit: dict[str, Any],
    *,
    pdf_hash: str,
    source_url: str,
    bundle: str | None = None,
) -> None:
    key = (matrix_row.get("code"), matrix_row.get("report_period"), matrix_row.get("metric"))
    audit_key = (
        audit.get("code"),
        audit.get("period", audit.get("report_period")),
        audit.get("metric"),
    )
    if key != audit_key:
        raise CheckError(f"matrix/audit slot identity mismatch: {key}")
    expected = {
        "status": audit.get("status"),
        "value": audit.get("parsed_value") if audit.get("status") == "verified" else None,
        "observed_raw_value": audit.get("raw_value"),
        "raw_unit": audit.get("raw_unit"),
        "pdf_page": audit.get("pdf_page"),
        "source_url": audit.get("source_url") or audit.get("url") or source_url,
        "original_sha256": audit.get("pdf_sha256") or audit.get("pdf_hash") or pdf_hash,
    }
    if expected["source_url"] != source_url or expected["original_sha256"] != pdf_hash:
        raise CheckError(f"audit/source provenance mismatch at {key}")
    if bundle is not None and matrix_row.get("audit_bundle") != bundle:
        raise CheckError(f"matrix audit_bundle differs from actual source bundle at {key}")
    for field, value in expected.items():
        if matrix_row.get(field) != value:
            raise CheckError(f"matrix {field} differs from audit at {key}")


def _validate_ccxi_identity(supplemental: dict[str, Any]) -> dict[str, str]:
    from investment_agent.industry_peers.bank_scope_bridges import ningbo_final_source_identity

    expected = ningbo_final_source_identity()
    for field, value in expected.items():
        if supplemental.get(field) != value:
            raise CheckError(f"Ningbo CCXI identity mismatch for {field}")
    return expected


def _supplier_inputs(evidence_root: Path) -> list[dict[str, Any]]:
    supplier_root = _safe_rel(evidence_root, FY25_SUPPLIER_ROOT, "FY2025 supplier evidence")
    paths = [supplier_root / "sample.json"] + [
        supplier_root / "raw/financial" / f"{code}_main.json" for code in BANK_ORDER
    ]
    for path in paths:
        _require_file(path, "FY2025 supplier input")
    try:
        from datetime import UTC, datetime

        from investment_agent.cli.commands.bank_annual import BANK_CODES, load_bank_plan

        if tuple(BANK_CODES) != BANK_ORDER:
            raise CheckError("candidate bank importer fixed roster differs from preflight roster")
        plans = load_bank_plan(supplier_root, date(2025, 12, 31), datetime.now(UTC))
    except (ImportError, OSError, ValueError, TypeError) as exc:
        raise CheckError(f"FY2025 supplier plan is invalid: {exc}") from exc
    if len(plans) != 10 or {plan["code"] for plan in plans} != BANKS:
        raise CheckError("FY2025 supplier plan does not cover exactly the fixed ten-bank roster")
    missing_facts = [
        f"{plan['code']}.{metric}"
        for plan in plans
        for metric in (
            "net_interest_margin",
            "npl_ratio",
            "provision_coverage",
            "roe_weighted_annual",
        )
        if plan["fields"][metric].value is None
    ]
    if missing_facts:
        raise CheckError(
            "FY2025 supplier data has null required facts: " + ", ".join(missing_facts)
        )
    return [
        {
            "path": str(path.relative_to(evidence_root)),
            "size_bytes": path.stat().st_size,
            "sha256": _sha(path),
        }
        for path in paths
    ]


def run(repo_root: Path, evidence_root: Path) -> dict[str, Any]:
    repo_root, evidence_root = repo_root.resolve(), evidence_root.resolve()
    supplier_inputs = _supplier_inputs(evidence_root)
    matrix_path = _safe_rel(repo_root, MATRIX, "matrix")
    matrix = _json(matrix_path)
    if not isinstance(matrix, dict):
        raise CheckError(f"matrix must be a JSON object: {matrix_path}")
    records = matrix.get("records")
    issues: list[str] = []
    if matrix.get("schema") != "bank-two-year-reviewed-matrix/1" or not isinstance(records, list):
        raise CheckError(f"unsupported or malformed matrix: {matrix_path}")
    if any(not isinstance(row, dict) for row in records):
        raise CheckError(f"matrix records must be JSON objects: {matrix_path}")
    keys = [(r.get("code"), r.get("report_period"), r.get("metric")) for r in records]
    expected_keys = {(c, p, m) for c in BANKS for p in PERIODS for m in METRICS}
    if len(records) != 100 or len(set(keys)) != 100 or set(keys) != expected_keys:
        issues.append("matrix must contain each of 100 fixed bank/year/metric slots exactly once")
    counts: dict[str, int] = {}
    for row in records:
        status = row.get("status")
        counts[status] = counts.get(status, 0) + 1
        if status not in EXPECTED:
            issues.append(
                f"unknown status at {row.get('code')} {row.get('report_period')} {row.get('metric')}: {status}"
            )
        if status == "verified" and (
            row.get("value") is None
            or row.get("reason") is not None
            or row.get("remediation") is not None
        ):
            issues.append(
                f"invalid verified row: {row.get('code')} {row.get('report_period')} {row.get('metric')}"
            )
        if status in {"candidate", "scope_mismatch"} and (
            not row.get("reason") or not row.get("remediation") or row.get("value") is not None
        ):
            issues.append(
                f"non-verified row needs null value, reason, and remediation: {row.get('code')} {row.get('report_period')} {row.get('metric')}"
            )
    if counts != EXPECTED:
        issues.append(f"status totals are {counts}, expected {EXPECTED}")

    documents: dict[tuple[str, str], dict[str, Any]] = {}
    audit_rows: dict[tuple[str, str, str], tuple[dict[str, Any], str | None]] = {}
    fy24_supplementals: list[dict[str, Any]] = []
    for rel in BUNDLES:
        bp = _safe_rel(repo_root, rel, "audit bundle")
        _require_file(bp, "audit bundle")
        bundle = _json(bp)
        extras = bundle.get("supplemental_sources", {})
        if isinstance(extras, dict):
            for owner, by_key in extras.items():
                if isinstance(by_key, dict):
                    for source_key, descriptor in by_key.items():
                        if isinstance(descriptor, dict):
                            fy24_supplementals.append(
                                {"owner": owner, "source_key": source_key, **descriptor}
                            )
        sources = bundle.get("sources")
        if not isinstance(sources, dict):
            raise CheckError(f"audit bundle sources must be an object: {bp}")
        for code, source in sources.items():
            if not isinstance(source, dict):
                raise CheckError(f"audit bundle source must be an object for {code}: {bp}")
            period = bundle.get("report_period", "2024-12-31")
            if period != "2024-12-31":
                raise CheckError(f"unexpected FY2024 bundle period in {bp}")
            documents[(code, period)] = {
                "code": code,
                "period": period,
                "pdf": source.get("pdf_path"),
                "metadata": source.get("metadata_path"),
                "sha256": source.get("pdf_sha256"),
                "title": source.get("title"),
                "url": source.get("url"),
                "disclosure_id": source.get("disclosure_id"),
                "published_at": source.get("published_at"),
                "acquired_at": source.get("acquired_at"),
            }
            for record in bundle.get("records", []):
                if record.get("code") != code:
                    continue
                slot = (code, period, record.get("metric"))
                if slot in audit_rows:
                    raise CheckError(f"duplicate FY2024 audit slot: {slot}")
                audit_rows[slot] = (record, rel)
    if len([k for k in documents if k[1] == "2024-12-31"]) != 10:
        issues.append("FY2024 evidence bundles must identify ten distinct annual reports")

    audit_path = _safe_rel(repo_root, FY25_AUDIT, "FY2025 audit manifest")
    _require_file(audit_path, "FY2025 audit manifest")
    audit = _json(audit_path)
    source_rel = audit.get("source_manifest")
    if not source_rel:
        raise CheckError("FY2025 audit manifest has no source_manifest")
    source_path = _safe_rel(evidence_root, source_rel, "source manifest")
    _require_file(source_path, "FY2025 source manifest")
    source_manifest = _json(source_path)
    fy25_rows: dict[str, list[dict[str, Any]]] = {}
    for row in audit.get("records", []):
        if row.get("period") == "2025-12-31":
            fy25_rows.setdefault(row.get("code"), []).append(row)
            slot = (row.get("code"), "2025-12-31", row.get("metric"))
            if slot in audit_rows:
                raise CheckError(f"duplicate FY2025 audit slot: {slot}")
            audit_rows[slot] = (row, None)
    sources = source_manifest.get("sources")
    if not isinstance(sources, dict):
        raise CheckError(f"FY2025 source manifest sources must be an object: {source_path}")
    for code, src in sources.items():
        if not isinstance(src, dict):
            raise CheckError(f"FY2025 source manifest entry must be an object for {code}")
        rows = fy25_rows.get(code, [])
        hashes = {r.get("pdf_sha256") for r in rows}
        paths = {r.get("pdf_path") for r in rows}
        if len(rows) != 5 or len(hashes) != 1 or paths != {src.get("pdf_path")}:
            issues.append(f"FY2025 audit records do not bind five fields to one source for {code}")
        digest = next(iter(hashes), None)
        representative = rows[0] if rows else {}
        for row in rows:
            for field in ("title", "url", "disclosure_id", "published_at"):
                if row.get(field) != representative.get(field):
                    issues.append(f"FY2025 field audit {field} differs by metric for {code}")
        documents[(code, "2025-12-31")] = {
            "code": code,
            "period": "2025-12-31",
            "pdf": src.get("pdf_path"),
            "metadata": src.get("metadata_path"),
            "metadata_inline": src.get("metadata"),
            "sha256": digest,
            "title": representative.get("title"),
            "url": representative.get("url"),
            "disclosure_id": representative.get("disclosure_id"),
            "published_at": representative.get("published_at"),
            "acquired_at": src.get("acquired_at") or representative.get("acquired_at"),
        }
    if len([k for k in documents if k[1] == "2025-12-31"]) != 10:
        issues.append("FY2025 source manifest must identify ten distinct annual reports")

    for row in records:
        doc = documents.get((row.get("code"), row.get("report_period")))
        if doc is None:
            issues.append(
                f"no actual annual-report manifest for {row.get('code')} {row.get('report_period')}"
            )
            continue
        audit_entry = audit_rows.get((row.get("code"), row.get("report_period"), row.get("metric")))
        if audit_entry is None:
            issues.append(
                f"no field audit record for {row.get('code')} {row.get('report_period')} {row.get('metric')}"
            )
            continue
        field_audit, bundle_rel = audit_entry
        try:
            _assert_slot_binding(
                row,
                field_audit,
                pdf_hash=doc.get("sha256"),
                source_url=doc.get("url"),
                bundle=bundle_rel,
            )
        except CheckError as exc:
            issues.append(str(exc))
        if row.get("report_period") == "2024-12-31":
            try:
                from investment_agent.industry_peers.bank_fy2024_profiles import (
                    APPROVED,
                    record_fingerprint,
                )

                key = (doc.get("sha256", "").lower(), row.get("metric"))
                expected = APPROVED.get(key)
                if (
                    expected is None
                    or expected[0] != field_audit.get("status")
                    or record_fingerprint(field_audit) != expected[1]
                ):
                    issues.append(
                        f"FY2024 reviewed locator fingerprint mismatch for {row.get('code')} {row.get('metric')}"
                    )
            except (ImportError, AttributeError, TypeError):
                issues.append("FY2024 restricted-layout fingerprint registry unavailable")
    for (code, period), doc in sorted(documents.items()):
        missing_meta = [
            k
            for k in ("title", "url", "disclosure_id", "published_at", "acquired_at")
            if not doc.get(k)
        ]
        if missing_meta:
            issues.append(
                f"{code} {period}: source manifest missing required metadata {', '.join(missing_meta)}"
            )
        if not all(doc.get(k) for k in ("pdf", "sha256")) or not (
            doc.get("metadata") or isinstance(doc.get("metadata_inline"), dict)
        ):
            issues.append(f"incomplete source metadata for {code} {period}")
            continue
        pdf = _safe_rel(evidence_root, doc["pdf"], "PDF")
        meta_path = (
            _safe_rel(evidence_root, doc["metadata"], "metadata") if doc.get("metadata") else None
        )
        try:
            _require_file(pdf, "annual report PDF")
            if meta_path is not None:
                _require_file(meta_path, "annual report metadata")
            digest = _sha(pdf)
            doc["pdf_size_bytes"] = pdf.stat().st_size
            if digest != doc["sha256"]:
                issues.append(f"SHA-256 mismatch for {code} {period}: {doc['pdf']}")
            metadata = _json(meta_path) if meta_path is not None else doc["metadata_inline"]
            if meta_path is not None:
                doc["metadata_size_bytes"] = meta_path.stat().st_size
                doc["metadata_sha256"] = _sha(meta_path)
            _metadata_ok(
                metadata,
                code=code,
                period=period,
                digest=doc["sha256"],
                source=doc,
                label=f"{code} {period}",
            )
            inline_metadata = doc.get("metadata_inline")
            if meta_path is not None and isinstance(inline_metadata, dict):
                _metadata_ok(
                    inline_metadata,
                    code=code,
                    period=period,
                    digest=doc["sha256"],
                    source=doc,
                    label=f"{code} {period} source-manifest metadata",
                )
        except CheckError as exc:
            issues.append(str(exc))

    supplemental = (
        source_manifest.get("sources", {})
        .get("002142.SZ", {})
        .get("supplemental_originals", {})
        .get("ningbo_final_scope")
    )
    if not supplemental:
        issues.append("Ningbo supplemental CCXI original is absent from source manifest")
    else:
        from investment_agent.industry_peers.bank_scope_bridges import ningbo_final_source_identity

        pinned = ningbo_final_source_identity()
        try:
            _validate_ccxi_identity(supplemental)
            if supplemental.get("pdf_sha256") != pinned["pdf_sha256"]:
                raise CheckError("Ningbo supplemental hash differs from pinned identity")
        except CheckError as exc:
            issues.append(str(exc))
        matching_fy24 = [item for item in fy24_supplementals if item.get("owner") == "002142.SZ"]
        if len(matching_fy24) != 1:
            issues.append("FY2024 Ningbo supplemental source must be declared exactly once")
        else:
            fy24_source = matching_fy24[0]
            try:
                for key in (
                    "source_kind",
                    "publisher",
                    "platform",
                    "disclosure_id",
                    "title",
                    "url",
                    "pdf_sha256",
                ):
                    if fy24_source.get(key) != pinned[key]:
                        raise CheckError(f"Ningbo CCXI identity mismatch for FY2024 field {key}")
                if not str(fy24_source.get("published_at", "")).startswith(pinned["published_on"]):
                    raise CheckError("Ningbo CCXI FY2024 source has a different publication date")
            except CheckError as exc:
                issues.append(f"FY2024 supplemental {exc}")
            for key in ("pdf_path", "pdf_sha256", "acquired_at"):
                if fy24_source.get(key) != supplemental.get(key):
                    issues.append(f"FY2024 Ningbo supplemental {key} differs from FY2025 manifest")
        for row in audit.get("records", []):
            descriptors = row.get("supplemental_originals")
            if descriptors is None:
                continue
            if (
                not isinstance(descriptors, list)
                or len(descriptors) != 1
                or not isinstance(descriptors[0], dict)
            ):
                issues.append(f"Ningbo audit supplemental binding malformed at {row.get('metric')}")
                continue
            try:
                _validate_ccxi_identity(descriptors[0])
            except CheckError as exc:
                issues.append(f"FY2025 field audit {exc}")
            if descriptors[0].get("source_key") != "ningbo_final_scope" or descriptors[0].get(
                "pdf_sha256"
            ) != supplemental.get("pdf_sha256"):
                issues.append(
                    f"Ningbo CCXI field audit is not bound to manifest at {row.get('metric')}"
                )
        if not supplemental.get("pdf_path") or not supplemental.get("pdf_sha256"):
            issues.append("Ningbo supplemental source lacks PDF path/hash")
        else:
            spdf = _safe_rel(evidence_root, supplemental["pdf_path"], "Ningbo supplement PDF")
            try:
                _require_file(spdf, "Ningbo supplemental PDF")
                supplemental["pdf_size_bytes"] = spdf.stat().st_size
                if _sha(spdf) != supplemental["pdf_sha256"]:
                    issues.append("Ningbo supplemental PDF SHA-256 mismatch")
            except CheckError as exc:
                issues.append(str(exc))
    if issues:
        raise CheckError("; ".join(dict.fromkeys(issues)))
    source_rows = [
        {
            "code": code,
            "period": period,
            "pdf_path": doc["pdf"],
            "pdf_size_bytes": doc.get("pdf_size_bytes"),
            "metadata_path": doc.get("metadata"),
            "metadata_size_bytes": doc.get("metadata_size_bytes"),
            "metadata_sha256": doc.get("metadata_sha256"),
            "sha256": doc["sha256"],
            "title": doc["title"],
            "url": doc["url"],
            "disclosure_id": doc["disclosure_id"],
            "published_at": doc["published_at"],
            "acquired_at": doc["acquired_at"],
        }
        for (code, period), doc in sorted(documents.items())
    ]
    optional_receipts = []
    for rel in BUNDLES:
        bundle = _json(_safe_rel(repo_root, rel, "audit bundle"))
        for by_code in bundle.get("supplemental_sources", {}).values():
            if not isinstance(by_code, dict):
                continue
            for source in by_code.values():
                if not isinstance(source, dict) or not source.get("metadata_path"):
                    continue
                receipt_path = _safe_rel(
                    evidence_root, source["metadata_path"], "optional archival receipt"
                )
                present = receipt_path.is_file() and not receipt_path.is_symlink()
                optional_receipts.append(
                    {
                        "path": source["metadata_path"],
                        "required_for_import": False,
                        "status": "available" if present else "not_present",
                        "size_bytes": receipt_path.stat().st_size if present else None,
                        "sha256": _sha(receipt_path) if present else None,
                    }
                )
    return {
        "schema": "bank-release-preflight/1",
        "ready": True,
        "scope": "artifact/evidence readiness only; not field-semantic or database acceptance",
        "matrix": MATRIX,
        "slot_count": 100,
        "status_counts": EXPECTED,
        "annual_reports": 20,
        "sources": source_rows,
        "supplier_inputs": supplier_inputs,
        "required_input_counts": {
            "annual_report_pdfs": 20,
            "annual_report_metadata_files": sum(
                bool(row.get("metadata_path")) for row in source_rows
            ),
            "fy2025_supplier_files": 11,
            "supplemental_original_pdfs": 1,
        },
        "optional_archival_inputs": optional_receipts,
        "supplemental_originals": 1,
        "supplemental_source": {
            k: supplemental.get(k)
            for k in (
                "publisher",
                "platform",
                "disclosure_id",
                "title",
                "url",
                "published_on",
                "pdf_path",
                "pdf_sha256",
                "pdf_size_bytes",
            )
        },
        "evidence_root": str(evidence_root),
        "result": "ready",
    }


def _protect_report(report: Path, repo_root: Path) -> None:
    if report.is_symlink():
        raise CheckError(f"report target may not be a symlink: {report}")
    if report.exists():
        st = report.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise CheckError(f"report target must be a single-link regular file: {report}")
        for rel in (MATRIX, *BUNDLES, FY25_AUDIT):
            source = repo_root / rel
            if source.exists() and os.path.samefile(report, source):
                raise CheckError(f"report target aliases input: {source}")
    if not report.parent.exists():
        report.parent.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="candidate checkout containing code and manifests",
    )
    parser.add_argument(
        "--evidence-root",
        required=True,
        type=Path,
        help="local tree against which manifest evidence paths resolve",
    )
    parser.add_argument(
        "--report", required=True, type=Path, help="new machine-readable JSON report path"
    )
    args = parser.parse_args(argv)
    report = args.report.absolute()
    try:
        repo = args.repo_root.resolve()
        _protect_report(report, repo)
    except (CheckError, OSError) as exc:
        print(
            json.dumps(
                {"schema": "bank-release-preflight/1", "ready": False, "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        result = run(repo, args.evidence_root.resolve())
    except (CheckError, OSError) as exc:
        result = {
            "schema": "bank-release-preflight/1",
            "ready": False,
            "scope": "artifact/evidence readiness only; not field-semantic or database acceptance",
            "error": str(exc),
        }
        try:
            with report.open("x", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError as write_exc:
            result["report_error"] = str(write_exc)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        with report.open("x", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (CheckError, OSError) as exc:
        print(
            json.dumps(
                {"schema": "bank-release-preflight/1", "ready": False, "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
