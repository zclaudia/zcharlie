"""Acquire immutable CNINFO annual-report originals for bank peer research.

This records original sources only; it never marks extracted financial facts
as verified. Discovery and download are bounded and keyed to a saved listing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investment_agent.evidence.disclosure_http import (
    DIRECTORY,
    QUERY,
    DisclosureError,
    OfficialHTTPS,
    SourceResponse,
)
from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.originals import AnnualOriginal, store_annual_original
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.reference import Listing

MAX_PAGES = 10
PAGE_SIZE = 30
MAX_QUERY_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_TOTAL_QUERY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ORIGINAL_BYTES = 64 * 1024 * 1024
_ORG_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CODE = re.compile(r"\d{6}")


def _exchange(listing: Listing) -> str:
    aliases = {"SSE": "SSE", "SHSE": "SSE", "SZSE": "SZSE"}
    if listing.market_code not in aliases or not _CODE.fullmatch(listing.ticker):
        raise DisclosureError("unsupported_or_missing_bank_listing")
    return aliases[listing.market_code]


def _json(response: SourceResponse) -> dict[str, Any]:
    if response.status != 200:
        raise DisclosureError(f"http_status_{response.status}")
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in {"application/json", "text/json"}:
        raise DisclosureError("invalid_json_mime")
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeError) as exc:
        raise DisclosureError("invalid_source_json") from exc
    if not isinstance(value, dict):
        raise DisclosureError("invalid_source_schema")
    return value


def _full_annual(title: str, year: int) -> bool:
    compact = re.sub(r"\s+", "", title)
    return (f"{year}年年度报告" in compact or f"{year}年度报告" in compact) and not any(
        word in compact
        for word in ("摘要", "英文", "取消", "H股", "更正公告", "更正说明", "修订说明", "补充说明")
    )


def _verify_pdf(body: bytes, code: str, year: int, company_name: str) -> None:
    if len(body) > MAX_PDF_BYTES or not body.startswith(b"%PDF-") or b"%%EOF" not in body[-4096:]:
        raise DisclosureError("invalid_original_mime_or_signature")
    try:
        front = bank_page_text(body, 1)
        first = re.sub(r"\s+", "", front)
        annual_year = rf"(?<!\d){year}\s*(?:(?:年\s*|/\s*)?年\s*度\s*报\s*告|ANNUAL\s*REPORT)"
        if re.search(annual_year, front, re.I) is None:
            raise DisclosureError("annual_pdf_issuer_or_year_mismatch")
        # Cover may name the issuer or explicitly identify its exact stock code.
        if company_name and company_name in first and str(year) in first and "年度报告" in first:
            return
        if (
            code in first
            and str(year) in first
            and ("年度报告" in first or "ANNUALREPORT" in first.upper())
        ):
            return
        # Covers sometimes omit the exchange code; permit the exact full issuer
        # name on an early identity page while still requiring the target year.
        for page in range(2, 6):
            try:
                text = re.sub(r"\s+", "", bank_page_text(body, page))
            except ValueError:
                break
            if company_name and company_name in text and str(year) in text and "年度报告" in text:
                return
    except DisclosureError:
        raise
    except Exception as exc:
        raise DisclosureError("annual_pdf_text_unreadable") from exc
    raise DisclosureError("annual_pdf_issuer_or_year_mismatch")


async def acquire_bank_annual_originals(
    session: AsyncSession,
    *,
    listing_id: int,
    report_period: date,
    acquired_at: datetime,
    data_dir: Path,
    http: Any = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch and store full annual report originals, preserving all revisions.

    ``http`` is an explicit test seam implementing the stable ``request`` API;
    tests using it get deterministic acquisition timestamps from ``acquired_at``.
    Live HTTPS stores each PDF's download completion time. Raw requests are
    preserved below ``data_dir/research-runs``.
    """
    if report_period.month != 12 or report_period.day != 31:
        return {
            "status": "error",
            "reasons": ["report_period_must_be_fiscal_year_end"],
            "originals": [],
            "verification": "unverified",
        }
    request_log: dict[str, Any] = {
        "listing_id": listing_id,
        "report_year": report_period.year,
        "requested_cutoff": acquired_at.isoformat(),
        "actual_acquisition_at": None,
        "requests": [],
    }
    run_key = f"{listing_id}-{report_period.year}-{uuid4().hex[:12]}"
    run_dir = Path(data_dir) / "research-runs" / f"bank-annual-{run_key}"

    def persist() -> None:
        if request_log["requests"] and request_log["actual_acquisition_at"] is None:
            request_log["actual_acquisition_at"] = datetime.now(UTC).isoformat()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "source-requests.json").write_text(
            json.dumps(request_log, ensure_ascii=False, indent=2)
        )

    try:
        if (
            acquired_at.tzinfo is None
            or acquired_at.utcoffset() is None
            or report_period.year > acquired_at.astimezone(UTC).year
        ):
            raise DisclosureError("invalid_or_future_acquisition_time")
        if acquired_at.astimezone(UTC) > datetime.now(UTC):
            raise DisclosureError("invalid_or_future_acquisition_time")
        if report_period.year >= acquired_at.astimezone(ZoneInfo("Asia/Shanghai")).year:
            persist()
            return {
                "status": "missing",
                "reasons": ["annual_report_not_yet_disclosed_for_period"],
                "originals": [],
                "verification": "unverified",
            }
        listing = await session.scalar(
            select(Listing).options(selectinload(Listing.company)).where(Listing.id == listing_id)
        )
        if listing is None:
            raise DisclosureError("unsupported_or_missing_bank_listing")
        exchange = _exchange(listing)
        client = http or OfficialHTTPS()
        # CNINFO directory is the sole authority for orgId; require exact code
        # and exchange. Directory endpoint currently serves the primary list.
        if not refresh:
            all_originals = list(
                (
                    await session.execute(
                        select(DisclosureOriginal).where(
                            DisclosureOriginal.owner_id == "local_owner",
                            DisclosureOriginal.payload["listing_id"].as_integer() == listing_id,
                        )
                    )
                ).scalars()
            )
            existing = [
                row
                for row in all_originals
                if row.payload.get("listing_id") == listing_id
                and str(report_period.year) in str(row.payload.get("title", ""))
            ]
            if existing:
                refs = []
                expected_ticker = listing.ticker + (".SZ" if exchange == "SZSE" else ".SH")
                expected_exchange = "SZSE" if exchange == "SZSE" else "SSE"
                for row in existing:
                    cache = row.payload
                    if (
                        row.owner_id != "local_owner"
                        or cache.get("listing_id") != listing_id
                        or cache.get("ticker") != expected_ticker
                        or cache.get("exchange") != expected_exchange
                        or cache.get("source") != "cninfo.v1"
                        or cache.get("id") != row.id
                        or cache.get("item_id") != row.item_id
                        or cache.get("content_hash") != row.content_hash
                        or cache.get("mime") != "application/pdf"
                        or not _full_annual(str(cache.get("title", "")), report_period.year)
                    ):
                        raise DisclosureError("cached_original_identity_or_period_mismatch")
                    cached_item = await session.get(DisclosureItem, row.item_id)
                    if (
                        cached_item is None
                        or cached_item.owner_id != "local_owner"
                        or cached_item.listing_id != listing_id
                        or cached_item.payload.get("listing_id") != listing_id
                        or cached_item.payload.get("id") != row.item_id
                        or cached_item.payload.get("ticker") != expected_ticker
                        or cached_item.payload.get("exchange") != expected_exchange
                        or cached_item.payload.get("source") != "cninfo.v1"
                        or cached_item.payload.get("issuer_id") != cache.get("issuer_id")
                        or cached_item.payload.get("disclosure_id") != cache.get("disclosure_id")
                        or cached_item.payload.get("title") != cache.get("title")
                        or cached_item.payload.get("source_url") != cache.get("source_url")
                    ):
                        raise DisclosureError("cached_original_item_binding_mismatch")
                    try:
                        published = datetime.fromisoformat(str(cache["first_published_at"]))
                        acquired = datetime.fromisoformat(str(cache["acquired_at"]))
                        available = datetime.fromisoformat(str(cache["available_from"]))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise DisclosureError("cached_original_timestamps_invalid") from exc
                    if any(
                        value.tzinfo is None or value.utcoffset() is None
                        for value in (published, acquired, available)
                    ):
                        raise DisclosureError("cached_original_timestamps_invalid")
                    cutoff = acquired_at.astimezone(UTC)
                    if any(
                        value.astimezone(UTC) > cutoff for value in (published, acquired, available)
                    ):
                        raise DisclosureError("cached_original_after_requested_cutoff")
                    blob = await session.get(DisclosureBlob, row.content_hash)
                    if blob is None or hashlib.sha256(blob.body).hexdigest() != row.content_hash:
                        raise DisclosureError("cached_original_blob_hash_mismatch")
                    if cache.get("size") != len(blob.body):
                        raise DisclosureError("cached_original_size_mismatch")
                    _verify_pdf(
                        blob.body,
                        listing.ticker,
                        report_period.year,
                        listing.company.canonical_name,
                    )
                    refs.append(
                        {
                            **cache,
                            "id": row.id,
                            "item_id": row.item_id,
                            "content_hash": row.content_hash,
                        }
                    )
                return {
                    "status": "ok" if len(refs) == 1 else "ambiguous",
                    "reasons": [],
                    "originals": refs,
                    "verification": "unverified",
                    "reused": True,
                }
        discovery_bytes = 0
        original_bytes = 0
        directory_response = await client.request(DIRECTORY, "directory", max_bytes=MAX_QUERY_BYTES)
        request_log["requests"].append(
            {
                "url": DIRECTORY,
                "status": directory_response.status,
                "sha256": hashlib.sha256(directory_response.body).hexdigest(),
            }
        )
        if len(directory_response.body) > MAX_QUERY_BYTES:
            raise DisclosureError("response_too_large")
        discovery_bytes += len(directory_response.body)
        request_log["requests"][-1]["response_body_base64"] = base64.b64encode(
            directory_response.body
        ).decode("ascii")
        stock_list = _json(directory_response).get("stockList")
        if not isinstance(stock_list, list):
            raise DisclosureError("invalid_identity_directory")
        # Directory rows may include an explicit exchange field. Never infer an
        # issuer from company name, arbitrary URL, or caller supplied identity.
        matches = [
            r
            for r in stock_list
            if isinstance(r, dict)
            and r.get("code") == listing.ticker
            and str(r.get("exchange", "")).upper() in {exchange, ""}
        ]
        if (
            len(matches) != 1
            or not isinstance(matches[0].get("orgId"), str)
            or not _ORG_ID.fullmatch(matches[0]["orgId"])
        ):
            raise DisclosureError("ambiguous_or_missing_official_issuer")
        issuer = matches[0]["orgId"]
        start = date(report_period.year + 1, 1, 1).isoformat()
        end = acquired_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        all_items: list[dict[str, Any]] = []
        pages = 0
        has_more = True
        for page in range(1, MAX_PAGES + 1):
            payload = urlencode(
                {
                    "pageNum": page,
                    "pageSize": PAGE_SIZE,
                    "column": "szse" if exchange == "SZSE" else "sse",
                    "tabName": "fulltext",
                    "stock": f"{listing.ticker},{issuer}",
                    "seDate": f"{start}~{end}",
                    "sortName": "time",
                    "sortType": "desc",
                    "plate": "",
                    "searchkey": f"{report_period.year}年度报告",
                    "secid": "",
                    "category": "",
                    "trade": "",
                    "isHLtitle": "false",
                }
            ).encode()
            if len(request_log["requests"]) >= 1 + MAX_PAGES + PAGE_SIZE:
                raise DisclosureError("request_limit_exceeded")
            response = await client.request(QUERY, "query", body=payload, max_bytes=MAX_QUERY_BYTES)
            request_log["requests"].append(
                {
                    "url": QUERY,
                    "page": page,
                    "body": payload.decode(),
                    "body_base64": base64.b64encode(payload).decode("ascii"),
                    "status": response.status,
                    "sha256": hashlib.sha256(response.body).hexdigest(),
                }
            )
            if len(response.body) > MAX_QUERY_BYTES:
                raise DisclosureError("response_too_large")
            discovery_bytes += len(response.body)
            if discovery_bytes > MAX_TOTAL_QUERY_BYTES:
                raise DisclosureError("discovery_total_bytes_exceeded")
            request_log["requests"][-1]["response_body_base64"] = base64.b64encode(
                response.body
            ).decode("ascii")
            material = _json(response)
            items = material.get("announcements")
            if not isinstance(items, list) or len(items) > PAGE_SIZE:
                raise DisclosureError("invalid_discovery_schema_or_count")
            all_items.extend(items)
            pages = page
            has_more = bool(material.get("hasMore"))
            if not has_more:
                break
        if has_more:
            raise DisclosureError("pagination_incomplete")
        candidates: list[dict[str, Any]] = []
        for item in all_items:
            if (
                not isinstance(item, dict)
                or item.get("secCode") != listing.ticker
                or str(item.get("orgId")) != issuer
            ):
                raise DisclosureError("source_identity_mismatch")
            title, ann_id, stamp, adjunct = (
                item.get("announcementTitle"),
                item.get("announcementId"),
                item.get("announcementTime"),
                item.get("adjunctUrl"),
            )
            if (
                not isinstance(title, str)
                or len(title) > 2000
                or not isinstance(ann_id, str)
                or not re.fullmatch(r"\d{1,32}", ann_id)
                or isinstance(stamp, bool)
                or not isinstance(stamp, int)
            ):
                raise DisclosureError("invalid_disclosure_identity_or_time")
            published = datetime.fromtimestamp(stamp / 1000, UTC)
            if published > acquired_at.astimezone(UTC):
                raise DisclosureError("publication_after_acquisition")
            if _full_annual(title, report_period.year):
                candidates.append(item)
        if not candidates:
            persist()
            return {
                "status": "missing",
                "reasons": ["annual_report_not_disclosed"],
                "originals": [],
                "pages": pages,
                "verification": "unverified",
            }
        prepared: list[tuple[dict[str, Any], str, datetime, bytes, datetime]] = []
        actual_acquisition_at = acquired_at if http is not None else None
        for item in sorted(
            candidates, key=lambda v: (int(v["announcementTime"]), v["announcementId"])
        ):
            adjunct = item.get("adjunctUrl")
            if not isinstance(adjunct, str):
                raise DisclosureError("missing_or_invalid_original_locator")
            adjunct_path = "/" + adjunct.lstrip("/")
            if not re.fullmatch(
                r"/finalpage/\d{4}-\d{2}-\d{2}/[0-9A-Za-z_-]{1,100}\.pdf", adjunct_path, re.I
            ):
                raise DisclosureError("missing_or_invalid_original_locator")
            if len(request_log["requests"]) >= 1 + MAX_PAGES + PAGE_SIZE:
                raise DisclosureError("request_limit_exceeded")
            url = "https://static.cninfo.com.cn" + adjunct_path
            pdf = await client.request(url, "original", max_bytes=MAX_PDF_BYTES)
            downloaded_at = datetime.now(UTC) if http is None else acquired_at
            if actual_acquisition_at is None or downloaded_at > actual_acquisition_at:
                actual_acquisition_at = downloaded_at
            request_log["actual_acquisition_at"] = actual_acquisition_at.isoformat()
            request_log["requests"].append(
                {"url": url, "status": pdf.status, "sha256": hashlib.sha256(pdf.body).hexdigest()}
            )
            if (
                pdf.status != 200
                or pdf.headers.get("content-type", "").split(";")[0].lower() != "application/pdf"
            ):
                raise DisclosureError("invalid_original_mime_or_signature")
            original_bytes += len(pdf.body)
            if original_bytes > MAX_TOTAL_ORIGINAL_BYTES:
                raise DisclosureError("original_total_bytes_exceeded")
            company_name = getattr(getattr(listing, "company", None), "canonical_name", "") or ""
            _verify_pdf(pdf.body, listing.ticker, report_period.year, company_name)
            published = datetime.fromtimestamp(int(item["announcementTime"]) / 1000, UTC)
            prepared.append((item, url, published, pdf.body, downloaded_at))

        # Validate every revision before writing any rows, so a malformed later
        # correction cannot leave a partial set of originals behind.
        if actual_acquisition_at is None:
            actual_acquisition_at = datetime.now(UTC)
            request_log["actual_acquisition_at"] = actual_acquisition_at.isoformat()
        original_refs = []
        for item, url, published, body, downloaded_at in prepared:
            original = AnnualOriginal(
                code=listing.ticker + (".SZ" if exchange == "SZSE" else ".SH"),
                period=report_period,
                body=body,
                metadata={"chosen": item, "url": url},
                pdf_hash=hashlib.sha256(body).hexdigest(),
                published_at=published,
            )
            row = await store_annual_original(session, original, listing_id, downloaded_at)
            original_refs.append(
                {
                    "id": row.id,
                    "item_id": row.item_id,
                    "content_hash": row.content_hash,
                    "announcement_id": item["announcementId"],
                    "title": item["announcementTitle"],
                    "published_at": published.isoformat(),
                    "acquired_at": downloaded_at.isoformat(),
                    "source_url": url,
                    "size": len(body),
                    "verification": "candidate",
                }
            )
        request_log["pagination"] = {
            "pages": pages,
            "complete": True,
            "candidate_count": len(candidates),
        }
        persist()
        return {
            "status": "ok" if len(original_refs) == 1 else "ambiguous",
            "reasons": []
            if len(original_refs) == 1
            else ["multiple_full_annual_reports_preserved"],
            "originals": original_refs,
            "pages": pages,
            "verification": "unverified",
        }
    except (DisclosureError, ValueError, OSError, OverflowError) as exc:
        request_log["error"] = str(exc)
        persist()
        return {
            "status": "error",
            "reasons": [str(exc)],
            "originals": [],
            "verification": "unverified",
        }
