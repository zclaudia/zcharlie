"""Small real PDF fixtures for proof/permission tests, never production evidence."""

import hashlib
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Any

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from investment_agent.industry_peers.originals import (
    ALIASES,
    AnnualOriginal,
    checked_locator,
    store_annual_original,
)


async def synthetic_original_proofs(
    session: Any, code: str, listing_id: int, fields: dict[str, Any], acquired_at: datetime
) -> dict[str, dict[str, Any]]:
    year = next(iter(fields.values())).period.end.year
    lines = [
        f"测试公司 公司代码：{code[:6]} {year}年年度报告",
        f"主要会计数据 单位：元 币种：人民币 {year}年 {year - 1}年",
    ]
    excerpts = {}
    for metric, field in fields.items():
        if metric not in ALIASES or field.value is None:
            continue
        if metric == "contract_liabilities":
            lines.append(
                f"合并资产负债表 单位：元 币种：人民币 {year}年12月31日 {year - 1}年12月31日"
            )
        elif metric == "operating_cash_flow":
            lines.append(f"合并现金流量表 单位：元 币种：人民币 {year}年度 {year - 1}年度")
        else:
            lines.append(f"主要财务指标 单位：元 币种：人民币 {year}年 {year - 1}年")
        raw = Decimal(field.value) * (100 if metric == "roe_weighted_annual" else 1)
        printed = f"{raw:.8f}"
        excerpt = f"{ALIASES[metric][0]} {printed} 90.00"
        lines.append(excerpt)
        excerpts[metric] = (excerpt, printed)
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/DescendantFonts"): ArrayObject(
                [
                    DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/CIDFontType0"),
                            NameObject("/BaseFont"): NameObject("/STSong-Light"),
                            NameObject("/CIDSystemInfo"): DictionaryObject(
                                {
                                    NameObject("/Registry"): TextStringObject("Adobe"),
                                    NameObject("/Ordering"): TextStringObject("GB1"),
                                    NameObject("/Supplement"): NumberObject(0),
                                }
                            ),
                        }
                    )
                ]
            ),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        (
            "BT /F1 10 Tf 30 770 Td "
            + " ".join("<" + line.encode("utf-16-be").hex() + "> Tj 0 -20 Td" for line in lines)
            + " ET"
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(600, 800)
    output = BytesIO()
    writer.write(output)
    body = output.getvalue()
    checksum = hashlib.sha256(body).hexdigest()
    published = acquired_at.replace(month=4, day=25)
    period = next(iter(fields.values())).period.end
    original = AnnualOriginal(
        code,
        period,
        body,
        {
            "chosen": {
                "announcementId": code[:6],
                "orgId": "test-fixture",
                "announcementTitle": "测试年报",
            },
            "url": "https://static.cninfo.com.cn/finalpage/2026-04-25/test.PDF",
        },
        checksum,
        published,
    )
    row = await store_annual_original(session, original, listing_id, acquired_at)
    proofs = {}
    for metric, (excerpt, raw) in excerpts.items():
        proof = {
            "metric": metric,
            "status": "verified",
            "original_ref": {"kind": "disclosure_original", "id": row.id, "revision": 1},
            "pdf_hash": checksum,
            "report_identity": {
                "code": code,
                "period": period.isoformat(),
                "disclosure_id": code[:6],
            },
            "pdf_page": 1,
            "header": ALIASES[metric][0],
            "raw_value": raw,
            "raw_unit": "percent" if metric == "roe_weighted_annual" else "CNY",
            "parsed_value": raw,
            "period": period.isoformat(),
            "locator_excerpt": excerpt,
            "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "checked_at": acquired_at.isoformat(),
            "checked_by": "test-fixture",
            "audit_record": {"source": "test-fixture"},
        }
        proof.update(checked_locator(original, metric, proof))
        proofs[metric] = proof
    return proofs
