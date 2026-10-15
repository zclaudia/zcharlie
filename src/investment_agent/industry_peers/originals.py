"""Bounded, offline annual-report checks. Text matches are never a PDF identity.

The fixed checklist is checked against actual bytes and official metadata,
then originals are stored in the existing disclosure library for readback and
backup. This is a small annual-table adapter, not a general PDF parser.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)

RULE = "peer-annual-original/1"
ISSUER_ALIASES = {"江苏银行": ("BANKOFJIANGSU",)}
ALIASES = {
    "operating_revenue": ("营业收入",),
    "net_profit_parent": (
        "归属于上市公司股东的净利润",
        "归属于母公司股东的净利润",
        "归属于母公司所有者的净利润",
        "归属于母公司所有者的净利润",
    ),
    "equity_parent": (
        "归属于上市公司股东的净资产",
        "归属于母公司所有者权益",
        "归属于母公司股东权益",
        "归属于上市公司股东的所有者权益",
    ),
    "contract_liabilities": ("合同负债",),
    "operating_cash_flow": ("经营活动产生的现金流量净额", "经营活动现金流量净额"),
    "roe_weighted_annual": ("加权平均净资产收益率",),
}


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")").replace("％", "%")


@lru_cache(maxsize=512)
def page_text(body: bytes, page: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(body))
    if not 1 <= page <= len(reader.pages):
        raise ValueError("original PDF page is outside the report")
    return reader.pages[page - 1].extract_text()


@dataclass(frozen=True)
class AnnualOriginal:
    code: str
    period: date
    body: bytes
    metadata: dict[str, Any]
    pdf_hash: str
    published_at: datetime


def load_annual_original(root: Path, code: str, period: date) -> AnnualOriginal:
    metadata = json.loads((root / "derived/filings" / f"{code}.json").read_text())
    body = (root / "raw/official" / f"{code}_{period.year}.pdf").read_bytes()
    chosen = metadata["chosen"]
    checksum = hashlib.sha256(body).hexdigest()
    if not body.startswith(b"%PDF-") or metadata.get("pdf_sha256") != checksum:
        raise ValueError("original PDF hash/signature mismatch")
    if metadata.get("code") != code or chosen.get("secCode") != code[:6]:
        raise ValueError("original issuer identity mismatch")
    title = normalized(str(chosen.get("announcementTitle", "")))
    full_year_titles = (f"{period.year}年年度报告", f"{period.year}年度报告")
    if not any(candidate in title for candidate in full_year_titles) or any(
        w in title for w in ("摘要", "英文", "取消")
    ):
        raise ValueError("original is not the full annual report for this period")
    url = "https://static.cninfo.com.cn/" + str(chosen["adjunctUrl"])
    from investment_agent.evidence.disclosure_http import validate_url

    validate_url(url, "original")
    if metadata.get("url") != url:
        raise ValueError("original source URL mismatch")
    stamp = chosen.get("announcementTime")
    if isinstance(stamp, bool) or not isinstance(stamp, int):
        raise ValueError("original publication time is missing")
    published = datetime.fromtimestamp(stamp / 1000, UTC)
    front = normalized(page_text(body, 1))
    issuer_name = normalized(str(chosen.get("secName", "")))
    aliases = ISSUER_ALIASES.get(issuer_name, ())
    cover_identity = (
        code[:6] in front or issuer_name in front or any(alias in front for alias in aliases)
    )
    if not cover_identity:
        from pypdf import PdfReader

        total_pages = len(PdfReader(BytesIO(body)).pages)
        cover_identity = any(
            code[:6] in normalized(page_text(body, n))
            for n in range(2, min(8, total_pages) + 1)
        )
    cover_period = str(period.year) in front and (
        "年度报告" in front or "ANNUALREPORT" in front
    )
    if (
        not chosen.get("secName")
        or not cover_identity
        or not cover_period
    ):
        raise ValueError("original cover does not confirm issuer and annual period")
    return AnnualOriginal(code, period, body, metadata, checksum, published)


def checked_locator(original: AnnualOriginal, metric: str, proof: dict[str, Any]) -> dict[str, Any]:
    """Check exact excerpt, current column, unit and nearest statement heading."""
    page = proof["pdf_page"]
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("invalid original page")
    # Reports carry continued tables and excerpts can cross a page boundary.
    pieces = [page_text(original.body, n) for n in range(max(1, page - 2), page + 1)]
    pieces.append(page_text(original.body, page + 1))
    context = normalized("\n".join(pieces))
    excerpt = normalized(proof["locator_excerpt"])
    offset = context.find(excerpt)
    if offset < 0:
        raise ValueError("checklist excerpt is not present at the original locator")
    header = normalized(proof["header"])
    aliases = ALIASES.get(metric, ())
    if header not in aliases:
        raise ValueError("original field header does not identify the requested metric")
    at = excerpt.find(header)
    if at < 0:
        raise ValueError("field header is not present in the located excerpt")
    header_match = re.search(
        r"\s*".join(re.escape(c) for c in proof["header"] if not c.isspace()),
        proof["locator_excerpt"],
    )
    if header_match is None:
        raise ValueError("field header is not present in the original excerpt")
    after = proof["locator_excerpt"][header_match.end() :]
    number = re.search(r"[-+−]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)", after[:100])
    expected = Decimal(str(proof["parsed_value"]))
    if number is None or Decimal(number.group().replace(",", "").replace("−", "-")) != expected:
        raise ValueError("original current-column value does not match the checklist")
    prefix = context[: offset + at]
    headings = list(
        re.finditer(
            r"合并资产负债表|母公司资产负债表|合并现金流量表|母公司现金流量表|主要会计数据|主要财务指标",
            prefix,
        )
    )
    heading = headings[-1].group() if headings else ""
    if metric == "contract_liabilities" and heading != "合并资产负债表":
        raise ValueError("contract liabilities require the consolidated balance sheet")
    if metric == "operating_cash_flow" and heading != "合并现金流量表":
        raise ValueError("cash flow requires the consolidated cash flow statement")
    annual_parent_narrative = (
        metric == "net_profit_parent" and f"公司{original.period.year}年度" in prefix[-100:]
    )
    if (
        metric not in {"contract_liabilities", "operating_cash_flow"}
        and not heading.startswith("主要")
        and not annual_parent_narrative
    ):
        raise ValueError("annual issuer key-indicator table is not confirmed")
    if str(original.period.year) not in prefix or (
        "元" not in context[: offset + at + 100] and metric != "roe_weighted_annual"
    ):
        raise ValueError("annual current-column period or unit is not confirmed")
    return {
        "accounting_scope": "issuer_weighted_annual"
        if metric == "roe_weighted_annual"
        else "parent"
        if metric in {"net_profit_parent", "equity_parent"}
        else "consolidated",
        "header_context_hash": hashlib.sha256(prefix.encode()).hexdigest(),
        "check_rule_version": RULE,
    }


async def store_annual_original(
    session: AsyncSession, original: AnnualOriginal, listing_id: int, acquired_at: datetime
) -> DisclosureOriginal:
    """Idempotent trusted import into the existing owner-scoped original store."""
    owner = "local_owner"
    chosen = original.metadata["chosen"]
    item_id = str(
        uuid5(NAMESPACE_URL, f"peer-original-item/{listing_id}/{chosen['announcementId']}")
    )
    original_id = str(uuid5(NAMESPACE_URL, f"peer-original/{listing_id}/{original.pdf_hash}"))
    old = await session.get(DisclosureOriginal, original_id)
    if old is not None:
        return old
    metadata = {
        "id": item_id,
        "listing_id": listing_id,
        "source": "cninfo.v1",
        "ticker": original.code,
        "exchange": "SZSE" if original.code.endswith("SZ") else "SSE",
        "issuer_id": chosen["orgId"],
        "disclosure_id": chosen["announcementId"],
        "first_published_at": original.published_at.isoformat(),
        "source_url": original.metadata["url"],
        "title": chosen["announcementTitle"],
        "discovered_at": acquired_at.isoformat(),
        "verified_correction_refs": [],
        "verification": "unverified",
    }
    if await session.get(DisclosureItem, item_id) is None:
        session.add(
            DisclosureItem(
                id=item_id,
                owner_id=owner,
                listing_id=listing_id,
                identity_hash=hashlib.sha256(item_id.encode()).hexdigest(),
                payload=metadata,
            )
        )
    if await session.get(DisclosureBlob, original.pdf_hash) is None:
        session.add(DisclosureBlob(content_hash=original.pdf_hash, body=original.body))
    await session.flush()
    row = DisclosureOriginal(
        id=original_id,
        owner_id=owner,
        item_id=item_id,
        content_hash=original.pdf_hash,
        payload={
            **metadata,
            "id": original_id,
            "item_id": item_id,
            "content_hash": original.pdf_hash,
            "mime": "application/pdf",
            "size": len(original.body),
            "acquired_at": acquired_at.isoformat(),
            "available_from": acquired_at.isoformat(),
            "verification": "candidate",
            "prior_original_refs": [],
            "resolved_url": original.metadata["url"],
        },
    )
    session.add(row)
    await session.flush()
    return row


async def store_bank_supplemental_original(
    session: AsyncSession,
    *,
    code: str,
    body: bytes,
    source: dict[str, Any],
    listing_id: int,
    acquired_at: datetime,
) -> DisclosureOriginal:
    """Store a verified non-CNINFO bank proof source with its real identity.

    The caller validates the issuer-specific source contract before storage.
    This method deliberately preserves the source namespace instead of
    fabricating CNINFO announcement metadata.
    """
    digest = hashlib.sha256(body).hexdigest()
    if digest != source.get("pdf_sha256") or not body.startswith(b"%PDF-"):
        raise ValueError("supplemental original bytes do not match the source hash")
    source_kind = source.get("source_kind")
    disclosure_id = source.get("disclosure_id")
    if not isinstance(source_kind, str) or not source_kind or not isinstance(disclosure_id, str) or not disclosure_id:
        raise ValueError("supplemental original source namespace is missing")
    owner = "local_owner"
    namespaced_id = f"{source_kind}:{disclosure_id}"
    item_id = str(uuid5(NAMESPACE_URL, f"peer-bank-supplemental-item/{listing_id}/{namespaced_id}"))
    original_id = str(uuid5(NAMESPACE_URL, f"peer-bank-supplemental/{listing_id}/{namespaced_id}/{digest}"))
    old = await session.get(DisclosureOriginal, original_id)
    if old is not None:
        if old.owner_id != owner or old.content_hash != digest or old.payload.get("source_identity") != namespaced_id:
            raise ValueError("stored supplemental original identity conflicts with the source")
        return old
    acquired_iso = acquired_at.astimezone(UTC).isoformat()
    metadata = {
        "id": item_id,
        "listing_id": listing_id,
        "source": source_kind,
        "source_kind": source_kind,
        "source_key": source.get("source_key"),
        "ticker": code,
        "exchange": "SZSE" if code.endswith("SZ") else "SSE",
        "issuer_id": source.get("issuer_id"),
        "publisher": source.get("publisher"),
        "platform": source.get("platform"),
        "disclosure_id": disclosure_id,
        "source_identity": namespaced_id,
        "published_on": source.get("published_on"),
        "published_display": source.get("published_display"),
        "publication_precision": source.get("publication_precision"),
        "source_url": source.get("url"),
        "listing_url": source.get("listing_url"),
        "title": source.get("title"),
        "discovered_at": acquired_iso,
        "verification": "unverified",
    }
    if await session.get(DisclosureItem, item_id) is None:
        session.add(
            DisclosureItem(
                id=item_id,
                owner_id=owner,
                listing_id=listing_id,
                identity_hash=hashlib.sha256(namespaced_id.encode()).hexdigest(),
                payload=metadata,
            )
        )
    if await session.get(DisclosureBlob, digest) is None:
        session.add(DisclosureBlob(content_hash=digest, body=body))
    await session.flush()
    row = DisclosureOriginal(
        id=original_id,
        owner_id=owner,
        item_id=item_id,
        content_hash=digest,
        payload={
            **metadata,
            "id": original_id,
            "item_id": item_id,
            "content_hash": digest,
            "mime": "application/pdf",
            "size": len(body),
            "acquired_at": acquired_iso,
            "available_from": acquired_iso,
            "verification": "candidate",
            "prior_original_refs": [],
            "resolved_url": source["url"],
        },
    )
    session.add(row)
    await session.flush()
    return row


async def validate_field_original(
    session: AsyncSession, snapshot: Any, metric: str, *, cutoff: datetime | None = None
) -> bool:
    """Re-read the actual bytes; stored 'verified' flags cannot self-certify."""
    proof = snapshot.verification.get(metric, {})
    # Versioned dispatch keeps the baijiu annual-original rule and its proof
    # semantics intact. Bank proofs use their own bounded statement checks.
    if isinstance(proof, dict) and proof.get("check_rule_version") == "bank-annual-original/1":
        return await _validate_bank_field_original(session, snapshot, metric, proof, cutoff=cutoff)
    try:
        ref = ObjectRef.model_validate(proof["original_ref"])
        if ref.kind != "disclosure_original" or ref.revision != 1:
            return False
        row = await session.get(DisclosureOriginal, str(ref.id))
        if (
            row is None
            or row.owner_id != "local_owner"
            or row.payload["listing_id"] != snapshot.listing_id
        ):
            return False
        from investment_agent.domain.a_share import format_listing_ticker
        from investment_agent.storage.models.reference import Listing

        listing = await session.get(Listing, snapshot.listing_id)
        if listing is None or row.payload["ticker"] != format_listing_ticker(
            ticker=listing.ticker, market_code=listing.market_code
        ):
            return False
        identity = proof["report_identity"]
        if (
            identity.get("code") != row.payload["ticker"]
            or identity.get("period") != snapshot.report_period.isoformat()
            or identity.get("disclosure_id") != row.payload["disclosure_id"]
        ):
            return False
        blob = await session.get(DisclosureBlob, row.content_hash)
        if (
            blob is None
            or hashlib.sha256(blob.body).hexdigest() != proof["pdf_hash"]
            or row.content_hash != proof["pdf_hash"]
        ):
            return False
        if (
            proof["check_rule_version"] != RULE
            or not proof["checked_at"]
            or not proof["report_identity"]
        ):
            return False
        field = snapshot.fields[metric]
        captured = (
            snapshot.captured_at.replace(tzinfo=UTC)
            if snapshot.captured_at.tzinfo is None
            else snapshot.captured_at
        )
        times = [
            datetime.fromisoformat(row.payload[k])
            for k in ("first_published_at", "acquired_at", "available_from")
        ]
        times.append(datetime.fromisoformat(proof["checked_at"]))
        boundary = min(captured, cutoff) if cutoff is not None else captured
        if any(t.tzinfo is None or t > boundary for t in times):
            return False
        if proof.get("raw_unit") != ("percent" if metric == "roe_weighted_annual" else "CNY"):
            return False
        if proof.get("period") != snapshot.report_period.isoformat():
            return False
        expected = Decimal(field["value"])
        parsed = Decimal(proof["parsed_value"])
        if expected != (parsed / 100 if metric == "roe_weighted_annual" else parsed):
            return False
        if (
            hashlib.sha256(proof["locator_excerpt"].encode()).hexdigest()
            != proof["locator_excerpt_sha256"]
        ):
            return False
        original = AnnualOriginal(
            row.payload["ticker"],
            snapshot.report_period,
            blob.body,
            {},
            row.content_hash,
            datetime.fromisoformat(row.payload["first_published_at"]),
        )
        checks = checked_locator(original, metric, proof)
        return all(proof.get(k) == v for k, v in checks.items())
    except (KeyError, ValueError, TypeError, ImportError):
        return False


async def _validate_bank_field_original(
    session: AsyncSession, snapshot: Any, metric: str, proof: dict[str, Any], *, cutoff: datetime | None
) -> bool:
    """Revalidate one bank field against its owner-scoped stored PDF bytes."""
    try:
        from investment_agent.industry_peers.bank_proofs import (
            BANK_METRICS,
            BANK_RULE,
            checked_bank_locator,
        )

        if metric not in BANK_METRICS or proof.get("check_rule_version") != BANK_RULE:
            return False
        ref = ObjectRef.model_validate(proof["original_ref"])
        if ref.kind != "disclosure_original" or ref.revision != 1:
            return False
        row = await session.get(DisclosureOriginal, str(ref.id))
        if row is None or row.owner_id != "local_owner" or row.payload["listing_id"] != snapshot.listing_id:
            return False
        if not isinstance(row.payload.get("title"), str) or not row.payload["title"].strip():
            return False
        if not isinstance(row.payload.get("source_url"), str) or not row.payload["source_url"].strip():
            return False
        from investment_agent.domain.a_share import format_listing_ticker
        from investment_agent.storage.models.reference import Listing

        listing = await session.get(Listing, snapshot.listing_id)
        if listing is None:
            return False
        ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
        if row.payload["ticker"] != ticker:
            return False
        period = snapshot.report_period.isoformat()
        identity = proof["report_identity"]
        if (
            identity.get("code") != ticker
            or identity.get("period") != period
            or identity.get("disclosure_id") != row.payload["disclosure_id"]
            or proof.get("period") != period
        ):
            return False
        blob = await session.get(DisclosureBlob, row.content_hash)
        if (
            blob is None
            or hashlib.sha256(blob.body).hexdigest() != proof["pdf_hash"]
            or row.content_hash != proof["pdf_hash"]
        ):
            return False
        field = snapshot.fields[metric]
        parsed = Decimal(str(proof["parsed_value"]))
        if field.get("value") is None or Decimal(str(field["value"])) != parsed:
            return False
        if metric == "equity_parent":
            if field.get("unit") != "CNY" or field.get("currency") != "CNY":
                return False
            if field.get("period", {}).get("basis") != "point_in_time":
                return False
        elif (
            field.get("unit") != "fraction"
            or field.get("period", {}).get("basis") != "annual_ratio"
        ):
            return False
        expected_start = period if metric == "equity_parent" else f"{snapshot.report_period.year}-01-01"
        if field.get("period", {}).get("start") != expected_start:
            return False
        if field.get("period", {}).get("end") != period:
            return False
        captured = snapshot.captured_at.replace(tzinfo=UTC) if snapshot.captured_at.tzinfo is None else snapshot.captured_at
        boundary = min(captured, cutoff) if cutoff is not None else captured
        audit = proof.get("audit_record")
        if not _bank_readback_times_valid(proof, audit, boundary):
            return False
        times = [
            datetime.fromisoformat(row.payload[key])
            for key in ("first_published_at", "acquired_at", "available_from")
        ]
        times.append(datetime.fromisoformat(proof["checked_at"]))
        if any(value.tzinfo is None or value > boundary for value in times):
            return False
        from investment_agent.industry_peers.bank_proofs import BANK_ISSUER_NAMES

        issuer_name = BANK_ISSUER_NAMES.get(ticker)
        if issuer_name is None:
            return False
        original = AnnualOriginal(
            ticker, snapshot.report_period, blob.body, {"chosen": {"announcementId": row.payload["disclosure_id"],
            "announcementTitle": row.payload["title"], "secName": issuer_name}, "url": row.payload["source_url"]},
            row.content_hash, datetime.fromisoformat(row.payload["first_published_at"]),
        )
        supplemental_originals = await _read_bank_supplemental_originals(
            session, snapshot, proof, boundary=boundary, ticker=ticker
        )
        checks = checked_bank_locator(
            original, metric, proof, supplemental_originals=supplemental_originals
        )
        return all(proof.get(key) == value for key, value in checks.items())
    except (KeyError, ValueError, TypeError, InvalidOperation, ImportError):
        return False


async def _read_bank_supplemental_originals(
    session: AsyncSession,
    snapshot: Any,
    proof: dict[str, Any],
    *,
    boundary: datetime,
    ticker: str,
) -> dict[str, AnnualOriginal]:
    """Reload and authenticate supplemental bank source bytes by stored ref."""
    descriptors = proof.get("supplemental_originals", [])
    audit = proof.get("audit_record")
    if not isinstance(descriptors, list) or not isinstance(audit, dict):
        raise ValueError("supplemental bank original descriptors are invalid")
    if descriptors != audit.get("supplemental_originals", []):
        raise ValueError("supplemental bank originals differ between proof and audit")
    if not descriptors:
        return {}
    if ticker != "002142.SZ" or snapshot.report_period.isoformat() != "2025-12-31" or len(descriptors) != 1:
        raise ValueError("only the approved FY2025 Ningbo supplemental source is supported")
    from investment_agent.industry_peers.bank_scope_bridges import ningbo_final_source_identity

    result: dict[str, AnnualOriginal] = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise ValueError("supplemental bank original descriptor must be an object")
        source_key = descriptor.get("source_key")
        expected = ningbo_final_source_identity()
        if source_key != "ningbo_final_scope":
            raise ValueError("supplemental bank original source key is unsupported")
        if any(descriptor.get(key) != value for key, value in expected.items()):
            raise ValueError("supplemental bank original descriptor identity mismatch")
        ref = ObjectRef.model_validate(descriptor["original_ref"])
        if ref.kind != "disclosure_original" or ref.revision != 1:
            raise ValueError("supplemental bank original ref is invalid")
        row = await session.get(DisclosureOriginal, str(ref.id))
        if (
            row is None
            or row.owner_id != "local_owner"
            or row.payload.get("listing_id") != snapshot.listing_id
            or row.payload.get("ticker") != ticker
            or row.payload.get("source_identity") != f"{expected['source_kind']}:{expected['disclosure_id']}"
            or row.payload.get("source") != expected["source_kind"]
            or row.payload.get("source_kind") != expected["source_kind"]
            or row.payload.get("source_key") != "ningbo_final_scope"
            or row.payload.get("title") != expected["title"]
            or row.payload.get("publisher") != expected["publisher"]
            or row.payload.get("platform") != expected["platform"]
            or row.payload.get("source_url") != expected["url"]
            or row.payload.get("listing_url") != expected["listing_url"]
            or row.payload.get("published_on") != expected["published_on"]
            or row.payload.get("published_display") != expected["published_display"]
            or row.payload.get("publication_precision") != expected["publication_precision"]
            or row.payload.get("disclosure_id") != expected["disclosure_id"]
            or row.payload.get("resolved_url") != expected["url"]
            or row.content_hash != descriptor.get("pdf_sha256")
            or row.payload.get("content_hash") != descriptor.get("pdf_sha256")
        ):
            raise ValueError("supplemental bank original stored identity/owner does not match")
        item = await session.get(DisclosureItem, row.item_id)
        if item is None or item.owner_id != "local_owner" or item.listing_id != snapshot.listing_id:
            raise ValueError("supplemental bank original metadata owner/listing does not match")
        if (
            item.payload.get("ticker") != ticker
            or item.payload.get("source_identity") != row.payload.get("source_identity")
            or item.payload.get("disclosure_id") != expected["disclosure_id"]
            or item.payload.get("source_key") != "ningbo_final_scope"
            or item.payload.get("source_url") != expected["url"]
        ):
            raise ValueError("supplemental bank disclosure item identity does not match")
        if (
            row.payload.get("acquired_at") != descriptor.get("acquired_at")
            or row.payload.get("available_from") != descriptor.get("available_from")
        ):
            raise ValueError("supplemental bank original acquisition metadata mismatch")
        try:
            acquired = datetime.fromisoformat(descriptor["acquired_at"])
            available = datetime.fromisoformat(descriptor["available_from"])
            publication_day = date.fromisoformat(descriptor["published_on"])
            checked = datetime.fromisoformat(proof["checked_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("supplemental bank original date/time is invalid") from exc
        if any(value.tzinfo is None or value.utcoffset() is None for value in (acquired, available, checked, boundary)):
            raise ValueError("supplemental bank original times must be timezone-aware")
        if acquired != available or acquired > checked or acquired > boundary:
            raise ValueError("supplemental bank original was unavailable at field check/capture")
        if publication_day > checked.astimezone(UTC).date():
            raise ValueError("supplemental bank original publication day is after field check")
        if descriptor.get("original_ref") != {"kind": "disclosure_original", "id": row.id, "revision": 1}:
            raise ValueError("supplemental bank original descriptor ref does not match stored row")
        blob = await session.get(DisclosureBlob, row.content_hash)
        if blob is None or hashlib.sha256(blob.body).hexdigest() != row.content_hash:
            raise ValueError("supplemental bank original bytes are missing or hash-mismatched")
        result[source_key] = AnnualOriginal(
            ticker,
            snapshot.report_period,
            blob.body,
            {"source": expected["source_kind"]},
            row.content_hash,
            acquired,
        )
    return result


def _bank_readback_times_valid(proof: dict[str, Any], audit: Any, boundary: datetime) -> bool:
    """Keep the proof review time bound to its immutable audit record."""
    if not isinstance(audit, dict):
        return False
    try:
        proof_checked = datetime.fromisoformat(proof["checked_at"])
        audit_checked = datetime.fromisoformat(audit["checked_at"])
        acquired = datetime.fromisoformat(audit["acquired_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if any(value.tzinfo is None for value in (proof_checked, audit_checked, acquired, boundary)):
        return False
    return (
        proof_checked == audit_checked
        and acquired <= audit_checked
        and acquired <= boundary
        and audit_checked <= boundary
    )
