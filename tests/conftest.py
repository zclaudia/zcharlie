"""Shared test fixtures."""

import pytest_asyncio

from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import (  # noqa: F401
    analysis,
    market_data,
    ops,
    reference,
    relationship,
    user_domain,
)
from investment_agent.storage.models.base import Base


@pytest_asyncio.fixture
async def db_session():
    engine = init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_market(db_session):
    from investment_agent.storage.models.reference import Market

    market = Market(
        code="NASDAQ", name="NASDAQ Stock Market", timezone="America/New_York",
        currency="USD",
    )
    db_session.add(market)
    await db_session.flush()
    return market
