import pytest

from investment_agent.storage.repositories.listing import ListingRepository


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
