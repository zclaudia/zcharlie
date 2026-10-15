"""Adversarial checks for Ningbo's FY2025 final consolidated ratio bridge."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from investment_agent.industry_peers.bank_proofs import BANK_RULE, checked_bank_locator
from investment_agent.industry_peers.originals import AnnualOriginal

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests/fixtures/bank_proofs_v2/audit-manifest.json"
SOURCE_MANIFEST_PATH = (
    ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15/source-manifest.json"
)
EXPECTED_CCXI_SHA256 = "73037b6f1cda68ebf483e04add43155cd1695b15faacb5605622b6d87fff0477"


def _inputs(metric: str) -> tuple[AnnualOriginal, dict[str, Any], dict[str, AnnualOriginal]]:
    if not FIXTURE_PATH.is_file() or not SOURCE_MANIFEST_PATH.is_file():
        pytest.skip("Ningbo FY2025 fixture and official source manifest are not present")
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    row = next(
        item for item in fixture["records"]
        if item.get("code") == "002142.SZ" and item.get("metric") == metric
    )
    annual_path = ROOT / row["pdf_path"]
    source_entry = manifest["sources"]["002142.SZ"]
    supplemental = source_entry["supplemental_originals"]["ningbo_final_scope"]
    rating_path = ROOT / supplemental["pdf_path"]
    if not annual_path.is_file() or not rating_path.is_file():
        pytest.skip("Ningbo annual-report or CCXI rating original PDF is not present")

    annual_body = annual_path.read_bytes()
    annual_hash = hashlib.sha256(annual_body).hexdigest()
    assert annual_hash == row["pdf_sha256"]
    annual = AnnualOriginal(
        code="002142.SZ",
        period=date(2025, 12, 31),
        body=annual_body,
        metadata=source_entry["metadata"],
        pdf_hash=annual_hash,
        published_at=datetime.fromisoformat(row["published_at"]).astimezone(UTC),
    )

    rating_body = rating_path.read_bytes()
    rating_hash = hashlib.sha256(rating_body).hexdigest()
    assert rating_hash == EXPECTED_CCXI_SHA256
    acquired_at = datetime.fromisoformat(supplemental["acquired_at"]).astimezone(UTC)
    rating = AnnualOriginal(
        code="002142.SZ",
        period=date(2025, 12, 31),
        body=rating_body,
        metadata=supplemental,
        pdf_hash=rating_hash,
        # We only know when these bytes were acquired, not a precise publication
        # instant with timezone, so use acquisition time as the conservative bound.
        published_at=acquired_at,
    )
    proof = copy.deepcopy(row)
    proof["pdf_hash"] = row["pdf_sha256"]
    proof["report_identity"] = {
        "code": row["code"],
        "period": row["period"],
        "disclosure_id": row["disclosure_id"],
    }
    proof["audit_record"] = copy.deepcopy(row)
    return annual, proof, {"ningbo_final_scope": rating}


def _sync_bridge(proof: dict[str, Any]) -> None:
    proof["audit_record"]["scope_bridge"] = copy.deepcopy(proof["scope_bridge"])


@pytest.mark.parametrize("metric", ["npl_ratio", "provision_coverage"])
def test_ningbo_final_consolidated_ratio_bridge_accepts_real_fy2025_originals(
    metric: str,
) -> None:
    annual, proof, supplements = _inputs(metric)
    checks = checked_bank_locator(
        annual, metric, proof, supplemental_originals=supplements
    )
    assert checks["accounting_scope"] == "group_consolidated"
    assert checks["scope_basis"] == "cross_statement_bridge"
    assert checks["check_rule_version"] == BANK_RULE


def _mutate_proof(mutation: str, proof: dict[str, Any]) -> None:
    bridge = proof["scope_bridge"]
    if mutation == "wrong_publication_identity":
        proof["supplemental_originals"][0]["publisher"] = "宁波银行股份有限公司"
    elif mutation == "wrong_download_url":
        proof["supplemental_originals"][0]["url"] = "https://example.invalid/report.pdf"
    elif mutation == "wrong_recorded_hash":
        proof["supplemental_originals"][0]["pdf_sha256"] = "0" * 64
    elif mutation == "wrong_period":
        proof["period"] = proof["audit_record"]["period"] = "2024-12-31"
        proof["report_identity"]["period"] = "2024-12-31"
    elif mutation == "wrong_issuer":
        proof["report_identity"]["code"] = "600919.SH"
        proof["audit_record"]["code"] = "600919.SH"
    elif mutation == "comparative_column":
        bridge["current_column"] = "2024"
    elif mutation == "comparative_coverage_value":
        bridge["source_value"] = "389.35"
    elif mutation == "threshold_not_ratio":
        proof["raw_value"] = proof["audit_record"]["raw_value"] = "150"
        bridge["source_value"] = "150"
    elif mutation == "borrow_ccxi_nim":
        proof["raw_value"] = proof["audit_record"]["raw_value"] = "1.77"
        bridge["source_metric_label"] = "净息差(%)"
        bridge["source_value"] = "1.77"
    elif mutation == "missing_note3":
        del bridge["note3_excerpt"]
    elif mutation == "partial_page_excerpt":
        bridge["source_excerpt"] = "宁波银行（合并口径）2023 2024 2025"
        bridge["source_excerpt_sha256"] = hashlib.sha256(
            bridge["source_excerpt"].encode("utf-8")
        ).hexdigest()
    elif mutation == "note3_without_annual_report_basis":
        bridge["note3_excerpt"] = "3.净息差等其他指标为中诚信国际统计口径。"
        bridge["note3_sha256"] = hashlib.sha256(
            bridge["note3_excerpt"].encode("utf-8")
        ).hexdigest()
    elif mutation == "bridge_audit_disagreement":
        bridge["source_value"] = "0.77"
        return
    else:  # pragma: no cover - keeps accidental test typos visible
        raise AssertionError(f"unknown mutation: {mutation}")
    _sync_bridge(proof)
    if mutation in {"wrong_publication_identity", "wrong_download_url", "wrong_recorded_hash"}:
        proof["audit_record"]["supplemental_originals"] = copy.deepcopy(
            proof["supplemental_originals"]
        )


@pytest.mark.parametrize(
    "metric,mutation",
    [
        ("npl_ratio", "wrong_publication_identity"),
        ("provision_coverage", "wrong_download_url"),
        ("npl_ratio", "wrong_recorded_hash"),
        ("npl_ratio", "wrong_period"),
        ("provision_coverage", "wrong_issuer"),
        ("npl_ratio", "comparative_column"),
        ("provision_coverage", "comparative_coverage_value"),
        ("provision_coverage", "threshold_not_ratio"),
        ("npl_ratio", "borrow_ccxi_nim"),
        ("npl_ratio", "missing_note3"),
        ("provision_coverage", "partial_page_excerpt"),
        ("npl_ratio", "note3_without_annual_report_basis"),
        ("provision_coverage", "bridge_audit_disagreement"),
    ],
)
def test_ningbo_final_scope_rejects_tampered_source_and_noncurrent_rows(
    metric: str, mutation: str
) -> None:
    annual, proof, supplements = _inputs(metric)
    _mutate_proof(mutation, proof)
    with pytest.raises(ValueError):
        checked_bank_locator(
            annual, metric, proof, supplemental_originals=supplements
        )


def test_ningbo_final_scope_rejects_missing_supplemental_bytes() -> None:
    annual, proof, _supplements = _inputs("npl_ratio")
    with pytest.raises(ValueError, match="source bytes are missing"):
        checked_bank_locator(annual, "npl_ratio", proof, supplemental_originals={})


def test_ningbo_final_scope_cannot_be_checked_before_supplement_was_acquired() -> None:
    annual, proof, supplements = _inputs("npl_ratio")
    acquired = datetime.fromisoformat(proof["supplemental_originals"][0]["acquired_at"])
    checked = (acquired - timedelta(microseconds=1)).isoformat()
    proof["checked_at"] = proof["audit_record"]["checked_at"] = checked
    with pytest.raises(ValueError, match="not available when the field was checked"):
        checked_bank_locator(annual, "npl_ratio", proof, supplemental_originals=supplements)


def test_ningbo_final_scope_cannot_use_an_acquisition_without_timezone() -> None:
    annual, proof, supplements = _inputs("provision_coverage")
    descriptor = proof["supplemental_originals"][0]
    acquired = datetime.fromisoformat(descriptor["acquired_at"])
    descriptor["acquired_at"] = acquired.replace(tzinfo=None).isoformat()
    proof["audit_record"]["supplemental_originals"] = copy.deepcopy(proof["supplemental_originals"])
    with pytest.raises(ValueError, match="must include timezone"):
        checked_bank_locator(annual, "provision_coverage", proof, supplemental_originals=supplements)


def test_ningbo_scope_supplement_cannot_be_attached_to_an_unapproved_metric() -> None:
    annual, ratio_proof, supplements = _inputs("npl_ratio")
    row = next(
        item for item in json.loads(FIXTURE_PATH.read_text())["records"]
        if item["code"] == "002142.SZ" and item["metric"] == "net_interest_margin"
    )
    proof = copy.deepcopy(row)
    proof.update(
        pdf_hash=row["pdf_sha256"], report_identity=ratio_proof["report_identity"],
        audit_record=copy.deepcopy(row),
    )
    proof["supplemental_originals"] = copy.deepcopy(ratio_proof["supplemental_originals"])
    proof["audit_record"]["supplemental_originals"] = copy.deepcopy(proof["supplemental_originals"])
    with pytest.raises(ValueError, match="unsupported for this bank field"):
        checked_bank_locator(annual, "net_interest_margin", proof, supplemental_originals=supplements)


@pytest.mark.parametrize("field,value", [("code", "600919.SH"), ("period", date(2024, 12, 31))])
def test_ningbo_final_scope_rejects_wrong_supplemental_original_identity(
    field: str, value: Any
) -> None:
    annual, proof, supplements = _inputs("provision_coverage")
    wrong_original = copy.copy(supplements["ningbo_final_scope"])
    object.__setattr__(wrong_original, field, value)
    supplements["ningbo_final_scope"] = wrong_original
    with pytest.raises(ValueError, match="source bytes do not match"):
        checked_bank_locator(
            annual, "provision_coverage", proof, supplemental_originals=supplements
        )


def test_ningbo_final_scope_rejects_altered_supplemental_pdf_bytes() -> None:
    annual, proof, supplements = _inputs("npl_ratio")
    original = supplements["ningbo_final_scope"]
    altered = copy.copy(original)
    object.__setattr__(altered, "body", original.body + b"\n")
    supplements["ningbo_final_scope"] = altered
    with pytest.raises(ValueError, match="source bytes do not match"):
        checked_bank_locator(
            annual, "npl_ratio", proof, supplemental_originals=supplements
        )


@pytest.mark.parametrize("field,value", [("code", "600919.SH"), ("period", date(2024, 12, 31))])
def test_ningbo_final_scope_rejects_wrong_annual_original_identity(
    field: str, value: Any
) -> None:
    annual, proof, supplements = _inputs("npl_ratio")
    wrong_annual = copy.copy(annual)
    object.__setattr__(wrong_annual, field, value)
    with pytest.raises(ValueError):
        checked_bank_locator(
            wrong_annual, "npl_ratio", proof, supplemental_originals=supplements
        )
