import copy
import hashlib
from datetime import UTC, date, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from pypdf import PdfReader, PdfWriter

from investment_agent.industry_peers import bank_annual_v2 as v2
from investment_agent.industry_peers.bank_annual_review import (
    append_review_event,
    build_rollover_snapshot,
    list_pending_bank_annual_reviews,
    load_bank_annual_review_events,
    resolve_bank_annual_review,
    review_id,
)
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.bank_annual_v2 import bank_annual_pdf
from tests.unit.test_bank_review_states import (
    METRIC as REVIEW_METRIC,
)
from tests.unit.test_bank_review_states import (
    PERIOD as REVIEW_PERIOD,
)
from tests.unit.test_bank_review_states import (
    _bank_listing,
    _baseline,
    _report,
    _state,
    _supplier_candidate,
)


def test_review_identity_is_semantic_and_year_metric_bound():
    args = dict(
        listing_id=12,
        report_period=date(2024, 12, 31),
        metric="npl_ratio",
        old_snapshot_id="old",
        candidate_snapshot_id="new",
        candidate_source_version="rev1",
    )
    assert review_id(**args) == review_id(**args)
    assert review_id(**args) != review_id(**{**args, "metric": "equity_parent"})
    assert review_id(**args) != review_id(**{**args, "report_period": date(2025, 12, 31)})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "captured_at", "message"),
    [
        (date(2024, 12, 31), datetime(2025, 5, 1), "timezone-aware"),
        (date(2024, 12, 31), datetime(2099, 5, 1, tzinfo=UTC), "future"),
        (date(2024, 6, 30), datetime(2025, 5, 1, tzinfo=UTC), "December 31"),
    ],
)
async def test_snapshot_rejects_bad_time_or_annual_period_before_empty_audit(
    db_session, period, captured_at, message
):
    with pytest.raises(ValueError, match=message):
        await build_rollover_snapshot(
            db_session,
            listing_id=1,
            company_id=1,
            report_period=period,
            audit={"fields": {}},
            captured_at=captured_at,
        )


@pytest.mark.asyncio
async def test_forged_resolve_event_does_not_hide_pending_review(db_session):
    company, listing = await _bank_listing(db_session)
    old, _ = await _baseline(db_session, company, listing)
    await _supplier_candidate(db_session, old, value="0.1325")
    review = next(
        row
        for row in await load_bank_annual_review_events(db_session, listing_id=listing.id)
        if row.get("event_type") == "review"
    )
    await append_review_event(
        db_session,
        event={
            "event_type": "resolve",
            "review_id": review["review_id"],
            "listing_id": listing.id,
            "company_id": company.id,
            "report_period": REVIEW_PERIOD.isoformat(),
            "metric": REVIEW_METRIC,
            "old_snapshot_id": old.id,
            "candidate_snapshot_id": review["candidate_snapshot_id"],
            "resulting_snapshot_id": "forged-nonexistent-snapshot",
            "decision": "reject_supplier",
            "checked_at": datetime(2025, 4, 1, tzinfo=UTC).isoformat(),
            "created_at": datetime(2025, 4, 1, tzinfo=UTC).isoformat(),
        },
    )
    pending = await list_pending_bank_annual_reviews(
        db_session, listing_id=listing.id, report_period=REVIEW_PERIOD
    )
    assert any(row["review_id"] == review["review_id"] for row in pending)


@pytest.mark.asyncio
async def test_review_event_is_persisted_and_idempotent(db_session):
    event = {
        "event_type": "review",
        "review_id": "review-1",
        "listing_id": 12,
        "report_period": "2024-12-31",
        "metric": "npl_ratio",
        "decision": None,
    }
    first = await append_review_event(db_session, event=event, review=True)
    duplicate = await append_review_event(db_session, event=event, review=True)
    assert first == duplicate
    rows = await load_bank_annual_review_events(db_session, listing_id=12)
    assert len(rows) == 1
    assert rows[0]["review_id"] == "review-1"


