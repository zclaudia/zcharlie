from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_agent.evidence.disclosure_http import DisclosureError, SourceResponse
from investment_agent.evidence.disclosure_pages import (
    DisclosurePageRequestV1,
    json_material,
    parse_page_response,
    validate_pdf,
)
from investment_agent.investor.common import ObjectRef
from tests.support.p4_disclosures import FIXTURE_ISSUER, FIXTURE_TICKER, fixture_protocol


def _request() -> DisclosurePageRequestV1:
    now = datetime(2026, 10, 16, 23, 59, tzinfo=UTC)
    return DisclosurePageRequestV1(
        listing_id=1,
        authorization_id=uuid4(),
        authorization_revision=1,
        authorization_hash="a" * 64,
        listing_ref=ObjectRef(kind="listing", id=1, revision=1),
        contract_hash="b" * 64,
        source_scope_hash="c" * 64,
        attempt_scope_hash="d" * 64,
        window_start=now - timedelta(days=1),
        window_end=now,
        page_num=1,
        protocol_hash=fixture_protocol().protocol_hash,
    )


def _row(
    *, doc_type: str = "fixture_annual_full", native_id: str = "10000001"
) -> dict[str, object]:
    published = datetime(2026, 10, 16, 12, tzinfo=UTC)
    return {
        "announcementId": native_id,
        "secCode": FIXTURE_TICKER,
        "orgId": FIXTURE_ISSUER,
        "announcementTime": int(published.timestamp() * 1000),
        "announcementTitle": "fixture report",
        "adjunctUrl": "finalpage/2026-10-16/report.PDF",
        "fixtureDocumentType": doc_type,
    }


def _response(rows: list[dict[str, object]], *, page_num: object = 1) -> SourceResponse:
    import json

    return SourceResponse(
        200,
        {"content-type": "application/json"},
        json.dumps(
            {
                "announcements": rows,
                "pageNum": page_num,
                "pageSize": 20,
                "hasMore": False,
                "totalAnnouncement": len(rows),
            }
        ).encode(),
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
    )


def test_pure_parser_classifies_allowlists_and_keeps_unknown() -> None:
    protocol = fixture_protocol()
    page = parse_page_response(
        _request(),
        _response(
            [
                _row(native_id="10000003"),
                _row(doc_type="fixture_annual_summary", native_id="10000002"),
                _row(doc_type="new_unmapped_type", native_id="10000001"),
            ]
        ),
        issuer_id=FIXTURE_ISSUER,
        ticker=FIXTURE_TICKER,
        protocol=protocol,
    )
    assert [doc.classification for doc in page.documents] == [
        "annual_report_full",
        "excluded",
        "unknown",
    ]


def test_pure_parser_rejects_bool_page_echo_and_duplicate_or_unordered_rows() -> None:
    protocol = fixture_protocol()
    with pytest.raises(DisclosureError, match="page_number_mismatch"):
        parse_page_response(
            _request(),
            _response([], page_num=True),
            issuer_id=FIXTURE_ISSUER,
            ticker=FIXTURE_TICKER,
            protocol=protocol,
        )
    with pytest.raises(DisclosureError, match="duplicate_native_identity"):
        parse_page_response(
            _request(),
            _response([_row(), _row()]),
            issuer_id=FIXTURE_ISSUER,
            ticker=FIXTURE_TICKER,
            protocol=protocol,
        )
    with pytest.raises(DisclosureError, match="source_page_order_drift"):
        parse_page_response(
            _request(),
            _response([_row(native_id="10000001"), _row(native_id="10000002")]),
            issuer_id=FIXTURE_ISSUER,
            ticker=FIXTURE_TICKER,
            protocol=protocol,
        )


def test_original_accepts_only_well_formed_pdf() -> None:
    assert validate_pdf(b"%PDF-1.4\nfixture\n%%EOF", "application/pdf")
    with pytest.raises(DisclosureError, match="unsupported_original_type"):
        validate_pdf(b"<html></html>", "text/html")


def test_deeply_nested_json_is_a_terminal_source_parse_failure() -> None:
    with pytest.raises(DisclosureError, match="invalid_source_json"):
        json_material(
            SourceResponse(
                200,
                {"content-type": "application/json"},
                b"[" * 10000 + b"0" + b"]" * 10000,
                "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            )
        )


def test_redirects_are_one_hop_and_preserve_post_method() -> None:
    from investment_agent.evidence.disclosure_pages import DisclosurePageService

    response = SourceResponse(
        307,
        {"location": "https://www.cninfo.com.cn/new/hisAnnouncement/query"},
        b"",
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
    )
    assert DisclosurePageService.next_redirect_url(
        response, "query", redirect_count=0, body_present=True
    ) == response.url
    with pytest.raises(DisclosureError, match="redirect_limit_or_method_change"):
        DisclosurePageService.next_redirect_url(
            SourceResponse(302, response.headers, response.body, response.url),
            "query",
            redirect_count=0,
            body_present=True,
        )


def test_redirect_location_is_revalidated_and_hop_budget_is_bounded() -> None:
    from investment_agent.evidence.disclosure_pages import DisclosurePageService

    response = SourceResponse(
        302,
        {"location": "https://static.cninfo.com.cn/finalpage/2026-10-16/report.PDF"},
        b"",
        "https://static.cninfo.com.cn/finalpage/2026-10-16/old.PDF",
    )
    assert DisclosurePageService.next_redirect_url(
        response, "original", redirect_count=0
    ) == "https://static.cninfo.com.cn/finalpage/2026-10-16/report.PDF"
    with pytest.raises(DisclosureError, match="unsupported_source_url"):
        DisclosurePageService.next_redirect_url(
            SourceResponse(
                302,
                {"location": "https://attacker.example/x"},
                response.body,
                response.url,
            ),
            "original",
            redirect_count=0,
        )
    with pytest.raises(DisclosureError, match="redirect_limit"):
        DisclosurePageService.next_redirect_url(
            response, "original", redirect_count=2
        )
