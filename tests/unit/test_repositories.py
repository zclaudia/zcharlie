from datetime import UTC, date, datetime

import pytest

from investment_agent.storage.models.reference import Company
from investment_agent.storage.models.stock_directory import StockDirectoryEntry
from investment_agent.storage.repositories.listing import ListingRepository


def _directory_entry(ticker: str, name: str) -> StockDirectoryEntry:
    return StockDirectoryEntry(
        ticker=ticker,
        name=name,
        pinyin_initials="zxjy",
        status="listed",
        synced_on=date(2026, 10, 23),
        provider="baostock",
        captured_at=datetime(2026, 10, 26, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_ensure_a_share_listing_uses_stock_directory_name(db_session):
    db_session.add(_directory_entry("002772.SZ", "众兴菌业"))
    await db_session.flush()

    listing = await ListingRepository(db_session).ensure_a_share_listing("002772.SZ")

    company = await db_session.get(Company, listing.company_id)
    assert company is not None and company.canonical_name == "众兴菌业"


@pytest.mark.asyncio
async def test_ensure_a_share_listing_prefers_explicit_name(db_session):
    db_session.add(_directory_entry("002772.SZ", "众兴菌业"))
    await db_session.flush()

    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "002772.SZ", company_name="菌业股份"
    )

    company = await db_session.get(Company, listing.company_id)
    assert company is not None and company.canonical_name == "菌业股份"


@pytest.mark.asyncio
async def test_ensure_a_share_listing_repairs_ticker_placeholder(db_session):
    repo = ListingRepository(db_session)
    listing = await repo.ensure_a_share_listing("002772.SZ")
    company = await db_session.get(Company, listing.company_id)
    assert company is not None and company.canonical_name == "002772.SZ"

    db_session.add(_directory_entry("002772.SZ", "众兴菌业"))
    await db_session.flush()
    assert await repo.ensure_a_share_listing("002772.SZ") is not None

    await db_session.refresh(company)
    assert company.canonical_name == "众兴菌业"


@pytest.mark.asyncio
async def test_ensure_a_share_listing_keeps_a_real_name(db_session):
    repo = ListingRepository(db_session)
    listing = await repo.ensure_a_share_listing("002772.SZ", company_name="众兴菌业")
    db_session.add(_directory_entry("002772.SZ", "别的名字"))
    await db_session.flush()

    await repo.ensure_a_share_listing("002772.SZ")

    company = await db_session.get(Company, listing.company_id)
    assert company is not None and company.canonical_name == "众兴菌业"


@pytest.mark.asyncio
async def test_upsert_listing_creates_company_and_listing(db_session, seeded_market):
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    assert listing.id is not None
    assert listing.ticker == "AAPL"
    assert listing.company_id is not None


@pytest.mark.asyncio
async def test_upsert_listing_is_idempotent(db_session, seeded_market):
    repo = ListingRepository(db_session)
    l1 = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    l2 = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    assert l1.id == l2.id
    assert l1.company_id == l2.company_id


@pytest.mark.asyncio
async def test_upsert_listing_unknown_market_raises(db_session):
    repo = ListingRepository(db_session)
    with pytest.raises(ValueError, match="Unknown market"):
        await repo.upsert_listing(
            market_code="GHOST", ticker="AAPL", company_name="Apple Inc."
        )
