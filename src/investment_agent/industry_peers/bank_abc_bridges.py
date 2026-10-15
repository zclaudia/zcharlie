"""Narrow FY2025 Agricultural Bank cross-statement scope bridges.

These checks supplement, but do not replace, ``checked_bank_locator``. They
prove the Group scope of four exact 601288.SH values by binding report rows,
column order, and calculations to the same annual-report PDF bytes.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.originals import AnnualOriginal, normalized

_SCHEMA = "bank-abc-scope-bridge/1"
_CODE = "601288.SH"
_PERIOD = date(2025, 12, 31)
_PDF_SHA256 = "9353f9a05d9cdf19cabef785e4c633fbd29ab63e7933dcd3c472ca02bc691d83"
_DISCLOSURE_ID = "1225056196"
_TITLE = "农业银行2025年度报告"
_URL = "https://static.cninfo.com.cn/finalpage/2026-03-31/1225056196.PDF"
_PUBLISHED_AT = "2026-03-30T16:00:00+00:00"

_FIELDS: dict[str, dict[str, Any]] = {
    "roe_weighted_annual": {
        "value": "10.16",
        "header": "加权平均净资产收益率",
        "marker": "3",
        "definition": "注3：依据证券发行信息披露规则计算。",
        "column": "2025 年",
        "values": ("10.16", "10.46", "10.91"),
        "roles": {
            "roe_supplement": 417,
            "group_income": 161,
            "group_equity_movement": 163,
        },
    },
    "net_interest_margin": {
        "value": "1.28",
        "header": "净利息收益率",
        "marker": "4",
        "definition": "注4：利息净收入除以生息资产平均余额。",
        "column": "2025 年",
        "values": ("1.28", "1.42", "1.60"),
        "roles": {"margin_table": 26, "group_vs_bank_income": 161},
    },
    "npl_ratio": {
        "value": "1.27",
        "header": "不良贷款率",
        "marker": "8",
        "definition": "注8：不良贷款余额（不含应计利息）除以发放贷款和垫款总额（不含应计利息）。",
        "column": "2025 年 12 月 31 日",
        "values": ("1.27", "1.30", "1.33"),
        "roles": {"risk_classification": 77, "group_loan_pool": 208, "group_stage3": 209},
    },
    "provision_coverage": {
        "value": "292.55",
        "header": "拨备覆盖率",
        "marker": "9",
        "definition": "注9：贷款减值准备余额除以不良贷款余额。",
        "column": "2025 年 12 月 31 日",
        "values": ("292.55", "299.61", "303.87"),
        "roles": {
            "risk_classification": 77,
            "group_loan_pool": 208,
            "allowance_movement": 79,
            "group_stage3": 209,
            "group_allowances": 209,
        },
    },
}


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid bridge number")
    try:
        result = Decimal(str(value).replace(",", "").replace("−", "-"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid bridge number") from exc
    if not result.is_finite():
        raise ValueError("non-finite bridge number")
    return result


def _evidence_pages(
    original: AnnualOriginal, bridge: dict[str, Any], roles: dict[str, int]
) -> dict[str, str] | None:
    evidence = bridge.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(roles):
        return None
    pages: dict[str, str] = {}
    for role, expected_page in roles.items():
        item = evidence.get(role)
        if not isinstance(item, dict) or item.get("page") != expected_page:
            return None
        excerpt = item.get("excerpt")
        digest = item.get("sha256")
        if not isinstance(excerpt, str) or not excerpt.strip() or not isinstance(digest, str):
            return None
        if hashlib.sha256(excerpt.encode("utf-8")).hexdigest() != digest:
            return None
        page_text = bank_page_text(original.body, expected_page)
        if normalized(excerpt) not in normalized(page_text):
            return None
        pages[role] = page_text
    return pages


def _numbers(line: str) -> list[Decimal]:
    return [
        _number(value)
        for value in re.findall(
            r"(?<![\w])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w])\d+(?:\.\d+)?", line
        )
    ]


def _current_indicator_row(
    page_text: str, metric: str, spec: dict[str, Any], proof: dict[str, Any]
) -> bool:
    rows = [line for line in page_text.splitlines() if spec["header"] in line]
    if metric == "roe_weighted_annual":
        rows = [line for line in rows if "扣除非经常性损益" not in line]
    if len(rows) != 1:
        return False
    row = rows[0]
    if spec["marker"] not in row:
        return False
    tail = row[row.find(spec["header"]) + len(spec["header"]) :]
    nums = _numbers(tail)
    page_norm = normalized(page_text)
    footnote_tokens = {
        "roe_weighted_annual": ("根据证监会", "规则第9号", "净资产收益率和每股收益的计算及披露"),
        "net_interest_margin": ("利息净收入除以生息资产平均余额",),
        "npl_ratio": ("不良贷款余额(不含应计利息)除以发放贷款和垫款总额(不含应计利息)",),
        "provision_coverage": (
            "贷款减值准备余额除以不良贷款余额(不含应计利息)",
            "票据与福费廷的减值准备余额",
        ),
    }[metric]
    if not all(token in page_norm for token in footnote_tokens):
        return False
    try:
        # First number is the footnote marker; the next three cells are 2025–2023.
        cells = nums[1:4]
        expected = tuple(_number(value) for value in spec["values"])
        return (
            len(cells) == 3
            and tuple(cells) == expected
            and _number(proof.get("raw_value")) == expected[0]
            and proof.get("current_column") == spec["column"]
            and proof.get("header") == spec["header"]
            and proof.get("definition_or_footnote") == spec["definition"]
        )
    except (ValueError, TypeError):
        return False


def _same_bank_row(proof: dict[str, Any], audit: dict[str, Any], metric: str) -> bool:
    if not isinstance(audit, dict) or proof.get("scope_bridge") != audit.get("scope_bridge"):
        return False
    for key in (
        "code",
        "period",
        "disclosure_id",
        "title",
        "url",
        "published_at",
        "pdf_sha256",
        "metric",
        "pdf_page",
        "table_title",
        "period_header",
        "current_column",
        "header",
        "raw_label",
        "raw_value",
        "raw_unit",
        "parsed_value",
        "parsed_unit",
        "accounting_scope",
        "definition_or_footnote",
        "locator_excerpt",
        "locator_excerpt_sha256",
        "footnote_marker",
    ):
        if proof.get(key) != audit.get(key):
            return False
    return (
        audit.get("metric") == metric
        and audit.get("code") == _CODE
        and audit.get("period") == _PERIOD.isoformat()
        and audit.get("disclosure_id") == _DISCLOSURE_ID
        and audit.get("title") == _TITLE
        and audit.get("url") == _URL
        and audit.get("published_at") == _PUBLISHED_AT
        and audit.get("pdf_sha256") == _PDF_SHA256
        and audit.get("pdf_page") == 11
        and audit.get("status") == "verified"
        and audit.get("accounting_scope") == "group_consolidated"
        and audit.get("raw_unit") == "percent"
        and audit.get("parsed_unit") == "fraction"
        and audit.get("locator_excerpt_sha256")
        == hashlib.sha256(str(audit.get("locator_excerpt", "")).encode("utf-8")).hexdigest()
    )


def _check_roe(pages: dict[str, str], proof: dict[str, Any]) -> bool:
    supplement = normalized(pages["roe_supplement"])
    income = normalized(pages["group_income"])
    equity = normalized(pages["group_equity_movement"])
    if not all(
        token in supplement
        for token in (
            "本行及合并子公司",
            "本集团亦按照",
            "三净资产收益率及每股收益",
            "归属于母公司普通股股东的净利润273,956262,800",
            "加权平均净资产收益率(%)10.1610.46",
            "扣除非经常性损益后归属于母公司普通股股东的净利润274,281262,278",
            "加权平均净资产收益率(%)10.1710.44",
        )
    ):
        return False
    if (
        "合并利润表和利润表" not in income
        or "本集团本行附注七2025年2024年2025年2024年" not in income
    ):
        return False
    if not all(
        token in income for token in ("归属于母公司股东的净利润291,041282,083280,961275,004",)
    ):
        return False
    # Whitespace and table separators vary; bind the exact row label and amount.
    if not all(
        token in equity for token in ("对其他权益工具持有者的", "(17,085)", "本年增减变动金额")
    ):
        return False
    return (
        _number("291041") - _number("17085") == _number("273956")
        and "10.16" in str(proof.get("raw_value"))
        and _number(proof.get("raw_value")) != _number("10.17")
    )


def _check_nim(pages: dict[str, str]) -> bool:
    margin = normalized(pages["margin_table"])
    income = normalized(pages["group_vs_bank_income"])
    if (
        "2025年2024年" not in margin
        or "总生息资产44,634,4831,201,3382.6940,929,2661,275,6803.12" not in margin
    ):
        return False
    if "利息净收入569,594580,692" not in margin or "净利息收益率1.281.42" not in margin:
        return False
    if "本集团本行附注七2025年2024年2025年2024年" not in income:
        return False
    # Audited income statement columns are Group 2025/2024, then Bank 2025/2024.
    if "利息净收入33569,594580,692562,279576,951" not in income:
        return False
    calculated = _number("569594") / _number("44634483") * _number("100")
    return calculated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == _number("1.28")


def _check_npl(pages: dict[str, str]) -> bool:
    risk = normalized(pages["risk_classification"])
    loans = normalized(pages["group_loan_pool"])
    stage3 = normalized(pages["group_stage3"])
    if "2025年12月31日2024年12月31日" not in risk:
        return False
    if "不良贷款343,4561.27322,1651.30" not in risk:
        return False
    if not all(
        token in risk for token in ("小计27,082,646100.0024,856,251100.00", "应计利息52,188")
    ):
        return False
    if "本集团本行注2025年12月31日2024年12月31日2025年12月31日2024年12月31日" not in loans:
        return False
    if "合计24,591,82423,038,91324,503,20322,941,591" not in loans:
        return False
    if "以公允价值计量且其变动计入其他综合收益(2)2,490,8221,817,3382,490,8221,817,338" not in loans:
        return False
    if "应计利息52,18849,93648,78349,513" not in loans:
        return False
    group_ex_interest = _number("24591824") + _number("2490822")
    full_group = group_ex_interest + _number("52188")
    risk_denominator = _number("27082646")
    if group_ex_interest != risk_denominator or full_group != _number("27134834"):
        return False
    if not all(
        token in stage3
        for token in (
            "本集团2025年12月31日阶段一阶段二阶段三合计",
            "以摊余成本计量的发放贷款和垫款余额23,827,107473,621343,28424,644,012",
            "以公允价值计量且其变动计入其他综合收益的发放贷款和垫款2,490,2004501722,490,822",
            "本集团2024年12月31日",
        )
    ):
        return False
    # Stage 3 is only a report-specific equal-value corroboration; it is not
    # treated as definitionally equivalent to regulatory non-performing loans.
    if _number("343284") + _number("172") != _number("343456"):
        return False
    rate = _number("343456") / risk_denominator * _number("100")
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == _number("1.27")


def _check_coverage(pages: dict[str, str]) -> bool:
    movement = normalized(pages["allowance_movement"])
    group = normalized(pages["group_allowances"])
    if not _check_npl(pages):
        return False
    if "2025年12月31日641,533111,740251,5071,004,780" not in movement:
        return False
    if "本集团2025年12月31日阶段一阶段二阶段三合计" not in group:
        return False
    if "减：损失准备(593,495)(111,650)(251,335)(956,480)" not in group:
        return False
    if (
        "以公允价值计量且其变动计入其他综合收益的发放贷款和垫款损失准备(48,038)(90)(172)(48,300)"
        not in group
    ):
        return False
    if not all(
        token in group
        for token in (
            "以摊余成本计量的发放贷款和垫款余额23,827,107473,621343,28424,644,012",
            "以公允价值计量且其变动计入其他综合收益的发放贷款和垫款2,490,2004501722,490,822",
            "本集团2024年12月31日",
        )
    ):
        return False
    allowance = _number("956480") + _number("48300")
    if allowance != _number("1004780"):
        return False
    if normalized(
        "本表包含了以公允价值计量且其变动计入其他综合收益的贷款减值准备"
    ) not in normalized(pages["allowance_movement"]):
        return False
    rate = allowance / _number("343456") * _number("100")
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == _number("292.55")


def checked_abc_scope_bridge(original: AnnualOriginal, metric: str, proof: dict[str, Any]) -> bool:
    """Validate one of ABC's report-specific Group-scope calculation bridges.

    Returns ``False`` when the bridge is absent or outside this exact issuer,
    year, metric, or proof format. A declared bridge must mirror the proof's
    ``audit_record`` and bind every evidence excerpt to the PDF page and hash.
    The regular bank locator remains responsible for its normal row, unit,
    annual-column, identity, and value checks.
    """

    if not isinstance(proof, dict) or "scope_bridge" not in proof:
        return False
    bridge = proof.get("scope_bridge")
    if not isinstance(bridge, dict) or bridge.get("schema") != _SCHEMA:
        return False
    if (
        metric not in _FIELDS
        or bridge.get("metric") != metric
        or original.code != _CODE
        or original.period != _PERIOD
        or original.pdf_hash != _PDF_SHA256
        or hashlib.sha256(original.body).hexdigest() != _PDF_SHA256
        or len(original.body) == 0
    ):
        return False
    chosen = original.metadata.get("chosen", {}) if isinstance(original.metadata, dict) else {}
    if (
        not isinstance(chosen, dict)
        or chosen.get("announcementId") != _DISCLOSURE_ID
        or chosen.get("announcementTitle") != _TITLE
        or chosen.get("secName") != "农业银行"
        or original.metadata.get("url") != _URL
        or original.published_at.isoformat() != _PUBLISHED_AT
        or proof.get("report_identity")
        != {
            "code": _CODE,
            "period": _PERIOD.isoformat(),
            "disclosure_id": _DISCLOSURE_ID,
        }
    ):
        return False
    audit = proof.get("audit_record")
    if not isinstance(audit, dict) or not _same_bank_row(proof, audit, metric):
        return False
    spec = _FIELDS[metric]
    if (
        audit.get("header") != spec["header"]
        or audit.get("raw_value") != spec["value"]
        or audit.get("definition_or_footnote") != spec["definition"]
        or audit.get("current_column") != spec["column"]
        or proof.get("raw_unit") != "percent"
    ):
        return False
    try:
        page11 = bank_page_text(original.body, 11)
        locator = proof.get("locator_excerpt")
        if not isinstance(locator, str) or not locator.strip():
            return False
        if (
            proof.get("locator_excerpt_sha256")
            != hashlib.sha256(locator.encode("utf-8")).hexdigest()
        ):
            return False
        if normalized(locator) not in normalized(page11):
            return False
        if not _current_indicator_row(page11, metric, spec, proof):
            return False
        pages = _evidence_pages(original, bridge, spec["roles"])
        if pages is None:
            return False
        if metric == "roe_weighted_annual":
            return _check_roe(pages, proof)
        if metric == "net_interest_margin":
            return _check_nim(pages)
        if metric == "npl_ratio":
            return _check_npl(pages)
        if metric == "provision_coverage":
            return _check_coverage(pages)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
    return False
