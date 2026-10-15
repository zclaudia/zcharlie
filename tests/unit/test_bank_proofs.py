from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.bank_proofs import (
    BANK_RULE,
    NINGBO_ROE_ROW,
    NINGBO_ROE_SUPPLEMENT,
    PINGAN_ROE_ROW,
    PINGAN_ROE_SUPPLEMENT,
    _ningbo_supplement_roe_scope,
    _pingan_supplement_roe_scope,
    checked_bank_locator,
    validate_bank_scope_exclusion,
)
from investment_agent.industry_peers.originals import AnnualOriginal, _bank_readback_times_valid

ROOT = Path(__file__).resolve().parents[2]
BANK_EVIDENCE_ROOT = ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15"


def _real_bank_original(code: str) -> tuple[AnnualOriginal, str]:
    manifest = json.loads((BANK_EVIDENCE_ROOT / "source-manifest.json").read_text(encoding="utf-8"))
    source = manifest["sources"][code]
    filing = json.loads((BANK_EVIDENCE_ROOT / "filings" / f"{code}.json").read_text(encoding="utf-8"))
    body = (ROOT / source["pdf_path"]).read_bytes()
    metadata = source["metadata"]
    metadata["url"] = filing["url"]
    published = datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC)
    return AnnualOriginal(
        code=code,
        period=date(2025, 12, 31),
        body=body,
        metadata=metadata,
        pdf_hash=hashlib.sha256(body).hexdigest(),
        published_at=published,
    ), filing["url"]


def _full_bank_proof(original: AnnualOriginal, url: str, metric: str) -> dict[str, object]:
    code = original.code
    page = 288 if code == "000001.SZ" else 236
    excerpt = bank_page_text(original.body, page)
    if code == "000001.SZ":
        table, period, column, header, value = (
            PINGAN_ROE_SUPPLEMENT, "2025年度", "2025年度 加权平均", PINGAN_ROE_ROW, "9.15"
        )
        bridge: dict[str, object] = {}
        for role, source_page in (("consolidated", 120), ("standalone", 122)):
            text = bank_page_text(original.body, source_page)
            row_at = text.index("六、净利润")
            bridge[f"{role}_page"] = source_page
            bridge[f"{role}_excerpt"] = text[:row_at] + text[row_at : text.index("\n", row_at)]
            bridge[f"{role}_sha256"] = hashlib.sha256(str(bridge[f"{role}_excerpt"]).encode()).hexdigest()
        scope_basis = "cross_statement_bridge"
    else:
        table, period, column, header, value = (
            NINGBO_ROE_SUPPLEMENT, "2025年", "2025年", NINGBO_ROE_ROW, "13.11"
        )
        bridge = {}
        scope_basis = "explicit_metric_scope_sentence_on_same_table"
    original.metadata["url"] = url
    pub = original.published_at.isoformat()
    checked_at = "2026-10-15T17:00:00+00:00"
    proof: dict[str, object] = {
        "check_rule_version": BANK_RULE,
        "pdf_hash": original.pdf_hash,
        "report_identity": {
            "code": code,
            "period": "2025-12-31",
            "disclosure_id": original.metadata["chosen"]["announcementId"],
        },
        "period": "2025-12-31",
        "pdf_page": page,
        "table_title": table,
        "period_header": period,
        "current_column": column,
        "header": header,
        "raw_value": value,
        "raw_unit": "percent",
        "parsed_value": str(float(value) / 100),
        "accounting_scope": "group_consolidated",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "definition_or_footnote": "按证监会规则第9号计算加权平均净资产收益率。",
        "scope_basis": scope_basis,
        "checked_at": checked_at,
    }
    if bridge:
        proof["scope_bridge"] = bridge
    audit: dict[str, object] = {
        "code": code,
        "period": "2025-12-31",
        "disclosure_id": original.metadata["chosen"]["announcementId"],
        "pdf_sha256": original.pdf_hash,
        "title": original.metadata["chosen"]["announcementTitle"],
        "url": url,
        "published_at": pub,
        "acquired_at": "2026-10-15T13:22:43.681578+00:00",
        "checked_at": checked_at,
        "check_rule_version": BANK_RULE,
        "metric": metric,
        "status": "verified",
        "raw_label": header,
        "definition_or_footnote": proof["definition_or_footnote"],
    }
    for key in (
        "pdf_page", "table_title", "period_header", "current_column", "header", "raw_value",
        "raw_unit", "parsed_value", "accounting_scope", "locator_excerpt", "locator_excerpt_sha256",
    ):
        audit[key] = proof[key]
    if bridge:
        audit["scope_bridge"] = bridge
        audit["scope_basis"] = scope_basis
    proof["audit_record"] = audit
    return proof


def test_checked_bank_locator_accepts_ningbo_wrapped_roe_row_from_report_pdf() -> None:
    source_pdf = BANK_EVIDENCE_ROOT / "originals/002142.SZ_2025.pdf"
    if not source_pdf.exists():
        pytest.skip("downloaded Ningbo official annual PDF is unavailable in this checkout")
    original, url = _real_bank_original("002142.SZ")
    proof = _full_bank_proof(original, url, "roe_weighted_annual")
    result = checked_bank_locator(original, "roe_weighted_annual", proof)
    assert result["accounting_scope"] == "group_consolidated"
    assert proof["header"] == "归属于公司普通股股东的净利润"
    assert proof["raw_value"] == "13.11"

    previous_year = copy.deepcopy(proof)
    previous_year["current_column"] = "2024年"
    previous_year["audit_record"]["current_column"] = "2024年"  # type: ignore[index]
    with pytest.raises(ValueError):
        checked_bank_locator(original, "roe_weighted_annual", previous_year)


