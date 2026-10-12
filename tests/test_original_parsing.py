"""Actual immutable HTML/PDF parsing and exact verification, offline only."""

import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.disclosure_http import DisclosureError
from investment_agent.evidence.disclosures import DisclosureService
from investment_agent.evidence.documents import (
    document_limits,
    numeric_rows,
    parse_staged,
    readback,
    save_candidates,
    stage,
)
from investment_agent.evidence.original_parser import MAX_INPUT, parse_original
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.storage.models.evidence import SavedEvidence
from tests.support.disclosure import NOW, PUBLISHED, FixtureHTTP, discovery
from tests.support.originals import html, original, pdf, snapshot


def chinese_pdf():
    """Real text PDF with explicit Unicode CMap; extraction is never mocked."""
    from pypdf.generic import ArrayObject

    lines = (
        "证券代码：000001",
        "净利润：人民币12,345.6789万元（合并口径，2025年1月1日至2025年12月31日）",
    )
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    cmap = DecodedStreamObject()
    chars = sorted(set("".join(lines)))
    cmap.set_data(
        (
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /TestUnicode def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange "
            + str(len(chars))
            + " beginbfchar\n"
            + "\n".join(f"<{ord(c):04X}> <{ord(c):04X}>" for c in chars)
            + "\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
        ).encode()
    )
    descendant = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/CIDSystemInfo"): DictionaryObject(),
        }
    )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/Encoding"): NameObject("/UniGB-UCS2-H"),
            NameObject("/DescendantFonts"): ArrayObject([writer._add_object(descendant)]),
            NameObject("/ToUnicode"): writer._add_object(cmap),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        (
            "BT /F1 7 Tf 30 750 Td\n"
            + "\n0 -20 Td\n".join("<" + line.encode("utf-16-be").hex() + "> Tj" for line in lines)
            + "\nET"
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_actual_subprocess_html_simple_table_safe_text_and_exact_headers():
    parsed = parse_original(html(), "text/html")
    assert parsed["status"] == "ok", parsed
    assert "fetch" not in json.dumps(parsed) and "verified=true" not in json.dumps(parsed)
    rows, warnings = numeric_rows(parsed, "000001")
    assert not warnings and len(rows) == 1
    assert rows[0]["quantity"].value == Decimal("123456789")
    assert rows[0]["period"].start == date(2025, 1, 1)
    assert rows[0]["period"].basis == "period_to_date/annual"
    assert rows[0]["fragment"]["locator"] == "table:1/row:2"
    assert len(rows[0]["headers"]) == 5


def test_actual_generated_pdf_and_scan_no_fake_ocr_success():
    material = pdf(
        (
            "Security code: 000001",
            "Fact: net_profit_total | 2025-01-01 | 2025-12-31 | period_to_date/annual | CNY | CNY | -123.45",
        )
    )
    parsed = parse_original(material, "application/pdf")
    assert parsed["status"] == "ok", parsed
    assert parsed["fragments"][1]["locator"] == "page:1/line:2"
    rows, _ = numeric_rows(parsed, "000001")
    assert rows[0]["quantity"].value == Decimal("-123.45")
    scanned = parse_original(pdf(scan=True), "application/pdf")
    assert scanned["status"] == "needs_data" and scanned["fragments"] == []
    assert scanned["warnings"] == ["page:1:ocr_needed_not_configured"]
    assert scanned["ocr"] == "unsupported_not_configured"


def test_encrypted_pdf_and_hidden_html_are_explicitly_unsupported():
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.encrypt("fixture-password")
    stream = io.BytesIO()
    writer.write(stream)
    assert (
        parse_original(stream.getvalue(), "application/pdf")["reason"]
        == "encrypted_pdf_unsupported"
    )
    assert (
        parse_original(html().replace(b"<table>", b'<table style="display: none">'), "text/html")[
            "reason"
        ]
        == "hidden_html_layout_needs_data"
    )


@pytest.mark.parametrize(
    "body,mime,reason",
    [
        (b"%PDF-1.7\nbroken\n%%EOF", "application/pdf", "original_parse_failed"),
        (b"a" * (MAX_INPUT + 1), "text/html", "original_parser_size_limit"),
        (b"<table><tr><td>x</tr></table>", "text/html", "malformed_table_needs_data"),
        (b"<table><table></table></table>", "text/html", "nested_table_needs_data"),
        (b"\xff", "text/html", "html_encoding_unsupported"),
    ],
)
def test_actual_parse_failure_bounds(body, mime, reason):
    result = parse_original(body, mime)
    assert result["status"] == "needs_data" and result["fragments"] == []
    assert result["reason"] == reason


def test_pdf_page_limit_and_misaligned_tables_are_not_facts():
    assert parse_original(pdf(pages=101), "application/pdf")["reason"] == "pdf_page_limit"
    parsed = parse_original(
        pdf(("Security code: 000001", "net_profit_total 2025 12345 67890")), "application/pdf"
    )
    assert numeric_rows(parsed, "000001")[0] == []
    complex_html = parse_original(
        html(row_suffix='<tr><td colspan="2">misaligned</td><td>999</td></tr>'), "text/html"
    )
    assert "complex_table_needs_data" in complex_html["warnings"]
    assert numeric_rows(complex_html, "000001")[0] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"code": "000002"},
        {"unit": "千元"},
        {"value": "+1"},
        {"value": "1e6"},
        {"value": "12,34"},
        {"value": "9" * 65},
    ],
)
def test_identity_unit_sign_or_number_ambiguity_needs_data(updates):
    assert numeric_rows(parse_original(html(**updates), "text/html"), "000001")[0] == []


