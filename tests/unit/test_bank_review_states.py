"""Trust-boundary checks for bank annual review journals and corrections."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers.bank_annual_review import (
    append_review_event,
    build_rollover_snapshot,
    load_bank_annual_review_events,
    resolve_bank_annual_review,
)
from investment_agent.industry_peers.bank_review_states import record_bank_online_reviews
from investment_agent.industry_peers.evidence import (
    PeerFieldConflictReport,
    PeerFieldVersionState,
    peer_field_states,
)
from investment_agent.industry_peers.hashing import canonical_json
from investment_agent.industry_peers.originals import validate_field_original
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.investor import MethodArtifact
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.bank_annual_v2 import bank_annual_pdf

PERIOD = date(2024, 12, 31)
METRIC = "roe_weighted_annual"
T0 = datetime(2025, 4, 3, 12, tzinfo=UTC)
T1 = datetime(2025, 4, 4, 12, tzinfo=UTC)
T2 = datetime(2025, 4, 6, 12, tzinfo=UTC)
T3 = datetime(2025, 4, 8, 12, tzinfo=UTC)


async def _bank_listing(session: AsyncSession) -> tuple[Company, Listing]:
    await session.merge(
        Market(code="SSE", name="上海证券交易所", timezone="Asia/Shanghai", currency="CNY")
    )
    company = Company(canonical_name="工商银行")
    session.add(company)
    await session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="601398", is_primary=True)
    session.add(listing)
    await session.flush()
    return company, listing


async def _stored_audit(
    session: AsyncSession, listing: Listing, *, raw_value: str = "12.50"
) -> tuple[dict[str, Any], dict[str, Any]]:
    captured = datetime(2025, 4, 2, 12, tzinfo=UTC)
    body = bank_annual_pdf(2024, raw_value, "10.00")
    writer = PdfWriter()
    writer.append_pages_from_reader(PdfReader(BytesIO(body)))
    while len(writer.pages) < 8:
        writer.add_blank_page(width=600, height=800)
    output = BytesIO()
    writer.write(output)
    body = output.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    source_url = f"https://static.cninfo.com.cn/finalpage/2025-03-27/annual-{uuid4().hex}.PDF"
    item_id = str(uuid4())
    original_id = str(uuid4())
    db_listing = f"{listing.ticker}.SH"
    disclosure_id = f"filing-{uuid4().hex}"
    session.add(DisclosureBlob(content_hash=digest, body=body))
    session.add(
        DisclosureItem(
            id=item_id,
            owner_id="local_owner",
            listing_id=listing.id,
            identity_hash=hashlib.sha256(item_id.encode()).hexdigest(),
            payload={
                "ticker": db_listing,
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
            content_hash=digest,
            payload={
                "listing_id": listing.id,
                "ticker": db_listing,
                "disclosure_id": disclosure_id,
                "title": "中国工商银行股份有限公司2024年度报告",
                "source_url": source_url,
                "resolved_url": source_url,
                "first_published_at": "2025-03-27T00:00:00+00:00",
                "acquired_at": "2025-04-01T00:00:00+00:00",
                "available_from": "2025-04-01T00:00:00+00:00",
            },
        )
    )
    await session.flush()
    from investment_agent.industry_peers.bank_annual_v2 import BANK_ANNUAL_V2_RULE
    from investment_agent.industry_peers.bank_pdf_text import bank_page_text

    excerpt = bank_page_text(body, 1)
    audit_field = {
        "check_rule_version": BANK_ANNUAL_V2_RULE,
        "status": "verified",
        "metric": METRIC,
        "period": PERIOD.isoformat(),
        "report_identity": {
            "code": db_listing,
            "period": PERIOD.isoformat(),
            "disclosure_id": disclosure_id,
        },
        "original_ref": {"kind": "disclosure_original", "id": original_id, "revision": 1},
        "pdf_hash": digest,
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
        "checked_at": captured.isoformat(),
        "acquired_at": "2025-04-01T00:00:00+00:00",
    }
    return {"schema": "bank-annual-review-audit/2", "fields": {METRIC: audit_field}}, audit_field


async def _baseline(
    session: AsyncSession, company: Company, listing: Listing
) -> tuple[PeerAnnualSnapshot, dict[str, Any]]:
    audit, _ = await _stored_audit(session, listing)
    snapshot, _ = await build_rollover_snapshot(
        session,
        listing_id=listing.id,
        company_id=company.id,
        report_period=PERIOD,
        audit=audit,
        captured_at=T0,
    )
    assert snapshot.fields[METRIC]["verification"] == "verified", snapshot.verification[METRIC]
    session.add(snapshot)
    await session.flush()
    return snapshot, audit


async def _supplier_candidate(
    session: AsyncSession,
    old: PeerAnnualSnapshot,
    *,
    captured_at: datetime = T1,
    value: str = "0.13",
    source_version: str = "eastmoney-rev-2",
) -> PeerAnnualSnapshot:
    fields = copy.deepcopy(old.fields)
    fields[METRIC]["value"] = value
    fields[METRIC]["verification"] = "candidate"
    fields[METRIC]["source_version"] = source_version
    proof = copy.deepcopy(old.verification[METRIC])
    proof["original_comparison"] = {
        "status": "mismatch",
        "new_value": value,
        "accounting_scope": "group_consolidated",
        "original_ref": copy.deepcopy(proof["original_ref"]),
    }
    row = PeerAnnualSnapshot(
        id=str(uuid4()),
        company_id=old.company_id,
        listing_id=old.listing_id,
        report_period=old.report_period,
        provider=old.provider,
        adapter_version=old.adapter_version,
        captured_at=captured_at,
        content_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        semantic_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        fields=fields,
        verification={**copy.deepcopy(old.verification), METRIC: proof},
    )
    session.add(row)
    await session.flush()
    await record_bank_online_reviews(session, row, {METRIC: old.id})
    return row


async def _same_value_copy(
    session: AsyncSession, source: PeerAnnualSnapshot, captured_at: datetime
) -> PeerAnnualSnapshot:
    row = PeerAnnualSnapshot(
        id=str(uuid4()),
        company_id=source.company_id,
        listing_id=source.listing_id,
        report_period=source.report_period,
        provider=source.provider,
        adapter_version=source.adapter_version,
        captured_at=captured_at,
        content_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        semantic_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        fields=copy.deepcopy(source.fields),
        verification=copy.deepcopy(source.verification),
    )
    session.add(row)
    await session.flush()
    return row


async def _resolution_result(
    session: AsyncSession,
    old: PeerAnnualSnapshot,
    candidate: PeerAnnualSnapshot,
    review: dict[str, Any],
    captured_at: datetime,
) -> tuple[PeerAnnualSnapshot, dict[str, Any]]:
    fields = copy.deepcopy(old.fields)
    proof = copy.deepcopy(old.verification[METRIC])
    proof["checked_at"] = captured_at.isoformat()
    proof["audit_record"]["checked_at"] = captured_at.isoformat()
    proof["resolution"] = {
        "review_id": review["review_id"],
        "decision": "reject_supplier",
        "basis": "v2_original_review",
        "old_snapshot_id": old.id,
        "candidate_snapshot_id": candidate.id,
        "resolved_value": old.fields[METRIC]["value"],
    }
    row = PeerAnnualSnapshot(
        id=str(uuid4()),
        company_id=old.company_id,
        listing_id=old.listing_id,
        report_period=old.report_period,
        provider=old.provider,
        adapter_version=old.adapter_version,
        captured_at=captured_at,
        content_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        semantic_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        fields=fields,
        verification={**copy.deepcopy(old.verification), METRIC: proof},
    )
    session.add(row)
    await session.flush()
    proof_hash = hashlib.sha256(canonical_json(proof).encode()).hexdigest()
    event = {
        "event_type": "resolve",
        "review_id": review["review_id"],
        "code": "601398.SH",
        "listing_id": old.listing_id,
        "company_id": old.company_id,
        "report_period": old.report_period.isoformat(),
        "metric": METRIC,
        "old_snapshot_id": old.id,
        "candidate_snapshot_id": candidate.id,
        "resulting_snapshot_id": row.id,
        "old_content_hash": old.content_hash,
        "candidate_content_hash": candidate.content_hash,
        "decision": "reject_supplier",
        "old_value": old.fields[METRIC]["value"],
        "candidate_value": candidate.fields[METRIC]["value"],
        "resolved_value": row.fields[METRIC]["value"],
        "period": row.fields[METRIC]["period"],
        "accounting_scope": "group_consolidated",
        "candidate_source_version": candidate.fields[METRIC]["source_version"],
        "revision_basis": None,
        "original_refs": [copy.deepcopy(proof["original_ref"])],
        "checked_at": captured_at.isoformat(),
        "proof_hashes": {METRIC: hashlib.sha256(canonical_json(proof).encode()).hexdigest()},
        "resolution_basis": "v2_original_review",
        "created_at": captured_at.isoformat(),
    }
    assert event["proof_hashes"][METRIC] == proof_hash
    return row, event


async def _report(
    session: AsyncSession, company_id: int, *, cutoff: datetime | None = None
) -> PeerFieldConflictReport:
    return await peer_field_states(
        session,
        company_id=company_id,
        metric=METRIC,
        report_period=PERIOD,
        cutoff=cutoff,
    )


def _state(report: PeerFieldConflictReport, row: PeerAnnualSnapshot) -> PeerFieldVersionState:
    return next(version for version in report.versions if str(version.source_ref.id) == row.id)


@pytest.mark.asyncio
async def test_unresolved_review_survives_real_pdf_reverification_and_same_value_copy(
    db_session: AsyncSession,
) -> None:
    company, listing = await _bank_listing(db_session)
    old, _ = await _baseline(db_session, company, listing)
    candidate = await _supplier_candidate(db_session, old)
    copy_row = await _same_value_copy(db_session, old, T2)

    assert await validate_field_original(db_session, old, METRIC, cutoff=T2)
    assert await validate_field_original(db_session, copy_row, METRIC, cutoff=T2)
    report = await _report(db_session, company.id, cutoff=T2)
    assert report.status == "pending_recheck"
    assert _state(report, old).state == "pending_recheck"
    assert _state(report, candidate).state == "pending_recheck"
    assert _state(report, copy_row).state == "pending_recheck"


@pytest.mark.asyncio
async def test_reject_supplier_resolution_corrects_versions_and_new_change_reopens_review(
    db_session: AsyncSession,
) -> None:
    company, listing = await _bank_listing(db_session)
    old, audit = await _baseline(db_session, company, listing)
    candidate = await _supplier_candidate(db_session, old)
    review = next(
        event
        for event in await load_bank_annual_review_events(db_session, listing_id=listing.id)
        if event.get("event_type") == "review"
    )
    resolution_time = datetime(2025, 4, 6, 12, tzinfo=UTC)
    resolution_audit = copy.deepcopy(audit)
    resolution_audit.update(
        listing_id=listing.id,
        code=listing.ticker,
        report_period=PERIOD.isoformat(),
    )
    resolution_audit["fields"][METRIC]["checked_at"] = resolution_time.isoformat()
    resolved = await resolve_bank_annual_review(
        db_session,
        review_id_value=review["review_id"],
        audit=resolution_audit,
        decision="reject_supplier",
        checked_at=resolution_time,
    )
    resolution_row = await db_session.get(PeerAnnualSnapshot, resolved["snapshot_id"])
    assert resolution_row is not None
    later_copy = await _same_value_copy(db_session, resolution_row, T3)

    report = await _report(db_session, company.id, cutoff=T3)
    assert report.status == "corrected"
    assert _state(report, old).state == "corrected"
    assert _state(report, candidate).state == "corrected"
    assert _state(report, resolution_row).state == "correction"
    assert _state(report, later_copy).state == "correction"
    assert _state(report, later_copy).corrects

    changed_again = await _supplier_candidate(
        db_session,
        old,
        captured_at=datetime(2025, 4, 9, 12, tzinfo=UTC),
        value="0.14",
        source_version="eastmoney-rev-3",
    )
    reopened = await _report(db_session, company.id)
    assert reopened.status == "pending_recheck"
    assert _state(reopened, changed_again).state == "pending_recheck"


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["event_hash", "listing", "year", "proof_hash", "future"])
async def test_forged_resolution_events_do_not_clear_pending_review(
    db_session: AsyncSession, forgery: str
) -> None:
    company, listing = await _bank_listing(db_session)
    old, _ = await _baseline(db_session, company, listing)
    candidate = await _supplier_candidate(db_session, old)
    review = next(
        event
        for event in await load_bank_annual_review_events(db_session, listing_id=listing.id)
        if event.get("event_type") == "review"
    )
    resolution_time = datetime(2025, 4, 6, 12, tzinfo=UTC)
    resolved_row, valid_resolution = await _resolution_result(
        db_session, old, candidate, review, resolution_time
    )
    assert await validate_field_original(db_session, resolved_row, METRIC, cutoff=resolution_time)
    forged = copy.deepcopy(valid_resolution)
    if forgery == "listing":
        forged["listing_id"] = listing.id + 1
    elif forgery == "year":
        forged["report_period"] = "2023-12-31"
    elif forgery == "proof_hash":
        forged["proof_hashes"] = {METRIC: "0" * 64}
    elif forgery == "future":
        forged["checked_at"] = (resolution_time + timedelta(days=1)).isoformat()
        forged["created_at"] = (resolution_time + timedelta(days=2)).isoformat()

    if forgery == "event_hash":
        forged_id = str(uuid4())
        forged["event_id"] = forged_id
        forged["semantic_hash"] = "0" * 64
        db_session.add(
            MethodArtifact(
                id=forged_id,
                owner_id="local_owner",
                kind="bank_annual_event",
                payload=forged,
            )
        )
        await db_session.flush()
    else:
        await append_review_event(db_session, event=forged)

    cutoff = resolution_time - timedelta(seconds=1) if forgery == "future" else None
    pending = await _report(db_session, company.id, cutoff=cutoff)
    assert pending.status == "pending_recheck", (forgery, pending)
    assert _state(pending, old).state == "pending_recheck"
    assert _state(pending, candidate).state == "pending_recheck"
