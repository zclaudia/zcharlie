from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.auxiliary.derivation import derive_auxiliary_listings
from investment_agent.storage.models.reference import (
    Market,
    Universe,
    UniverseMembership,
)
from investment_agent.storage.models.relationship import CompanyRelationship
from investment_agent.storage.models.user_domain import AuxiliaryListing
from investment_agent.storage.repositories.listing import ListingRepository


async def _seed_basic(session):
    session.add(
        Market(
            code="NASDAQ",
            name="NASDAQ",
            timezone="America/New_York",
            currency="USD",
        )
    )
    session.add(
        Market(code="TPE", name="Taiwan", timezone="Asia/Taipei", currency="TWD")
    )
    await session.flush()


async def _make_rel(session, from_id, to_id, strength):
    now = datetime.now(UTC)
    rel = CompanyRelationship(
        from_company_id=from_id,
        to_company_id=to_id,
        relationship_type="supplier_of",
        strength=Decimal(str(strength)),
        source="llm_extracted",
        confidence=Decimal("0.6"),
        valid_from=date.today(),
        discovered_at=now,
        last_verified_at=now,
    )
    session.add(rel)
    await session.flush()
    return rel


@pytest.mark.asyncio
async def test_derivation_adds_strong_counterparty_listing(db_session):
    await _seed_basic(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    tsmc = await repo.upsert_listing(
        market_code="TPE", ticker="2330", company_name="TSMC"
    )
    await _make_rel(db_session, aapl.company_id, tsmc.company_id, 0.9)

    result = await derive_auxiliary_listings(db_session)
    assert result["added"] == 1
    rows = (await db_session.execute(select(AuxiliaryListing))).scalars().all()
    assert len(rows) == 1
    assert rows[0].listing_id == tsmc.id
    assert rows[0].added_by == "derivation"


@pytest.mark.asyncio
async def test_derivation_respects_strength_threshold(db_session):
    await _seed_basic(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    tsmc = await repo.upsert_listing(
        market_code="TPE", ticker="2330", company_name="TSMC"
    )
    await _make_rel(db_session, aapl.company_id, tsmc.company_id, 0.3)

    result = await derive_auxiliary_listings(db_session)
    assert result["added"] == 0


@pytest.mark.asyncio
async def test_derivation_skips_counterparty_in_universe(db_session):
    await _seed_basic(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
    )
    db_session.add(Universe(code="NASDAQ_100", name="N100"))
    db_session.add(
        UniverseMembership(
            universe_code="NASDAQ_100",
            listing_id=msft.id,
            valid_from=date(2024, 1, 1),
        )
    )
    await db_session.flush()
    await _make_rel(db_session, aapl.company_id, msft.company_id, 0.9)

    result = await derive_auxiliary_listings(db_session)
    assert result["added"] == 0
    assert result["skipped_already_universe"] == 1


@pytest.mark.asyncio
async def test_derivation_respects_max_per_company(db_session):
    await _seed_basic(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    for i in range(8):
        c = await repo.upsert_listing(
            market_code="TPE", ticker=f"T{i}", company_name=f"Sup{i}"
        )
        await _make_rel(db_session, aapl.company_id, c.company_id, 0.9 - i * 0.01)

    result = await derive_auxiliary_listings(db_session, max_per_company=3)
    assert result["added"] == 3


@pytest.mark.asyncio
async def test_derivation_respects_total_cap(db_session):
    await _seed_basic(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft"
    )
    a = await repo.upsert_listing(market_code="TPE", ticker="A", company_name="A")
    b = await repo.upsert_listing(market_code="TPE", ticker="B", company_name="B")
    await _make_rel(db_session, aapl.company_id, a.company_id, 0.9)
    await _make_rel(db_session, msft.company_id, b.company_id, 0.9)

    result = await derive_auxiliary_listings(db_session, total_cap=1)
    assert result["added"] == 1
    assert result["skipped_cap_reached"] >= 1
