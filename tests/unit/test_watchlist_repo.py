"""Tests for WatchlistRepository."""

import pytest
from sqlalchemy import select

from investment_agent.storage.models.reference import Market
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository


async def _seed_listings(session):
    session.add(
        Market(
            code="NASDAQ",
            name="NASDAQ",
            timezone="America/New_York",
            currency="USD",
        )
    )
    await session.flush()
    repo = ListingRepository(session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft",
    )
    return aapl, msft


@pytest.mark.asyncio
async def test_add_inserts_new(db_session):
    aapl, _ = await _seed_listings(db_session)
    item = await WatchlistRepository(db_session).add(aapl.id, note="favorite")
    assert item.listing_id == aapl.id
    assert item.note == "favorite"
    assert item.added_at is not None


@pytest.mark.asyncio
async def test_add_existing_updates_note(db_session):
    aapl, _ = await _seed_listings(db_session)
    repo = WatchlistRepository(db_session)
    first = await repo.add(aapl.id, note="v1")
    second = await repo.add(aapl.id, note="v2")
    assert first.id == second.id
    rows = (await db_session.execute(select(WatchlistItem))).scalars().all()
    assert len(rows) == 1
    assert rows[0].note == "v2"


@pytest.mark.asyncio
async def test_remove_returns_true_when_present(db_session):
    aapl, _ = await _seed_listings(db_session)
    repo = WatchlistRepository(db_session)
    await repo.add(aapl.id)
    assert await repo.remove(aapl.id) is True
    assert await repo.remove(aapl.id) is False


@pytest.mark.asyncio
async def test_list_all_orders_by_added_at_desc(db_session):
    aapl, msft = await _seed_listings(db_session)
    repo = WatchlistRepository(db_session)
    # Insert msft first, then aapl — list_all returns newest first
    await repo.add(msft.id, note="msft")
    await repo.add(aapl.id, note="aapl")
    rows = await repo.list_all()
    assert [r.listing_id for r in rows] == [aapl.id, msft.id]
