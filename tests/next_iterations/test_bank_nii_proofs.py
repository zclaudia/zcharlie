"""BT-010 actual stored PDF locators; UUIDs below are synthetic, not imported refs."""

import json
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from investment_agent.bank_topics.contracts import AnnualPeriod, TopicFactRevision
from investment_agent.bank_topics.facts import normalize_fact_value
from investment_agent.bank_topics.proofs import (
    ANNUAL_TITLE,
    NII_RULE,
    BankProofError,
    check_nii_nim_proof,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/bank_topics"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text())
EXPECTED = json.loads((FIXTURE / "expected.json").read_text())


def make_fact(fact_id: str) -> TopicFactRevision:
    row = next(row for row in EXPECTED["facts"] if row["id"] == fact_id)
    locator = MANIFEST["locators"][row["locator_id"]]
    year = 2024 if row["period_key"] == "FY2024" else 2025
    original_ref = {"kind": "disclosure_original", "id": str(uuid4()), "revision": 1}
    return TopicFactRevision.model_validate({
        "id": str(uuid4()), "owner_id": "local_owner", "case_id": str(uuid4()),
        "stable_fact_id": row["id"], "revision": 1, "metric": row["metric"],
        "value": row["value"], "unit": row["unit"], "raw_value": row["raw_value"], "raw_unit": row["raw_unit"],
        "period": {"start": f"{year}-01-01", "end": f"{year}-12-31"},
        "comparison_period": None, "source_refs": [original_ref], "verification": "candidate", "unknown_reason": None,
        "proof": {"original_ref": original_ref, "pdf_sha256": MANIFEST["source"]["sha256"],
                  "pdf_page": row["pdf_page"], "printed_page": row["printed_page"],
                  "table_title": ANNUAL_TITLE, "row_path": row["row"], "column_path": row["column_path"],
                  "excerpt": locator["excerpt"], "excerpt_hash": locator["excerpt_sha256"], "rule_version": NII_RULE},
        "timing": {"published_at": None, "published_date": "2026-03-28", "published_precision": "date",
                   "acquired_at": MANIFEST["source"]["acquired_at"], "checked_at": None, "review_actor": None},
    })


@pytest.fixture(scope="module")
def body() -> bytes:
    return (ROOT / str(MANIFEST["source"]["path"])).read_bytes()


@pytest.mark.parametrize("fact_id", ["nii_2025", "nii_2024", "nim_2025", "nim_2024"])
def test_exact_nii_nim_columns_rederive_values_without_promoting_candidate(body: bytes, fact_id: str) -> None:
    fact = make_fact(fact_id)
    proof = check_nii_nim_proof(body, fact)
    assert proof.value == fact.value and proof.raw_value == fact.raw_value
    assert proof.pdf_page == 22 and proof.printed_page == "21"
    assert fact.verification == "candidate" and fact.timing.checked_at is None


@pytest.mark.parametrize("change", [
    {"column_path": "2024 年 / 利息收入"}, {"row_path": "净利差"}, {"column_path": "本行 / 2025 年 / 利息收入"},
    {"pdf_page": 23, "printed_page": "22"}, {"printed_page": "22"},
    {"pdf_sha256": "a" * 64}, {"excerpt_hash": "a" * 64},
    {"table_title": "公司利润表"}, {"rule_version": "bank-annual-original/2"},
])
def test_wrong_page_column_scope_hash_and_old_rule_are_rejected(body: bytes, change: dict[str, Any]) -> None:
    fact = make_fact("nii_2025")
    assert fact.proof is not None
    with pytest.raises(BankProofError):
        check_nii_nim_proof(body, fact.model_copy(update={"proof": fact.proof.model_copy(update=change)}))


@pytest.mark.parametrize("change", [
    {"value": "215593"}, {"raw_unit": "CNY"}, {"raw_value": "211,277"},
    {"unit": "fraction"}, {"reporting_scope": "standalone"}, {"currency": "USD"},
])
def test_standardization_cannot_be_repeated_or_scope_claims_swapped(body: bytes, change: dict[str, Any]) -> None:
    fact = make_fact("nii_2025").model_copy(update=change)
    with pytest.raises(BankProofError):
        check_nii_nim_proof(body, fact)


def test_quarter_and_altered_original_are_rejected(body: bytes) -> None:
    quarterly = AnnualPeriod.model_construct(start=date(2025, 1, 1), end=date(2025, 3, 31),
                                             basis="period_to_date/quarter")
    with pytest.raises(BankProofError) as caught:
        check_nii_nim_proof(body, make_fact("nii_2025").model_copy(update={"period": quarterly}))
    assert caught.value.code == "period_mismatch"
    with pytest.raises(BankProofError) as caught:
        check_nii_nim_proof(body + b"changed", make_fact("nii_2025"))
    assert caught.value.code == "source_conflict"


@pytest.mark.parametrize("raw", [True, 1.5, "NaN", "Infinity", "1e1000000", "1,23", "(1)-", ""])
def test_normalization_has_no_float_nonfinite_or_ambiguous_token_route(raw: Any) -> None:
    with pytest.raises(ValueError):
        normalize_fact_value("nii", raw, "CNY_million")


def test_normalization_retains_printed_unit_and_applies_single_exact_conversion() -> None:
    amount = normalize_fact_value("nii", "215,593", "CNY_million")
    margin = normalize_fact_value("nim", "1.87", "percent")
    assert str(amount.value) == "215593000000" and amount.raw_value == "215,593"
    assert str(margin.value) == "0.0187" and margin.raw_unit == "percent"
    with pytest.raises(ValueError):
        normalize_fact_value("nim", "0.0187", "fraction")