def test_unit_conversion_does_not_round_under_global_decimal_context():
    with localcontext() as context:
        context.prec = 4
        rows, _ = numeric_rows(
            parse_original(html(value="123456789012345678901234567890.12345"), "text/html"),
            "000001",
        )
    assert rows[0]["quantity"].value == Decimal("1234567890123456789012345678901234.5")


async def test_saved_actual_original_candidate_verified_provenance_and_cutoff(db_session):
    payload, body, request = await original(db_session)
    with pytest.raises(DisclosureError, match="not_visible"):
        await stage(db_session, request.model_copy(update={"as_of": NOW}))
    parsed = await parse_staged(body, payload["mime"])
    candidate = await save_candidates(db_session, payload, body, parsed, request)
    assert candidate["facts"][0]["verification"] == "candidate"
    assert candidate["requires_new_message"]
    candidate_ref = ObjectRef.model_validate(candidate["fact_refs"][0])
    assert (
        await EvidenceService(db_session).get_evidence("local_owner", candidate_ref, request.as_of)
    ).status == "missing"
    manifest = await db_session.get(SavedEvidence, (candidate["parse_artifact_ref"]["id"], 1))
    assert len(json.loads(manifest.payload["text"])["fragments"]) == len(parsed["fragments"])
    await snapshot(db_session, request.listing_id)
    later = request.model_copy(update={"as_of": datetime.now(UTC)})
    verified = await save_candidates(db_session, payload, body, parsed, later)
    fact = EvidenceRecord.model_validate(verified["facts"][0])
    assert fact.verification == "verified" and fact.id != candidate_ref.id
    assert fact.document_provenance.original_hash == payload["content_hash"]
    assert fact.document_provenance.header_locators and fact.document_provenance.crosscheck_refs
    assert (await db_session.get(SavedEvidence, (str(candidate_ref.id), 1))).payload[
        "verification"
    ] == "candidate"
    visible = datetime.now(UTC)
    assert (
        await NumericResolver(EvidenceService(db_session), fact.subject_ref, visible).fact(
            ObjectRef(kind="evidence", id=fact.id, revision=1), require_verified=True
        )
    ).value.value == Decimal("123456789")
    with pytest.raises(InvestorError, match="Document ingestion"):
        await EvidenceService(db_session).save_evidence(
            "local_owner", fact.model_copy(update={"verification": "verified"}), uuid4()
        )
    assert (await save_candidates(db_session, payload, body, parsed, later))[
        "fact_refs"
    ] == verified["fact_refs"]


async def test_saved_real_chinese_pdf_exact_labelled_annual_fact_verification(db_session):
    payload, body, request = await original(db_session, body=chinese_pdf(), mime="application/pdf")
    await snapshot(db_session, request.listing_id)
    parsed = await parse_staged(body, payload["mime"])
    assert parsed["status"] == "ok", parsed
    result = await save_candidates(db_session, payload, body, parsed, request)
    assert len(result["facts"]) == 1, result
    fact = EvidenceRecord.model_validate(result["facts"][0])
    assert fact.verification == "verified" and fact.locator == "page:1/line:2"
    assert fact.period.basis == "period_to_date/annual"
    assert fact.value.value == Decimal("123456789.0000")
    assert fact.document_provenance.reporting_scope == "consolidated"


