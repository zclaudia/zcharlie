"""Bounded field checks for bank annual-report evidence.

This deliberately recognizes only the five bank annual fields in the bank
metric pack. A proof is rechecked against the bytes of its stored PDF; manifest
status flags are never evidence by themselves.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from typing import Any

from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.originals import AnnualOriginal, normalized

BANK_RULE = "bank-annual-original/1"
ISSUER_ALIASES = {"江苏银行": ("BANKOFJIANGSU",), "常熟银行": ("江苏常熟农村商业银行股份有限公司",)}
BANK_ISSUER_NAMES = {
    "601398.SH": "工商银行",
    "601939.SH": "建设银行",
    "601288.SH": "农业银行",
    "600036.SH": "招商银行",
    "601166.SH": "兴业银行",
    "000001.SZ": "平安银行",
    "600919.SH": "江苏银行",
    "601009.SH": "南京银行",
    "002142.SZ": "宁波银行",
    "601128.SH": "常熟银行",
}

RATIO_HEADERS: dict[str, tuple[str, ...]] = {
    "roe_weighted_annual": ("加权平均净资产收益率", "加权平均ROE", "加权平均净资产收益率（ROE）"),
    "net_interest_margin": ("净利息收益率", "净息差"),
    "npl_ratio": ("不良贷款率", "不良贷款比率", "不良贷款比率(%)", "集团贷款不良率"),
    "provision_coverage": ("拨备覆盖率", "拨备覆盖率(%)", "集团拨备覆盖率"),
}
EQUITY_HEADERS = (
    "归属于母公司股东权益合计",
    "归属于母公司所有者权益合计",
    "归属于母公司股东的所有者权益合计",
    "归属于母公司股东权益",
    "归属于母公司所有者权益",
    "归属于母公司股东的权益",
    "归属于本行股东权益合计",
)
EQUITY_UNITS = {"CNY": Decimal(1), "CNY_thousand": Decimal(1000), "CNY_million": Decimal(1000000)}
RATIO_METRICS = frozenset(RATIO_HEADERS)
BANK_METRICS = RATIO_METRICS | {"equity_parent"}
JIANGSU_CONSOLIDATION_FOOTNOTE = (
    "本行发放贷款和垫款合并金融租赁子公司长期应收款，"
    "不良贷款率、拨备覆盖率等涉及指标计算口径同步调整"
)

PINGAN_ROE_SUPPLEMENT = "净资产收益率和每股收益"
PINGAN_ROE_ROW = "归属于母公司普通股股东的净利润"
PINGAN_ROE_RULE_NOTE = "以上净资产收益率和每股收益按证监会于2010年1月11日修订之"
NINGBO_ROE_SUPPLEMENT = "二、净资产收益率和每股收益"
NINGBO_ROE_ROW = "归属于公司普通股股东的净利润"
NINGBO_ROE_GROUP_NOTE = "本集团对每股收益及净资产收益率的计算按照证监会的"


def _pingan_supplement_roe_scope(
    original: AnnualOriginal, metric: str, proof: dict[str, Any], page_content: str
) -> bool:
    """Recognize Ping An's annual-report parent-common-shareholder ROE matrix.

    The narrow combination of the annual-report financial-statement supplement,
    CSRC Rule 9 calculation note, and the exact parent ordinary-shareholder
    numerator identifies consolidated weighted ROE. A generic parent-owner
    phrase or unrelated group mention cannot establish bank scope.
    """
    if (
        original.code != "000001.SZ"
        or metric != "roe_weighted_annual"
        or proof.get("pdf_page") != 288
        or proof.get("table_title") != PINGAN_ROE_SUPPLEMENT
        or proof.get("header") != PINGAN_ROE_ROW
        or normalized(proof.get("period_header", "")) != "2025年度"
        or normalized(proof.get("current_column", "")) not in {"2025年度加权平均", "2025年度净资产收益率加权平均"}
    ):
        return False
    page = normalized(page_content)
    if not all(
        token in page
        for token in (
            "财务报表补充资料",
            "2025年度",
            "净资产收益率和每股收益",
            "净资产收益率每股收益人民币元",
            "加权平均基本稀释",
            PINGAN_ROE_RULE_NOTE,
        )
    ):
        return False
    bridge = proof.get("scope_bridge")
    if not isinstance(bridge, dict):
        return False
    if bridge.get("consolidated_page") != 120 or bridge.get("standalone_page") != 122:
        return False
    try:
        consolidated_excerpt = _require_text(bridge, "consolidated_excerpt")
        standalone_excerpt = _require_text(bridge, "standalone_excerpt")
        if bridge.get("consolidated_sha256") != hashlib.sha256(consolidated_excerpt.encode()).hexdigest():
            return False
        if bridge.get("standalone_sha256") != hashlib.sha256(standalone_excerpt.encode()).hexdigest():
            return False
        consolidated_page = bank_page_text(original.body, 120)
        standalone_page = bank_page_text(original.body, 122)
        if normalized(consolidated_excerpt) not in normalized(consolidated_page):
            return False
        if normalized(standalone_excerpt) not in normalized(standalone_page):
            return False
        consolidated = normalized(consolidated_page)
        standalone = normalized(standalone_page)
        if not all(token in consolidated for token in ("合并利润表", "本集团", "六、净利润42,63344,508")):
            return False
        if not all(token in standalone for token in ("银行利润表", "本行", "六、净利润41,15842,586")):
            return False
        if "归属于母公司股东的净利润42,63344,508" not in page:
            return False
        if "母公司优先股宣告股息(874)(874)" not in page or "母公司永续债利息(1,645)(1,975)" not in page:
            return False
        if "归属于母公司普通股股东的净利润40,11441,659" not in page:
            return False
        if Decimal("42633") - Decimal("874") - Decimal("1645") != Decimal("40114"):
            return False
        row = next((line for line in page_content.splitlines() if PINGAN_ROE_ROW in line and "扣除非经常性损益" not in line), "")
        return _matches_current_cell(row, PINGAN_ROE_ROW, _decimal(proof.get("raw_value"), "raw_value"), proof, source="supplement")
    except (ValueError, TypeError):
        return False


def _ningbo_supplement_roe_scope(
    original: AnnualOriginal, metric: str, proof: dict[str, Any], page_content: str
) -> bool:
    """Recognize the direct Group Rule 9 note attached to Ningbo's ROE matrix."""
    if (
        original.code != "002142.SZ"
        or metric != "roe_weighted_annual"
        or proof.get("pdf_page") != 236
        or normalized(proof.get("table_title", "")) != NINGBO_ROE_SUPPLEMENT
        or proof.get("header") != NINGBO_ROE_ROW
        or normalized(proof.get("period_header", "")) not in {"2025年", "2025年度", "2025年2024年", "2025年度2024年度"}
        or normalized(proof.get("current_column", "")) not in {"2025年", "2025年度"}
        or _decimal(proof.get("raw_value"), "raw_value") != Decimal("13.11")
    ):
        return False
    page = normalized(page_content)
    try:
        section = page.split(NINGBO_ROE_SUPPLEMENT, 1)[1]
        matrix = section.split("2024年", 1)[0]
        row = "归属于公司普通股股东的净利润13.114.294.29"
        return (
            "2025年加权平均净资产收益率(%)每股收益(人民币元)基本稀释" in matrix
            and row in matrix
            and NINGBO_ROE_GROUP_NOTE in section
            and "净资产收益率和每股收益的计算及披露" in section
            and "2010年修订" in section
        )
    except (IndexError, ValueError):
        return False


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"bank proof {name} is invalid")
    try:
        result = Decimal(str(value).replace(",", "").replace("−", "-"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"bank proof {name} is invalid") from exc
    if not result.is_finite():
        raise ValueError(f"bank proof {name} is not finite")
    return result


def _require_text(proof: dict[str, Any], key: str) -> str:
    value = proof.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"bank proof {key} is missing")
    return value


