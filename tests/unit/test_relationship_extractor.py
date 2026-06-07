from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.domain.strategy_types import ExtractedRelationship
from investment_agent.relationships.extractor import persist_extracted_relationships
from investment_agent.relationships.resolver import resolve_counterparty
from investment_agent.storage.models.reference import Company, Market
from investment_agent.storage.models.relationship import CompanyRelationship
from investment_agent.storage.repositories.listing import ListingRepository


async def _seed_market(session):
    session.add(Market(code="NASDAQ", name="NASDAQ", timezone="America/New_York", currency="USD"))
    await session.flush()


@pytest.mark.asyncio
async def test_resolver_matches_by_ticker(db_session):
    await _seed_market(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc.")
    cid = await resolve_counterparty(db_session, "anything", "AAPL")
    assert cid == aapl.company_id


@pytest.mark.asyncio
async def test_resolver_matches_by_fuzzy_name(db_session):
    db_session.add(Company(canonical_name="Taiwan Semiconductor Manufacturing"))
    await db_session.flush()
    cid = await resolve_counterparty(db_session, "Taiwan Semiconductor")
    rows = (await db_session.execute(select(Company).where(Company.id == cid))).scalars().all()
    assert rows[0].canonical_name == "Taiwan Semiconductor Manufacturing"


@pytest.mark.asyncio
async def test_resolver_creates_placeholder(db_session):
    cid = await resolve_counterparty(db_session, "TotallyNewCo")
    row = (await db_session.execute(select(Company).where(Company.id == cid))).scalar_one()
    assert row.canonical_name == "TotallyNewCo"
    assert "Placeholder" in (row.description or "")


@pytest.mark.asyncio
async def test_extractor_inserts_new_relationship(db_session):
    await _seed_market(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc.")
    extracted = [
        ExtractedRelationship(
            counterparty_name="TSMC",
            counterparty_ticker=None,
            relationship_type="supplier_of",
            strength=0.9,
            revenue_share_pct=20.0,
            description="Chip supplier",
            evidence="10-K",
        ),
    ]
    n = await persist_extracted_relationships(
        db_session, from_company_id=aapl.company_id, extracted=extracted,
    )
    assert n == 1
    rels = (await db_session.execute(select(CompanyRelationship))).scalars().all()
    assert len(rels) == 1
    assert rels[0].relationship_type == "supplier_of"
    assert rels[0].source == "llm_extracted"
    assert rels[0].confidence == Decimal("0.600")


@pytest.mark.asyncio
async def test_extractor_skips_invalid_type(db_session):
    await _seed_market(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc.")
    extracted = [
        ExtractedRelationship(
            counterparty_name="X",
            relationship_type="founded_by",
            strength=0.8,
            description="x",
            evidence="x",
        ),
    ]
    n = await persist_extracted_relationships(
        db_session, from_company_id=aapl.company_id, extracted=extracted,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_extractor_updates_existing_strength(db_session):
    await _seed_market(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc.")

    extracted_initial = [
        ExtractedRelationship(
            counterparty_name="TSMC",
            relationship_type="supplier_of",
            strength=0.7,
            description="x",
            evidence="x",
        ),
    ]
    await persist_extracted_relationships(
        db_session, from_company_id=aapl.company_id, extracted=extracted_initial,
    )

    extracted_updated = [
        ExtractedRelationship(
            counterparty_name="TSMC",
            relationship_type="supplier_of",
            strength=0.9,
            description="x",
            evidence="x",
        ),
    ]
    n = await persist_extracted_relationships(
        db_session, from_company_id=aapl.company_id, extracted=extracted_updated,
    )
    assert n == 0  # Updated, not inserted
    rels = (await db_session.execute(select(CompanyRelationship))).scalars().all()
    assert len(rels) == 1
    assert rels[0].strength == Decimal("0.900")
