"""Independent exact-source rules for CMB FY2025 NII/NIM.

This conservative profile covers the reviewed local annual report only. A new
PDF/correction needs its own audited rule; existing bank-v2/bridge rules do not
implicitly support these topic fields. Pure checks do not change saved status.
"""

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from investment_agent.bank_topics.contracts import TopicFactRevision
from investment_agent.bank_topics.facts import normalize_fact_value
from investment_agent.industry_peers.bank_annual_identity import checked_bank_front_identity
from investment_agent.industry_peers.bank_pdf_text import bank_page_text

NII_RULE = "bank-topic-cmb-fy2025-nii-nim/1"
CMB_FY2025_SHA256 = "abe612a273468072b176dd51ea460c1e1596f8ca729cbc6db3fa28ba9a57ea79"
ANNUAL_TITLE = "3.2.5 净利息收入"


class BankProofError(ValueError):
    def __init__(self, code: Literal["unsupported_source", "source_conflict", "scope_conflict", "invalid_proof", "unit_mismatch", "period_mismatch"]) -> None:
        self.code = code
        super().__init__(f"bank topic proof rejected: {code}")


@dataclass(frozen=True)
class CheckedBankProof:
    metric: str
    value: Decimal
    raw_value: str
    rule_version: str
    pdf_sha256: str
    pdf_page: int
    printed_page: str
    row: str
    column: str


def canonical_excerpt(text: str) -> str:
    return " ".join(text.split())


def _base_checks(body: bytes, fact: TopicFactRevision, *, rule: str, page: int) -> str:
    proof = fact.proof
    if proof is None or proof.rule_version != rule:
        raise BankProofError("invalid_proof")
    digest = hashlib.sha256(body).hexdigest()
    if not body.startswith(b"%PDF-") or digest != proof.pdf_sha256:
        raise BankProofError("source_conflict")
    if digest != CMB_FY2025_SHA256:
        raise BankProofError("unsupported_source")
    if proof.original_ref.kind != "disclosure_original" or proof.original_ref.revision != 1 or proof.original_ref.field_path is not None:
        raise BankProofError("invalid_proof")
    if fact.reporting_scope != "consolidated" or fact.accounting_standard != "CAS" or fact.currency != "CNY":
        raise BankProofError("scope_conflict")
    checked_bank_front_identity(body, 2025, "600036", "招商银行股份有限公司")
    if proof.pdf_page != page or proof.printed_page != str(page - 1):
        raise BankProofError("invalid_proof")
    actual = bank_page_text(body, page)
    if actual.splitlines()[2] != proof.printed_page:
        raise BankProofError("invalid_proof")
    text = canonical_excerpt(actual)
    excerpt = canonical_excerpt(proof.excerpt)
    if hashlib.sha256(proof.excerpt.encode()).hexdigest() != proof.excerpt_hash or excerpt not in text:
        raise BankProofError("invalid_proof")
    # Scope, CAS and calendar-year definitions are checked from this same PDF.
    group = canonical_excerpt(bank_page_text(body, 3))
    accounting = canonical_excerpt(bank_page_text(body, 143))
    if "本集团 ： 招商银行及其子公司" not in group or "企业会计准则" not in accounting or "会计年度自公历1 月1日起至12月 31日止。" not in accounting:
        raise BankProofError("scope_conflict")
    return text


