"""Synthetic font fixtures exercise decoding, never stand in for bank facts."""

import hashlib
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from investment_agent.industry_peers.bank_pdf_text import bank_page_text


def pdf(*, ordering="GB1", existing_mapping=False, form=False):
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/SyntheticCIDFont"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([DictionaryObject({
            NameObject("/Subtype"): NameObject("/CIDFontType0"),
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/BaseFont"): NameObject("/SyntheticCIDFont"),
            NameObject("/CIDSystemInfo"): DictionaryObject({
                NameObject("/Registry"): TextStringObject("Adobe"),
                NameObject("/Ordering"): TextStringObject(ordering),
                NameObject("/Supplement"): NumberObject(4),
            }),
        })]),
    })
    if existing_mapping:
        cmap = DecodedStreamObject()
        cmap.set_data(b"1 begincodespacerange <0000> <ffff> endcodespacerange "
                      b"4 beginbfchar <1171> <0041> <0d05> <0042> <107f> <0043> <0f9b> <0044> endbfchar")
        font[NameObject("/ToUnicode")] = cmap
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 30 750 Td <11710d05107f0f9b> Tj ET")
    if form:
        content[NameObject("/Type")] = NameObject("/XObject")
        content[NameObject("/Subtype")] = NameObject("/Form")
        content[NameObject("/BBox")] = ArrayObject([NumberObject(n) for n in (0, 0, 600, 800)])
        content[NameObject("/Resources")] = resources
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({
            NameObject("/Header"): writer._add_object(content),
        })})
        invocation = DecodedStreamObject()
        invocation.set_data(b"/Header Do")
        page[NameObject("/Contents")] = writer._add_object(invocation)
    else:
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("form", [False, True])
def test_declared_adobe_gb1_cids_decode_without_changing_source_bytes(form):
    body = pdf(form=form)
    digest = hashlib.sha256(body).hexdigest()
    assert "招商银行" not in PdfReader(BytesIO(body)).pages[0].extract_text()
    assert "招商银行" in bank_page_text(body, 1)
    assert hashlib.sha256(body).hexdigest() == digest
    assert "招商银行" not in PdfReader(BytesIO(body)).pages[0].extract_text()


def test_does_not_invent_mapping_for_other_character_collections():
    body = pdf(ordering="Japan1")
    assert bank_page_text(body, 1) == PdfReader(BytesIO(body)).pages[0].extract_text()


def test_pdf_own_unicode_mapping_takes_precedence():
    body = pdf(existing_mapping=True)
    assert "ABCD" in bank_page_text(body, 1)
    assert "招商银行" not in bank_page_text(body, 1)


def test_layout_decodes_the_same_glyphs_and_rejects_invalid_page():
    body = pdf()
    assert "招商银行" in bank_page_text(body, 1, layout=True)
    with pytest.raises(ValueError, match="outside"):
        bank_page_text(body, 2)
    with pytest.raises(ValueError, match="outside"):
        bank_page_text(body, True)
