"""Issuer-specific, tightly bounded bank accounting-scope bridges.

These checks supplement (and do not replace) ``checked_bank_locator``. A
bridge can establish the scope of one known fact only when every cited page,
period, source excerpt, hash, and calculation matches the stored annual-report
PDF bytes.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.originals import AnnualOriginal, normalized

_NINGBO_CODE = "002142.SZ"
_NINGBO_PERIOD = "2025-12-31"
_NINGBO_DISCLOSURE_ID = "1225189419"
_NINGBO_NIM_BRIDGE = "ningbo-fy2025-nim-consolidated-interest-bridge-v1"
_NIM_FORMULA = "净息差=利息净收入÷生息资产平均余额"
_EXPECTED_BRIDGE_KEYS = frozenset(
    {
        "method",
        "pdf_sha256",
        "period",
        "source_page",
        "source_page_sha256",
        "source_excerpt",
        "source_excerpt_sha256",
        "formula_excerpt",
        "formula_excerpt_sha256",
        "consolidated_page",
        "consolidated_page_sha256",
        "consolidated_excerpt",
        "consolidated_excerpt_sha256",
        "standalone_page",
        "standalone_page_sha256",
        "standalone_excerpt",
        "standalone_excerpt_sha256",
        "average_earning_assets",
        "net_interest_income",
        "standalone_net_interest_income",
        "reported_nim",
        "formula",
    }
)

_NINGBO_FINAL_SCOPE_BRIDGE = "ningbo-fy2025-final-ratio-ccxi-scope-v1"
_NINGBO_RATING_DISCLOSURE_ID = "3343869"
_NINGBO_RATING_PDF_SHA256 = "73037b6f1cda68ebf483e04add43155cd1695b15faacb5605622b6d87fff0477"
_NINGBO_RATING_TITLE = "宁波银行股份有限公司2026年第一期无固定期限资本债券信用评级报告"
_NINGBO_RATING_URL = "https://www.chinamoney.com.cn/dqs/cm-s-notice-query/fileDownLoad.do?contentId=3343869&mode=save&priority=0"
_NINGBO_RATING_LISTING_URL = "https://www.chinamoney.com.cn/chinese/zxpjbg/20260521/3343869.html?cp=zxpjbg"
_NINGBO_RATING_PUBLISHER = "中诚信国际信用评级有限责任公司"
_NINGBO_SOURCE_ITEM_KEY = "ningbo_final_scope"
_NINGBO_FINAL_BRIDGE_KEYS = frozenset({
    "method", "source_key", "source_page", "source_page_sha256", "source_excerpt",
    "source_excerpt_sha256", "note3_excerpt", "note3_sha256", "note4_excerpt",
    "note4_sha256", "metric_page", "table_title", "period_header", "current_column",
    "source_metric_label", "source_value", "source_unit", "annual_page",
    "annual_page_sha256", "annual_excerpt", "annual_excerpt_sha256",
    "npl_balance_亿元", "annual_npl_balance_million", "annual_loan_pool_million",
})


def ningbo_final_source_identity() -> dict[str, str]:
    """Immutable identity fields for the sole accepted Ningbo scope source."""
    return {
        "source_kind": "chinamoney_ccxi_rating_v1",
        "publisher": _NINGBO_RATING_PUBLISHER,
        "platform": "中国货币网",
        "disclosure_id": _NINGBO_RATING_DISCLOSURE_ID,
        "title": _NINGBO_RATING_TITLE,
        "listing_url": _NINGBO_RATING_LISTING_URL,
        "url": _NINGBO_RATING_URL,
        "published_on": "2026-05-21",
        "published_display": "2026-05-21 10:02",
        "publication_precision": "minute_local_timezone_unspecified",
        "pdf_sha256": _NINGBO_RATING_PDF_SHA256,
    }


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checked_ningbo_final_ratio_scope(
    original: AnnualOriginal,
    metric: str,
    proof: dict[str, Any],
    supplemental_originals: dict[str, AnnualOriginal] | None,
) -> bool:
    """Validate CCXI's final audited FY2025 consolidated NPL/coverage table.

    The rating report is a distinct, owner-stored source. Its explicit group
    caption applies to both requested rows; its footnotes identify coverage's
    annual-report definition and FY2025 audited year-end data. NPL additionally
    closes the balance back to the annual report's
    five-category classification table and consolidated loan pool.
    """
    bridge = proof.get("scope_bridge")
    audit = proof.get("audit_record")
    if not isinstance(bridge, dict) or not isinstance(audit, dict):
        raise ValueError("Ningbo final scope requires a bridge in proof and audit record")
    if set(bridge) != _NINGBO_FINAL_BRIDGE_KEYS:
        raise ValueError("Ningbo final scope bridge fields do not match the approved schema")
    if bridge.get("method") != _NINGBO_FINAL_SCOPE_BRIDGE or bridge.get("source_key") != _NINGBO_SOURCE_ITEM_KEY:
        raise ValueError("Ningbo final scope bridge method/source is unsupported")
    if original.code != _NINGBO_CODE or original.period.isoformat() != _NINGBO_PERIOD or metric not in {"npl_ratio", "provision_coverage"}:
        raise ValueError("Ningbo final scope bridge only supports FY2025 NPL/coverage")
    if proof.get("accounting_scope") != "group_consolidated" or proof.get("scope_basis") != "cross_statement_bridge":
        raise ValueError("Ningbo final scope proof must declare consolidated bridge scope")
    if proof.get("supplemental_originals") != audit.get("supplemental_originals"):
        raise ValueError("Ningbo supplemental original binding differs between proof and audit")
    refs = proof.get("supplemental_originals")
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
        raise ValueError("Ningbo final scope requires exactly one stored supplemental original")
    supplemental = refs[0]
    expected_source = {"source_key": _NINGBO_SOURCE_ITEM_KEY, **ningbo_final_source_identity()}
    if any(supplemental.get(key) != value for key, value in expected_source.items()):
        raise ValueError("Ningbo supplemental source identity is not the approved CCXI ChinaMoney report")
    if not supplemental.get("acquired_at"):
        raise ValueError("Ningbo supplemental source lacks its actual acquisition time")
    if supplemental.get("available_from") is not None and supplemental.get("available_from") != supplemental.get("acquired_at"):
        raise ValueError("Ningbo supplemental source availability must equal its stored acquisition time")
    source_original = (supplemental_originals or {}).get(_NINGBO_SOURCE_ITEM_KEY)
    if source_original is None:
        raise ValueError("Ningbo final scope source bytes are missing")
    try:
        acquired_at = datetime.fromisoformat(str(supplemental["acquired_at"]))
        checked_at = datetime.fromisoformat(str(proof["checked_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Ningbo supplemental source/field check time is invalid") from exc
    if any(value.tzinfo is None or value.utcoffset() is None for value in (acquired_at, checked_at)):
        raise ValueError("Ningbo supplemental source/field check times must include timezone")
    if acquired_at > checked_at or acquired_at > source_original.published_at:
        raise ValueError("Ningbo supplemental original was not available when the field was checked")
    if date.fromisoformat(str(supplemental["published_on"])) > checked_at.astimezone(UTC).date():
        raise ValueError("Ningbo supplemental source publication day is after the field check")
    if (
        source_original.code != _NINGBO_CODE
        or source_original.period.isoformat() != _NINGBO_PERIOD
        or hashlib.sha256(source_original.body).hexdigest() != _NINGBO_RATING_PDF_SHA256
        or source_original.pdf_hash != _NINGBO_RATING_PDF_SHA256
    ):
        raise ValueError("Ningbo supplemental source bytes do not match the approved source identity")
    if bridge.get("source_page") != 4 or bridge.get("current_column") != "2025":
        raise ValueError("Ningbo CCXI table must use its 2025 current column")
    if (
        bridge.get("metric_page") != 4
        or normalized(str(bridge.get("table_title", ""))) != "宁波银行(合并口径)"
        or normalized(str(bridge.get("period_header", ""))) != "202320242025"
    ):
        raise ValueError("Ningbo CCXI table title/period header is not the approved group table")
    source_page = normalized(bank_page_text(source_original.body, 4))
    source_excerpt = bridge.get("source_excerpt")
    if not isinstance(source_excerpt, str) or not source_excerpt.strip() or normalized(source_excerpt) != source_page:
        raise ValueError("Ningbo CCXI excerpt must reproduce the complete declared source page")
    if bridge.get("source_page_sha256") != _hash_text(source_page) or bridge.get("source_excerpt_sha256") != _hash_text(source_excerpt):
        raise ValueError("Ningbo CCXI page hash does not match the actual page bytes")
    note3 = bridge.get("note3_excerpt")
    note4 = bridge.get("note4_excerpt")
    if not isinstance(note3, str) or not isinstance(note4, str):
        raise ValueError("Ningbo CCXI audited-data footnotes are missing")
    note3_norm, note4_norm = normalized(note3), normalized(note4)
    if bridge.get("note3_sha256") != _hash_text(note3) or note3_norm not in source_page:
        raise ValueError("Ningbo CCXI note 3 is absent or hash-mismatched")
    if bridge.get("note4_sha256") != _hash_text(note4) or note4_norm not in source_page:
        raise ValueError("Ningbo CCXI note 4 is absent or hash-mismatched")
    if not all(token in note3_norm for token in ("不良贷款拨备覆盖率", "贷款损失准备/总贷款", "年报披露口径")):
        raise ValueError("Ningbo CCXI note 3 does not scope coverage to annual-report disclosure data")
    if not all(token in note4_norm for token in ("2025年财务数据", "2025年经审计", "期末数")):
        raise ValueError("Ningbo CCXI note 4 does not establish FY2025 audited year-end data")
    page_required = (
        "宁波银行(合并口径)202320242025",
        "不良贷款余额(亿元)94.99112.67131.47",
        "不良贷款率(%)0.760.760.76",
        "不良贷款拨备覆盖率(%)461.04389.35373.16",
    )
    if not all(token in source_page for token in page_required):
        raise ValueError("Ningbo CCXI page 4 group table/current-column values are incomplete")
    expected_label, expected_value = {
        "npl_ratio": ("不良贷款率(%)", "0.76"),
        "provision_coverage": ("不良贷款拨备覆盖率(%)", "373.16"),
    }[metric]
    if bridge.get("source_metric_label") != expected_label or str(bridge.get("source_value")) != expected_value or bridge.get("source_unit") != "percent":
        raise ValueError("Ningbo CCXI source metric/value/unit does not match the approved FY2025 field")
    try:
        main_value = Decimal(str(proof.get("raw_value")))
    except InvalidOperation as exc:
        raise ValueError("Ningbo annual-report ratio value is invalid") from exc
    if main_value != Decimal(expected_value):
        raise ValueError("Ningbo annual-report field value does not match the final CCXI value")
    if metric == "npl_ratio":
        if bridge.get("annual_page") != 29:
            raise ValueError("Ningbo NPL bridge must cite annual-report page 29")
        annual_page = normalized(bank_page_text(original.body, 29))
        excerpt = bridge.get("annual_excerpt")
        if not isinstance(excerpt, str) or normalized(excerpt) not in annual_page:
            raise ValueError("Ningbo annual NPL excerpt is absent from annual-report page 29")
        if bridge.get("annual_page_sha256") != _hash_text(annual_page) or bridge.get("annual_excerpt_sha256") != _hash_text(excerpt):
            raise ValueError("Ningbo annual NPL page hash mismatch")
        if not all(token in annual_page for token in ("五级分类", "不良贷款小计：13,1470.76%", "客户贷款合计1,733,314100.00%")):
            raise ValueError("Ningbo annual NPL group loan-pool table is incomplete")
        if bridge.get("npl_balance_亿元") != "131.47" or bridge.get("annual_npl_balance_million") != "13147" or bridge.get("annual_loan_pool_million") != "1733314":
            raise ValueError("Ningbo annual NPL bridge amounts do not match the audited table")
    elif any(bridge.get(key) is not None for key in (
        "annual_page", "annual_page_sha256", "annual_excerpt", "annual_excerpt_sha256",
        "npl_balance_亿元", "annual_npl_balance_million", "annual_loan_pool_million",
    )):
        raise ValueError("Ningbo coverage scope proof cannot borrow NPL-only bridge fields")
    if proof.get("scope_bridge") != audit.get("scope_bridge"):
        raise ValueError("Ningbo final scope bridge differs between proof and audit")
    return True


def _claimed_text(bridge: dict[str, Any], key: str) -> str:
    value = bridge.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Ningbo NIM bridge {key} is missing")
    return value


def _page_excerpt(
    original: AnnualOriginal,
    bridge: dict[str, Any],
    *,
    page_key: str,
    page_hash_key: str,
    excerpt_key: str,
    excerpt_hash_key: str,
    expected_page: int,
) -> str:
    page = bridge.get(page_key)
    if isinstance(page, bool) or not isinstance(page, int) or page != expected_page:
        raise ValueError(f"Ningbo NIM bridge {page_key} is not the approved source page")
    page_text = normalized(bank_page_text(original.body, expected_page))
    if bridge.get(page_hash_key) != _hash_text(page_text):
        raise ValueError(f"Ningbo NIM bridge {page_hash_key} does not match the PDF page")
    excerpt = _claimed_text(bridge, excerpt_key)
    if bridge.get(excerpt_hash_key) != _hash_text(excerpt):
        raise ValueError(f"Ningbo NIM bridge {excerpt_hash_key} does not match its excerpt")
    if normalized(excerpt) not in page_text:
        raise ValueError(f"Ningbo NIM bridge {excerpt_key} is absent from its PDF page")
    return normalized(excerpt)


def _has_nim_source_table(source: str) -> bool:
    """Require the 2025 first-column NIM row, its comparative column, formula inputs and dates."""
    required = (
        "单位：(人民币)百万元项目2025年2024年平均余额利息收支平均利率平均余额利息收支平均利率资产",
        "总生息资产3,056,484105,2053.44%2,585,537102,5853.97%",
        "利息净收入53,16147,993",
        "净利差(NIS)1.76%1.91%",
        "净息差(NIM)1.74%1.86%",
    )
    return all(token in source for token in required)


def _has_formula_excerpt(formula_excerpt: str) -> bool:
    return _NIM_FORMULA in formula_excerpt


def _has_statement_row(
    excerpt: str,
    *,
    statement_title: str,
    note_title: str,
    note_reference: str,
    current_value: str,
    comparative_value: str,
) -> bool:
    page = normalized(excerpt)
    expected_header = f"{statement_title}2025年度人民币百万元"
    expected_periods = f"{note_title}2025年度2024年度"
    expected_row = f"利息净收入{note_reference}{current_value}{comparative_value}"
    return expected_header in page and expected_periods in page and expected_row in page


def checked_ningbo_nim_bridge(original: AnnualOriginal, proof: dict[str, Any]) -> bool:
    """Validate Ningbo Bank FY2025 NIM's consolidated-scope bridge.

    If neither the proof nor its audit record declares a scope bridge, return
    ``False`` so the caller can continue its ordinary scope checks. Once either
    declares a bridge, every condition is mandatory; malformed, stale, or
    altered evidence raises ``ValueError``.

    The accepted source is deliberately fixed to the FY2025 NIM row and
    formula on original PDF page 19, the FY2025 consolidated P&L on page 86,
    and the FY2025 company-only P&L on page 94. It does not generalize to other
    issuers, years, ratios, or adjusted NIM values.
    """
    if not isinstance(proof, dict):
        raise ValueError("bank proof must be an object")
    proof_bridge = proof.get("scope_bridge")
    audit = proof.get("audit_record")
    audit_bridge = audit.get("scope_bridge") if isinstance(audit, dict) else None
    if proof_bridge is None and audit_bridge is None:
        return False
    if not isinstance(proof_bridge, dict) or not isinstance(audit, dict):
        raise ValueError("Ningbo NIM bridge must be present in both proof and audit record")
    if proof_bridge != audit_bridge:
        raise ValueError("Ningbo NIM bridge differs between proof and audit record")

    bridge = proof_bridge
    if set(bridge) != _EXPECTED_BRIDGE_KEYS:
        raise ValueError("Ningbo NIM bridge fields do not match the approved schema")
    if bridge.get("method") != _NINGBO_NIM_BRIDGE:
        raise ValueError("Ningbo NIM bridge method is unsupported")
    if bridge.get("period") != _NINGBO_PERIOD:
        raise ValueError("Ningbo NIM bridge period is not FY2025")
    if (
        original.code != _NINGBO_CODE
        or original.period.isoformat() != _NINGBO_PERIOD
        or proof.get("period") != _NINGBO_PERIOD
        or audit.get("code") != _NINGBO_CODE
        or audit.get("period") != _NINGBO_PERIOD
        or audit.get("disclosure_id") != _NINGBO_DISCLOSURE_ID
        or audit.get("status") != "verified"
    ):
        raise ValueError("Ningbo NIM bridge only supports 002142.SZ FY2025")

    actual_pdf_hash = hashlib.sha256(original.body).hexdigest()
    if original.pdf_hash != actual_pdf_hash or proof.get("pdf_hash") != actual_pdf_hash:
        raise ValueError("Ningbo NIM bridge PDF hash does not match the original bytes")
    if bridge.get("pdf_sha256") != actual_pdf_hash:
        raise ValueError("Ningbo NIM bridge is bound to a different original PDF")
    if audit.get("pdf_sha256") != actual_pdf_hash:
        raise ValueError("Ningbo NIM bridge audit record is bound to a different PDF")
    identity = proof.get("report_identity")
    chosen = original.metadata.get("chosen", {})
    if (
        not isinstance(identity, dict)
        or identity.get("code") != _NINGBO_CODE
        or identity.get("period") != _NINGBO_PERIOD
        or identity.get("disclosure_id") != _NINGBO_DISCLOSURE_ID
        or chosen.get("announcementId") != _NINGBO_DISCLOSURE_ID
    ):
        raise ValueError("Ningbo NIM bridge report identity is invalid")
    if (
        proof.get("raw_value") != "1.74"
        or proof.get("raw_unit") != "percent"
        or proof.get("accounting_scope") != "group_consolidated"
        or audit.get("raw_value") != "1.74"
        or audit.get("raw_unit") != "percent"
        or audit.get("accounting_scope") != "group_consolidated"
        or audit.get("metric") != "net_interest_margin"
    ):
        raise ValueError("Ningbo NIM bridge does not match the verified NIM field")
    if bridge.get("formula") != _NIM_FORMULA:
        raise ValueError("Ningbo NIM bridge formula is unsupported")

    source = _page_excerpt(
        original,
        bridge,
        page_key="source_page",
        page_hash_key="source_page_sha256",
        excerpt_key="source_excerpt",
        excerpt_hash_key="source_excerpt_sha256",
        expected_page=19,
    )
    formula_excerpt = _page_excerpt(
        original,
        bridge,
        page_key="source_page",
        page_hash_key="source_page_sha256",
        excerpt_key="formula_excerpt",
        excerpt_hash_key="formula_excerpt_sha256",
        expected_page=19,
    )
    if not _has_nim_source_table(source) or not _has_formula_excerpt(formula_excerpt):
        raise ValueError("Ningbo NIM source table or formula is incomplete")

    consolidated_excerpt = _page_excerpt(
        original,
        bridge,
        page_key="consolidated_page",
        page_hash_key="consolidated_page_sha256",
        excerpt_key="consolidated_excerpt",
        excerpt_hash_key="consolidated_excerpt_sha256",
        expected_page=86,
    )
    if not _has_statement_row(
        consolidated_excerpt,
        statement_title="合并利润表",
        note_title="附注五",
        note_reference="33",
        current_value="53,161",
        comparative_value="47,993",
    ):
        raise ValueError("Ningbo NIM consolidated current-year interest row is missing")

    standalone_excerpt = _page_excerpt(
        original,
        bridge,
        page_key="standalone_page",
        page_hash_key="standalone_page_sha256",
        excerpt_key="standalone_excerpt",
        excerpt_hash_key="standalone_excerpt_sha256",
        expected_page=94,
    )
    if not _has_statement_row(
        standalone_excerpt,
        statement_title="公司利润表",
        note_title="附注十四",
        note_reference="3",
        current_value="43,405",
        comparative_value="39,597",
    ):
        raise ValueError("Ningbo NIM standalone current-year interest row is missing")
    if normalized("53,161") == normalized("43,405"):
        raise ValueError("Ningbo NIM group and standalone numerators must differ")

    fixed_claims = {
        "average_earning_assets": "3,056,484",
        "net_interest_income": "53,161",
        "standalone_net_interest_income": "43,405",
        "reported_nim": "1.74",
    }
    if any(bridge.get(key) != value for key, value in fixed_claims.items()):
        raise ValueError("Ningbo NIM bridge claims do not match the approved FY2025 values")
    if "净息差(NIM)1.92%" in source or "净利差(NIS)1.92%" in source:
        raise ValueError("an adjusted NIM or net spread cannot replace the reported NIM row")
    try:
        interest_income = Decimal(bridge["net_interest_income"].replace(",", ""))
        earning_assets = Decimal(bridge["average_earning_assets"].replace(",", ""))
        calculated = interest_income / earning_assets * Decimal(100)
        rounded = calculated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ZeroDivisionError) as exc:  # fixed constants, retained defensively
        raise ValueError("Ningbo NIM bridge calculation is invalid") from exc
    if rounded != Decimal("1.74"):
        raise ValueError("Ningbo NIM bridge calculation does not reproduce the reported value")
    return True
