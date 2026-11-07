"""BT-011 actual annual decomposition table and interaction attribution footnote."""

import hashlib
from decimal import Decimal
from typing import Any

import pytest
from tests.next_iterations.test_bank_nii_proofs import MANIFEST, ROOT, make_fact

from investment_agent.bank_topics.contracts import AnnualPeriod, TopicFactRevision
from investment_agent.bank_topics.facts import normalize_fact_value
from investment_agent.bank_topics.proofs import (
    DECOMPOSITION_RULE,
    DECOMPOSITION_TITLE,
    BankProofError,
    check_bank_fact_proof,
    check_decomposition_proof,
)


def decomposition_fact(fact_id: str) -> TopicFactRevision:
    fact = make_fact(fact_id)
    assert fact.proof is not None
    return fact.model_copy(update={
        "proof": fact.proof.model_copy(update={"rule_version": DECOMPOSITION_RULE, "table_title": DECOMPOSITION_TITLE}),
        "comparison_period": AnnualPeriod.model_validate({"start": "2024-01-01", "end": "2024-12-31"}),
    })


@pytest.fixture(scope="module")
def body() -> bytes:
    return (ROOT / str(MANIFEST["source"]["path"])).read_bytes()


@pytest.mark.parametrize(("fact_id", "raw", "value"), [
    ("nii_volume_effect", "16,798", "16798000000"),
    ("nii_rate_effect", "(12,482)", "-12482000000"),
    ("nii_change_disclosed", "4,316", "4316000000"),
])
def test_official_decomposition_row_rederives_sign_column_unit_and_period(
    body: bytes, fact_id: str, raw: str, value: str,
) -> None:
    fact = decomposition_fact(fact_id)
    checked = check_bank_fact_proof(body, fact)
    assert checked.raw_value == raw and checked.value == Decimal(value)
    assert checked.pdf_page == 23 and checked.printed_page == "22"
    assert checked.rule_version == DECOMPOSITION_RULE
    assert fact.verification == "candidate"


@pytest.mark.parametrize("change", [
    {"column_path": "2025 年对比2024 年 / 增（减）因素 / 规模"},
    {"row_path": "利息支出变动"}, {"pdf_page": 22, "printed_page": "21"},
    {"rule_version": "bank-annual-original/2"}, {"rule_version": "bank-scope-bridge/1"},
    {"table_title": "本行规模变化"},
])
def test_wrong_net_row_column_page_or_scope_bridge_rule_is_rejected(body: bytes, change: dict[str, Any]) -> None:
    fact = decomposition_fact("nii_rate_effect")
    assert fact.proof is not None
    with pytest.raises(BankProofError):
        check_decomposition_proof(body, fact.model_copy(update={"proof": fact.proof.model_copy(update=change)}))


@pytest.mark.parametrize("change", [
    {"raw_value": "12,482"}, {"value": Decimal("12482000000")}, {"raw_unit": "CNY"},
    {"comparison_period": None},
    {"comparison_period": AnnualPeriod.model_validate({"start": "2023-01-01", "end": "2023-12-31"})},
])
def test_negative_sign_conversion_and_annual_comparison_are_not_inferred(body: bytes, change: dict[str, Any]) -> None:
    with pytest.raises(BankProofError):
        check_decomposition_proof(body, decomposition_fact("nii_rate_effect").model_copy(update=change))


def test_proof_cannot_omit_the_interaction_allocation_rule(body: bytes) -> None:
    fact = decomposition_fact("nii_volume_effect")
    assert fact.proof is not None
    # This suffix remains in the real page, but drops the scope and interaction note.
    excerpt = fact.proof.excerpt[fact.proof.excerpt.index("2025 年对比2024 年"):]
    changed = fact.proof.model_copy(update={"excerpt": excerpt, "excerpt_hash": hashlib.sha256(excerpt.encode()).hexdigest()})
    with pytest.raises(BankProofError):
        check_decomposition_proof(body, fact.model_copy(update={"proof": changed}))


def test_parenthesized_printed_amount_is_normalized_once() -> None:
    value = normalize_fact_value("nii_rate_effect", "(12,482)", "CNY_million")
    assert value.value == Decimal("-12482000000") and value.raw_value == "(12,482)"
    with pytest.raises(ValueError):
        normalize_fact_value("nii_rate_effect", "-12482000000", "CNY")