@pytest.mark.asyncio
async def test_v2_rollover_rechecks_pdf_bytes_and_persists_complete_snapshot(
    db_session,
):
    captured = datetime(2025, 5, 1, tzinfo=UTC)
    await db_session.merge(
        Market(code="SSE", name="上海证券交易所", timezone="Asia/Shanghai", currency="CNY")
    )
    company = Company(canonical_name="工商银行")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="601398", is_primary=True)
    db_session.add(listing)
    await db_session.flush()
    body = bank_annual_pdf(2024, "12.50", "10.00")
    pdf_hash = hashlib.sha256(body).hexdigest()
    source_url = "https://static.cninfo.com.cn/finalpage/2025-03-27/filing2024.PDF"
    from investment_agent.industry_peers.bank_pdf_text import bank_page_text

    page = bank_page_text(body, 1)
    item_id, original_id = str(uuid4()), str(uuid4())
    db_session.add(DisclosureBlob(content_hash=pdf_hash, body=body))
    db_session.add(
        DisclosureItem(
            id=item_id,
            owner_id="local_owner",
            listing_id=listing.id,
            identity_hash="a" * 64,
            payload={
                "ticker": "601398.SH",
                "source_url": source_url,
                "disclosure_id": "filing-2024",
            },
        )
    )
    db_session.add(
        DisclosureOriginal(
            id=original_id,
            owner_id="local_owner",
            item_id=item_id,
            content_hash=pdf_hash,
            payload={
                "listing_id": listing.id,
                "ticker": "601398.SH",
                "disclosure_id": "filing-2024",
                "title": "工商银行2024年度报告",
                "source_url": source_url,
                "resolved_url": source_url,
                "first_published_at": "2025-03-27T00:00:00+00:00",
                "acquired_at": "2025-04-01T00:00:00+00:00",
                "scope_context": None,
                "available_from": "2025-04-01T00:00:00+00:00",
            },
        )
    )
    await db_session.flush()
    excerpt = page
    audit = {
        "schema": "bank-annual-review-audit/2",
        "fields": {
            "roe_weighted_annual": {
                "check_rule_version": v2.BANK_ANNUAL_V2_RULE,
                "status": "verified",
                "metric": "roe_weighted_annual",
                "period": "2024-12-31",
                "report_identity": {
                    "code": "601398.SH",
                    "period": "2024-12-31",
                    "disclosure_id": "filing-2024",
                },
                "original_ref": {"kind": "disclosure_original", "id": original_id, "revision": 1},
                "pdf_hash": pdf_hash,
                "pdf_page": 1,
                "table_title": "财务指标",
                "period_header": "2024 2023",
                "current_column": "2024",
                "header": "加权平均净资产收益率",
                "raw_value": "12.50",
                "raw_unit": "percent",
                "parsed_value": "0.125",
                "accounting_scope": "group_consolidated",
                "source_url": source_url,
                "locator_excerpt": excerpt,
                "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                "definition_or_footnote": "本集团",
                "checked_at": captured.isoformat(),
                "acquired_at": "2025-04-01T00:00:00+00:00",
            }
        },
    }
    snapshot, proofs = await build_rollover_snapshot(
        db_session,
        listing_id=listing.id,
        company_id=company.id,
        report_period=date(2024, 12, 31),
        audit=audit,
        captured_at=captured,
    )
    assert set(snapshot.fields) == {
        "roe_weighted_annual",
        "net_interest_margin",
        "npl_ratio",
        "provision_coverage",
        "equity_parent",
    }
    assert snapshot.fields["roe_weighted_annual"]["verification"] == "verified", proofs[
        "roe_weighted_annual"
    ]
    assert snapshot.fields["equity_parent"]["period"]["basis"] == "point_in_time"
    assert all(
        snapshot.fields[m]["verification"] == "candidate"
        for m in set(snapshot.fields) - {"roe_weighted_annual"}
    )
    db_session.add(snapshot)
    await db_session.flush()
    assert await v2.validate_bank_annual_v2(
        db_session, snapshot, "roe_weighted_annual", proofs["roe_weighted_annual"], cutoff=captured
    )
    db_session.add(snapshot)
    await db_session.flush()
    candidate_fields = copy.deepcopy(snapshot.fields)
    candidate_fields["roe_weighted_annual"]["value"] = "0.13"
    candidate_fields["roe_weighted_annual"]["verification"] = "candidate"
    candidate = PeerAnnualSnapshot(
        id=str(uuid4()),
        company_id=company.id,
        listing_id=listing.id,
        report_period=date(2024, 12, 31),
        provider="eastmoney",
        adapter_version="bank-annual-v1",
        captured_at=captured,
        content_hash="c" * 64,
        semantic_hash="c" * 64,
        fields=candidate_fields,
        verification=copy.deepcopy(snapshot.verification),
    )
    db_session.add(candidate)
    await db_session.flush()
    pending_id = review_id(
        listing_id=listing.id,
        report_period=date(2024, 12, 31),
        metric="roe_weighted_annual",
        old_snapshot_id=snapshot.id,
        candidate_snapshot_id=candidate.id,
        candidate_source_version="supplier-rev-2",
    )
    await append_review_event(
        db_session,
        review=True,
        event={
            "event_type": "review",
            "review_id": pending_id,
            "code": "601398.SH",
            "listing_id": listing.id,
            "company_id": company.id,
            "report_period": "2024-12-31",
            "metric": "roe_weighted_annual",
            "old_snapshot_id": snapshot.id,
            "candidate_snapshot_id": candidate.id,
            "resulting_snapshot_id": candidate.id,
            "old_content_hash": snapshot.content_hash,
            "candidate_content_hash": candidate.content_hash,
            "candidate_source_version": "supplier-rev-2",
            "decision": None,
        },
    )
    resolved = await resolve_bank_annual_review(
        db_session,
        review_id_value=pending_id,
        audit={
            **audit,
            "listing_id": listing.id,
            "code": listing.ticker,
            "report_period": "2024-12-31",
        },
        decision="reject_supplier",
        checked_at=datetime(2025, 5, 1, 1, tzinfo=UTC),
    )
    assert resolved["decision"] == "reject_supplier"
    journal = await load_bank_annual_review_events(db_session, listing_id=listing.id)
    assert any(
        row.get("event_type") == "resolve" and row.get("review_id") == pending_id for row in journal
    )


