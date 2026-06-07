import pytest

from investment_agent.storage.database import init_engine
from investment_agent.storage.models.base import Base
from investment_agent.storage.models import (  # noqa: F401
    analysis, market_data, ops, reference, relationship, user_domain,
)
from investment_agent.storage.seeders.nasdaq_100 import seed_nasdaq_100


@pytest.mark.asyncio
async def test_seed_creates_universe_and_listings(tmp_path):
    yaml = tmp_path / "u.yaml"
    yaml.write_text(
        "code: TEST_UNIV\n"
        "name: Test\n"
        "description: test\n"
        "market: NASDAQ\n"
        "sector: TECH\n"
        "constituents:\n"
        "  - ticker: AAPL\n"
        "    company_name: Apple Inc.\n"
        "    sector_code: TECH\n"
        "  - ticker: MSFT\n"
        "    company_name: Microsoft Corporation\n"
        "    sector_code: TECH\n"
    )
    engine = init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    n = await seed_nasdaq_100(yaml)
    assert n == 2
    n2 = await seed_nasdaq_100(yaml)
    assert n2 == 0
    await engine.dispose()