def test_checked_bank_locator_pingan_roe_bridge_rejects_wrong_row_or_missing_bridge() -> None:
    source_pdf = BANK_EVIDENCE_ROOT / "originals/000001.SZ_2025.pdf"
    if not source_pdf.exists():
        pytest.skip("downloaded Ping An official annual PDF is unavailable in this checkout")
    original, url = _real_bank_original("000001.SZ")
    proof = _full_bank_proof(original, url, "roe_weighted_annual")
    checks = checked_bank_locator(original, "roe_weighted_annual", proof)
    assert checks["scope_basis"] == "cross_statement_bridge"
    assert checks["accounting_scope"] == "group_consolidated"

    attacks = []
    eps = copy.deepcopy(proof)
    eps["header"] = "每股收益"
    eps["audit_record"]["header"] = "每股收益"  # type: ignore[index]
    eps["audit_record"]["raw_label"] = "每股收益"  # type: ignore[index]
    attacks.append(eps)

    nonrecurring_same_value = copy.deepcopy(proof)
    nonrecurring_same_value["header"] = "扣除非经常性损益后归属于母公司普通股股东的净利润"
    nonrecurring_same_value["audit_record"]["header"] = nonrecurring_same_value["header"]  # type: ignore[index]
    nonrecurring_same_value["audit_record"]["raw_label"] = nonrecurring_same_value["header"]  # type: ignore[index]
    attacks.append(nonrecurring_same_value)

    prior_year = copy.deepcopy(proof)
    prior_year["current_column"] = "2024年度 加权平均"
    prior_year["audit_record"]["current_column"] = prior_year["current_column"]  # type: ignore[index]
    attacks.append(prior_year)

    forged_bridge = copy.deepcopy(proof)
    forged_bridge["scope_bridge"]["standalone_page"] = 120  # type: ignore[index]
    forged_bridge["audit_record"]["scope_bridge"] = copy.deepcopy(forged_bridge["scope_bridge"])  # type: ignore[index]
    attacks.append(forged_bridge)

    missing_bridge = copy.deepcopy(proof)
    missing_bridge.pop("scope_bridge")
    missing_bridge.pop("scope_basis")
    missing_bridge["audit_record"].pop("scope_bridge")  # type: ignore[union-attr]
    missing_bridge["audit_record"].pop("scope_basis")  # type: ignore[union-attr]
    attacks.append(missing_bridge)

    for attack in attacks:
        with pytest.raises(ValueError):
            checked_bank_locator(original, "roe_weighted_annual", attack)


def test_bank_readback_review_time_is_bound_to_audit_time() -> None:
    boundary = datetime(2026, 10, 15, 15, tzinfo=UTC)
    proof = {"checked_at": "2026-10-15T13:00:00+00:00"}
    audit = {
        "checked_at": "2026-10-15T14:00:00+00:00",
        "acquired_at": "2026-10-15T12:00:00+00:00",
    }
    assert not _bank_readback_times_valid(proof, audit, boundary)
    proof["checked_at"] = audit["checked_at"]
    assert _bank_readback_times_valid(proof, audit, boundary)
    audit["acquired_at"] = "2026-10-15T14:30:00+00:00"
    assert not _bank_readback_times_valid(proof, audit, boundary)


def test_pingan_supplement_roe_requires_consolidated_profit_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_agent.industry_peers import bank_proofs

    supplement = """财务报表补充资料 2025年度 净资产收益率和每股收益
2025年度 净资产收益率 每股收益人民币元 加权平均 基本 稀释
归属于母公司普通股股东的净利润 9.15% 2.07 2.07
扣除非经常性损益后归属于母公司普通股股东的净利润 9.15% 2.07 2.07
其中，扣除非经常性损益后归属于母公司普通股股东的净利润：
归属于母公司股东的净利润 42,633 44,508
减：母公司优先股宣告股息 (874) (874)
母公司永续债利息 (1,645) (1,975)
归属于母公司普通股股东的净利润 40,114 41,659
以上净资产收益率和每股收益按证监会于2010年1月11日修订之《规则第9号》计算公式计算。"""
    consolidated = "平安银行股份有限公司 合并利润表 2025年度 本集团 附注四 2025年度 2024年度 六、净利润 42,633 44,508"
    standalone = "平安银行股份有限公司 银行利润表 2025年度 本行 附注四 2025年度 2024年度 六、净利润 41,158 42,586"
    pages = {288: supplement, 120: consolidated, 122: standalone}
    monkeypatch.setattr(bank_proofs, "bank_page_text", lambda _body, page, **_kwargs: pages[page])
    original = AnnualOriginal(
        code="000001.SZ", period=date(2025, 12, 31), body=b"pdf", metadata={},
        pdf_hash="hash", published_at=datetime(2026, 4, 25, tzinfo=UTC),
    )
    proof: dict[str, object] = {
        "pdf_page": 288,
        "table_title": PINGAN_ROE_SUPPLEMENT,
        "header": PINGAN_ROE_ROW,
        "period_header": "2025年度",
        "current_column": "2025年度 加权平均",
        "raw_value": "9.15",
        "scope_bridge": {
            "consolidated_page": 120,
            "standalone_page": 122,
            "consolidated_excerpt": consolidated,
            "consolidated_sha256": hashlib.sha256(consolidated.encode()).hexdigest(),
            "standalone_excerpt": standalone,
            "standalone_sha256": hashlib.sha256(standalone.encode()).hexdigest(),
        },
    }
    assert _pingan_supplement_roe_scope(original, "roe_weighted_annual", proof, supplement)
    assert not _pingan_supplement_roe_scope(
        original, "roe_weighted_annual", {**proof, "header": "扣除非经常性损益后" + PINGAN_ROE_ROW}, supplement
    )
    assert not _pingan_supplement_roe_scope(
        original, "roe_weighted_annual", {**proof, "pdf_page": 17}, supplement
    )
    assert not _pingan_supplement_roe_scope(
        original, "roe_weighted_annual", {**proof, "raw_value": "2.07"}, supplement
    )
    assert not _pingan_supplement_roe_scope(
        original, "roe_weighted_annual", {**proof, "current_column": "2024年度 加权平均"}, supplement
    )
    broken_bridge = {**proof, "scope_bridge": {**proof["scope_bridge"], "standalone_page": 120}}
    assert not _pingan_supplement_roe_scope(original, "roe_weighted_annual", broken_bridge, supplement)


