"""Valuation seed helpers (moved from tests/test_valuation_calculations.py)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_agent.evidence.contracts import CalculationAssumption, EvidenceRecord, Period
from investment_agent.factors.valuation import years_before
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.valuation.contracts import ReverseDCFRequest, ValuationQualification

AS_OF = datetime(2026, 10, 1, 9, tzinfo=UTC)
D = Decimal


def assumption(name, value, **changes):
    return CalculationAssumption(name=name, value=Quantity(value=value, unit="fraction"),
        provenance="agent_scenario", rationale="Fictional sensitivity; no asserted company return", **changes)


class FixtureQualification:
    def __init__(self, qualification):
        self.qualification = qualification

    async def resolve(self, session, owner, request, evidence):
        return self.qualification


async def seed(session, *, amounts=("7.6", "8.2", "9.1"), market="212.4", unit="亿元"):
    listing = await ListingRepository(session).ensure_a_share_listing("600519.SH", company_name="Fictional fixture")
    subject = ObjectRef(kind="listing", id=listing.id, revision=1)
    refs = []
    for index, amount in enumerate((market, *amounts)):
        period = Period(start=date(2026, 9, 30), end=date(2026, 9, 30), basis="ordinary_equity_total") if index == 0 else Period(
            start=date(2022 + index, 1, 1), end=date(2022 + index, 12, 31), basis="verified_fcfe_fixture.v1")
        identifier = uuid4()
        record = EvidenceRecord(id=identifier, revision=1, actor="verified_fixture", origin_ref=None,
            created_at=AS_OF - timedelta(days=1), source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1),
            source_kind="document", locator="fictional.fixture", content_hash="fixture-hash",
            subject_ref=subject, period=period, published_at=AS_OF - timedelta(days=1),
            acquired_at=AS_OF - timedelta(days=1), available_from=AS_OF - timedelta(days=1),
            verification="verified", conflict="none", value=Quantity(value=amount, unit=unit, currency="CNY"))
        # Explicit fixture ingestion; production document/FCFE ingestion remains disabled.
        session.add(SavedEvidence(id=str(identifier), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        refs.append(ObjectRef(kind="evidence", id=identifier, revision=1))
    await session.flush()
    request = ReverseDCFRequest(as_of=AS_OF, equity_value_ref=refs[0], annual_fcfe_refs=tuple(refs[1:]),
        main_rate=assumption("discount_rate", "0.09"),
        sensitivity_rates=(assumption("discount_rate", "0.08"), assumption("discount_rate", "0.10")),
        terminal_growth=assumption("terminal_growth", "0.02"))
    qualification = ValuationQualification(equity_value_ref=refs[0], annual_fcfe_refs=tuple(refs[1:]),
        derivation_version="verified_fixture.v1", accounting_basis="ordinary_equity_fcfe",
        latest_complete_year=2025, ordinary_equity=True, non_financial=True,
        same_currency=True, consistent_scope=True, known_net_borrowing=True, known_cash_interest=True,
        interest_classification_checked=True, no_overlapping_financing=True,
        no_unhandled_material_changes=True, non_operating_assets_consistent=True)
    return request, qualification, subject


async def seed_bars(session, *, negative=False, count=260, old=False):
    listing = await ListingRepository(session).ensure_a_share_listing("600519.SH", company_name="Fictional fixture")
    repository = MarketSnapshotRepository(session)
    end = date(2026, 9, 30) - (timedelta(days=30) if old else timedelta())
    rows = []
    # Exact left boundary is stored and audited but must not enter the distribution.
    dates = [years_before(AS_OF.date(), 5), *(end - timedelta(days=count - 1 - i) for i in range(count))]
    for index, day in enumerate(dates):
        pe = D(-1) if index == len(dates) - 1 and negative else D(index % 17 + 1)
        row = await repository.insert(listing_id=listing.id, trade_date=day,
            open=D(10), high=D(10), low=D(10), close=D(10), volume=D(100),
            pe_ttm=pe, pb_mrq=D("1.2"), provider="baostock",
            captured_at=AS_OF - timedelta(days=1), is_backfill=False)
        rows.append(row)
    # A later recapture and future-known backfill must never enter a strict historical view.
    await repository.insert(listing_id=listing.id, trade_date=end,
        open=D(10), high=D(10), low=D(10), close=D(10), volume=D(100),
        pe_ttm=D(9999), pb_mrq=D(9999), provider="baostock",
        captured_at=AS_OF + timedelta(seconds=1), is_backfill=True)
    return listing, rows
