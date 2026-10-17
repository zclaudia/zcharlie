"""Synthetic locator tests for the cross-year bank annual rule."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers import bank_annual_v2 as v2
from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.bank_annual_v2 import bank_annual_pdf


@pytest.mark.parametrize(("year", "value"), [(2023, "11.25"), (2024, "12.50")])
def test_v2_locator_accepts_distinct_report_years(year: int, value: str) -> None:
    body = bank_annual_pdf(year, value, "10.00")
    page = bank_page_text(body, 1)
    proof = {
        "pdf_page": 1,
        "table_title": "财务指标",
        "period_header": f"{year} {year - 1}",
        "current_column": f"本集团{year}年度",
        "header": "加权平均净资产收益率",
        "accounting_scope": "group_consolidated",
        "raw_value": value,
        "raw_unit": "percent",
        "parsed_value": str(float(value) / 100),
        "locator_excerpt": page,
        "locator_excerpt_sha256": hashlib.sha256(page.encode()).hexdigest(),
        "definition_or_footnote": "本集团",
    }
    v2._check_locator(body, date(year, 12, 31), "roe_weighted_annual", proof)


@pytest.mark.parametrize(
    ("changes", "metric"),
    [
        ({"period_header": "2022年度", "current_column": "本集团2022年度"}, "roe_weighted_annual"),
        ({"raw_unit": "CNY_million"}, "roe_weighted_annual"),
        ({"current_column": "本行2024年度"}, "roe_weighted_annual"),
        ({"current_column": "2023"}, "roe_weighted_annual"),
        ({"header": "股东权益合计"}, "equity_parent"),
    ],
)
def test_v2_locator_rejects_unproven_semantics(changes: dict[str, str], metric: str) -> None:
    body = bank_annual_pdf(2024, "12.50", "10.00")
    page = bank_page_text(body, 1)
    proof: dict[str, object] = {
        "pdf_page": 1,
        "table_title": "财务指标",
        "period_header": "2024 2023",
        "current_column": "本集团2024年度",
        "header": "加权平均净资产收益率",
        "accounting_scope": "group_consolidated",
        "raw_value": "12.50",
        "raw_unit": "percent",
        "parsed_value": "0.125",
        "locator_excerpt": page,
        "locator_excerpt_sha256": hashlib.sha256(page.encode()).hexdigest(),
        "definition_or_footnote": "本集团",
    }
    proof.update(changes)
    with pytest.raises(ValueError):
        v2._check_locator(body, date(2024, 12, 31), metric, proof)


def _eight_page_pdf(year: int, current: str = "12.50", comparative: str = "10.00") -> bytes:
    """Use the small real PDF fixture and pad it to the reader's cover-page window."""
    reader = PdfReader(BytesIO(bank_annual_pdf(year, current, comparative)))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    for _ in range(7):
        writer.add_blank_page(width=600, height=800)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


async def _store_v2_report(
    session: AsyncSession,
    *,
    pdf_year: int,
    report_year: int,
    acquired_at: datetime,
    checked_at: datetime,
) -> tuple[Listing, date, dict[str, Any]]:
    """Store a synthetic annual original with auditable DB ownership and bytes."""
    await session.merge(
        Market(code="SSE", name="上海证券交易所", timezone="Asia/Shanghai", currency="CNY")
    )
    company = Company(canonical_name="工商银行")
    session.add(company)
    await session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="601398", is_primary=True)
    session.add(listing)
    await session.flush()

    body = _eight_page_pdf(pdf_year)
    pdf_hash = hashlib.sha256(body).hexdigest()
    source_url = (
        f"https://static.cninfo.com.cn/finalpage/{report_year + 1}-03-27/annual-{uuid4()}.PDF"
    )
    disclosure_id, item_id, original_id = f"annual-{uuid4()}", str(uuid4()), str(uuid4())
    published = datetime(report_year + 1, 3, 27, tzinfo=UTC)
    available = max(acquired_at, published)
    session.add(DisclosureBlob(content_hash=pdf_hash, body=body))
    session.add(
        DisclosureItem(
            id=item_id,
            owner_id="local_owner",
            listing_id=listing.id,
            identity_hash=hashlib.sha256(disclosure_id.encode()).hexdigest(),
            payload={
                "ticker": "601398.SH",
                "source_url": source_url,
                "disclosure_id": disclosure_id,
            },
        )
    )
    session.add(
        DisclosureOriginal(
            id=original_id,
            owner_id="local_owner",
            item_id=item_id,
            content_hash=pdf_hash,
            payload={
                "listing_id": listing.id,
                "ticker": "601398.SH",
                "disclosure_id": disclosure_id,
                "title": f"中国工商银行股份有限公司{report_year}年度报告",
                "source_url": source_url,
                "resolved_url": source_url,
                "first_published_at": published.isoformat(),
                "acquired_at": acquired_at.isoformat(),
                "available_from": available.isoformat(),
            },
        )
    )
    await session.flush()

    period = date(report_year, 12, 31)
    excerpt = bank_page_text(body, 1)
    audit = {
        "check_rule_version": v2.BANK_ANNUAL_V2_RULE,
        "status": "verified",
        "metric": "roe_weighted_annual",
        "period": period.isoformat(),
        "report_identity": {
            "code": "601398.SH",
            "period": period.isoformat(),
            "disclosure_id": disclosure_id,
        },
        "original_ref": {"kind": "disclosure_original", "id": original_id, "revision": 1},
        "pdf_hash": pdf_hash,
        "pdf_page": 1,
        "table_title": "财务指标",
        "period_header": f"{report_year} {report_year - 1}",
        "current_column": f"本集团{report_year}年度",
        "header": "加权平均净资产收益率",
        "raw_value": "12.50",
        "raw_unit": "percent",
        "parsed_value": "0.125",
        "accounting_scope": "group_consolidated",
        "source_url": source_url,
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "definition_or_footnote": "本集团",
        "checked_at": checked_at.isoformat(),
        "acquired_at": acquired_at.isoformat(),
    }
    return listing, period, audit


@pytest.mark.asyncio
async def test_v2_rejects_fy2025_pdf_as_proof_of_fy2024_comparative_column(
    db_session: AsyncSession,
) -> None:
    captured = datetime(2026, 2, 1, tzinfo=UTC)
    listing, period, audit = await _store_v2_report(
        db_session,
        pdf_year=2025,
        report_year=2024,
        acquired_at=datetime(2025, 4, 1, tzinfo=UTC),
        checked_at=datetime(2025, 4, 2, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="cover year"):
        await v2.build_bank_annual_v2_field(
            db_session,
            listing_id=listing.id,
            report_period=period,
            audit=audit,
            captured_at=captured,
        )


@pytest.mark.asyncio
async def test_v2_rejects_audit_checked_before_original_was_acquired(
    db_session: AsyncSession,
) -> None:
    captured = datetime(2025, 4, 10, tzinfo=UTC)
    listing, period, audit = await _store_v2_report(
        db_session,
        pdf_year=2024,
        report_year=2024,
        acquired_at=datetime(2025, 4, 1, tzinfo=UTC),
        checked_at=datetime(2025, 3, 31, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="audit time"):
        await v2.build_bank_annual_v2_field(
            db_session,
            listing_id=listing.id,
            report_period=period,
            audit=audit,
            captured_at=captured,
        )