async def _revised_report_audit(
    session,
    listing: Listing,
    *,
    raw_value: str = "13.25",
    title: str = "中国工商银行股份有限公司2024年度报告（修订稿）",
    published_at: str = "2025-04-05T00:00:00+00:00",
) -> dict:
    """Persist a separately hashed small PDF revision and bind its real locator."""
    from investment_agent.industry_peers.bank_pdf_text import bank_page_text

    raw = bank_annual_pdf(2024, raw_value, "10.00")
    writer = PdfWriter()
    writer.append_pages_from_reader(PdfReader(BytesIO(raw)))
    while len(writer.pages) < 8:
        writer.add_blank_page(width=600, height=800)
    stream = BytesIO()
    writer.write(stream)
    body = stream.getvalue()
    pdf_hash = hashlib.sha256(body).hexdigest()
    source_url = "https://static.cninfo.com.cn/finalpage/2025-04-05/revised-2024-report.PDF"
    disclosure_id = f"filing-revised-{uuid4().hex}"
    item_id, original_id = str(uuid4()), str(uuid4())
    ticker = f"{listing.ticker}.SH"
    session.add(DisclosureBlob(content_hash=pdf_hash, body=body))
    session.add(
        DisclosureItem(
            id=item_id,
            owner_id="local_owner",
            listing_id=listing.id,
            identity_hash=hashlib.sha256(item_id.encode()).hexdigest(),
            payload={"ticker": ticker, "source_url": source_url, "disclosure_id": disclosure_id},
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
                "ticker": ticker,
                "disclosure_id": disclosure_id,
                "title": title,
                "source_url": source_url,
                "resolved_url": source_url,
                "first_published_at": published_at,
                "acquired_at": "2025-04-05T01:00:00+00:00",
                "available_from": "2025-04-05T01:00:00+00:00",
            },
        )
    )
    await session.flush()
    from investment_agent.industry_peers.bank_annual_v2 import BANK_ANNUAL_V2_RULE

    excerpt = bank_page_text(body, 1)
    checked_at = datetime(2025, 4, 6, 12, tzinfo=UTC)
    field = {
        "check_rule_version": BANK_ANNUAL_V2_RULE,
        "status": "verified",
        "metric": REVIEW_METRIC,
        "period": REVIEW_PERIOD.isoformat(),
        "report_identity": {
            "code": ticker,
            "period": REVIEW_PERIOD.isoformat(),
            "disclosure_id": disclosure_id,
        },
        "original_ref": {"kind": "disclosure_original", "id": original_id, "revision": 1},
        "pdf_hash": pdf_hash,
        "pdf_page": 1,
        "table_title": "财务指标",
        "period_header": "2024 2023",
        "current_column": "本集团2024年度",
        "header": "加权平均净资产收益率",
        "raw_value": raw_value,
        "raw_unit": "percent",
        "parsed_value": str(float(raw_value) / 100),
        "accounting_scope": "group_consolidated",
        "source_url": source_url,
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "definition_or_footnote": "本集团",
        "scope_context": None,
        "checked_at": checked_at.isoformat(),
        "acquired_at": "2025-04-05T01:00:00+00:00",
        "source_version": "cninfo-revised-full-report-2024",
        "revision_basis": "official_full_report_revision",
    }
    return {
        "schema": "bank-annual-review-audit/2",
        "listing_id": listing.id,
        "code": listing.ticker,
        "report_period": REVIEW_PERIOD.isoformat(),
        "fields": {REVIEW_METRIC: field},
    }