@lru_cache(maxsize=16)
def _validate_bank_pdf_identity(
    body: bytes, code: str, report_year: int, issuer_name: str, aliases: tuple[str, ...]
) -> None:
    """Cache successful bounded identity checks by actual PDF bytes and claims."""
    from pypdf import PdfReader

    total_pages = len(PdfReader(BytesIO(body)).pages)
    pages = [normalized(bank_page_text(body, n)) for n in range(1, min(8, total_pages) + 1)]
    front = "".join(pages)
    if not any(identity in front for identity in (code[:6], issuer_name, *aliases)):
        raise ValueError("bank report PDF does not confirm the filing issuer")
    year = str(report_year)
    full_title = any(
        year in page
        and ("年度报告" in page or "ANNUALREPORT" in page)
        and not any(token in page for token in ("摘要", "SUMMARY", "取消"))
        for page in pages
    )
    if not full_title:
        raise ValueError("bank report PDF does not confirm the full target-year annual report")


def validate_bank_report_identity(original: AnnualOriginal) -> None:
    """Confirm issuer and full target-year report title in the actual PDF.

    Only the first eight pages are inspected. Metadata supplies an expected
    issuer name; it cannot supply missing PDF title or period evidence.
    Successful checks are cached by the actual PDF bytes plus identity claims.
    """
    chosen = original.metadata.get("chosen", {}) if isinstance(original.metadata, dict) else {}
    issuer_name = normalized(str(chosen.get("secName", "")))
    if not issuer_name:
        raise ValueError("bank report issuer name is missing from filing identity")
    _validate_bank_pdf_identity(
        original.body,
        original.code,
        original.period.year,
        issuer_name,
        tuple(ISSUER_ALIASES.get(issuer_name, ())),
    )


def _layout_page(body: bytes, page: int) -> str:
    return bank_page_text(body, page, layout=True)


def _validated_scope_context(original: AnnualOriginal, proof: dict[str, Any]) -> bool:
    """Check an explicit report/section consolidation scope excerpt in the PDF."""
    context = proof.get("scope_context")
    if context is None:
        return False
    if not isinstance(context, dict):
        raise ValueError("bank scope context must be an object")
    page = context.get("page")
    excerpt = context.get("excerpt")
    kind = context.get("kind")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("bank scope context page is invalid")
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise ValueError("bank scope context excerpt is missing")
    if context.get("sha256") != hashlib.sha256(excerpt.encode()).hexdigest():
        raise ValueError("bank scope context excerpt hash mismatch")
    if kind not in {"report_default", "section_default"}:
        raise ValueError("bank scope context kind is unsupported")
    if kind == "section_default" and page != proof.get("pdf_page"):
        raise ValueError("bank section scope context must be on the field's page")
    page_text = normalized(bank_page_text(original.body, page))
    excerpt_text = normalized(excerpt)
    if excerpt_text not in page_text:
        raise ValueError("bank scope context excerpt is absent from the declared PDF page")
    if kind == "report_default":
        # This exact-language family is intentionally small. A generic group
        # mention or glossary definition cannot set the default accounting scope.
        explicit_default = re.search(
            r"(?:除特别(?:注明|说明)外|若无特别说明).{0,40}(?:均为|为|按)?"
            r"(?:本集团|集团合并|合并口径|合并报表数据|合并数据|本行及其子公司合并数据)",
            excerpt_text,
        )
        if explicit_default is None:
            raise ValueError("bank report does not declare a consolidated default scope")
    else:
        if not any(
            phrase in excerpt_text
            for phrase in ("本集团口径", "本集团主要", "集团口径", "合并口径", "本集团财务指标", "本集团财务比率")
        ):
            raise ValueError("bank section context does not explicitly identify group/consolidated scope")
    if "本行单体" in excerpt_text or "母公司口径" in excerpt_text:
        raise ValueError("bank scope context identifies standalone entity reporting")
    return True


