"""All portfolio stress tests require the shared fail-closed offline runner."""

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.storage.models import Base


def pytest_configure(config):
    if os.environ.get("NEXT_ITERATIONS_OFFLINE") != "1" or not config.pluginmanager.hasplugin(
        "next_iterations_offline"
    ):
        raise pytest.UsageError(
            "Portfolio stress tests require -p next_iterations_offline and NEXT_ITERATIONS_OFFLINE=1"
        )


@pytest.fixture
async def portfolio_stress_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'portfolio-stress.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()
