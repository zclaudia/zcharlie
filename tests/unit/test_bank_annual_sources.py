from __future__ import annotations

import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select, update

from investment_agent.evidence.disclosure_http import (
    QUERY,
    DisclosureError,
    SourceResponse,
)
from investment_agent.industry_peers import bank_annual_sources
from investment_agent.industry_peers.bank_annual_sources import (
    MAX_PAGES,
    _full_annual,
    _json,
    acquire_bank_annual_originals,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.repositories.listing import ListingRepository

ACQUIRED = datetime(2026, 10, 17, 2, tzinfo=UTC)


def pdf(code: str, year: int) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 30 750 Td ({code} {year} ANNUALREPORT) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeHTTP:
    def __init__(
        self,
        *,
        ticker: str,
        org: str,
        items: list[dict],
        pdf_body: bytes | None = None,
        has_more_after: int | None = None,
        fail_page: int | None = None,
    ):
        self.ticker, self.org, self.items = ticker, org, items
        self.pdf_body = pdf_body or pdf(ticker, 2025)
        self.has_more_after, self.fail_page = has_more_after, fail_page
        self.calls: list[tuple[str, bytes | None]] = []

    async def request(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        del max_bytes, timeout_seconds
        self.calls.append((url, body))
        if purpose == "directory":
            return SourceResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"stockList": [{"code": self.ticker, "orgId": self.org}]}).encode(),
                url,
            )
        if purpose == "query":
            page = int(parse_qs((body or b"").decode())["pageNum"][0])
            if self.fail_page == page:
                return SourceResponse(503, {"content-type": "application/json"}, b"{}", url)
            has_more = self.has_more_after is not None and page <= self.has_more_after
            entries = self.items if page == 1 else []
            return SourceResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"announcements": entries, "hasMore": has_more}).encode(),
                url,
            )
        return SourceResponse(200, {"content-type": "application/pdf"}, self.pdf_body, url)


def announcement(
    ticker: str,
    org: str,
    *,
    title: str = "某行2025年度报告",
    ann_id: str = "12345",
    year: int = 2026,
    adjunct: str = "finalpage/2026-03-28/12345.PDF",
    name: str | None = None,
) -> dict:
    return {
        "secCode": ticker,
        "secName": name or "某行",
        "orgId": org,
        "announcementId": ann_id,
        "announcementTitle": title,
        "announcementTime": int(datetime(year, 3, 28, tzinfo=UTC).timestamp() * 1000),
        "adjunctUrl": adjunct,
    }


@pytest.fixture
async def bank_session(tmp_path: Path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path}/bank-sources.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        repository = ListingRepository(session)
        sh = await repository.ensure_a_share_listing("601398.SH", company_name="工商银行")
        sz = await repository.ensure_a_share_listing("000001.SZ", company_name="平安银行")
        yield session, sh.id, sz.id
    await engine.dispose()


@pytest.mark.parametrize(("ticker", "exchange"), [("601398", ".SH"), ("000001", ".SZ")])
async def test_acquires_original_and_idempotent(bank_session, tmp_path, ticker: str, exchange: str):
    session, sh_id, sz_id = bank_session
    listing_id = sh_id if exchange == ".SH" else sz_id
    org = "jjxt0000019" if exchange == ".SH" else "orgsz0001"
    fake = FakeHTTP(ticker=ticker, org=org, items=[announcement(ticker, org)])
    result = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
    )
    assert result["status"] == "ok"
    assert len(result["originals"]) == 1
    assert result["originals"][0]["title"] == "某行2025年度报告"
    assert result["originals"][0]["content_hash"]
    assert (tmp_path / "research-runs").is_dir()
    import json

    logged = next((tmp_path / "research-runs").glob("*/source-requests.json"))
    audit = json.loads(logged.read_text())
    query_entry = next(entry for entry in audit["requests"] if entry["url"] == QUERY)
    params = parse_qs(query_entry["body"])
    assert params["column"] == ["sse" if exchange == ".SH" else "szse"]
    assert params["searchkey"] == ["2025年度报告"]
    assert "response_body_base64" in query_entry
    rowcount = len(list((await session.execute(select(DisclosureOriginal))).scalars()))
    second = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
    )
    assert second["reused"] is True
    assert len(list((await session.execute(select(DisclosureOriginal))).scalars())) == rowcount


