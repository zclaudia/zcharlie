from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.pit_snapshot import (
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.supplemental_rules.inputs import freeze_subject


@pytest.mark.asyncio
async def test_freeze_reads_only_rows_captured_before_exact_datetime(db_session) -> None:
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Example Co")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600001", is_primary=True)
    db_session.add(listing)
    await db_session.flush()

    cutoff = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
    db_session.add(IndustryClassification(
        listing_id=listing.id, system="csrc", code="A01", name="Industry",
        valid_from=date(2020, 1, 1), valid_to=None, source="fixture",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
    ))
    db_session.add_all([
        MarketSnapshot(
            listing_id=listing.id, trade_date=date(2026, 11, 15), open=Decimal("10"),
            high=Decimal("11"), low=Decimal("9"), close=Decimal("10.5"), volume=Decimal("1"),
            amount=None, total_shares=None, pe_ttm=Decimal("10"), pb_mrq=None, ps_ttm=None,
            turnover=None, trade_status=1, is_st=False, adjust="none", provider="baostock",
            captured_at=datetime(2026, 11, 16, 3, 59, tzinfo=UTC), raw_ref="saved:1",
            is_backfill=False, version=1,
        ),
        MarketSnapshot(
            listing_id=listing.id, trade_date=date(2026, 11, 15), open=Decimal("10"),
            high=Decimal("12"), low=Decimal("9"), close=Decimal("11.5"), volume=Decimal("1"),
            amount=None, total_shares=None, pe_ttm=Decimal("12"), pb_mrq=None, ps_ttm=None,
            turnover=None, trade_status=1, is_st=False, adjust="none", provider="baostock",
            captured_at=datetime(2026, 11, 16, 4, 1, tzinfo=UTC), raw_ref="late-backfill",
            is_backfill=True, version=2,
        ),
    ])
    db_session.add_all([
        FinancialSnapshot(
            listing_id=listing.id, report_period=date(2025, 12, 31), period_kind="annual",
            available_from=date(2026, 3, 1), availability="actual", fields={"roe": 0.1},
            provider="baostock", captured_at=datetime(2026, 11, 16, 3, 58, tzinfo=UTC),
            raw_ref="financial:1", is_backfill=False, version=1,
        ),
        FinancialSnapshot(
            listing_id=listing.id, report_period=date(2025, 12, 31), period_kind="annual",
            available_from=date(2026, 3, 1), availability="actual", fields={"roe": 0.9},
            provider="baostock", captured_at=datetime(2026, 11, 16, 4, 1, tzinfo=UTC),
            raw_ref="financial:late-backfill", is_backfill=True, version=2,
        ),
    ])
    await db_session.flush()

    subject = await freeze_subject(
        db_session, ObjectRef(kind="listing", id=listing.id, revision=1), cutoff,
        owner_id="local_owner",
    )
    market_rows = [item for item in subject.source_manifest if item["kind"] == "market_snapshot"]
    financial_rows = [item for item in subject.source_manifest if item["kind"] == "financial_snapshot"]
    assert len(market_rows) == 1
    assert market_rows[0]["content"]["close"] == "10.500000"
    assert len(financial_rows) == 1
    assert financial_rows[0]["content"]["fields"] == {"roe": 0.1}
    assert subject.input_status == "ready"


@pytest.mark.asyncio
async def test_truncated_financial_scan_cannot_make_older_factor_known(db_session) -> None:
    """A bounded source scan must not turn an older surviving row into truth."""
    from investment_agent.supplemental_rules.contracts import MetricLeaf
    from investment_agent.supplemental_rules.metric_evaluation import evaluate_metric

    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Capped issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600004", is_primary=True)
    db_session.add(listing)
    await db_session.flush()

    cutoff = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
    db_session.add(IndustryClassification(
        listing_id=listing.id, system="csrc", code="A01", name="Industry",
        valid_from=date(2020, 1, 1), valid_to=None, source="fixture",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
    ))
    db_session.add(MarketSnapshot(
        listing_id=listing.id, trade_date=date(2026, 11, 15), open=Decimal("10"),
        high=Decimal("11"), low=Decimal("9"), close=Decimal("10.5"), volume=Decimal("1"),
        amount=None, total_shares=None, pe_ttm=Decimal("12"), pb_mrq=None, ps_ttm=None,
        turnover=None, trade_status=1, is_st=False, adjust="none", provider="baostock",
        captured_at=datetime(2026, 11, 16, 3, 59, tzinfo=UTC), raw_ref="market:1",
        is_backfill=False, version=1,
    ))
    # Both rows are genuine, cutoff-visible annual reports. The ordered bounded
    # scan sees the older report first; it cannot prove that it is still latest.
    db_session.add_all([
        FinancialSnapshot(
            listing_id=listing.id, report_period=date(2024, 12, 31), period_kind="annual",
            available_from=date(2025, 3, 1), availability="actual", fields={"roe": 0.15},
            provider="baostock", captured_at=datetime(2025, 3, 2, tzinfo=UTC),
            raw_ref="financial:older", is_backfill=False, version=1,
        ),
        FinancialSnapshot(
            listing_id=listing.id, report_period=date(2025, 12, 31), period_kind="annual",
            available_from=date(2026, 3, 1), availability="actual", fields={"roe": 0.20},
            provider="baostock", captured_at=datetime(2026, 3, 2, tzinfo=UTC),
            raw_ref="financial:newer", is_backfill=False, version=1,
        ),
    ])
    await db_session.flush()

    subject = await freeze_subject(
        db_session, ObjectRef(kind="listing", id=listing.id, revision=1), cutoff,
        owner_id="local_owner", factor_names=("roe", "pe_ttm"), max_source_rows=1,
    )
    result = evaluate_metric(subject, MetricLeaf(
        factor_id="roe", operator="<", value=Decimal("0.10"), unit="ratio",
    ))
    assert result.status == "unknown"
    assert result.leaves[0]["reason"] == "factor_source_coverage_unknown"
    assert subject.factor_values["roe"]["quality"] == "ok"  # P1 quality is unchanged.
    assert "source_coverage_unknown" in subject.factor_values["roe"]["flags"]
    # A financial-row cap does not contaminate a separately proven market factor.
    assert subject.factor_values["pe_ttm"]["quality"] == "ok"
    assert "source_coverage_unknown" not in subject.factor_values["pe_ttm"]["flags"]
    assert any(item.startswith("financial_rows_truncated:") for item in subject.diagnostics)
    assert subject.input_status == "partial"


