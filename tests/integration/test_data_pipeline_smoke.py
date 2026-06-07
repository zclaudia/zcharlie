"""End-to-end smoke: seed universe + insert daily price + repository query."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.sources.records import DailyPriceRecord
from investment_agent.storage.models.market_data import DailyPrice
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.listing import ListingRepository


@pytest.mark.asyncio
async def test_full_data_path_seed_and_query(db_session, seeded_market):
    """Verify the seed -> insert daily price -> query path works end-to-end."""
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )

    record = DailyPriceRecord(
        ticker="AAPL", date=date(2025, 6, 3),
        open=Decimal("196.50"), high=Decimal("198.20"),
        low=Decimal("195.80"), close=Decimal("197.40"),
        volume=50_000_000, adjusted_close=Decimal("197.40"),
    )
    db_session.add(
        DailyPrice(
            listing_id=listing.id, date=record.date,
            open=record.open, high=record.high, low=record.low,
            close=record.close, volume=record.volume,
            adjusted_close=record.adjusted_close,
        )
    )
    await db_session.flush()

    stmt = select(DailyPrice).where(DailyPrice.listing_id == listing.id)
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].close == Decimal("197.40")
