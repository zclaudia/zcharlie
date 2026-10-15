"""Independent acceptance: new scope rules cannot promote old weak locators."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from investment_agent.industry_peers.bank_proofs import checked_bank_locator
from investment_agent.industry_peers.originals import AnnualOriginal

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/bank_proofs_v2"
SOURCE = ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15/source-manifest.json"
BEFORE = json.loads((FIXTURES / "candidates-before-followup.json").read_text())["records"]


def _original_and_proof(record: dict) -> tuple[AnnualOriginal, dict]:
    if not SOURCE.is_file() or not (ROOT / record["pdf_path"]).is_file():
        pytest.skip("acceptance requires the exact bank PDF and official source manifest")
    source = json.loads(SOURCE.read_text())["sources"][record["code"]]
    metadata = source["metadata"]
    body = (ROOT / record["pdf_path"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == record["pdf_sha256"]
    original = AnnualOriginal(
        record["code"], date(2025, 12, 31), body, metadata, record["pdf_sha256"],
        datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    proof = {
        **copy.deepcopy(record),
        "pdf_hash": record["pdf_sha256"],
        "report_identity": {
            "code": record["code"], "period": record["period"],
            "disclosure_id": record["disclosure_id"],
        },
        "audit_record": copy.deepcopy(record),
    }
    return original, proof


@pytest.mark.parametrize("record", BEFORE, ids=lambda r: f"{r['code']}-{r['metric']}")
def test_old_candidate_cannot_be_promoted_with_only_status_and_scope_claim(record: dict) -> None:
    original, proof = _original_and_proof(record)
    for obj in (proof, proof["audit_record"]):
        obj["status"] = "verified"
        obj["accounting_scope"] = "group_consolidated"
        obj["current_column"] = "本集团 2025年"
    with pytest.raises(ValueError):
        checked_bank_locator(original, record["metric"], proof)


BRIDGED_FIELDS = [
    ("601288.SH", metric) for metric in (
        "roe_weighted_annual", "net_interest_margin", "npl_ratio", "provision_coverage",
    )
] + [("000001.SZ", "roe_weighted_annual"), ("002142.SZ", "net_interest_margin")]


@pytest.mark.parametrize("code,metric", BRIDGED_FIELDS)
def test_removing_the_bridge_cannot_borrow_group_words_from_other_sections(code: str, metric: str) -> None:
    records = json.loads((FIXTURES / "audit-manifest.json").read_text())["records"]
    record = next(r for r in records if r["code"] == code and r["metric"] == metric)
    assert record["status"] == "verified"
    assert record["scope_basis"] == "cross_statement_bridge"
    original, proof = _original_and_proof(record)
    for obj in (proof, proof["audit_record"]):
        obj.pop("scope_bridge")
        obj["current_column"] = "本集团 2025年"
    with pytest.raises(ValueError):
        checked_bank_locator(original, metric, proof)
