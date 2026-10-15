from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.bank_proofs import BANK_RULE, checked_bank_locator
from investment_agent.industry_peers.bank_scope_bridges import checked_ningbo_nim_bridge
from investment_agent.industry_peers.originals import AnnualOriginal, normalized

ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15/originals/002142.SZ_2025.pdf"
SOURCE_MANIFEST = ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15/source-manifest.json"
FORMULA = "净息差=利息净收入÷生息资产平均余额"
METHOD = "ningbo-fy2025-nim-consolidated-interest-bridge-v1"

pytestmark = pytest.mark.skipif(
    not PDF_PATH.is_file() or not SOURCE_MANIFEST.is_file(),
    reason="official Ningbo FY2025 PDF and source manifest are ignored research originals",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _excerpt(text: str, start: str, end: str) -> str:
    value = normalized(text)
    first = value.index(start)
    last = value.index(end, first) + len(end)
    return value[first:last]


def _original() -> AnnualOriginal:
    source = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))["sources"]["002142.SZ"]
    metadata = source["metadata"]
    body = PDF_PATH.read_bytes()
    return AnnualOriginal(
        code="002142.SZ",
        period=date(2025, 12, 31),
        body=body,
        metadata=metadata,
        pdf_hash=hashlib.sha256(body).hexdigest(),
        published_at=datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )


def _bridge(original: AnnualOriginal) -> dict[str, Any]:
    source_page = normalized(bank_page_text(original.body, 19))
    source_excerpt = _excerpt(
        source_page,
        "单位：(人民币)百万元项目2025年2024年平均余额利息收支平均利率平均余额利息收支平均利率",
        "净息差(NIM)1.74%1.86%",
    )
    formula_excerpt = "净利差=生息资产平均利率-付息负债平均利率；" + FORMULA + "。"
    consolidated_page = normalized(bank_page_text(original.body, 86))
    consolidated_row = "利息净收入3353,16147,993"
    consolidated_excerpt = consolidated_page[: consolidated_page.index(consolidated_row) + len(consolidated_row)]
    standalone_page = normalized(bank_page_text(original.body, 94))
    standalone_row = "利息净收入343,40539,597"
    standalone_excerpt = standalone_page[: standalone_page.index(standalone_row) + len(standalone_row)]
    return {
        "method": METHOD,
        "pdf_sha256": original.pdf_hash,
        "period": "2025-12-31",
        "source_page": 19,
        "source_page_sha256": _sha(source_page),
        "source_excerpt": source_excerpt,
        "source_excerpt_sha256": _sha(source_excerpt),
        "formula_excerpt": formula_excerpt,
        "formula_excerpt_sha256": _sha(formula_excerpt),
        "consolidated_page": 86,
        "consolidated_page_sha256": _sha(consolidated_page),
        "consolidated_excerpt": consolidated_excerpt,
        "consolidated_excerpt_sha256": _sha(consolidated_excerpt),
        "standalone_page": 94,
        "standalone_page_sha256": _sha(standalone_page),
        "standalone_excerpt": standalone_excerpt,
        "standalone_excerpt_sha256": _sha(standalone_excerpt),
        "average_earning_assets": "3,056,484",
        "net_interest_income": "53,161",
        "standalone_net_interest_income": "43,405",
        "reported_nim": "1.74",
        "formula": FORMULA,
    }


def _proof(original: AnnualOriginal) -> dict[str, Any]:
    bridge = _bridge(original)
    audit_record = {
        "code": "002142.SZ",
        "period": "2025-12-31",
        "disclosure_id": "1225189419",
        "pdf_sha256": original.pdf_hash,
        "metric": "net_interest_margin",
        "status": "verified",
        "raw_value": "1.74",
        "raw_unit": "percent",
        "accounting_scope": "group_consolidated",
        "scope_bridge": copy.deepcopy(bridge),
    }
    return {
        "report_identity": {
            "code": "002142.SZ",
            "period": "2025-12-31",
            "disclosure_id": "1225189419",
        },
        "period": "2025-12-31",
        "pdf_hash": original.pdf_hash,
        "raw_value": "1.74",
        "raw_unit": "percent",
        "accounting_scope": "group_consolidated",
        "scope_bridge": bridge,
        "audit_record": audit_record,
    }


def _locator_proof(original: AnnualOriginal) -> dict[str, Any]:
    bridge = _bridge(original)
    page = bank_page_text(original.body, 19)
    start = page.index("下表列示了生息资产和付息负债")
    end = page.index("下表列示了利息收入和利息支出", start)
    excerpt = page[start:end]
    table = "下表列示了生息资产和付息负债的平均余额、利息收入和支出、平均收益率和平均付息率。"
    period_header = (
        "项目                                 2025年                           2024年\n"
        "                         平均余额      利息收支       平均利率      平均余额       利息收支      平均利率"
    )
    row = {
        "code": original.code,
        "name": "宁波银行",
        "period": original.period.isoformat(),
        "metric": "net_interest_margin",
        "disclosure_id": "1225189419",
        "title": original.metadata["chosen"]["announcementTitle"],
        "url": original.metadata["url"],
        "published_at": original.published_at.isoformat(),
        "pdf_sha256": original.pdf_hash,
        "pdf_page": 19,
        "table_title": table,
        "period_header": period_header,
        "current_column": "2025年",
        "header": "净息差（NIM）",
        "raw_label": "净息差（NIM）",
        "raw_value": "1.74",
        "raw_unit": "percent",
        "parsed_value": "0.0174",
        "parsed_unit": "fraction",
        "accounting_scope": "group_consolidated",
        "definition_or_footnote": "第19页注3：净息差=利息净收入÷生息资产平均余额。",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": _sha(excerpt),
        "scope_bridge": bridge,
        "scope_basis": "cross_statement_bridge",
        "status": "verified",
        "check_rule_version": BANK_RULE,
    }
    return {
        **row,
        "pdf_hash": original.pdf_hash,
        "check_rule_version": BANK_RULE,
        "report_identity": {
            "code": original.code,
            "period": original.period.isoformat(),
            "disclosure_id": "1225189419",
        },
        "audit_record": copy.deepcopy(row),
    }