@pytest.mark.parametrize("tamper", ["blob", "suffix", "future", "item_listing"])
async def test_cache_hit_validates_blob_identity_and_cutoff(bank_session, tmp_path, tamper: str):
    session, listing_id, _ = bank_session
    ticker, org = "601398", "jjxt0000019"
    fake = FakeHTTP(ticker=ticker, org=org, items=[announcement(ticker, org)])
    first = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
    )
    assert first["status"] == "ok"
    row = await session.get(DisclosureOriginal, first["originals"][0]["id"])
    assert row is not None
    if tamper == "blob":
        await session.execute(
            update(DisclosureBlob)
            .where(DisclosureBlob.content_hash == row.content_hash)
            .values(body=b"corrupted body")
        )
    elif tamper == "item_listing":
        item = await session.get(DisclosureItem, row.item_id)
        assert item is not None
        payload = dict(item.payload)
        payload["listing_id"] = listing_id + 100
        await session.execute(
            update(DisclosureItem).where(DisclosureItem.id == item.id).values(payload=payload)
        )
    else:
        payload = dict(row.payload)
        if tamper == "suffix":
            payload["ticker"] = "601398.SZ"
        else:
            payload["acquired_at"] = "2027-01-01T00:00:00+00:00"
        await session.execute(
            update(DisclosureOriginal)
            .where(DisclosureOriginal.id == row.id)
            .values(payload=payload)
        )
    calls_before = len(fake.calls)
    cached = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
    )
    assert cached["status"] == "error"
    assert len(fake.calls) == calls_before


async def test_comparison_year_title_refetches_instead_of_becoming_a_cache_hit(
    bank_session, tmp_path
):
    session, listing_id, _ = bank_session
    ticker, org = "601398", "jjxt0000019"
    fake = FakeHTTP(ticker=ticker, org=org, items=[announcement(ticker, org)])
    first = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
    )
    row = await session.get(DisclosureOriginal, first["originals"][0]["id"])
    assert row is not None
    payload = dict(row.payload)
    payload["title"] = "工商银行2024年度报告"
    await session.execute(
        update(DisclosureOriginal).where(DisclosureOriginal.id == row.id).values(payload=payload)
    )
    fake.items = []
    calls_before = len(fake.calls)
    result = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
    )
    assert result["status"] == "missing"
    assert len(fake.calls) > calls_before


async def test_live_transport_never_backdates_original_to_requested_cutoff(
    bank_session, tmp_path, monkeypatch
):
    session, listing_id, _ = bank_session
    ticker, org = "601398", "jjxt0000019"
    transport = FakeHTTP(ticker=ticker, org=org, items=[announcement(ticker, org)])
    monkeypatch.setattr(bank_annual_sources, "OfficialHTTPS", lambda: transport)
    requested_cutoff = datetime(2026, 10, 16, 8, tzinfo=UTC)
    started_at = datetime.now(UTC)
    result = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=requested_cutoff,
        data_dir=tmp_path,
    )
    assert result["status"] == "ok"
    stored_at = datetime.fromisoformat(result["originals"][0]["acquired_at"])
    assert stored_at >= started_at and stored_at > requested_cutoff
    row = await session.get(DisclosureOriginal, result["originals"][0]["id"])
    assert row is not None
    assert row.payload["acquired_at"] == stored_at.isoformat()
    assert row.payload["available_from"] == stored_at.isoformat()
    log_path = next((tmp_path / "research-runs").glob("*/source-requests.json"))
    request_log = json.loads(log_path.read_text())
    assert request_log["requested_cutoff"] == requested_cutoff.isoformat()
    assert datetime.fromisoformat(request_log["actual_acquisition_at"]) >= started_at
    later_cutoff = datetime.now(UTC)
    reused = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=later_cutoff,
        data_dir=tmp_path,
    )
    assert reused["reused"] is True
    assert reused["originals"][0]["acquired_at"] == stored_at.isoformat()


