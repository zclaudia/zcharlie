"""Generate tiny real PDFs for bank annual v2 locator tests."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def bank_annual_pdf(year: int, current: str, comparative: str) -> bytes:
    lines = [
        f"中国工商银行股份有限公司 601398 {year}年度报告",
        "财务指标",
        f"{year} {year - 1}",
        f"本集团 {year}年度",
        f"加权平均净资产收益率（1） {current} {comparative} %",
    ]
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
    cmap = DecodedStreamObject()
    codepoints = sorted({ord(character) for line in lines for character in line})
    mappings = " ".join(f"<{code:04x}> <{code:04x}>" for code in codepoints)
    cmap.set_data(
        (
            "1 begincodespacerange <0000> <ffff> endcodespacerange\n"
            f"{len(codepoints)} beginbfchar {mappings} endbfchar"
        ).encode()
    )
    font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = DecodedStreamObject()
    content.set_data(
        (
            "BT /F1 10 Tf 30 770 Td "
            + " ".join(f"<{line.encode('utf-16-be').hex()}> Tj 0 -20 Td" for line in lines)
            + " ET"
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