async def test_parser_concurrency_and_cancel_keep_slots_until_real_children_reaped(monkeypatch):
    import asyncio
    import threading
    import time

    import investment_agent.evidence.documents as documents

    real = documents.parse_original
    lock = threading.Lock()
    active = maximum = 0

    def observed(body, mime):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.1)
            return real(body, mime)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(documents, "parse_original", observed)
    tasks = [asyncio.create_task(parse_staged(html(), "text/html")) for _ in range(5)]
    await asyncio.sleep(0.03)
    tasks[0].cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert all(r["status"] == "ok" for r in results[1:])
    assert maximum == 2 and active == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"reporting_scope": "parent"},
        {"currency": "USD"},
        {"verification": {"net_profit_total": "candidate"}},
        {"net_profit": 1},
    ],
)
async def test_crosscheck_exact_scope_currency_value_verification_required(db_session, fields):
    payload, body, request = await original(db_session)
    await snapshot(db_session, request.listing_id, **fields)
    result = await save_candidates(
        db_session, payload, body, await parse_staged(body, payload["mime"]), request
    )
    assert result["facts"][0]["verification"] == "candidate"


async def test_owner_hash_subject_and_safe_fragment_readback(db_session):
    payload, body, request = await original(db_session)
    with pytest.raises(DisclosureError, match="not_found"):
        await stage(db_session, request, owner="other_owner")
    with pytest.raises(DisclosureError, match="scope"):
        await stage(db_session, request.model_copy(update={"listing_id": request.listing_id + 1}))
    parsed = await parse_staged(body, payload["mime"])
    result = readback(payload, parsed, request.model_copy(update={"offset": 2, "limit": 1}))
    assert (
        len(result["fragments"]) == 1
        and result["candidate_facts"] == []
        and result["candidate_fact_count"] == 1
    )
    with pytest.raises(DisclosureError, match="integrity"):
        await save_candidates(db_session, payload, body + b"changed", parsed, request)


async def test_conflicting_verified_facts_immediate_limits_and_historical_view(db_session):
    payload, body, request = await original(db_session)
    await snapshot(db_session, request.listing_id)
    first = await save_candidates(
        db_session, payload, body, await parse_staged(body, payload["mime"]), request
    )
    fact = EvidenceRecord.model_validate(first["facts"][0])
    before = datetime.now(UTC)
    # A different immutable original and independently verified later snapshot.
    service = DisclosureService(db_session)
    http = FixtureHTTP()
    http.body, http.mime = html(value="20,000.00"), "text/html"
    from investment_agent.evidence.disclosures import FetchRequest

    second_original = await DisclosureService.for_tests(db_session, http).execute(
        uuid4(),
        FetchRequest(
            listing_id=request.listing_id, item_id=UUID(payload["item_id"]), as_of=datetime.now(UTC)
        ),
    )
    newer = second_original["original"]
    await snapshot(db_session, request.listing_id, value=200000000)
    req = request.model_copy(update={"original_id": UUID(newer["id"]), "as_of": datetime.now(UTC)})
    other = await save_candidates(
        db_session, newer, http.body, await parse_staged(http.body, "text/html"), req
    )
    assert other["facts"][0]["verification"] == "verified"
    assert await document_limits(db_session, fact, before) == []
    assert await document_limits(db_session, fact, datetime.now(UTC)) == [
        "critical_document_conflict"
    ]
    with pytest.raises(InvestorError, match="known correction or conflict"):
        await NumericResolver(
            EvidenceService(db_session), fact.subject_ref, datetime.now(UTC)
        ).fact(ObjectRef(kind="evidence", id=fact.id, revision=1))
    ref = ObjectRef(kind="evidence", id=fact.id, revision=1)
    evidence = EvidenceService(db_session)
    current = await evidence.get_evidence("local_owner", ref, datetime.now(UTC))
    assert current.status == "conflict" and "critical_document_conflict" in current.warnings
    assert current.data.conflict == "none"
    historical = await evidence.get_evidence("local_owner", ref, before)
    assert historical.status == "ok"
    visible_before = await evidence.get_evidence(
        "local_owner", ref, datetime.now(UTC), visible_at=before
    )
    assert visible_before.status == "ok"
    old = await db_session.get(SavedEvidence, (str(fact.id), 1))
    assert old.payload["value"]["value"] == "123456789.0000" and old.payload["conflict"] == "none"
    assert (await service.read(request.original_id, datetime.now(UTC)))[1] == body


