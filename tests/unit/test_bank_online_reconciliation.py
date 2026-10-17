from datetime import UTC, date, datetime

import pytest

from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.bank_online_reconciliation import reconcile_bank_online_fields
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market


@pytest.mark.asyncio
async def test_same_supplier_value_does_not_inherit_unreadable_original(db_session):
    """A verified label and equal number cannot replace failed PDF byte readback."""
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="测试银行")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600919", is_primary=True)
    db_session.add(listing)
    await db_session.flush()

    period = date(2025, 12, 31)
    captured = datetime(2026, 10, 17, tzinfo=UTC)
    field = PeerAnnualField(
        metric="npl_ratio",
        value="0.008",
        unit="fraction",
        period=Period(start=date(2025, 1, 1), end=period, basis="annual_ratio"),
        source_field="NPL",
        raw_value="0.80",
        raw_unit="percent",
        acquired_at=captured,
        verification=PeerFieldVerification.VERIFIED,
    )
    row = PeerAnnualSnapshot(
        id="baseline",
        company_id=company.id,
        listing_id=listing.id,
        report_period=period,
        provider="old",
        adapter_version="1",
        captured_at=datetime(2026, 4, 1, tzinfo=UTC),
        content_hash="a" * 64,
        semantic_hash="b" * 64,
        fields={"npl_ratio": field.model_dump(mode="json")},
        verification={
            "npl_ratio": {
                "status": "verified",
                "original_ref": {"kind": "disclosure_original", "id": "missing", "revision": 1},
                "report_identity": {},
                "check_rule_version": "bank-annual-original/1",
                "checked_at": "2026-04-01T00:00:00+00:00",
            }
        },
    )
    db_session.add(row)
    await db_session.flush()

    candidate = field.model_copy(update={"verification": PeerFieldVerification.CANDIDATE})
    fields, proofs, summary = await reconcile_bank_online_fields(
        db_session,
        listing_id=listing.id,
        company_id=company.id,
        report_period=period,
        fields={"npl_ratio": candidate},
        proofs={"npl_ratio": {"response_hash": "c" * 64}},
        captured_at=captured,
    )

    assert fields["npl_ratio"].verification == PeerFieldVerification.CANDIDATE
    assert (
        proofs["npl_ratio"]["reconciliation"]["reason"] == "baseline_original_failed_byte_readback"
    )
    assert summary["status"] == "review_needed"


@pytest.mark.asyncio
async def test_missing_same_listing_baseline_keeps_online_candidate(db_session):
    period = date(2025, 12, 31)
    captured = datetime(2026, 10, 17, tzinfo=UTC)
    candidate = PeerAnnualField(
        metric="net_interest_margin",
        value="0.018",
        unit="fraction",
        period=Period(start=date(2025, 1, 1), end=period, basis="annual_ratio"),
        acquired_at=captured,
    )
    fields, proofs, summary = await reconcile_bank_online_fields(
        db_session,
        listing_id=999,
        company_id=999,
        report_period=period,
        fields={"net_interest_margin": candidate},
        proofs={"net_interest_margin": {"response_hash": "d" * 64}},
        captured_at=captured,
    )
    assert fields["net_interest_margin"].verification == PeerFieldVerification.CANDIDATE
    assert (
        proofs["net_interest_margin"]["reconciliation"]["reason"]
        == "no_same_listing_annual_baseline"
    )
    assert summary["status"] == "review_needed"


@pytest.mark.asyncio
async def test_changed_online_value_is_recorded_for_review(db_session, monkeypatch):
    async def verified_byte_readback(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        "investment_agent.industry_peers.bank_online_reconciliation._verified_baseline",
        verified_byte_readback,
    )
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="测试银行")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600919", is_primary=True)
    db_session.add(listing)
    await db_session.flush()
    period = date(2025, 12, 31)
    captured = datetime(2026, 10, 17, tzinfo=UTC)
    old = PeerAnnualField(
        metric="net_interest_margin",
        value="0.018",
        unit="fraction",
        period=Period(start=date(2025, 1, 1), end=period, basis="annual_ratio"),
        acquired_at=datetime(2026, 4, 1, tzinfo=UTC),
        verification=PeerFieldVerification.VERIFIED,
    )
    db_session.add(
        PeerAnnualSnapshot(
            id="baseline-change",
            company_id=company.id,
            listing_id=listing.id,
            report_period=period,
            provider="old",
            adapter_version="1",
            captured_at=datetime(2026, 4, 1, tzinfo=UTC),
            content_hash="e" * 64,
            semantic_hash="f" * 64,
            fields={"net_interest_margin": old.model_dump(mode="json")},
            verification={
                "net_interest_margin": {
                    "status": "verified",
                    "original_ref": {"kind": "disclosure_original", "id": "missing", "revision": 1},
                    "report_identity": {},
                    "check_rule_version": "bank-annual-original/1",
                    "checked_at": "2026-04-01T00:00:00+00:00",
                }
            },
        )
    )
    await db_session.flush()
    new = old.model_copy(
        update={
            "value": "0.019",
            "acquired_at": captured,
            "verification": PeerFieldVerification.CANDIDATE,
        }
    )

    fields, proofs, summary = await reconcile_bank_online_fields(
        db_session,
        listing_id=listing.id,
        company_id=company.id,
        report_period=period,
        fields={"net_interest_margin": new},
        proofs={"net_interest_margin": {"source_response_sha256": "1" * 64}},
        captured_at=captured,
    )

    assert fields["net_interest_margin"].value == "0.019"
    assert proofs["net_interest_margin"]["reconciliation"] == {
        "status": "review_needed",
        "reason": "online_value_changed",
        "old_value": "0.018",
        "new_value": "0.019",
    }
    assert summary["status"] == "review_needed"