def validate_bank_scope_exclusion(
    original: AnnualOriginal, metric: str, record: dict[str, Any]
) -> None:
    """Validate Ping An's explicit exclusion of two standalone regulatory ratios.

    This narrow negative-evidence path prevents bank-only NPL/coverage values
    from entering the consolidated peer cohort. It does not generalize to
    other issuers, metrics, or standalone-scope statements.
    """
    if (
        original.code != "000001.SZ"
        or metric not in {"npl_ratio", "provision_coverage"}
        or record.get("status") != "scope_mismatch"
        or record.get("accounting_scope") != "bank_standalone"
    ):
        raise ValueError("bank scope exclusion is not an approved Ping An standalone field")
    validate_bank_report_identity(original)
    if record.get("pdf_hash", record.get("pdf_sha256")) != original.pdf_hash:
        raise ValueError("bank scope exclusion PDF hash mismatch")
    page = record.get("pdf_page")
    if page != 20:
        raise ValueError("Ping An regulatory scope exclusion must cite page 20")
    excerpt = _require_text(record, "locator_excerpt")
    if record.get("locator_excerpt_sha256") != hashlib.sha256(excerpt.encode()).hexdigest():
        raise ValueError("bank scope exclusion locator hash mismatch")
    raw_page_text = bank_page_text(original.body, 20)
    page_text = normalized(raw_page_text)
    excerpt_text = normalized(excerpt)
    if excerpt_text not in page_text:
        raise ValueError("bank scope exclusion locator is absent from the actual PDF page")
    if "2.4.1主要监管指标" not in excerpt_text:
        raise ValueError("bank scope exclusion does not identify the regulatory-indicator table")
    period_header = normalized(_require_text(record, "period_header"))
    current_column = normalized(_require_text(record, "current_column"))
    if "2025年12月31日" not in period_header or "2025年12月31日" not in current_column:
        raise ValueError("bank scope exclusion does not identify the 2025 current period")
    if record.get("raw_unit") != "percent":
        raise ValueError("bank scope exclusion requires the printed percent unit")
    header = _require_text(record, "header")
    expected_header = {
        "npl_ratio": "不良贷款率",
        "provision_coverage": "拨备覆盖率",
    }[metric]
    if header != expected_header:
        raise ValueError("bank scope exclusion metric label is not the exact Ping An table row")
    raw_value = _decimal(record.get("raw_value"), "raw_value")
    expected_threshold = {"npl_ratio": "≤5", "provision_coverage": "≥130（注3）"}[metric]
    threshold = _require_text(record, "threshold")
    if normalized(threshold) != normalized(expected_threshold):
        raise ValueError("bank scope exclusion regulatory threshold is unexpected")
    row = next((line for line in raw_page_text.splitlines() if header in normalized(line)), "")
    if not row or normalized(expected_threshold) not in normalized(row):
        raise ValueError("bank scope exclusion threshold is absent from the actual metric row")
    row_tail = row[normalized(row).find(normalized(header)) + len(header) :]
    threshold_pattern = r"≤\s*5" if metric == "npl_ratio" else r"≥\s*130\s*[（(]\s*注\s*3\s*[）)]"
    threshold_match = re.search(threshold_pattern, row_tail)
    after_threshold = row_tail[threshold_match.end() :] if threshold_match is not None else ""
    numbers = [Decimal(value.replace(",", "")) for value, _ in _numbers(after_threshold)]
    if not numbers or numbers[0] != raw_value:
        raise ValueError("bank scope exclusion current-period value does not follow its printed threshold")
    scope = record.get("scope_context")
    if not isinstance(scope, dict) or scope.get("page") != 21 or scope.get("kind") != "scope_exclusion":
        raise ValueError("Ping An standalone scope note must be bound to page 21")
    scope_excerpt = _require_text(scope, "excerpt")
    if scope.get("sha256") != hashlib.sha256(scope_excerpt.encode()).hexdigest():
        raise ValueError("Ping An standalone scope note hash mismatch")
    scope_norm = normalized(scope_excerpt)
    actual_scope = normalized(bank_page_text(original.body, 21))
    required_note = "除资本充足率指标为本集团口径外，其他指标均为本行口径"
    if required_note not in scope_norm or scope_norm not in actual_scope:
        raise ValueError("Ping An page 21 does not explicitly scope this metric to the standalone bank")


def _numbers(line: str) -> list[tuple[str, int]]:
    return [
        (match.group().replace("−", "-"), match.start())
        for match in re.finditer(r"[-+−]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", line)
    ]


def _row_label_suffix_is_allowed(suffix: str, marker: str | None) -> bool:
    """Only accept a complete printed label followed by notation, not qualifiers."""
    suffix = suffix.lstrip()
    if not suffix:
        return True
    if marker is not None:
        marker_match = re.match(r"(?:[（(]\s*|)(\d{1,2})(?:\s*[）)]|)\s*", suffix)
        if marker_match is not None and marker_match.group(1) == marker:
            suffix = suffix[marker_match.end() :]
    # Percent notation and punctuation are harmless; words such as 普通股,
    # 扣除非经常性损益, 本行, etc. alter the metric's meaning and must remain
    # part of the proof header instead of being silently stripped.
    return not re.search(r"[^\s%％()（）:：,，。·•]", suffix)


def _year_values(line: str) -> list[int]:
    """Read ordered year tokens, including tables that omit 年 after years."""
    return [int(match.group()[:4]) for match in re.finditer(r"(?<!\d)20\d{2}(?:\s*年(?:度)?)?(?!\d)", line)]


def _changshu_narrative_matches(original: AnnualOriginal, metric: str, value: Decimal, page_text: str) -> bool:
    if original.code != "601128.SH":
        return False
    literal = {
        "roe_weighted_annual": f"加权平均净资产收益率（ROE）{value}%",
        "net_interest_margin": f"净息差{value}%",
        "npl_ratio": f"集团贷款不良率{value}%",
        "provision_coverage": f"集团拨备覆盖率{value}%",
    }.get(metric)
    return bool(literal and normalized(literal) in normalized(page_text))


def _matches_current_cell(
    line: str, header: str, raw_value: Decimal, proof: dict[str, Any], *, source: str,
    regulatory_threshold: str | None = None,
) -> bool:
    header_pattern = re.compile(r"\s*".join(re.escape(char) for char in header))
    marker = proof.get("footnote_marker")
    if marker is not None and (
        not isinstance(marker, str) or not re.fullmatch(r"\d{1,2}", marker)
    ):
        raise ValueError("bank footnote marker must be a short printed integer")
    # Locator excerpts may contain the table caption before the actual row
    # (e.g. a section title repeats the metric name). Examine line-bounded
    # row candidates; the positioned page-layout check below still chooses and
    # validates the actual current-column row.
    candidates = line.splitlines()
    for index, candidate in enumerate(candidates):
        match = header_pattern.search(candidate)
        if match is None:
            continue
        if "加权平均净资产收益率" in normalized(header) and "非经常性损益" in normalized(
            candidate[max(0, match.start() - 40) : match.start()]
        ):
            # The qualifying words may precede the generic metric label too.
            # Cropping them out of the proof cannot change the actual PDF row.
            continue
        tail = candidate[match.end() : match.end() + 240]
        if regulatory_threshold is not None:
            if marker is not None:
                return False
            threshold_pattern = r"^\s*≤\s*5\s*" if regulatory_threshold == "npl_ratio" else r"^\s*≥\s*150\s*"
            combined_tail = tail
            if not combined_tail and index + 1 < len(candidates):
                combined_tail = candidates[index + 1]
            threshold_match = re.match(threshold_pattern, combined_tail)
            if threshold_match is None:
                continue
            values = [value for value, _ in _numbers(combined_tail[threshold_match.end() :])]
            if values and _decimal(values[0], f"{source} current-column value") == raw_value:
                return True
            continue
        values = [value for value, _ in _numbers(tail)]
        before_first_value = tail[: _numbers(tail)[0][1]] if values else tail
        if not _row_label_suffix_is_allowed(before_first_value, marker):
            continue
        if marker is not None:
            if values and _decimal(values[0], "footnote marker") == Decimal(marker):
                values = values[1:]
            elif index + 1 < len(candidates) and re.match(
                rf"\s*[（(]\s*{re.escape(marker)}\s*[）)]", candidates[index + 1]
            ):
                wrapped = re.sub(
                    rf"^\s*[（(]\s*{re.escape(marker)}\s*[）)]", "", candidates[index + 1], count=1
                )
                values = [value for value, _ in _numbers(wrapped)]
            else:
                continue
        if values and _decimal(values[0], f"{source} current-column value") == raw_value:
            return True
    return False