@pytest.mark.parametrize(
    "extra",
    [
        "<h2>母公司利润表</h2>",
        "<table><tr><td>unrelated equal number</td><td>12,345.6789</td></tr></table>",
        "<p>报告期：2024年1月1日至2024年12月31日</p>",
        "<p>单位：人民币元</p>",
    ],
)
def test_table_scope_linkage_ambiguity_even_equal_values_rejected(extra):
    body = html().replace(b"</body>", extra.encode() + b"</body>")
    assert numeric_rows(parse_original(body, "text/html"), "000001")[0] == []


@pytest.mark.parametrize("public_number", [False, True])
async def test_explicit_saved_issuer_correction_preserves_old_fact_and_both_origins(
    db_session, public_number
):
    from investment_agent.evidence.disclosures import FetchRequest

    payload, body, request = await original(
        db_session, body=html(correction="<p>公告编号：2026-011</p>") if public_number else None
    )
    await snapshot(db_session, request.listing_id)
    first = await save_candidates(
        db_session, payload, body, await parse_staged(body, "text/html"), request
    )
    old = EvidenceRecord.model_validate(first["facts"][0])
    historical = datetime.now(UTC)
    http = FixtureHTTP()
    http.items[0].update(
        announcementId="67890",
        announcementTime=int((PUBLISHED + timedelta(days=1)).timestamp() * 1000),
        adjunctUrl="finalpage/2026-10-09/67890.html",
    )
    http.body, http.mime = (
        html(
            value="20,000.00",
            correction="<p>本公告更正原公告编号：2026-011</p>"
            if public_number
            else "<p>本公告更正原公告编号：12345</p>",
        ),
        "text/html",
    )
    service = DisclosureService.for_tests(db_session, http)
    found = await service.execute(uuid4(), discovery(request.listing_id))
    fetched = await service.execute(
        uuid4(),
        FetchRequest(
            listing_id=request.listing_id,
            item_id=UUID(found["items"][0]["id"]),
            as_of=datetime.now(UTC),
        ),
    )
    newer = fetched["original"]
    await snapshot(db_session, request.listing_id, value=200000000)
    req = request.model_copy(update={"original_id": UUID(newer["id"]), "as_of": datetime.now(UTC)})
    result = await save_candidates(
        db_session, newer, http.body, await parse_staged(http.body, "text/html"), req
    )
    corrected = EvidenceRecord.model_validate(result["facts"][0])
    assert corrected.verification == "verified" and corrected.correction_refs == (
        ObjectRef(kind="evidence", id=old.id, revision=1),
    )
    assert (
        corrected.document_provenance.correction_locator
        and corrected.document_provenance.correction_fragment_hash
    )
    assert await document_limits(db_session, old, historical) == []
    assert "source_corrected" in await document_limits(db_session, old, datetime.now(UTC))
    assert await document_limits(db_session, corrected, datetime.now(UTC)) == []
    assert (await db_session.get(SavedEvidence, (str(old.id), 1))).payload["correction_refs"] == []
    assert (await DisclosureService(db_session).read(request.original_id, datetime.now(UTC)))[
        1
    ] == body
    assert (await DisclosureService(db_session).read(req.original_id, datetime.now(UTC)))[
        1
    ] == http.body


@pytest.mark.parametrize("placement", ["after_table", "unrelated_section"])
def test_ordinary_table_requires_preceding_bound_headers(placement):
    body = html().decode()
    if placement == "after_table":
        header = "<p>报告期：2025年1月1日至2025年12月31日</p>"
        body = body.replace(header, "").replace("</table>", "</table>" + header)
    else:
        body = body.replace("<table>", "<h2>其他业务统计</h2><table>")
    rows, warnings = numeric_rows(parse_original(body.encode(), "text/html"), "000001")
    assert rows == [] and warnings
