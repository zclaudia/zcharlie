"""Shared test fixtures."""

import pytest
import pytest_asyncio

from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import (  # noqa: F401
    analysis,
    app_settings,
    candidates,
    earnings_events,
    market_data,
    ops,
    pit_snapshot,
    provider_call,
    reference,
    relationship,
    rules,
    stock_directory,
    user_domain,
    verification,
)
from investment_agent.storage.models.base import Base


@pytest.fixture(autouse=True, scope="session")
def _uncached_structlog_loggers():
    """``configure_logging`` (CLI callback, API lifespan) enables structlog's
    ``cache_logger_on_first_use``; a module logger first used after that keeps
    its processors forever, so later ``capture_logs`` blocks see nothing and
    log assertions depend on test order. Tests never cache loggers."""

    import structlog

    original = structlog.configure

    def configure(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs["cache_logger_on_first_use"] = False
        return original(*args, **kwargs)

    patcher = pytest.MonkeyPatch()
    patcher.setattr(structlog, "configure", configure)
    yield
    patcher.undo()


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Never read the checkout's ``.env``: tests set the env they need explicitly."""

    from investment_agent.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _no_real_akshare(request, monkeypatch):
    """Offline tests must never reach AkShare (swsresearch.com / legulegu.com /
    eastmoney.com)."""

    if request.node.get_closest_marker("network") is not None:
        return
    from investment_agent.providers import akshare_eastmoney, akshare_shenwan

    def refuse(self, func_name, *args, **kwargs):
        raise RuntimeError(f"offline test tried to call akshare {func_name}; use a fake backend")

    monkeypatch.setattr(akshare_shenwan.AkshareShenwanBackend, "_call", refuse)
    monkeypatch.setattr(akshare_eastmoney.AkshareEastmoneyBackend, "_call", refuse)


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


@pytest_asyncio.fixture
async def api_client(tmp_path, monkeypatch):
    """Shared FastAPI TestClient against a fresh disk-backed SQLite per test.

    Sets:
      API_KEY=secret, LLM_TIER3_API_KEY=x,
      DATA_DIR=<tmp_path>, DATABASE_URL=sqlite+aiosqlite:///<tmp_path>/api.db
    """
    from fastapi.testclient import TestClient

    from investment_agent.api.app import create_app

    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TestClient(create_app())
    await engine.dispose()