def test_ningbo_roe_matrix_requires_exact_current_row_and_group_rule_note() -> None:
    text = """二、净资产收益率和每股收益
2025年 加权平均净资产收益率(%) 每股收益(人民币元) 基本 稀释
归属于公司普通股股东的净利润 13.11 4.29 4.29
扣除非经常性损益后归属于公司普通股股东的净利润 13.16 4.30 4.30
2024年 加权平均净资产收益率(%) 每股收益(人民币元) 基本 稀释
归属于公司普通股股东的净利润 13.59 3.95 3.95
本集团对每股收益及净资产收益率的计算按照证监会的《公开发行证券的公司信息披露编报规则第9号－净资产收益率和每股收益的计算及披露》（2010年修订）的规定执行。"""
    original = AnnualOriginal(
        code="002142.SZ", period=date(2025, 12, 31), body=b"pdf", metadata={},
        pdf_hash="hash", published_at=datetime(2026, 4, 25, tzinfo=UTC),
    )
    proof: dict[str, object] = {
        "pdf_page": 236,
        "table_title": NINGBO_ROE_SUPPLEMENT,
        "period_header": "2025年 2024年",
        "current_column": "2025年",
        "header": NINGBO_ROE_ROW,
        "raw_value": "13.11",
    }
    assert _ningbo_supplement_roe_scope(original, "roe_weighted_annual", proof, text)
    assert not _ningbo_supplement_roe_scope(
        original, "roe_weighted_annual", {**proof, "raw_value": "4.29"}, text
    )
    assert not _ningbo_supplement_roe_scope(
        original, "roe_weighted_annual", {**proof, "current_column": "2024年"}, text
    )
    assert not _ningbo_supplement_roe_scope(
        original, "roe_weighted_annual", proof, text.replace("本集团对每股收益及净资产收益率", "本集团对每股收益")
    )


