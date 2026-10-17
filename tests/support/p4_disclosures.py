"""Synthetic protocol and real loopback HTTP transport for B engineering only.

No production source service imports this module. These fields are deliberate
test data, not a claim about CNInfo's current response schema or categories.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from investment_agent.evidence.disclosure_http import DisclosureError, SourceResponse
from investment_agent.evidence.disclosure_pages import CNInfoPageProtocolV1
from investment_agent.supplemental_rules.canonical import canonical_hash

FIXTURE_ISSUER = "fixture_org_300901"
FIXTURE_TICKER = "300901"
FIXTURE_PDF = b"%PDF-1.4\nSynthetic annual report fixture, no investment facts.\n%%EOF"


def fixture_protocol() -> CNInfoPageProtocolV1:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "validated": False,
        "query_sort_name": "time",
        "query_sort_type": "desc",
        "stable_tie_break_order": "desc",
        "native_id_field": "announcementId",
        "issuer_code_field": "secCode",
        "issuer_id_field": "orgId",
        "published_millis_field": "announcementTime",
        "title_field": "announcementTitle",
        "locator_field": "adjunctUrl",
        "document_type_field": "fixtureDocumentType",
        "page_number_field": "pageNum",
        "page_size_field": "pageSize",
        "has_more_field": "hasMore",
        "total_field": "totalAnnouncement",
        "stable_tie_break_field": "announcementId",
        "annual_full_text_type_values": ("fixture_annual_full",),
        "correction_type_values": ("fixture_annual_correction",),
        "supplement_type_values": ("fixture_annual_supplement",),
        "excluded_type_values": ("fixture_annual_summary", "fixture_other"),
    }
    return CNInfoPageProtocolV1.model_validate(
        {
            **fields,
            "protocol_hash": canonical_hash({"profile": "fixture_only", **fields}),
        }
    )


def fixture_source_response(
    purpose: str,
    body: bytes,
    *,
    published_at: datetime,
    mode: str = "normal",
) -> tuple[int, dict[str, str], bytes]:
    """Serve a tiny bounded source, including honest zero-eligible windows."""
    if mode == "rate_limited":
        return (
            429,
            {"content-type": "application/json", "retry-after": "60"},
            b'{"fixture":"rate_limited"}',
        )
    if purpose == "original":
        return 200, {"content-type": "application/pdf"}, FIXTURE_PDF
    if purpose == "directory":
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "stockList": [
                        {"code": FIXTURE_TICKER, "orgId": FIXTURE_ISSUER},
                    ]
                }
            ).encode(),
        )
    if purpose != "query":
        return 400, {"content-type": "application/json"}, b'{"error":"fixture purpose"}'
    params = parse_qs(body.decode())
    page_num = int(params.get("pageNum", ["1"])[0])
    page_size = int(params.get("pageSize", ["20"])[0])
    if mode in {"two_pages", "late_drift"}:
        all_rows = [
            {
                "announcementId": f"{number:08d}",
                "secCode": FIXTURE_TICKER,
                "orgId": FIXTURE_ISSUER,
                "announcementTime": int(published_at.astimezone(UTC).timestamp() * 1000),
                "announcementTitle": "工程夹具明确排除类型（非真实公告）",
                "adjunctUrl": f"finalpage/2026-10-16/{number:08d}.html",
                "fixtureDocumentType": "fixture_other",
            }
            for number in range(21, 0, -1)
        ]
        rows = all_rows[(page_num - 1) * page_size : page_num * page_size]
    else:
        all_rows = (
            []
            if mode == "empty"
            else [
                {
                    "announcementId": "10000001",
                    "secCode": FIXTURE_TICKER,
                    "orgId": FIXTURE_ISSUER,
                    "announcementTime": int(published_at.astimezone(UTC).timestamp() * 1000),
                    "announcementTitle": "工程夹具年度报告全文（非真实公告）",
                    "adjunctUrl": "finalpage/2026-10-16/10000001.PDF",
                    "fixtureDocumentType": "fixture_annual_full",
                }
            ]
        )
        rows = all_rows if page_num == 1 else []
    total = len(all_rows)
    return (
        200,
        {"content-type": "application/json"},
        json.dumps(
            {
                "announcements": rows,
                "pageNum": page_num,
                "pageSize": page_size,
                "hasMore": page_num * page_size < total,
                "totalAnnouncement": total,
            },
            ensure_ascii=False,
        ).encode(),
    )


class LoopbackDisclosureHTTP:
    """Real HTTP to a local controlled server, explicitly injected for tests."""

    def __init__(self, base: str, *, headers: dict[str, str] | None = None) -> None:
        parsed = urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("B fixture transport requires loopback HTTP")
        self.base, self.headers = base.rstrip("/"), headers or {}
        self.calls: list[tuple[str, str]] = []

    async def request_hop(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        self.calls.append((purpose, url))
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=timeout_seconds
        ) as client:
            response = await client.post(
                self.base + "/fixture/source",
                content=body or b"",
                headers={**self.headers, "X-Fixture-Purpose": purpose},
            )
        if len(response.content) > max_bytes:
            raise DisclosureError("response_too_large")
        return SourceResponse(response.status_code, dict(response.headers), response.content, url)
