from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.attribution.tools import (
    get_peer_returns,
    get_recent_earnings,
    get_recent_news,
    get_recent_ratings,
    get_related_company_events,
)
from investment_agent.storage.models.market_data import (
    AnalystRatingChange,
    DailyPrice,
    FinancialReport,
    NewsItem,
)
from investment_agent.storage.models.reference import (
    Company,
    Market,
    Sector,
)
from investment_agent.storage.models.relationship import CompanyRelationship
from investment_agent.storage.repositories.listing import ListingRepository


async def _seed_markets(session):
    session.add(Market(code="NASDAQ", name="NASDAQ",
                       timezone="America/New_York", currency="USD"))
    session.add(Sector(code="TECH", name="Technology"))
    await session.flush()


@pytest.mark.asyncio
async def test_get_recent_news_filters_by_days(db_session):
    await _seed_markets(db_session)
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    now = datetime.now(UTC)
    db_session.add(NewsItem(
        company_id=listing.company_id, published_at=now,
        source="reuters", title="fresh news", url="https://1",
    ))
    db_session.add(NewsItem(
        company_id=listing.company_id, published_at=now - timedelta(days=30),
        source="reuters", title="old news", url="https://2",
    ))
    await db_session.flush()
    rows = await get_recent_news(db_session, company_id=listing.company_id, days=7)
    assert len(rows) == 1
    assert rows[0]["headline"] == "fresh news"


@pytest.mark.asyncio
async def test_get_recent_ratings_returns_recent(db_session):
    await _seed_markets(db_session)
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    today = date.today()
    db_session.add(AnalystRatingChange(
        company_id=listing.company_id, date=today,
        firm="GS", action="upgraded",
        from_rating="Hold", to_rating="Buy", price_target=Decimal("220"),
    ))
    db_session.add(AnalystRatingChange(
        company_id=listing.company_id, date=today - timedelta(days=60),
        firm="MS", action="downgraded",
        from_rating="Buy", to_rating="Hold", price_target=None,
    ))
    await db_session.flush()
    rows = await get_recent_ratings(db_session, company_id=listing.company_id, days=14)
    assert len(rows) == 1
    assert rows[0]["firm"] == "GS"
    assert rows[0]["price_target"] == 220.0


@pytest.mark.asyncio
async def test_get_recent_earnings_orders_desc(db_session):
    await _seed_markets(db_session)
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    for period, end in [
        ("2024-Q4", date(2024, 12, 31)),
        ("2025-Q1", date(2025, 3, 31)),
        ("2025-Q2", date(2025, 6, 30)),
        ("2025-Q3", date(2025, 9, 30)),
    ]:
        db_session.add(FinancialReport(
            company_id=listing.company_id, fiscal_period=period, period_end=end,
            revenue=Decimal("100"), net_income=Decimal("10"), fcf=Decimal("5"),
        ))
    await db_session.flush()
    rows = await get_recent_earnings(db_session, company_id=listing.company_id, periods=2)
    assert len(rows) == 2
    assert rows[0]["fiscal_period"] == "2025-Q3"
    assert rows[1]["fiscal_period"] == "2025-Q2"


@pytest.mark.asyncio
async def test_get_related_company_events_includes_news(db_session):
    await _seed_markets(db_session)
    db_session.add(Market(code="TPE", name="Taiwan",
                          timezone="Asia/Taipei", currency="TWD"))
    await db_session.flush()
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    tsmc = await repo.upsert_listing(
        market_code="TPE", ticker="2330", company_name="TSMC",
    )
    now = datetime.now(UTC)
    db_session.add(CompanyRelationship(
        from_company_id=aapl.company_id, to_company_id=tsmc.company_id,
        relationship_type="supplier_of", strength=Decimal("0.9"),
        source="llm_extracted", confidence=Decimal("0.6"),
        valid_from=date.today(), discovered_at=now, last_verified_at=now,
    ))
    db_session.add(NewsItem(
        company_id=tsmc.company_id, published_at=now,
        source="reuters", title="TSMC capex up", url="https://t",
    ))
    await db_session.flush()
    rows = await get_related_company_events(
        db_session, company_id=aapl.company_id, days=7,
    )
    assert len(rows) == 1
    assert rows[0]["related_ticker"] == "2330"
    assert rows[0]["relationship_type"] == "supplier_of"
    news = rows[0]["recent_news"]
    assert isinstance(news, list) and len(news) == 1
    assert news[0]["headline"] == "TSMC capex up"


@pytest.mark.asyncio
async def test_get_peer_returns_excludes_self(db_session):
    await _seed_markets(db_session)
    repo = ListingRepository(db_session)
    aapl = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple",
    )
    msft = await repo.upsert_listing(
        market_code="NASDAQ", ticker="MSFT", company_name="Microsoft",
    )
    # Both companies need sector_code TECH; upsert_listing leaves it None.
    # Patch both companies' sector_code via direct ORM mutation.
    from sqlalchemy import select as _sel
    for c in (await db_session.execute(_sel(Company))).scalars().all():
        c.sector_code = "TECH"
    # Seed prices for MSFT (peer) only — AAPL has none, peer has 2 points
    today = date.today()
    db_session.add(DailyPrice(
        listing_id=msft.id, date=today - timedelta(days=1),
        open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
        close=Decimal("100"), volume=1, adjusted_close=Decimal("100"),
    ))
    db_session.add(DailyPrice(
        listing_id=msft.id, date=today,
        open=Decimal("105"), high=Decimal("105"), low=Decimal("105"),
        close=Decimal("105"), volume=1, adjusted_close=Decimal("105"),
    ))
    await db_session.flush()
    rows = await get_peer_returns(db_session, listing_id=aapl.id, days=1, limit=5)
    tickers = [r["ticker"] for r in rows]
    assert "AAPL" not in tickers
    assert "MSFT" in tickers