def _ningbo_regulatory_threshold(metric: str, original: AnnualOriginal, proof: dict[str, Any]) -> str | None:
    if (
        original.code == "002142.SZ"
        and metric in {"npl_ratio", "provision_coverage"}
        and isinstance(proof.get("scope_bridge"), dict)
        and proof["scope_bridge"].get("method") == "ningbo-fy2025-final-ratio-ccxi-scope-v1"
        and proof.get("pdf_page") == 10
        and proof.get("footnote_marker") is None
    ):
        return metric
    return None


def _equity_statement_header_context(
    original: AnnualOriginal, proof: dict[str, Any], page: int, *, continuation: bool
) -> str:
    """Return only the actual statement title/header before its first data row."""
    text = bank_page_text(original.body, page)
    lines = text.splitlines()
    title = normalized(_require_text(proof, "table_title"))
    title_aliases = [title]
    if continuation and "(" in title:
        title_aliases.append(title.split("(", 1)[0])
    title_index = next(
        (i for i, line in enumerate(lines) if any(alias and alias in normalized(line) for alias in title_aliases)),
        None,
    )
    if title_index is None:
        raise ValueError("bank equity statement title is absent from its unit context page")
    if continuation:
        # For a true continuation, only inherit the adjacent preceding page's
        # table masthead, bounded at the first financial row.
        stop = min(len(lines), title_index + 31)
        for index in range(title_index + 1, stop):
            line = normalized(lines[index])
            if line and _numbers(lines[index]) and any(
                token in line for token in ("资产", "负债", "现金", "股本", "项目", "存放", "贷款", "债券")
            ):
                stop = index
                break
        preceding = [i for i in range(title_index) if lines[i].strip()]
        start = preceding[-5] if len(preceding) >= 5 else 0
        return "\n".join(lines[start:stop])
    header = _require_text(proof, "header")
    header_norm = normalized(header)
    row_index = next(
        (i for i, line in enumerate(lines) if header_norm in normalized(line)),
        None,
    )
    if row_index is None:
        raise ValueError("bank equity row is absent from its unit context page")
    if title_index < row_index:
        preceding = [i for i in range(title_index) if lines[i].strip()]
        start = preceding[-5] if len(preceding) >= 5 else 0
        return "\n".join(lines[start:row_index])
    # Some Chinese annual reports print the statement title as a footer after
    # the table. Keep the unit context on the same physical page, before the
    # selected equity row, and still require that the declared title exists on
    # that page. Text after the row can never supply a unit.
    return "\n".join(lines[:row_index])