def check_nii_nim_proof(body: bytes, fact: TopicFactRevision) -> CheckedBankProof:
    if fact.metric not in {"nii", "nim"}:
        raise BankProofError("invalid_proof")
    text = _base_checks(body, fact, rule=NII_RULE, page=22)
    proof = fact.proof
    assert proof is not None
    year = fact.period.end.year
    if year not in {2024, 2025} or fact.period.start.isoformat() != f"{year}-01-01" or fact.period.end.isoformat() != f"{year}-12-31" or fact.period.basis != "period_to_date/annual":
        raise BankProofError("period_mismatch")
    if fact.comparison_period is not None and (year != 2025 or fact.comparison_period.start.isoformat() != "2024-01-01" or fact.comparison_period.end.isoformat() != "2024-12-31" or fact.comparison_period.basis != "period_to_date/annual"):
        raise BankProofError("period_mismatch")
    row = "净利息收入" if fact.metric == "nii" else "净利息收益率"
    column = f"{year} 年 / " + ("利息收入" if fact.metric == "nii" else "平均收益率%")
    if proof.table_title != ANNUAL_TITLE or proof.row_path != row or proof.column_path != column:
        raise BankProofError("invalid_proof")
    if fact.metric == "nii":
        pattern = r"净利息收入 ／ ([\d,]+) ／ ／ ([\d,]+) ／"
    else:
        pattern = r"净利息收益率 ／ ／ (\d+\.\d+) ／ ／ (\d+\.\d+)"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise BankProofError("invalid_proof")
    match = matches[0]
    excerpt = canonical_excerpt(proof.excerpt)
    required = (ANNUAL_TITLE, "本集团资产负债项目", "2025 年 2024 年", "（人民币百万元，百分比除外）", match.group())
    if any(token not in excerpt for token in required):
        raise BankProofError("invalid_proof")
    raw = match.group(1 if year == 2025 else 2)
    if fact.raw_value != raw:
        raise BankProofError("invalid_proof")
    try:
        normalized = normalize_fact_value(fact.metric, raw, fact.raw_unit or "")
    except ValueError as exc:
        raise BankProofError("unit_mismatch") from exc
    if fact.unit != normalized.unit or fact.value != normalized.value:
        raise BankProofError("unit_mismatch")
    return CheckedBankProof(fact.metric, normalized.value, raw, NII_RULE, proof.pdf_sha256,
                            22, "21", row, column)


DECOMPOSITION_RULE = "bank-topic-cmb-fy2025-disclosed-volume-rate/1"
DECOMPOSITION_TITLE = "本集团由于规模变化和利率变化导致利息收入和利息支出变化的分布情况"
INTERACTION_RULE = "由规模变化和利率变化共同引起的利息收支变化，计入规模变化对利息收支变化的影响金额。"


def check_decomposition_proof(body: bytes, fact: TopicFactRevision) -> CheckedBankProof:
    columns = {"nii_volume_effect": (1, "规模"), "nii_rate_effect": (2, "利率"),
               "nii_change_disclosed": (3, "增（减）净值")}
    if fact.metric not in columns:
        raise BankProofError("invalid_proof")
    text = _base_checks(body, fact, rule=DECOMPOSITION_RULE, page=23)
    proof = fact.proof
    assert proof is not None
    if fact.period.start.isoformat() != "2025-01-01" or fact.period.end.isoformat() != "2025-12-31" or fact.period.basis != "period_to_date/annual":
        raise BankProofError("period_mismatch")
    comparison = fact.comparison_period
    if comparison is None or comparison.start.isoformat() != "2024-01-01" or comparison.end.isoformat() != "2024-12-31" or comparison.basis != "period_to_date/annual":
        raise BankProofError("period_mismatch")
    position, column_name = columns[fact.metric]
    column = f"2025 年对比2024 年 / 增（减）因素 / {column_name}" if position != 3 else "2025 年对比2024 年 / 增（减）净值"
    if proof.table_title != DECOMPOSITION_TITLE or proof.row_path != "净利息收入变动" or proof.column_path != column:
        raise BankProofError("invalid_proof")
    # This page also has quarter-on-quarter tables; they never enter this segment.
    annual = text.split("下表列出所示期间本集团资产负债项目", 1)[0]
    matches = list(re.finditer(r"净利息收入变动 ([\d,]+) (\([\d,]+\)) ([\d,]+)", annual))
    if len(matches) != 1:
        raise BankProofError("invalid_proof")
    match = matches[0]
    excerpt = canonical_excerpt(proof.excerpt)
    required = (DECOMPOSITION_TITLE, "2025 年对比2024 年", "（人民币百万元） 规模 利率 增 （减） 净值", match.group())
    if any(token not in excerpt for token in required) or INTERACTION_RULE not in excerpt.replace(" ", ""):
        raise BankProofError("invalid_proof")
    raw = match.group(position)
    if fact.raw_value != raw:
        raise BankProofError("invalid_proof")
    try:
        normalized = normalize_fact_value(fact.metric, raw, fact.raw_unit or "")
    except ValueError as exc:
        raise BankProofError("unit_mismatch") from exc
    if fact.unit != normalized.unit or fact.value != normalized.value:
        raise BankProofError("unit_mismatch")
    return CheckedBankProof(fact.metric, normalized.value, raw, DECOMPOSITION_RULE,
                            proof.pdf_sha256, 23, "22", "净利息收入变动", column)


def check_bank_fact_proof(body: bytes, fact: TopicFactRevision) -> CheckedBankProof:
    return check_nii_nim_proof(body, fact) if fact.metric in {"nii", "nim"} else check_decomposition_proof(body, fact)