def _original(text: str, *, own_first: bool = False, report_default_scope: bool = False) -> AnnualOriginal:
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/STSong-Light"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/CIDSystemInfo"): DictionaryObject({
                NameObject("/Registry"): TextStringObject("Adobe"),
                NameObject("/Ordering"): TextStringObject("GB1"),
                NameObject("/Supplement"): NumberObject(0),
            }),
        })]),
    })
    cmap = DecodedStreamObject()
    cmap.set_data(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
        b"/CMapName /Adobe-Identity-UCS def /CMapType 2 def "
        b"1 begincodespacerange <0000> <FFFF> endcodespacerange "
        b"1 beginbfrange <0000> <FFFF> <0000> endbfrange "
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    if "合并资产负债表" in text:
        row = text.splitlines()[-1]
        unit_line = "单位：百万元" if "百万元" in text else "单位：千元"
        lines = [
            "600919.SH 江苏银行 2025年年度报告",
            "合并资产负债表",
            unit_line,
            "本行                                      本集团" if own_first else "本集团                                      本行",
            "附注五 2025年12月31日 2024年12月31日 2025年12月31日 2024年12月31日",
            row,
        ]
    else:
        lines = ["600919.SH 江苏银行 2025年年度报告"]
        if report_default_scope:
            lines.append("除特别注明外，为本集团数据")
        lines.extend([
            "主要财务指标",
            "集团口径",
            "2025年度 2024年度",
            text.splitlines()[-1],
        ])
    ops = "BT /F1 10 Tf 30 770 Td "
    for line in lines:
        ops += "<" + line.encode("utf-16-be").hex() + "> Tj 0 -20 Td "
    stream.set_data((ops + "ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    body = output.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    title = "2025年年度报告"
    url = "https://static.cninfo.com.cn/finalpage/2026-04-25/test.pdf"
    return AnnualOriginal(
        code="600919.SH", period=date(2025, 12, 31), body=body,
        metadata={"chosen": {"announcementId": "notice-1", "announcementTitle": title, "secName": "江苏银行"}, "url": url},
        pdf_hash=digest, published_at=datetime(2026, 4, 25, tzinfo=UTC),
    )


def _proof(metric: str, text: str, **overrides: object) -> tuple[AnnualOriginal, dict[str, object]]:
    own_first = bool(overrides.pop("_own_first", False))
    original = _original(
        text,
        own_first=own_first,
        report_default_scope=isinstance(overrides.get("scope_context"), dict),
    )
    if metric == "equity_parent":
        fields = {
            "table_title": "合并资产负债表",
            "period_header": "2025年12月31日",
            "current_column": "本集团 2025年12月31日",
            "header": "归属于母公司股东权益合计",
            "raw_value": "337,961,238",
            "raw_unit": "CNY_thousand",
            "parsed_value": "337961238000",
            "accounting_scope": "group_consolidated",
        }
    else:
        fields = {
            "table_title": "主要财务指标",
            "period_header": "2025年度",
            "current_column": "集团口径 2025年度",
            "header": "加权平均净资产收益率",
            "raw_value": "9.45",
            "raw_unit": "percent",
            "parsed_value": "0.0945",
            "accounting_scope": "group_consolidated",
        }
    fields.update(overrides)
    audit = {
        "code": original.code,
        "period": "2025-12-31",
        "disclosure_id": "notice-1",
        "title": "2025年年度报告",
        "url": "https://static.cninfo.com.cn/finalpage/2026-04-25/test.pdf",
        "published_at": "2026-04-25T00:00:00+00:00",
        "pdf_sha256": original.pdf_hash,
        "metric": metric,
        "check_rule_version": BANK_RULE,
        "pdf_page": 1,
        **fields,
        "locator_excerpt": text,
        "locator_excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "raw_label": fields["header"],
        "definition_or_footnote": "以年报合并集团口径披露；无额外脚注",
        "status": "verified",
    }
    proof: dict[str, object] = {
        "check_rule_version": BANK_RULE,
        "pdf_hash": original.pdf_hash,
        "report_identity": {"code": original.code, "period": "2025-12-31", "disclosure_id": "notice-1"},
        "period": "2025-12-31",
        "pdf_page": 1,
        **fields,
        "locator_excerpt": text,
        "locator_excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "checked_at": "2026-04-26T00:00:00+00:00",
        "audit_record": audit,
    }
    return original, proof


@pytest.mark.parametrize(
    ("metric", "text", "expected_scope"),
    [
        (
            "equity_parent",
        "合并资产负债表 单位：千元 本集团 本行 附注五 2025年12月31日 2024年12月31日 2025年12月31日 2024年12月31日\n归属于母公司股东权益合计 337,961,238 300,000,000",
            "group_consolidated",
        ),
        (
            "roe_weighted_annual",
            "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 9.45% 10.20%",
            "group_consolidated",
        ),
    ],
)
def test_valid_bank_locator_binds_page_value_scope_and_unit(metric: str, text: str, expected_scope: str) -> None:
    original, proof = _proof(metric, text)
    checks = checked_bank_locator(original, metric, proof)
    assert checks["accounting_scope"] == expected_scope
    assert checks["check_rule_version"] == BANK_RULE


def test_manifest_status_true_cannot_replace_a_real_page_excerpt() -> None:
    original, proof = _proof(
        "roe_weighted_annual",
        "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 9.45% 10.20%",
    )
    proof["locator_excerpt"] = "随便编的 9.45%"
    with pytest.raises(ValueError, match="excerpt hash mismatch|absent"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_synchronized_audit_and_proof_value_edit_still_fails_against_pdf() -> None:
    original, proof = _proof(
        "roe_weighted_annual",
        "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 9.45% 10.20%",
        raw_value="8.88",
        parsed_value="0.0888",
    )
    proof["audit_record"]["raw_value"] = "8.88"  # type: ignore[index]
    with pytest.raises(ValueError, match="source value"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_previous_year_column_is_rejected() -> None:
    text = "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 9.45% 10.20%"
    original, proof = _proof("roe_weighted_annual", text, current_column="2024年度")
    proof["audit_record"]["current_column"] = "2024年度"  # type: ignore[index]
    with pytest.raises(ValueError, match="previous year|target annual period"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_ratio_own_first_entity_columns_cannot_be_overridden_by_group_default() -> None:
    scope_excerpt = "除特别注明外，为本集团数据"
    scope = {
        "page": 1,
        "kind": "report_default",
        "excerpt": scope_excerpt,
        "sha256": hashlib.sha256(scope_excerpt.encode()).hexdigest(),
    }
    row = (
        "主要财务指标 本行 2025年度 2024年度 本集团 2025年度 2024年度 "
        "加权平均净资产收益率 9.45% 10.20% 8.88% 8.80%"
    )
    original, proof = _proof(
        "roe_weighted_annual",
        row,
        current_column="本集团 2025年度",
        scope_context=scope,
    )
    with pytest.raises(ValueError, match="本行 before|current-column"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_equity_unit_cannot_be_borrowed_from_unrelated_text_after_the_row() -> None:
    text = (
        "合并资产负债表\n单位：百万元\n本集团 本行\n"
        "附注五 2025年12月31日 2024年12月31日 2025年12月31日 2024年12月31日\n"
        "归属于母公司股东权益合计 337,961,238 300,000,000 附注：其他资料单位千元"
    )
    original, proof = _proof(
        "equity_parent",
        text,
        raw_unit="CNY_thousand",
        parsed_value="337961238000",
    )
    with pytest.raises(ValueError, match="printed unit"):
        checked_bank_locator(original, "equity_parent", proof)


def test_parent_equity_header_cannot_hide_common_stock_qualification() -> None:
    text = (
        "合并资产负债表 单位：百万元 本集团 本行 附注五 "
        "2025年12月31日 2024年12月31日 2025年12月31日 2024年12月31日\n"
        "归属于母公司股东的权益（普通股） 337,961,238 300,000,000"
    )
    original, proof = _proof(
        "equity_parent",
        text,
        header="归属于母公司股东的权益",
        raw_value="337961238",
        raw_unit="CNY_million",
        parsed_value="337961238000000",
    )
    with pytest.raises(ValueError, match="source value"):
        checked_bank_locator(original, "equity_parent", proof)


def test_common_stock_equity_is_not_parent_total_equity() -> None:
    text = "合并资产负债表 单位：百万元 本集团 本行 附注五 2025年12月31日 2024年12月31日 2025年12月31日 2024年12月31日\n普通股股东权益 3859602"
    original, proof = _proof(
        "equity_parent", text, header="普通股股东权益", raw_value="3859602",
        raw_unit="CNY_million", parsed_value="3859602000000",
    )
    proof["audit_record"]["header"] = "普通股股东权益"  # type: ignore[index]
    proof["audit_record"]["raw_label"] = "普通股股东权益"  # type: ignore[index]
    proof["audit_record"]["raw_value"] = "3859602"  # type: ignore[index]
    proof["audit_record"]["raw_unit"] = "CNY_million"  # type: ignore[index]
    proof["audit_record"]["parsed_value"] = "3859602000000"  # type: ignore[index]
    with pytest.raises(ValueError, match="total equity attributable"):
        checked_bank_locator(original, "equity_parent", proof)


def test_ratio_must_be_converted_from_percent_once() -> None:
    text = "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 9.45% 10.20%"
    original, proof = _proof("roe_weighted_annual", text, parsed_value="0.000945")
    proof["audit_record"]["parsed_value"] = "0.000945"  # type: ignore[index]
    with pytest.raises(ValueError, match="unit conversion"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_equity_unit_cannot_be_rescaled_by_editing_proof_and_audit_together() -> None:
    text = (
        "合并资产负债表 单位：千元 本集团 本行 附注五 "
        "2025年12月31日 2024年12月31日 2025年12月31日 2024年12月31日\n"
        "归属于母公司股东权益合计 337,961,238 303,589,817"
    )
    original, proof = _proof("equity_parent", text, raw_unit="CNY_million", parsed_value="337961238000000")
    proof["audit_record"]["raw_unit"] = "CNY_million"  # type: ignore[index]
    proof["audit_record"]["parsed_value"] = "337961238000000"  # type: ignore[index]
    with pytest.raises(ValueError, match="printed unit"):
        checked_bank_locator(original, "equity_parent", proof)


def test_explicit_ratio_footnote_marker_is_skipped_before_current_cell() -> None:
    text = "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 3 9.45% 10.20%"
    original, proof = _proof("roe_weighted_annual", text, footnote_marker="3")
    assert checked_bank_locator(original, "roe_weighted_annual", proof)["check_rule_version"] == BANK_RULE


def test_declared_footnote_cannot_hide_a_previous_year_value() -> None:
    text = "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 3 9.45% 10.20%"
    original, proof = _proof(
        "roe_weighted_annual", text, raw_value="10.20", parsed_value="0.1020", footnote_marker="3"
    )
    proof["audit_record"]["raw_value"] = "10.20"  # type: ignore[index]
    proof["audit_record"]["parsed_value"] = "0.1020"  # type: ignore[index]
    with pytest.raises(ValueError, match="current-column|consolidated current"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_bank_column_before_group_cannot_claim_the_group_current_value() -> None:
    text = (
        "合并资产负债表 单位：千元 本行 本集团 附注五 2025年12月31日 "
        "2024年12月31日 2025年12月31日 2024年12月31日\n"
        "归属于母公司股东权益合计 326,222,007 294,472,926 337,961,238 303,589,817"
    )
    original, proof = _proof("equity_parent", text, _own_first=True)
    proof["raw_value"] = "326,222,007"
    proof["parsed_value"] = "326222007000"
    proof["audit_record"]["raw_value"] = "326,222,007"  # type: ignore[index]
    proof["audit_record"]["parsed_value"] = "326222007000"  # type: ignore[index]
    with pytest.raises(ValueError, match="group before|current-column"):
        checked_bank_locator(original, "equity_parent", proof)


def test_wrong_page_and_pdf_hash_are_rejected() -> None:
    text = "主要财务指标 集团口径 2025年度 2024年度 加权平均净资产收益率 9.45% 10.20%"
    original, proof = _proof("roe_weighted_annual", text, pdf_page=2)
    proof["audit_record"]["pdf_page"] = 2  # type: ignore[index]
    with pytest.raises(ValueError, match="outside|page"):
        checked_bank_locator(original, "roe_weighted_annual", proof)
    original, proof = _proof("roe_weighted_annual", text)
    proof["pdf_hash"] = "0" * 64
    with pytest.raises(ValueError, match="PDF hash"):
        checked_bank_locator(original, "roe_weighted_annual", proof)


def test_real_jiangsu_parent_equity_locator_checks_the_original_pdf() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "tests/fixtures/bank_proofs_v2/audit-manifest.json").read_text())
    audit = next(
        row for row in manifest["records"]
        if row["code"] == "600919.SH" and row["metric"] == "equity_parent"
    )
    body = (root / audit["pdf_path"]).read_bytes()
    metadata = json.loads(
        (root / "research/industry-data-validation/evidence/run-02/derived/filings/600919.SH.json").read_text()
    )
    original = AnnualOriginal(
        code="600919.SH",
        period=date(2025, 12, 31),
        body=body,
        metadata=metadata,
        pdf_hash=hashlib.sha256(body).hexdigest(),
        published_at=datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    proof = {
        "pdf_hash": audit["pdf_sha256"],
        "report_identity": {
            "code": audit["code"],
            "period": audit["period"],
            "disclosure_id": audit["disclosure_id"],
        },
        "period": audit["period"],
        "check_rule_version": BANK_RULE,
        "audit_record": audit,
        **{
            key: audit[key]
            for key in (
                "pdf_page", "table_title", "period_header", "current_column", "header",
                "raw_value", "raw_unit", "parsed_value", "accounting_scope",
                "locator_excerpt", "locator_excerpt_sha256",
            )
        },
        "checked_at": audit["acquired_at"],
    }
    assert checked_bank_locator(original, "equity_parent", proof)["check_rule_version"] == BANK_RULE


def test_real_icbc_equity_year_only_columns_bind_group_before_bank() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "tests/fixtures/bank_proofs_v2/audit-manifest.json").read_text())
    audit = dict(next(
        row for row in manifest["records"]
        if row["code"] == "601398.SH" and row["metric"] == "equity_parent"
    ))
    # Local checker accepts only verified records; this flag is not the evidence
    # itself and is set here to exercise the actual table semantics in isolation.
    audit["status"] = "verified"
    pdf_path = root / audit["pdf_path"]
    if not pdf_path.exists() or not audit.get("pdf_page"):
        pytest.skip("downloaded ICBC equity proof is unavailable in this checkout")
    body = pdf_path.read_bytes()
    metadata = json.loads(
        (root / "research/industry-data-validation/evidence/run-02/derived/filings/601398.SH.json").read_text()
    )
    original = AnnualOriginal(
        "601398.SH", date(2025, 12, 31), body, metadata, hashlib.sha256(body).hexdigest(),
        datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    proof = {
        "pdf_hash": audit["pdf_sha256"],
        "report_identity": {
            "code": audit["code"], "period": audit["period"],
            "disclosure_id": audit["disclosure_id"],
        },
        "period": audit["period"],
        "check_rule_version": BANK_RULE,
        "audit_record": audit,
        **{
            key: audit[key]
            for key in (
                "pdf_page", "table_title", "period_header", "current_column", "header",
                "raw_value", "raw_unit", "parsed_value", "accounting_scope",
                "locator_excerpt", "locator_excerpt_sha256",
            )
        },
        "checked_at": audit["acquired_at"],
    }
    assert checked_bank_locator(original, "equity_parent", proof)["check_rule_version"] == BANK_RULE


def test_real_cmb_parent_equity_continuation_requires_adjacent_statement_context() -> None:
    root = Path(__file__).resolve().parents[2]
    pdf_path = root / "research/industry-data-validation/evidence/run-02/raw/official/600036.SH_2025.pdf"
    if not pdf_path.exists():
        pytest.skip("downloaded CMB official annual PDF is unavailable in this checkout")
    body = pdf_path.read_bytes()
    metadata = json.loads(
        (root / "research/industry-data-validation/evidence/run-02/derived/filings/600036.SH.json").read_text()
    )
    digest = hashlib.sha256(body).hexdigest()
    original = AnnualOriginal(
        code="600036.SH",
        period=date(2025, 12, 31),
        body=body,
        metadata=metadata,
        pdf_hash=digest,
        published_at=datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    excerpt = bank_page_text(body, 128)
    context = bank_page_text(body, 127)
    fields = {
        "pdf_page": 128,
        "table_title": "合并资产负债表",
        "period_header": "2025年12月31日",
        "current_column": "合并口径 2025年12月31日",
        "header": "归属于本行股东权益合计",
        "raw_value": "1,272,875",
        "raw_unit": "CNY_million",
        "parsed_value": "1272875000000",
        "accounting_scope": "group_consolidated",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "context_page": 127,
        "context_excerpt": context,
        "context_excerpt_sha256": hashlib.sha256(context.encode()).hexdigest(),
    }
    proof = {
        "check_rule_version": BANK_RULE,
        "pdf_hash": digest,
        "report_identity": {
            "code": "600036.SH",
            "period": "2025-12-31",
            "disclosure_id": metadata["chosen"]["announcementId"],
        },
        "period": "2025-12-31",
        "checked_at": "2026-03-29T00:00:00+00:00",
        **fields,
    }
    proof["audit_record"] = {
        "code": "600036.SH",
        "period": "2025-12-31",
        "disclosure_id": metadata["chosen"]["announcementId"],
        "title": metadata["chosen"]["announcementTitle"],
        "url": metadata["url"],
        "published_at": original.published_at.isoformat(),
        "pdf_sha256": digest,
        "metric": "equity_parent",
        "check_rule_version": BANK_RULE,
        **fields,
        "raw_label": fields["header"],
        "definition_or_footnote": "合并资产负债表中归属于本行股东权益合计",
        "status": "verified",
    }
    assert checked_bank_locator(original, "equity_parent", proof)["check_rule_version"] == BANK_RULE

    # Self-consistent proof/audit edits cannot use a nonadjacent or fabricated
    # context page to make the CMB-specific row pass as consolidated equity.
    proof["context_page"] = 126
    proof["audit_record"]["context_page"] = 126  # type: ignore[index]
    with pytest.raises(ValueError, match="immediately preceding"):
        checked_bank_locator(original, "equity_parent", proof)


def test_jiangsu_npl_scope_uses_actual_consolidated_subsidiary_footnote() -> None:
    root = Path(__file__).resolve().parents[2]
    pdf_path = root / "research/industry-data-validation/evidence/run-02/raw/official/600919.SH_2025.pdf"
    if not pdf_path.exists():
        pytest.skip("downloaded Jiangsu Bank official annual PDF is unavailable in this checkout")
    body = pdf_path.read_bytes()
    metadata = json.loads(
        (root / "research/industry-data-validation/evidence/run-02/derived/filings/600919.SH.json").read_text()
    )
    digest = hashlib.sha256(body).hexdigest()
    excerpt = bank_page_text(body, 19)
    original = AnnualOriginal(
        "600919.SH", date(2025, 12, 31), body, metadata, digest,
        datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    fields = {
        "pdf_page": 19,
        "table_title": "主要指标（%）",
        "period_header": "2025 年末",
        "current_column": "合并口径 2025年末",
        "header": "不良贷款率",
        "raw_value": "0.84",
        "raw_unit": "percent",
        "parsed_value": "0.0084",
        "accounting_scope": "group_consolidated",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
    }
    proof = {
        "check_rule_version": BANK_RULE,
        "pdf_hash": digest,
        "report_identity": {
            "code": "600919.SH", "period": "2025-12-31",
            "disclosure_id": metadata["chosen"]["announcementId"],
        },
        "period": "2025-12-31",
        "checked_at": "2026-04-30T00:00:00+00:00",
        **fields,
    }
    proof["audit_record"] = {
        "code": "600919.SH",
        "period": "2025-12-31",
        "disclosure_id": metadata["chosen"]["announcementId"],
        "title": metadata["chosen"]["announcementTitle"],
        "url": metadata["url"],
        "published_at": original.published_at.isoformat(),
        "pdf_sha256": digest,
        "metric": "npl_ratio",
        "check_rule_version": BANK_RULE,
        **fields,
        "raw_label": fields["header"],
        "definition_or_footnote": "本行发放贷款和垫款合并金融租赁子公司长期应收款，不良贷款率、拨备覆盖率等涉及指标计算口径同步调整。",
        "status": "verified",
    }
    assert checked_bank_locator(original, "npl_ratio", proof)["check_rule_version"] == BANK_RULE

    proof["current_column"] = "本行 2025年末"
    proof["audit_record"]["current_column"] = "本行 2025年末"  # type: ignore[index]
    with pytest.raises(ValueError, match="consolidated group"):
        checked_bank_locator(original, "npl_ratio", proof)


def test_jiangsu_nim_annual_single_year_table_requires_group_narrative() -> None:
    root = Path(__file__).resolve().parents[2]
    pdf_path = root / "research/industry-data-validation/evidence/run-02/raw/official/600919.SH_2025.pdf"
    if not pdf_path.exists():
        pytest.skip("downloaded Jiangsu Bank official annual PDF is unavailable in this checkout")
    body = pdf_path.read_bytes()
    metadata = json.loads(
        (root / "research/industry-data-validation/evidence/run-02/derived/filings/600919.SH.json").read_text()
    )
    digest = hashlib.sha256(body).hexdigest()
    excerpt = bank_page_text(body, 23)
    original = AnnualOriginal(
        "600919.SH", date(2025, 12, 31), body, metadata, digest,
        datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    fields = {
        "pdf_page": 23,
        "table_title": "十、生息资产平均收益率和计息负债平均付息率",
        "period_header": "2025 年",
        "current_column": "集团口径 2025年",
        "header": "净息差",
        "raw_value": "1.73",
        "raw_unit": "percent",
        "parsed_value": "0.0173",
        "accounting_scope": "group_consolidated",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "footnote_marker": "4",
    }
    proof = {
        "check_rule_version": BANK_RULE,
        "pdf_hash": digest,
        "report_identity": {
            "code": "600919.SH", "period": "2025-12-31",
            "disclosure_id": metadata["chosen"]["announcementId"],
        },
        "period": "2025-12-31",
        "checked_at": "2026-04-30T00:00:00+00:00",
        **fields,
    }
    proof["audit_record"] = {
        "code": "600919.SH",
        "period": "2025-12-31",
        "disclosure_id": metadata["chosen"]["announcementId"],
        "title": metadata["chosen"]["announcementTitle"],
        "url": metadata["url"],
        "published_at": original.published_at.isoformat(),
        "pdf_sha256": digest,
        "metric": "net_interest_margin",
        "check_rule_version": BANK_RULE,
        **fields,
        "raw_label": fields["header"],
        "definition_or_footnote": "报告期内，集团净息差1.73%；附注4说明还原口径计算方法。",
        "status": "verified",
    }
    assert checked_bank_locator(original, "net_interest_margin", proof)["check_rule_version"] == BANK_RULE

    proof["raw_value"] = "1.71"
    proof["parsed_value"] = "0.0171"
    proof["audit_record"]["raw_value"] = "1.71"  # type: ignore[index]
    proof["audit_record"]["parsed_value"] = "0.0171"  # type: ignore[index]
    with pytest.raises(ValueError, match="current year|comparative years|source value|current-column"):
        checked_bank_locator(original, "net_interest_margin", proof)


def test_ping_an_total_equity_needs_report_specific_zero_nci_derivation() -> None:
    root = Path(__file__).resolve().parents[2]
    source_manifest = json.loads(
        (root / "research/industry-data-validation/evidence/bank-proof-2026-10-15/source-manifest.json").read_text()
    )
    pdf_path = root / source_manifest["sources"]["000001.SZ"]["pdf_path"]
    if not pdf_path.exists():
        pytest.skip("downloaded Ping An official annual PDF is unavailable in this checkout")
    body = pdf_path.read_bytes()
    metadata = source_manifest["sources"]["000001.SZ"]["metadata"]
    digest = hashlib.sha256(body).hexdigest()
    excerpt = bank_page_text(body, 117)
    original = AnnualOriginal(
        "000001.SZ", date(2025, 12, 31), body, metadata, digest,
        datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    derivation = {
        "method": "wholly_owned_group_total_as_parent",
        "nci_value": "0",
    }
    for key in ("scope", "ownership", "nci_policy"):
        page = {"scope": 5, "ownership": 173, "nci_policy": 136}[key]
        page_excerpt = bank_page_text(body, page)
        derivation[key] = {
            "page": page,
            "excerpt": page_excerpt,
            "excerpt_sha256": hashlib.sha256(page_excerpt.encode()).hexdigest(),
        }
    fields = {
        "pdf_page": 117,
        "table_title": "合并资产负债表(续)",
        "period_header": "2025年12月31日",
        "current_column": "本集团 2025年12月31日",
        "header": "股东权益合计",
        "raw_value": "551,184",
        "raw_unit": "CNY_million",
        "parsed_value": "551184000000",
        "accounting_scope": "group_consolidated",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "equity_derivation": derivation,
    }
    proof = {
        "check_rule_version": BANK_RULE,
        "pdf_hash": digest,
        "report_identity": {
            "code": "000001.SZ", "period": "2025-12-31",
            "disclosure_id": metadata["chosen"]["announcementId"],
        },
        "period": "2025-12-31",
        "checked_at": "2026-04-01T00:00:00+00:00",
        "derived_parent_total": True,
        **fields,
    }
    proof["audit_record"] = {
        "code": "000001.SZ",
        "period": "2025-12-31",
        "disclosure_id": metadata["chosen"]["announcementId"],
        "title": metadata["chosen"]["announcementTitle"],
        "url": metadata["url"],
        "published_at": original.published_at.isoformat(),
        "pdf_sha256": digest,
        "metric": "equity_parent",
        "check_rule_version": BANK_RULE,
        **fields,
        "raw_label": fields["header"],
        "definition_or_footnote": "全资集团范围及少数股东权益单独列示政策支持总权益等于归母权益",
        "derived_parent_total": True,
        "status": "verified",
    }
    assert checked_bank_locator(original, "equity_parent", proof)["check_rule_version"] == BANK_RULE

    proof["equity_derivation"]["scope"]["excerpt"] = "本集团 指 平安银行"
    proof["audit_record"]["equity_derivation"]["scope"]["excerpt"] = "本集团 指 平安银行"  # type: ignore[index]
    proof["equity_derivation"]["scope"]["excerpt_sha256"] = hashlib.sha256(
        "本集团 指 平安银行".encode()
    ).hexdigest()
    proof["audit_record"]["equity_derivation"]["scope"]["excerpt_sha256"] = proof["equity_derivation"]["scope"]["excerpt_sha256"]  # type: ignore[index]
    with pytest.raises(ValueError, match="complete group as wholly owned"):
        checked_bank_locator(original, "equity_parent", proof)


def test_ping_an_standalone_regulatory_scope_exclusion_is_bound_to_both_pages() -> None:
    root = Path(__file__).resolve().parents[2]
    source_path = root / "research/industry-data-validation/evidence/bank-proof-2026-10-15/originals/000001.SZ_2025.pdf"
    if not source_path.exists():
        pytest.skip("downloaded Ping An official annual PDF is unavailable in this checkout")
    body = source_path.read_bytes()
    metadata = json.loads(
        (root / "research/industry-data-validation/evidence/bank-proof-2026-10-15/source-manifest.json").read_text()
    )["sources"]["000001.SZ"]["metadata"]
    digest = hashlib.sha256(body).hexdigest()
    original = AnnualOriginal(
        code="000001.SZ",
        period=date(2025, 12, 31),
        body=body,
        metadata=metadata,
        pdf_hash=digest,
        published_at=datetime.fromtimestamp(metadata["chosen"]["announcementTime"] / 1000, UTC),
    )
    excerpt = bank_page_text(body, 20)
    scope_excerpt = "除资本充足率指标为本集团口径外，其他指标均为本行口径。"
    record: dict[str, object] = {
        "code": "000001.SZ",
        "period": "2025-12-31",
        "pdf_hash": digest,
        "pdf_page": 20,
        "table_title": "2.4.1 主要监管指标",
        "period_header": "2025年12月31日",
        "current_column": "2025年12月31日",
        "header": "不良贷款率",
        "raw_value": "1.05",
        "raw_unit": "percent",
        "threshold": "≤5",
        "accounting_scope": "bank_standalone",
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "scope_context": {
            "page": 21,
            "kind": "scope_exclusion",
            "excerpt": scope_excerpt,
            "sha256": hashlib.sha256(scope_excerpt.encode()).hexdigest(),
        },
        "status": "scope_mismatch",
    }
    validate_bank_scope_exclusion(original, "npl_ratio", record)
    coverage_record = dict(
        record,
        header="拨备覆盖率",
        raw_value="220.88",
        threshold="≥130（注3）",
    )
    validate_bank_scope_exclusion(original, "provision_coverage", coverage_record)

    wrong_threshold = dict(record, threshold="≥5")
    with pytest.raises(ValueError, match="threshold"):
        validate_bank_scope_exclusion(original, "npl_ratio", wrong_threshold)
    wrong_scope = dict(record)
    wrong_scope["scope_context"] = {
        **record["scope_context"],  # type: ignore[arg-type]
        "excerpt": "资本充足率指标为本集团口径",
        "sha256": hashlib.sha256("资本充足率指标为本集团口径".encode()).hexdigest(),
    }
    with pytest.raises(ValueError, match="standalone"):
        validate_bank_scope_exclusion(original, "npl_ratio", wrong_scope)
