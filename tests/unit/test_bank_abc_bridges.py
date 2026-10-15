from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from investment_agent.industry_peers.bank_abc_bridges import checked_abc_scope_bridge
from investment_agent.industry_peers.originals import AnnualOriginal

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests/fixtures/bank_proofs_v2/audit-manifest.json"
SOURCE_MANIFEST_PATH = (
    ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15/source-manifest.json"
)


def _real_inputs() -> tuple[AnnualOriginal, dict[str, dict[str, Any]]]:
    if not SOURCE_MANIFEST_PATH.is_file() or not FIXTURE_PATH.is_file():
        pytest.skip("FY2025 source manifest or persistent bank-proof fixture is not present")
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    source = manifest["sources"]["601288.SH"]
    pdf_path = ROOT / source["pdf_path"]
    if not pdf_path.is_file():
        pytest.skip("ignored Agricultural Bank original PDF is not present")
    body = pdf_path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    rows = {
        row["metric"]: row
        for row in fixture["records"]
        if row.get("code") == "601288.SH"
        and row.get("metric")
        in {"roe_weighted_annual", "net_interest_margin", "npl_ratio", "provision_coverage"}
    }
    assert set(rows) == {
        "roe_weighted_annual",
        "net_interest_margin",
        "npl_ratio",
        "provision_coverage",
    }
    assert all(row.get("scope_bridge") for row in rows.values()), (
        "fixture is missing ABC scope bridges"
    )
    assert all(row.get("pdf_sha256") == digest for row in rows.values()), (
        "local Agricultural Bank PDF hash mismatch"
    )
    metadata = source["metadata"]
    original = AnnualOriginal(
        code="601288.SH",
        period=date(2025, 12, 31),
        body=body,
        metadata=metadata,
        pdf_hash=digest,
        published_at=datetime.fromisoformat("2026-03-30T16:00:00+00:00").astimezone(UTC),
    )
    return original, rows


def _proof(row: dict[str, Any]) -> dict[str, Any]:
    proof = copy.deepcopy(row)
    proof["pdf_hash"] = row["pdf_sha256"]
    proof["report_identity"] = {
        "code": row["code"],
        "period": row["period"],
        "disclosure_id": row["disclosure_id"],
    }
    proof["audit_record"] = copy.deepcopy(row)
    return proof


@pytest.mark.parametrize(
    "metric",
    ["roe_weighted_annual", "net_interest_margin", "npl_ratio", "provision_coverage"],
)
def test_abc_scope_bridges_accept_the_reviewed_original(metric: str) -> None:
    original, rows = _real_inputs()
    assert checked_abc_scope_bridge(original, metric, _proof(rows[metric]))


def test_abc_bridge_without_a_declaration_returns_false() -> None:
    original, rows = _real_inputs()
    proof = _proof(rows["net_interest_margin"])
    proof.pop("scope_bridge")
    assert not checked_abc_scope_bridge(original, "net_interest_margin", proof)


@pytest.mark.parametrize(
    "mutation",
    ["wrong_scope", "wrong_year", "wrong_value", "fake_excerpt", "column_order", "audit_mismatch"],
)
def test_abc_scope_bridge_rejects_tampered_scope_evidence(mutation: str) -> None:
    original, rows = _real_inputs()
    row = rows["net_interest_margin"]
    proof = _proof(row)
    if mutation == "wrong_scope":
        proof["accounting_scope"] = proof["audit_record"]["accounting_scope"] = "bank_standalone"
    elif mutation == "wrong_year":
        proof["current_column"] = proof["audit_record"]["current_column"] = "2024 年"
    elif mutation == "wrong_value":
        proof["raw_value"] = proof["audit_record"]["raw_value"] = "1.42"
    elif mutation == "fake_excerpt":
        evidence = proof["scope_bridge"]["evidence"]["group_vs_bank_income"]
        evidence["excerpt"] += "伪造的本集团利息净收入 569,594"
        evidence["sha256"] = hashlib.sha256(evidence["excerpt"].encode()).hexdigest()
        proof["audit_record"]["scope_bridge"] = copy.deepcopy(proof["scope_bridge"])
    elif mutation == "column_order":
        evidence = proof["scope_bridge"]["evidence"]["group_vs_bank_income"]
        evidence["excerpt"] = (
            evidence["excerpt"]
            .replace("本集团", "GROUP-TMP")
            .replace("本行", "本集团")
            .replace("GROUP-TMP", "本行")
        )
        evidence["sha256"] = hashlib.sha256(evidence["excerpt"].encode()).hexdigest()
        proof["audit_record"]["scope_bridge"] = copy.deepcopy(proof["scope_bridge"])
    elif mutation == "audit_mismatch":
        proof["scope_bridge"]["metric"] = "npl_ratio"
    if mutation in {"wrong_scope", "wrong_year", "wrong_value"}:
        proof["audit_record"]["scope_bridge"] = copy.deepcopy(proof["scope_bridge"])
    assert not checked_abc_scope_bridge(original, "net_interest_margin", proof)