def _verify_layout_column(
    original: AnnualOriginal, metric: str, proof: dict[str, Any], raw_value: Decimal
) -> None:
    """Bind the row value to the first (group/current-year) table column.

    Layout text preserves the x-order of multilevel statement headers. The
    complete declared PDF page is parsed so a client cannot crop off an earlier
    ``本行`` or prior-year column in its locator excerpt.
    """

    lines = _layout_page(original.body, proof["pdf_page"]).splitlines()
    title = normalized(proof["table_title"])
    header = normalized(proof["header"])
    title_index = next((i for i, line in enumerate(lines) if title in normalized(line)), None)
    continued = metric == "equity_parent" and proof.get("context_page") == proof["pdf_page"] - 1
    # Some PDFs expose a same-page heading in ordinary extraction but omit it
    # from pypdf's positioned layout stream. The caller has already checked
    # that the exact title occurs in the declared page excerpt.
    page_text = normalized(bank_page_text(original.body, proof["pdf_page"]))
    ningbo_supplement_roe = _ningbo_supplement_roe_scope(
        original, metric, proof, bank_page_text(original.body, proof["pdf_page"])
    )
    threshold_metric = _ningbo_regulatory_threshold(metric, original, proof)
    if title_index is None and (continued or title in page_text):
        title_index = 0
    row_index = next(
        (i for i, line in enumerate(lines[title_index:], start=title_index) if header in normalized(line)),
        None,
    ) if title_index is not None else None
    if ningbo_supplement_roe and title_index is not None:
        target_start = next(
            (i for i in range(title_index + 1, len(lines)) if "归属于公司普通股" in normalized(lines[i])),
            None,
        )
        if target_start is None:
            raise ValueError("Ningbo ROE matrix does not contain the complete 2025 numerator row")
        continuation = next(
            (i for i in range(target_start + 1, min(len(lines), target_start + 3)) if "股东的净利润" in normalized(lines[i])),
            None,
        )
        if continuation is None or "扣除非经常性损益" in normalized(lines[target_start]):
            raise ValueError("Ningbo ROE matrix row label is incomplete or qualified")
        row_index = continuation
    if metric != "equity_parent" and title_index is not None:
        raw_value = _decimal(proof.get("raw_value"), "raw_value")
        page_content = normalized(bank_page_text(original.body, proof["pdf_page"]))
        one_year_jiangsu_nim = (
            metric == "net_interest_margin"
            and original.code == "600919.SH"
            and normalized(f"报告期内，集团净息差 {raw_value}%") in page_content
        )
        one_year_ping_an_nim = (
            metric == "net_interest_margin"
            and original.code == "000001.SZ"
            and normalized(f"2025年，本集团净息差{raw_value}%") in page_content
        )
        one_year_ping_an_supplement_roe = _pingan_supplement_roe_scope(
            original, metric, proof, bank_page_text(original.body, proof["pdf_page"])
        )
        one_year_ningbo_supplement_roe = _ningbo_supplement_roe_scope(
            original, metric, proof, bank_page_text(original.body, proof["pdf_page"])
        )
        one_year_changshu_narrative = _changshu_narrative_matches(original, metric, raw_value, page_content)
        candidate_rows = [
            i for i in range(title_index + 1, len(lines)) if header in normalized(lines[i])
        ]
        valid_rows: list[int] = []
        for candidate in candidate_rows:
            try:
                value_matches = _matches_current_cell(
                    "\n".join(lines[candidate : min(len(lines), candidate + 2)]),
                    proof["header"], raw_value, proof, source="layout",
                    regulatory_threshold=threshold_metric,
                )
            except ValueError:
                value_matches = False
            if not value_matches:
                continue
            preceding = lines[:candidate]
            min_years = 1 if (one_year_jiangsu_nim or one_year_ping_an_nim or one_year_ping_an_supplement_roe or one_year_ningbo_supplement_roe or one_year_changshu_narrative) else 2
            period_line = next(
                (
                    line for line in reversed(preceding)
                    if len(_year_values(line)) >= min_years
                ),
                "",
            )
            period_years = (
                _year_values(lines[candidate])
                if one_year_ping_an_nim
                else _year_values(period_line)
            )
            if not period_years and one_year_changshu_narrative and str(original.period.year) in normalized(
                proof["period_header"]
            ):
                period_years = [original.period.year]
            if period_years and period_years[0] == original.period.year and (
                original.period.year - 1 in period_years
                or one_year_jiangsu_nim
                or one_year_ping_an_nim
                or one_year_ping_an_supplement_roe
                or one_year_ningbo_supplement_roe
                or one_year_changshu_narrative
            ):
                valid_rows.append(candidate)
        if valid_rows:
            row_index = valid_rows[0]
    if title_index is None or row_index is None or (row_index <= title_index and not continued):
        raise ValueError("bank table title and metric row are not ordered in the PDF layout")
    table_lines = lines[title_index : min(len(lines), row_index + 1)]
    group_line = next((line for line in table_lines if "本集团" in line), "")
    own_line = next((line for line in table_lines if "本行" in line), "")
    if metric == "equity_parent":
        consolidated_context = False
        if continued:
            context = _layout_page(original.body, proof["context_page"])
            context_norm = normalized(context)
            plain_context_norm = normalized(bank_page_text(original.body, proof["context_page"]))
            if not any(title in context_norm for title in ("合并资产负债表", "合并及银行资产负债表", "合并及公司资产负债表")):
                if not any(title in plain_context_norm for title in ("合并资产负债表", "合并及银行资产负债表", "合并及公司资产负债表")):
                    raise ValueError("bank equity continuation context is not a consolidated balance sheet")
                context_norm = plain_context_norm
            consolidated_context = True
            if "本集团" in context_norm and "本行" in context_norm and context_norm.find("本集团") > context_norm.find("本行"):
                raise ValueError("bank statement context puts the bank column before the consolidated group")
            context_dates = re.findall(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", context_norm)
            if not context_dates or not context_dates[0].startswith(str(original.period.year)):
                raise ValueError("bank equity continuation context does not establish the current period")
        elif group_line and own_line:
            if group_line.find("本集团") > group_line.find("本行"):
                raise ValueError("bank statement layout does not put the consolidated group before the bank column")
        elif any(
            token in normalized(proof["table_title"])
            for token in ("合并资产负债表", "合并及银行资产负债表", "合并及公司资产负债表")
        ):
            consolidated_context = True
        else:
            raise ValueError("bank statement layout does not establish consolidated statement scope")
        expected = [original.period.year, original.period.year - 1]
        full_date_lines = [
            line for line in table_lines
            if len(re.findall(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", line)) >= 2
        ]
        year_lines = [
            line for line in table_lines
            if len(_year_values(line)) >= (2 if (continued or consolidated_context) else 4)
        ]
        if continued or consolidated_context:
            full_dates = [
                int(match.group()[:4])
                for line in full_date_lines
                for match in re.finditer(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", line)
            ]
            years = _year_values(year_lines[-1]) if year_lines else []
            actual = full_dates[:2] if len(full_dates) >= 2 else years[:2]
            if actual != expected:
                raise ValueError("bank statement layout does not show current year before comparative column")
        else:
            # Some statements print one report date, then group/bank labels,
            # then year-only columns (2025, 2024, 2025, 2024) and month/day
            # on a separate line. Validate that exact x-ordered pattern.
            full_dates = [
                int(match.group()[:4])
                for line in full_date_lines
                for match in re.finditer(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", line)
            ]
            if len(full_dates) >= 4:
                actual = full_dates[:4]
            else:
                year_line = next((line for line in year_lines if len(_year_values(line)) >= 4), "")
                actual = _year_values(year_line)
                has_report_date = any(
                    str(original.period.year) in re.findall(
                        r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", line
                    )[0]
                    for line in table_lines
                    if re.findall(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", line)
                )
                if not has_report_date:
                    # Some positioned streams retain the ordered columns but
                    # omit the statement date. The same-page plain extraction
                    # is already bound by the caller's exact excerpt/hash and
                    # table/date checks, so it may supply this date context.
                    has_report_date = (
                        str(original.period.year) in normalized(proof["period_header"])
                        and normalized(proof["period_header"]) in page_text
                    )
                if not has_report_date:
                    raise ValueError("bank statement layout does not identify its report-date context")
            if actual[:4] != expected * 2:
                raise ValueError("bank statement layout does not put group current year before comparative columns")
    else:
        # Annual tables can have dozens of blank layout lines between their
        # headers and rows. Find the closest preceding year-header in the full
        # declared table context rather than applying an arbitrary line window.
        # Some reports print the period heading immediately before a subsection
        # label (CMB's "资产质量指标" is the concrete example), while others
        # put it after the title. Search only the current page and stop at the
        # metric row; the nearest header remains tied to the actual layout.
        header_lines = lines[: row_index + 1]
        # Entity labels must be read from this table's own layout, starting
        # at its title. A report-wide default-scope excerpt or current_column
        # claim cannot override an actual 本行-before-本集团 column order.
        table_layout = lines[title_index : row_index + 1]
        group_position = next(
            ((i, line.find("本集团")) for i, line in enumerate(table_layout) if "本集团" in line),
            None,
        )
        own_position = next(
            ((i, line.find("本行")) for i, line in enumerate(table_layout) if "本行" in line),
            None,
        )
        group_precedes_own = own_position is None or (
            group_position is not None
            and (group_position[0], group_position[1]) < (own_position[0], own_position[1])
        )
        if own_position is not None and (group_position is None or not group_precedes_own):
            raise ValueError("bank indicator table puts 本行 before the consolidated group column")
        # A consolidated-only table may omit a legal-entity comparison; the
        # proof's current-column header must still identify the group.
        if not group_precedes_own and not any(
            token in normalized(proof["current_column"])
            for token in ("本集团", "集团口径", "集团", "合并口径", "合并报表")
        ):
            raise ValueError("bank indicator layout does not establish consolidated group scope")
        if (
            metric in {"npl_ratio", "provision_coverage"}
            and not group_precedes_own
            and not _validated_scope_context(original, proof)
        ):
            full_page = normalized(bank_page_text(original.body, proof["pdf_page"]))
            if JIANGSU_CONSOLIDATION_FOOTNOTE not in full_page:
                raise ValueError("bank indicator page does not contain the approved consolidated-scope footnote")
        year_line = lines[row_index] if one_year_ping_an_nim else next(
            (
                line for line in reversed(header_lines[:-1])
                if len(_year_values(line)) >= (
                    1 if (metric == "net_interest_margin" and original.code == "600919.SH") or one_year_ping_an_supplement_roe or one_year_ningbo_supplement_roe else 2
                )
            ),
            "",
        )
        years = _year_values(year_line)
        jiangsu_annual_group_margin = (
            metric == "net_interest_margin"
            and original.code == "600919.SH"
            and normalized(f"报告期内，集团净息差 {raw_value}%") in normalized(
                bank_page_text(original.body, proof["pdf_page"])
            )
        )
        changshu_annual_group_narrative = _changshu_narrative_matches(
            original, metric, raw_value, bank_page_text(original.body, proof["pdf_page"])
        ) and str(original.period.year) in normalized(proof["period_header"])
        if changshu_annual_group_narrative and not years:
            years = [original.period.year]
        valid_period_columns = (
            bool(years)
            and years[0] == original.period.year
            and (
                original.period.year - 1 in years
                or jiangsu_annual_group_margin
                or one_year_ping_an_nim
                or one_year_ping_an_supplement_roe
                or one_year_ningbo_supplement_roe
                or changshu_annual_group_narrative
            )
        )
        if not valid_period_columns:
            raise ValueError("bank indicator layout does not put the current year before comparative years")
    row = lines[row_index]
    if ningbo_supplement_roe:
        if not _matches_current_cell(
            lines[row_index], "股东的净利润", raw_value, proof, source="layout"
        ):
            raise ValueError("Ningbo ROE row's first layout value is not the 2025 weighted-average ROE")
    else:
        row_text = (
            "\n".join(lines[row_index : min(len(lines), row_index + 2)])
            if threshold_metric is not None
            else row
        )
        if not _matches_current_cell(
            row_text, proof["header"], raw_value, proof, source="layout",
            regulatory_threshold=threshold_metric,
        ):
            raise ValueError("bank row's first layout value is not the consolidated current-column value")


def _identity_matches(original: AnnualOriginal, proof: dict[str, Any], audit: dict[str, Any]) -> None:
    period = original.period.isoformat()
    identity = proof.get("report_identity")
    if not isinstance(identity, dict):
        raise ValueError("bank proof report identity is missing")
    if identity.get("code") != original.code or identity.get("period") != period:
        raise ValueError("bank proof issuer or annual period mismatch")
    metadata = original.metadata
    chosen = metadata.get("chosen", {}) if isinstance(metadata, dict) else {}
    if identity.get("disclosure_id") != chosen.get("announcementId"):
        raise ValueError("bank proof disclosure identity mismatch")
    if (
        audit.get("code") != original.code
        or audit.get("period") != period
        or audit.get("disclosure_id") != chosen.get("announcementId")
        or audit.get("pdf_sha256") != original.pdf_hash
    ):
        raise ValueError("bank audit record identity does not match the original")
    if audit.get("title") != chosen.get("announcementTitle"):
        raise ValueError("bank audit filing title mismatch")
    if audit.get("url") != metadata.get("url"):
        raise ValueError("bank audit filing URL mismatch")
    from investment_agent.evidence.disclosure_http import validate_url

    validate_url(_require_text(audit, "url"), "original")
    if audit.get("published_at") != original.published_at.isoformat():
        raise ValueError("bank audit publication time mismatch")


def _validate_ping_an_parent_equity_derivation(
    original: AnnualOriginal, proof: dict[str, Any], audit: dict[str, Any], page_content: str
) -> None:
    """Prove Ping An's group total equals parent equity from report-specific facts.

    This exception is intentionally bound to the FY2025 report's definition of
    the complete group, the wholly-owned subsidiary ownership row, and the
    accounting policy that requires any non-parent interest to be presented
    separately. It never accepts a total-equity row on its own.
    """

    if (
        original.code != "000001.SZ"
        or proof.get("header") != "股东权益合计"
        or proof.get("derived_parent_total") is not True
        or audit.get("derived_parent_total") is not True
    ):
        raise ValueError("only the evidenced Ping An total-equity derivation is supported")
    derivation = proof.get("equity_derivation")
    if not isinstance(derivation, dict) or derivation.get("method") != "wholly_owned_group_total_as_parent":
        raise ValueError("Ping An group total requires an explicit wholly-owned derivation")
    if derivation.get("nci_value") not in (0, "0", "0.0"):
        raise ValueError("Ping An parent-equity derivation requires zero noncontrolling interest")
    if audit.get("equity_derivation") != derivation:
        raise ValueError("Ping An equity derivation audit evidence does not match proof")
    expected = {"scope": 5, "ownership": 173, "nci_policy": 136}
    pages = {
        "scope": bank_page_text(original.body, 5),
        "ownership": bank_page_text(original.body, 173),
        "nci_policy": bank_page_text(original.body, 136),
    }
    normalized_pages = {name: normalized(content) for name, content in pages.items()}
    for name, expected_page in expected.items():
        item = derivation.get(name)
        if not isinstance(item, dict) or item.get("page") != expected_page:
            raise ValueError("Ping An equity derivation uses an unexpected report page")
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            raise ValueError("Ping An equity derivation excerpt is missing")
        if hashlib.sha256(excerpt.encode()).hexdigest() != item.get("excerpt_sha256"):
            raise ValueError("Ping An equity derivation excerpt hash mismatch")
        if normalized(excerpt) not in normalized_pages[name]:
            raise ValueError("Ping An equity derivation excerpt is absent from its report page")
    scope_text = normalized(derivation["scope"]["excerpt"])
    if "本集团指平安银行股份有限公司及其全资子公司平安理财有限责任公司" not in scope_text:
        raise ValueError("Ping An report does not define the complete group as wholly owned")
    ownership_text = normalized(derivation["ownership"]["excerpt"])
    if "平安理财有限责任公司" not in ownership_text or "100%" not in ownership_text:
        raise ValueError("Ping An report does not confirm full ownership of the defined subsidiary")
    policy_text = normalized(derivation["nci_policy"]["excerpt"])
    if "少数股东权益" not in policy_text or "单独列示" not in policy_text:
        raise ValueError("Ping An accounting policy does not require separate NCI presentation")
    if "少数股东权益" in normalized(page_content):
        raise ValueError("Ping An balance sheet presents nonzero or explicit NCI")


def checked_bank_locator(
    original: AnnualOriginal,
    metric: str,
    proof: dict[str, Any],
    *,
    supplemental_originals: dict[str, AnnualOriginal] | None = None,
) -> dict[str, Any]:
    """Validate one bank proof against its exact PDF page and return rule facts.

    ``audit_record`` is the per-issuer/per-field manifest record. The record's
    own status is ignored. Its locator excerpt must occur on the declared page,
    with the declared table, annual period/current column, metric label and
    raw value. The caller separately binds the parsed value to the snapshot.
    """

    if metric not in BANK_METRICS:
        raise ValueError("unsupported bank annual metric")
    audit = proof.get("audit_record")
    if not isinstance(audit, dict):
        raise ValueError("bank field audit record is missing")
    descriptors = proof.get("supplemental_originals", [])
    if not isinstance(descriptors, list) or descriptors != audit.get("supplemental_originals", []):
        raise ValueError("bank supplemental original descriptors differ from the audit record")
    if descriptors:
        bridge = proof.get("scope_bridge")
        if not (
            original.code == "002142.SZ"
            and original.period.isoformat() == "2025-12-31"
            and metric in {"npl_ratio", "provision_coverage"}
            and isinstance(bridge, dict)
            and bridge.get("method") == "ningbo-fy2025-final-ratio-ccxi-scope-v1"
        ):
            raise ValueError("supplemental originals are unsupported for this bank field")
    validate_bank_report_identity(original)
    if proof.get("check_rule_version") != BANK_RULE:
        raise ValueError("bank proof rule version mismatch")
    _identity_matches(original, proof, audit)
    if proof.get("pdf_hash") != original.pdf_hash:
        raise ValueError("bank proof PDF hash mismatch")
    if audit.get("check_rule_version") != BANK_RULE:
        raise ValueError("bank audit rule version mismatch")
    if audit.get("metric") != metric or audit.get("status") != "verified":
        raise ValueError("bank audit record is not verified for the requested metric")
    if audit.get("raw_label") != proof.get("header"):
        raise ValueError("bank audit raw label does not match the checked field header")
    _require_text(audit, "definition_or_footnote")

    page = proof.get("pdf_page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("invalid bank original page")
    # No neighboring-page or document-wide search: the declared page must
    # contain the full locator excerpt from the annual-report original.
    page_content = bank_page_text(original.body, page)
    excerpt = _require_text(proof, "locator_excerpt")
    excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    if proof.get("locator_excerpt_sha256") != excerpt_hash:
        raise ValueError("bank locator excerpt hash mismatch")
    if normalized(excerpt) not in normalized(page_content):
        raise ValueError("bank locator excerpt is absent from the declared PDF page")
    scope_context_valid = False
    if proof.get("scope_context") is not None:
        if audit.get("scope_context") != proof.get("scope_context"):
            raise ValueError("bank scope context audit evidence does not match proof")
        scope_context_valid = _validated_scope_context(original, proof)
    scope_bridge_valid = False
    if proof.get("scope_bridge") is not None or audit.get("scope_bridge") is not None:
        if not isinstance(proof.get("scope_bridge"), dict) or proof.get("scope_bridge") != audit.get("scope_bridge"):
            raise ValueError("bank scope bridge must match between proof and audit record")
        if _pingan_supplement_roe_scope(original, metric, proof, page_content):
            scope_bridge_valid = True
        elif original.code == "002142.SZ" and metric in {"npl_ratio", "provision_coverage"}:
            from investment_agent.industry_peers.bank_scope_bridges import (
                checked_ningbo_final_ratio_scope,
            )

            scope_bridge_valid = checked_ningbo_final_ratio_scope(
                original, metric, proof, supplemental_originals
            )
        elif original.code == "002142.SZ" and metric == "net_interest_margin":
            from investment_agent.industry_peers.bank_scope_bridges import checked_ningbo_nim_bridge

            scope_bridge_valid = checked_ningbo_nim_bridge(original, proof)
        elif original.code == "601288.SH":
            from investment_agent.industry_peers.bank_abc_bridges import checked_abc_scope_bridge

            scope_bridge_valid = checked_abc_scope_bridge(original, metric, proof)
        if not scope_bridge_valid:
            raise ValueError("bank scope bridge is not valid for this issuer and metric")

    table = _require_text(proof, "table_title")
    period_header = _require_text(proof, "period_header")
    current_column = _require_text(proof, "current_column")
    header = _require_text(proof, "header")
    excerpt_norm = normalized(excerpt)
    context_page = proof.get("context_page")
    context_excerpt = proof.get("context_excerpt")
    context_norm = ""
    if context_page is not None or context_excerpt is not None:
        if (
            metric != "equity_parent"
            or isinstance(context_page, bool)
            or not isinstance(context_page, int)
            or context_page != page - 1
        ):
            raise ValueError("bank table context must be the immediately preceding equity page")
        context_excerpt = _require_text(proof, "context_excerpt")
        context_hash = hashlib.sha256(context_excerpt.encode("utf-8")).hexdigest()
        if proof.get("context_excerpt_sha256") != context_hash:
            raise ValueError("bank table context excerpt hash mismatch")
        context_content = bank_page_text(original.body, context_page)
        context_norm = normalized(context_excerpt)
        if context_norm not in normalized(context_content):
            raise ValueError("bank table context excerpt is absent from the preceding PDF page")
        if not any(title in context_norm for title in ("合并资产负债表", "合并及银行资产负债表", "合并及公司资产负债表")):
            raise ValueError("bank continuation context does not identify a consolidated balance sheet")
    table_context = excerpt_norm + context_norm
    for label, value in (("table title", table), ("period header", period_header)):
        if normalized(value) not in table_context:
            raise ValueError(f"bank {label} is absent from the declared PDF context")
    year = str(original.period.year)
    normalized_column = normalized(current_column)
    if year not in normalized(period_header) or year not in normalized_column:
        raise ValueError("bank current column does not identify the target annual period")
    if str(original.period.year - 1) in normalized_column:
        raise ValueError("bank current column points to the previous year")
    pingan_supplement_scope = _pingan_supplement_roe_scope(original, metric, proof, page_content)
    ningbo_supplement_scope = _ningbo_supplement_roe_scope(original, metric, proof, page_content)
    if not any(token in normalized_column for token in ("本集团", "集团口径", "集团", "合并口径", "合并报表")) and not pingan_supplement_scope and not ningbo_supplement_scope and not scope_bridge_valid:
        raise ValueError("bank current column does not explicitly identify the consolidated group")
    explicit_group = any(token in table_context for token in ("本集团", "集团口径", "集团", "合并资产负债表"))
    if original.code == "000001.SZ" and metric == "roe_weighted_annual" and table == PINGAN_ROE_SUPPLEMENT:
        # The same page contains unrelated group-language elsewhere. Only the
        # validated statement bridge above establishes scope for this matrix.
        explicit_group = pingan_supplement_scope
    if not explicit_group and not scope_context_valid and not pingan_supplement_scope and not ningbo_supplement_scope and not scope_bridge_valid:
        approved_footnote_scope = (
            metric in {"npl_ratio", "provision_coverage"}
            and JIANGSU_CONSOLIDATION_FOOTNOTE in normalized(page_content)
        )
        if not approved_footnote_scope:
            raise ValueError("bank locator excerpt does not establish group/consolidated scope")
    scope = proof.get("accounting_scope")
    if scope != "group_consolidated":
        raise ValueError("bank annual proof requires the consolidated group scope")
    if not scope_context_valid and not pingan_supplement_scope and not ningbo_supplement_scope and not scope_bridge_valid and not any(token in table_context for token in ("本集团", "集团口径", "集团", "合并")):
        raise ValueError("bank locator excerpt does not establish group/consolidated scope")
    ningbo_nim_bridge = scope_bridge_valid and original.code == "002142.SZ" and metric == "net_interest_margin"

    if metric == "equity_parent":
        derived_ping_an_total = header == "股东权益合计"
        if derived_ping_an_total:
            _validate_ping_an_parent_equity_derivation(original, proof, audit, page_content)
        elif header not in EQUITY_HEADERS:
            raise ValueError("bank equity header must identify total equity attributable to parent owners")
        if any(token in header for token in ("普通股", "少数股东", "母公司报表")) or (
            "本行" in header and not any(
                token in (table_context + context_norm)
                for token in ("本集团", "合并资产负债表")
            )
        ):
            raise ValueError("bank equity header has an incompatible ownership or entity scope")
        if not any(token in normalized(table_context) for token in ("合并资产负债表", "资产负债表")):
            raise ValueError("bank parent equity requires the consolidated balance sheet")
        raw_unit = proof.get("raw_unit")
        if not isinstance(raw_unit, str):
            raise ValueError("bank equity original unit is unsupported")
        factor = EQUITY_UNITS.get(raw_unit)
        if factor is None:
            raise ValueError("bank equity original unit is unsupported")
        continuation_unit = isinstance(context_page, int) and context_page == page - 1
        unit_page = context_page if continuation_unit and isinstance(context_page, int) else page
        unit_context = _equity_statement_header_context(
            original, proof, unit_page, continuation=continuation_unit
        )
        unit_markers = {
            "CNY": ("元",),
            "CNY_thousand": ("千元",),
            "CNY_million": ("百万元",),
        }[raw_unit]
        if not any(unit in unit_context for unit in unit_markers):
            raise ValueError("bank equity printed unit does not match its declared original unit")
        if raw_unit == "CNY" and any(unit in unit_context for unit in ("千元", "百万元")):
            raise ValueError("bank equity printed unit does not match its declared original unit")
        if "千元" in unit_context and "百万元" in unit_context:
            raise ValueError("bank equity statement header contains conflicting printed units")
        scope_result = "group_consolidated"
    else:
        aliases = RATIO_HEADERS[metric]
        ningbo_nim_header = ningbo_nim_bridge and normalized(header) == "净息差(NIM)"
        if header not in aliases and not pingan_supplement_scope and not ningbo_supplement_scope and not ningbo_nim_header:
            raise ValueError("bank ratio header does not identify the requested metric")
        ningbo_nim_table = ningbo_nim_bridge and "生息资产和付息负债的平均余额" in normalized(table)
        if not pingan_supplement_scope and not ningbo_supplement_scope and not ningbo_nim_table and not any(token in normalized(table) for token in ("主要财务指标", "主要指标", "经营情况", "风险", "净息差", "净利息收益率", "财务指标", "财务比率", "财务摘要", "资产质量指标", "生息资产平均收益率", "加权平均净资产收益率的计算过程")):
            raise ValueError("bank ratio table identity is not a recognized annual indicator table")
        if proof.get("raw_unit") != "percent":
            raise ValueError("bank ratio must preserve the original percent unit")
        factor = Decimal("0.01")
        scope_result = "group_consolidated"

    raw_value = _decimal(proof.get("raw_value"), "raw_value")
    parsed_value = _decimal(proof.get("parsed_value"), "parsed_value")
    if parsed_value != raw_value * factor:
        raise ValueError("bank parsed value does not match the source value and unit conversion")
    # Bind the row label to the first numeric cell following it. Annual tables
    # put the current year first, followed by the comparative year; accepting a
    # later number here would let last year's cell masquerade as current.
    header_at = excerpt_norm.find(normalized(header))
    if header_at < 0:
        raise ValueError("bank metric header is absent from the locator excerpt")
    one_year_jiangsu_nim_evidence = (
        metric == "net_interest_margin"
        and original.code == "600919.SH"
        and normalized(f"报告期内，集团净息差 {raw_value}%") in excerpt_norm
    )
    changshu_narrative_evidence = _changshu_narrative_matches(
        original, metric, raw_value, excerpt
    )
    threshold_metric = _ningbo_regulatory_threshold(metric, original, proof)
    if (
        not _matches_current_cell(
            excerpt, header, raw_value, proof, source="excerpt",
            regulatory_threshold=threshold_metric,
        )
        and not one_year_jiangsu_nim_evidence
        and not changshu_narrative_evidence
        and not ningbo_supplement_scope
    ):
        raise ValueError("bank source value is not the current-column value for its metric row")
    if metric in RATIO_METRICS and "%" not in excerpt_norm and "％" not in excerpt_norm:
        raise ValueError("bank ratio locator does not show a percent unit")
    _verify_layout_column(original, metric, proof, raw_value)
    # The manifest row is itself an audit object. Critical locator fields must
    # agree exactly with the proof fields so a stale/edited proof cannot borrow
    # an unrelated audit status.
    bound = {
        "pdf_page": page,
        "table_title": table,
        "period_header": period_header,
        "current_column": current_column,
        "header": header,
        "raw_value": proof.get("raw_value"),
        "raw_unit": proof.get("raw_unit"),
        "parsed_value": proof.get("parsed_value"),
        "accounting_scope": scope,
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": excerpt_hash,
        "footnote_marker": proof.get("footnote_marker"),
    }
    if context_page is not None:
        bound.update({
            "context_page": context_page,
            "context_excerpt": context_excerpt,
            "context_excerpt_sha256": proof["context_excerpt_sha256"],
        })
    if proof.get("scope_context") is not None:
        bound["scope_context"] = proof["scope_context"]
    if proof.get("scope_bridge") is not None:
        bound["scope_bridge"] = proof["scope_bridge"]
        if proof.get("scope_basis") != "cross_statement_bridge":
            raise ValueError("bank scope bridge must declare cross_statement_bridge scope basis")
        bound["scope_basis"] = proof["scope_basis"]
    if proof.get("supplemental_originals") is not None:
        bound["supplemental_originals"] = proof["supplemental_originals"]
    for key, value in bound.items():
        if audit.get(key) != value:
            raise ValueError(f"bank audit record {key} does not match the proof")
    checks = {
        "accounting_scope": scope_result,
        "header_context_hash": hashlib.sha256(normalized(excerpt).encode()).hexdigest(),
        "check_rule_version": BANK_RULE,
    }
    if scope_bridge_valid:
        checks["scope_basis"] = "cross_statement_bridge"
    return checks
