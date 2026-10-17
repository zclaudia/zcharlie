"""Safety and completeness boundaries for the offline release preflight."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bank_release_preflight", ROOT / "tools/bank_release_preflight.py"
)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for rel in (*preflight.BUNDLES, preflight.MATRIX, preflight.FY25_AUDIT):
        src = ROOT / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return repo


def _source_manifest(tmp_path: Path) -> Path:
    evidence = tmp_path / "evidence"
    audit = json.loads((ROOT / preflight.FY25_AUDIT).read_text())
    records = audit["records"]
    identity = next(
        r["supplemental_originals"][0] for r in records if r.get("supplemental_originals")
    )
    sources: dict[str, dict] = {}
    for row in records:
        source = sources.setdefault(
            row["code"],
            {
                "source": "fixture",
                "pdf_path": row["pdf_path"],
                "metadata_path": None,
                "acquired_at": row["acquired_at"],
            },
        )
        source["metadata"] = {
            "code": row["code"],
            "pdf_sha256": row["pdf_sha256"],
            "url": row["url"],
            "chosen": {
                "secCode": row["code"][:6],
                "announcementId": row["disclosure_id"],
                "announcementTitle": row["title"],
            },
        }
        if row["code"] == "002142.SZ":
            source["supplemental_originals"] = {
                "ningbo_final_scope": {
                    **identity,
                    "pdf_path": "research/fixture/ningbo-ccxi.pdf",
                }
            }
    manifest = {
        "schema": "bank-proof-source-manifest/1",
        "report_period": "2025-12-31",
        "sources": sources,
    }
    source_rel = audit["source_manifest"]
    dest = evidence / source_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    supplier_fixture = ROOT / "tests/fixtures/bank_run02"
    supplier_root = evidence / preflight.FY25_SUPPLIER_ROOT
    for rel in (
        "sample.json",
        *(f"raw/financial/{code}_main.json" for code in preflight.BANK_ORDER),
    ):
        target = supplier_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(supplier_fixture / rel, target)
    return evidence


def test_missing_actual_original_fails_with_full_location(tmp_path):
    repo = _fixture_repo(tmp_path)
    evidence = _source_manifest(tmp_path)
    with pytest.raises(preflight.CheckError) as exc:
        preflight.run(repo, evidence)
    message = str(exc.value)
    assert "missing or non-regular annual report PDF" in message
    assert "2024" in message or "2025" in message


def test_matrix_status_count_or_missing_remediation_fails(tmp_path):
    repo = _fixture_repo(tmp_path)
    matrix_path = repo / preflight.MATRIX
    matrix = json.loads(matrix_path.read_text())
    candidate = next(r for r in matrix["records"] if r["status"] == "candidate")
    candidate["remediation"] = ""
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    evidence = _source_manifest(tmp_path)
    with pytest.raises(preflight.CheckError) as exc:
        preflight.run(repo, evidence)
    assert "non-verified row needs null value, reason, and remediation" in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("value", "999", "matrix value differs from audit"),
        ("observed_raw_value", "tampered", "matrix observed_raw_value differs from audit"),
        ("pdf_page", 999, "matrix pdf_page differs from audit"),
    ],
)
def test_matrix_slot_fields_are_bound_to_field_audit(tmp_path, field, value, expected):
    repo = _fixture_repo(tmp_path)
    matrix_path = repo / preflight.MATRIX
    matrix = json.loads(matrix_path.read_text())
    row = next(
        r
        for r in matrix["records"]
        if r["report_period"] == "2024-12-31" and r["status"] == "verified"
    )
    row[field] = value
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(preflight.CheckError) as exc:
        preflight.run(repo, _source_manifest(tmp_path))
    assert expected in str(exc.value)


def test_matrix_status_cannot_diverge_from_field_audit(tmp_path):
    repo = _fixture_repo(tmp_path)
    matrix_path = repo / preflight.MATRIX
    matrix = json.loads(matrix_path.read_text())
    row = next(
        r
        for r in matrix["records"]
        if r["report_period"] == "2024-12-31" and r["status"] == "verified"
    )
    row["status"] = "candidate"
    row["value"] = None
    row["reason"] = "changed status"
    row["remediation"] = "repair source audit binding"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(preflight.CheckError) as exc:
        preflight.run(repo, _source_manifest(tmp_path))
    assert "matrix status differs from audit" in str(exc.value)


def test_fy2024_locator_mutation_breaks_registered_fingerprint(tmp_path):
    repo = _fixture_repo(tmp_path)
    bundle_path = repo / preflight.BUNDLES[0]
    bundle = json.loads(bundle_path.read_text())
    bundle["records"][0]["locator_excerpt_sha256"] = "0" * 64
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(preflight.CheckError) as exc:
        preflight.run(repo, _source_manifest(tmp_path))
    assert "FY2024 reviewed locator fingerprint mismatch" in str(exc.value)


def test_changed_ccxi_identity_fails_even_when_manifest_is_self_consistent(tmp_path):
    repo = _fixture_repo(tmp_path)
    evidence = _source_manifest(tmp_path)
    audit_path = repo / preflight.FY25_AUDIT
    audit = json.loads(audit_path.read_text())
    source_path = evidence / audit["source_manifest"]
    source = json.loads(source_path.read_text())
    ccxi = source["sources"]["002142.SZ"]["supplemental_originals"]["ningbo_final_scope"]
    replacement = b"%PDF-1.7 replacement report\n%%EOF\n"
    replacement_hash = hashlib.sha256(replacement).hexdigest()
    replacement_path = evidence / "research/fixture/ningbo-ccxi.pdf"
    replacement_path.parent.mkdir(parents=True, exist_ok=True)
    replacement_path.write_bytes(replacement)
    ccxi["pdf_path"] = "research/fixture/ningbo-ccxi.pdf"
    ccxi["pdf_sha256"] = replacement_hash
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    for row in audit["records"]:
        descriptors = row.get("supplemental_originals")
        if descriptors:
            descriptors[0]["pdf_sha256"] = replacement_hash
    audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(preflight.CheckError) as exc:
        preflight.run(repo, evidence)
    assert "Ningbo CCXI identity mismatch for pdf_sha256" in str(exc.value)


def test_supplier_input_inventory_reports_all_11_file_hashes(tmp_path):
    evidence = _source_manifest(tmp_path)
    inventory = preflight._supplier_inputs(evidence)
    assert len(inventory) == 11
    assert all(row["size_bytes"] > 0 and len(row["sha256"]) == 64 for row in inventory)
    assert inventory[0]["path"].endswith("run-02/sample.json")


def test_missing_supplier_file_and_missing_supplier_field_fail(tmp_path):
    evidence = _source_manifest(tmp_path)
    supplier_root = evidence / preflight.FY25_SUPPLIER_ROOT
    missing_file = supplier_root / "raw/financial/601398.SH_main.json"
    missing_file.unlink()
    with pytest.raises(preflight.CheckError, match="missing or non-regular FY2025 supplier input"):
        preflight._supplier_inputs(evidence)

    evidence = _source_manifest(tmp_path / "field")
    supplier_file = evidence / preflight.FY25_SUPPLIER_ROOT / "raw/financial/601398.SH_main.json"
    payload = json.loads(supplier_file.read_text())
    del payload["result"]["data"][0]["ROEJQ"]
    supplier_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(preflight.CheckError, match="supplier plan is invalid"):
        preflight._supplier_inputs(evidence)


def test_null_supplier_fact_is_not_reported_ready(tmp_path):
    evidence = _source_manifest(tmp_path)
    supplier_file = evidence / preflight.FY25_SUPPLIER_ROOT / "raw/financial/601398.SH_main.json"
    payload = json.loads(supplier_file.read_text())
    payload["result"]["data"][0]["ROEJQ"] = None
    supplier_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(preflight.CheckError, match="supplier data has null required facts"):
        preflight._supplier_inputs(evidence)


def test_supplier_sample_roster_must_match_fixed_ten(tmp_path):
    evidence = _source_manifest(tmp_path)
    sample = evidence / preflight.FY25_SUPPLIER_ROOT / "sample.json"
    payload = json.loads(sample.read_text())
    payload["bank"] = payload["bank"][:-1]
    sample.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(preflight.CheckError, match="sample bank roster differs"):
        preflight._supplier_inputs(evidence)


def test_symlinked_input_cannot_escape_evidence_root(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"not evidence")
    (root / "escape.pdf").symlink_to(outside)
    with pytest.raises(preflight.CheckError, match="escapes configured root"):
        preflight._safe_rel(root.resolve(), "escape.pdf", "PDF")


def test_report_refuses_symlink_and_hardlink_targets(tmp_path):
    repo = _fixture_repo(tmp_path)
    actual = tmp_path / "actual-input.json"
    actual.write_text("{}", encoding="utf-8")
    symlink = tmp_path / "symlink-report.json"
    symlink.symlink_to(actual)
    with pytest.raises(preflight.CheckError, match="may not be a symlink"):
        preflight._protect_report(symlink, repo)

    hardlink = tmp_path / "hardlink-report.json"
    os.link(actual, hardlink)
    with pytest.raises(preflight.CheckError, match="single-link regular file"):
        preflight._protect_report(hardlink, repo)


def test_report_cannot_replace_matrix_by_same_path_or_existing_file(tmp_path):
    repo = _fixture_repo(tmp_path)
    matrix = repo / preflight.MATRIX
    before = matrix.read_bytes()
    with pytest.raises(preflight.CheckError, match="aliases input"):
        preflight._protect_report(matrix, repo)
    assert matrix.read_bytes() == before