def test_ningbo_fy2025_nim_bridge_verifies_group_scope() -> None:
    original = _original()
    proof = _proof(original)
    assert checked_ningbo_nim_bridge(original, proof) is True

    result = (
        Decimal(proof["scope_bridge"]["net_interest_income"].replace(",", ""))
        / Decimal(proof["scope_bridge"]["average_earning_assets"].replace(",", ""))
        * Decimal(100)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert result == Decimal("1.74")


def test_ningbo_nim_bridge_is_accepted_by_primary_bank_locator() -> None:
    original = _original()
    proof = _locator_proof(original)
    checks = checked_bank_locator(original, "net_interest_margin", proof)
    assert checks["accounting_scope"] == "group_consolidated"
    assert checks["scope_basis"] == "cross_statement_bridge"


def test_ningbo_nim_bridge_without_declaration_defers_to_primary_validator() -> None:
    original = _original()
    proof = _proof(original)
    del proof["scope_bridge"]
    del proof["audit_record"]["scope_bridge"]
    assert checked_ningbo_nim_bridge(original, proof) is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["scope_bridge"].__setitem__("source_page_sha256", "0" * 64),
        lambda p: p["scope_bridge"].__setitem__("source_excerpt_sha256", "0" * 64),
        lambda p: p["scope_bridge"].__setitem__("consolidated_page", 87),
        lambda p: p["scope_bridge"].__setitem__("standalone_page", 95),
        lambda p: p["scope_bridge"].__setitem__("period", "2024-12-31"),
        lambda p: p["scope_bridge"].__setitem__("net_interest_income", "47,993"),
        lambda p: p["scope_bridge"].__setitem__("standalone_net_interest_income", "39,597"),
        lambda p: p["scope_bridge"].__setitem__("reported_nim", "1.92"),
        lambda p: p.__setitem__("raw_value", "1.92"),
        lambda p: p.__setitem__("raw_value", "1.76"),
        lambda p: p.__setitem__("raw_unit", "fraction"),
        lambda p: p["scope_bridge"].__setitem__("formula", "净利差=生息资产平均利率-付息负债平均利率"),
        lambda p: p["scope_bridge"].__setitem__("average_earning_assets", "2,585,537"),
        lambda p: p["scope_bridge"].__setitem__("method", "generic-group-mention"),
    ],
)
def test_ningbo_nim_bridge_rejects_altered_identity_hash_period_and_metric(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    original = _original()
    proof = _proof(original)
    mutate(proof)
    # Mutations of the bridge must also be copied to the audit record to prove
    # that the content check, rather than only proof/audit disagreement, rejects them.
    if isinstance(proof.get("scope_bridge"), dict):
        proof["audit_record"]["scope_bridge"] = copy.deepcopy(proof["scope_bridge"])
    proof["audit_record"]["raw_value"] = proof.get("raw_value", "1.74")
    proof["audit_record"]["raw_unit"] = proof.get("raw_unit", "percent")
    with pytest.raises(ValueError):
        checked_ningbo_nim_bridge(original, proof)


def test_ningbo_nim_bridge_rejects_nis_and_altered_nim_row() -> None:
    original = _original()
    proof = _proof(original)
    bridge = proof["scope_bridge"]
    bridge["source_excerpt"] = bridge["source_excerpt"].replace("净息差(NIM)1.74%1.86%", "净息差(NIM)1.86%1.74%")
    bridge["source_excerpt_sha256"] = _sha(bridge["source_excerpt"])
    proof["audit_record"]["scope_bridge"] = copy.deepcopy(bridge)
    with pytest.raises(ValueError, match="absent from its PDF page"):
        checked_ningbo_nim_bridge(original, proof)


def test_ningbo_nim_bridge_rejects_proof_audit_scope_bridge_disagreement() -> None:
    original = _original()
    proof = _proof(original)
    proof["scope_bridge"]["reported_nim"] = "1.92"
    with pytest.raises(ValueError, match="differs between proof and audit record"):
        checked_ningbo_nim_bridge(original, proof)


def test_ningbo_nim_bridge_rejects_other_issuer_or_report_bytes() -> None:
    original = _original()
    proof = _proof(original)
    other_issuer = copy.copy(original)
    object.__setattr__(other_issuer, "code", "600919.SH")
    with pytest.raises(ValueError, match="only supports 002142.SZ FY2025"):
        checked_ningbo_nim_bridge(other_issuer, proof)
    proof["pdf_hash"] = "0" * 64
    with pytest.raises(ValueError, match="PDF hash"):
        checked_ningbo_nim_bridge(original, proof)