async def test_preserves_full_correction_and_excludes_correction_notice(bank_session, tmp_path):
    session, listing_id, _ = bank_session
    ticker, org = "601398", "jjxt0000019"
    fake = FakeHTTP(
        ticker=ticker,
        org=org,
        items=[
            announcement(ticker, org, ann_id="10001", title="工商银行2025年度报告"),
            announcement(
                ticker,
                org,
                ann_id="10002",
                title="工商银行2025年度报告（修订版）",
                adjunct="finalpage/2026-04-01/10002.PDF",
            ),
            announcement(
                ticker,
                org,
                ann_id="10003",
                title="关于2025年度报告的更正公告",
                adjunct="finalpage/2026-04-02/10003.PDF",
            ),
        ],
    )
    result = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
        refresh=True,
    )
    assert result["status"] == "ambiguous"
    assert {ref["announcement_id"] for ref in result["originals"]} == {"10001", "10002"}


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda ticker, org: announcement(ticker, org, year=2027), "publication_after_acquisition"),
        (lambda ticker, org: announcement("999999", org), "source_identity_mismatch"),
        (lambda ticker, org: announcement(ticker, "wrong-org"), "source_identity_mismatch"),
    ],
)
async def test_rejects_future_and_identity_mismatch(bank_session, tmp_path, mutate, reason):
    session, listing_id, _ = bank_session
    fake = FakeHTTP(ticker="601398", org="jjxt0000019", items=[mutate("601398", "jjxt0000019")])
    result = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
        refresh=True,
    )
    assert result["status"] == "error" and reason in result["reasons"]
    assert not list((await session.execute(select(DisclosureOriginal))).scalars())


async def test_rejects_wrong_period_pdf_and_incomplete_pagination(bank_session, tmp_path):
    session, listing_id, _ = bank_session
    ticker, org = "601398", "jjxt0000019"
    item = announcement(ticker, org)
    wrong_pdf = FakeHTTP(ticker=ticker, org=org, items=[item], pdf_body=pdf("999999", 2025))
    bad = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=wrong_pdf,
        refresh=True,
    )
    assert bad["status"] == "error" and "annual_pdf_issuer_or_year_mismatch" in bad["reasons"]
    wrong_year = FakeHTTP(ticker=ticker, org=org, items=[item], pdf_body=pdf(ticker, 2024))
    wrong_year_result = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=wrong_year,
        refresh=True,
    )
    assert wrong_year_result["status"] == "error"
    assert "annual_pdf_issuer_or_year_mismatch" in wrong_year_result["reasons"]
    truncated = FakeHTTP(ticker=ticker, org=org, items=[], has_more_after=MAX_PAGES)
    incomplete = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=truncated,
        refresh=True,
    )
    assert incomplete["status"] == "error" and "pagination_incomplete" in incomplete["reasons"]


async def test_rejects_non_year_end_and_transport_failure(bank_session, tmp_path):
    session, listing_id, _ = bank_session
    invalid = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 6, 30),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
    )
    assert "report_period_must_be_fiscal_year_end" in invalid["reasons"]
    fake = FakeHTTP(ticker="601398", org="jjxt0000019", items=[], fail_page=1)
    failed = await acquire_bank_annual_originals(
        session,
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
        data_dir=tmp_path,
        http=fake,
        refresh=True,
    )
    assert failed["status"] == "error" and "http_status_503" in failed["reasons"]


def test_disclosure_error_is_value_error():
    assert issubclass(DisclosureError, ValueError)


def test_real_cninfo_probe_rows_filter_annual_report_exactly():
    fixture = (
        Path(__file__).parents[2]
        / "research/industry-data-validation/evidence/bank-rollover-2026-10-17/root/query-601398-2024.json"
    )
    data = json.loads(fixture.read_text())
    titles = [item["announcementTitle"] for item in data["announcements"]]
    assert len(titles) == 3
    assert [_full_annual(title, 2024) for title in titles] == [False, False, True]


def test_json_contract_rejects_html_invalid_json_and_arrays():
    for response in (
        SourceResponse(200, {"content-type": "text/html"}, b"{}", "https://test.invalid"),
        SourceResponse(200, {"content-type": "application/json"}, b"[]", "https://test.invalid"),
        SourceResponse(200, {"content-type": "application/json"}, b"{", "https://test.invalid"),
    ):
        with pytest.raises(DisclosureError):
            _json(response)