@pytest.mark.asyncio
async def test_accept_original_revised_pdf_creates_verified_correction_and_binds_replay(
    db_session,
):
    company, listing = await _bank_listing(db_session)
    old, _ = await _baseline(db_session, company, listing)
    candidate = await _supplier_candidate(db_session, old, value="0.1325")
    review = next(
        row
        for row in await load_bank_annual_review_events(db_session, listing_id=listing.id)
        if row.get("event_type") == "review"
    )
    audit = await _revised_report_audit(db_session, listing)
    resolved = await resolve_bank_annual_review(
        db_session,
        review_id_value=review["review_id"],
        audit=audit,
        decision="accept_original",
        checked_at=datetime(2025, 4, 6, 12, tzinfo=UTC),
    )
    resolution_row = await db_session.get(PeerAnnualSnapshot, resolved["snapshot_id"])
    assert resolution_row is not None
    assert resolution_row.fields[REVIEW_METRIC]["value"] == candidate.fields[REVIEW_METRIC]["value"]
    assert resolution_row.fields[REVIEW_METRIC]["verification"] == "verified"

    report = await _report(db_session, company.id)
    assert report.status == "corrected"
    assert _state(report, old).state == "corrected"
    assert _state(report, candidate).state == "corrected"
    assert _state(report, resolution_row).state == "correction"
    assert _state(report, resolution_row).corrects

    changed_request = copy.deepcopy(audit)
    changed_request["reviewer_note"] = "different request under same review"
    with pytest.raises(ValueError, match="different decision or audit"):
        await resolve_bank_annual_review(
            db_session,
            review_id_value=review["review_id"],
            audit=changed_request,
            decision="accept_original",
            checked_at=datetime(2025, 4, 6, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "published_at", "raw_value", "message"),
    [
        (
            "中国工商银行股份有限公司2024年度报告",
            "2025-04-05T00:00:00+00:00",
            "13.25",
            "newer official full-report revision",
        ),
        (
            "中国工商银行股份有限公司2024年度报告（修订稿）",
            "2025-03-26T00:00:00+00:00",
            "13.25",
            "newer official full-report revision",
        ),
        (
            "中国工商银行股份有限公司2024年度报告（修订稿）",
            "2025-04-05T00:00:00+00:00",
            "14.00",
            "match the candidate",
        ),
    ],
)
async def test_accept_original_rejects_unproven_revision_or_candidate_mismatch(
    db_session, title: str, published_at: str, raw_value: str, message: str
):
    company, listing = await _bank_listing(db_session)
    old, _ = await _baseline(db_session, company, listing)
    await _supplier_candidate(db_session, old, value="0.1325")
    review = next(
        row
        for row in await load_bank_annual_review_events(db_session, listing_id=listing.id)
        if row.get("event_type") == "review"
    )
    audit = await _revised_report_audit(
        db_session, listing, raw_value=raw_value, title=title, published_at=published_at
    )
    with pytest.raises(ValueError, match=message):
        await resolve_bank_annual_review(
            db_session,
            review_id_value=review["review_id"],
            audit=audit,
            decision="accept_original",
            checked_at=datetime(2025, 4, 6, 12, tzinfo=UTC),
        )