@pytest.mark.asyncio
async def test_service_freeze_marks_capped_source_coverage_incomplete(db_session, monkeypatch) -> None:
    """The real service freeze carries source uncertainty into the manifest."""
    from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
    from investment_agent.supplemental_rules import inputs
    from investment_agent.supplemental_rules.contracts import EvaluationRequest
    from investment_agent.supplemental_rules.service import SupplementalRuleService

    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Service cap issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600005", is_primary=True)
    db_session.add(listing)
    await db_session.flush()
    cutoff = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
    db_session.add_all([
        FinancialSnapshot(
            listing_id=listing.id, report_period=date(2024, 12, 31), period_kind="annual",
            available_from=date(2025, 3, 1), availability="actual", fields={"roe": 0.15},
            provider="baostock", captured_at=datetime(2025, 3, 2, tzinfo=UTC),
            raw_ref="service:older", is_backfill=False, version=1,
        ),
        FinancialSnapshot(
            listing_id=listing.id, report_period=date(2025, 12, 31), period_kind="annual",
            available_from=date(2026, 3, 1), availability="actual", fields={"roe": 0.20},
            provider="baostock", captured_at=datetime(2026, 3, 2, tzinfo=UTC),
            raw_ref="service:newer", is_backfill=False, version=1,
        ),
    ])
    await db_session.flush()

    real_freeze_subject = inputs.freeze_subject

    async def capped_freeze_subject(*args, **kwargs):
        kwargs["max_source_rows"] = 1
        return await real_freeze_subject(*args, **kwargs)

    monkeypatch.setattr(inputs, "freeze_subject", capped_freeze_subject)
    store = SupplementalRulesRepository(db_session)
    await store.reserve_registry("local_owner")
    request = EvaluationRequest(
        idempotency_key=uuid4(),
        listing_refs=(ObjectRef(kind="listing", id=listing.id, revision=1),),
        contexts={"company_research"},
        mode="live",
    )
    manifest = await SupplementalRuleService(db_session).freeze(request, as_of=cutoff)
    assert any("financial_rows_truncated:" in item for item in manifest.scan_diagnostics)
    assert manifest.pairs == ()


@pytest.mark.asyncio
async def test_rule_source_refs_are_validated_by_their_real_storage_kind(db_session) -> None:
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Source reference issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600003")
    db_session.add(listing)
    await db_session.flush()
    subject = await freeze_subject(
        db_session, ObjectRef(kind="listing", id=listing.id, revision=1),
        datetime(2026, 11, 16, 4, tzinfo=UTC), owner_id="local_owner",
        evidence_refs=(
            ObjectRef(kind="financial_snapshot", id=987654, revision=1),
            ObjectRef(kind="research_event", id="00000000-0000-4000-8000-000000000001", revision=1),
        ),
    )
    assert "requested_source_unavailable:financial_snapshot:987654@1" in subject.diagnostics
    assert any(item.startswith("requested_source_unavailable:research_event:") for item in subject.diagnostics)
    assert all(item["kind"] != "saved_evidence" for item in subject.source_manifest)


@pytest.mark.asyncio
async def test_public_invalid_and_unsupported_listing_refs_are_typed_errors(db_session) -> None:
    with pytest.raises(InvestorError) as missing:
        await freeze_subject(
            db_session, ObjectRef(kind="listing", id=999999, revision=1),
            datetime(2026, 11, 16, 4, tzinfo=UTC), owner_id="local_owner",
        )
    assert missing.value.detail.code == "invalid_reference"

    db_session.add(Market(code="NASDAQ", name="NASDAQ", timezone="America/New_York", currency="USD"))
    company = Company(canonical_name="Unsupported issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="NASDAQ", ticker="MSFT")
    db_session.add(listing)
    await db_session.flush()
    with pytest.raises(InvestorError) as unsupported:
        await freeze_subject(
            db_session, ObjectRef(kind="listing", id=listing.id, revision=1),
            datetime(2026, 11, 16, 4, tzinfo=UTC), owner_id="local_owner",
        )
    assert unsupported.value.detail.code == "unsupported_subject"


def test_strict_source_helpers_reject_naive_cutoffs() -> None:
    from investment_agent.supplemental_rules.sources import build_strict_snapshot_view

    with pytest.raises(ValueError, match="timezone-aware"):
        build_strict_snapshot_view("600001", datetime(2026, 10, 16), (), (), (), (), ())
