from datetime import date

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tools.probe_wmshare_local import _load, load_local_context

from investment_agent.storage.models import Base
from investment_agent.storage.models.reference import Universe, UniverseMembership
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from tests.support.factor_view import AS_OF, ON_TIME, TICKER, _seed


@pytest.mark.asyncio
async def test_read_only_point_in_time_local_data(tmp_path):
    db = tmp_path / "local database.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        listing_id = await _seed(session)
        session.add(Universe(code="CSI300", name="CSI 300"))
        session.add_all(
            [
                UniverseMembership(
                    universe_code="CSI300",
                    listing_id=listing_id,
                    valid_from=date(2025, 1, 1),
                    valid_to=AS_OF,
                ),
                UniverseMembership(
                    universe_code="CSI300", listing_id=listing_id, valid_from=AS_OF, valid_to=None
                ),
                WatchlistItem(listing_id=listing_id, added_at=ON_TIME, enabled=True),
            ]
        )
        await FinancialSnapshotRepository(session).insert(
            listing_id=listing_id,
            report_period=date(2026, 3, 31),
            period_kind="q1",
            fields={"roe": 0.99},
            provider="baostock",
            captured_at=ON_TIME,
            availability="actual",
            available_from=date(2026, 5, 1),
        )
        dividends = DividendRepository(session)
        await dividends.record_coverage(
            listing_id=listing_id, provider="baostock", year=2025, checked_at=ON_TIME, event_count=0
        )
        await session.commit()
    await engine.dispose()

    before = db.read_bytes()
    result = await _load(db, AS_OF, [TICKER, "000001.SZ"])
    assert db.read_bytes() == before
    assert result["universe_tickers"] == [TICKER]
    assert result["watchlist_tickers"] == [TICKER]
    stock = result["stocks"][TICKER]
    assert stock["price"] == 1504
    assert stock["annual_roe"] == pytest.approx(0.33)
    assert stock["report_period"] == "2025-12-31"
    assert stock["dividend_yield_ttm"] is None
    assert stock["dividend_coverage"] == {"2025": "2026-05-06"}
    assert result["stocks"]["000001.SZ"]["price"] is None

    # A checked empty year is a known zero, unlike an unchecked year.
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    async with async_sessionmaker(engine)() as session:
        await DividendRepository(session).record_coverage(
            listing_id=listing_id,
            provider="baostock",
            year=2026,
            checked_at=ON_TIME,
            event_count=0,
        )
        await session.commit()
    await engine.dispose()
    result = await _load(db, AS_OF, [TICKER])
    assert result["stocks"][TICKER]["dividend_yield_ttm"] == 0
    assert result["stocks"][TICKER]["dividend_events"] == []


@pytest.mark.asyncio
async def test_unpopulated_universe_is_unknown_but_empty_watchlist_is_known(tmp_path):
    db = tmp_path / "no-universe.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        await _seed(session)
        await session.commit()
    await engine.dispose()

    result = await _load(db, AS_OF, [TICKER])
    assert result["status"] == "partial"
    assert result["universe_tickers"] is None
    assert "membership unavailable" in result["note"]
    assert result["watchlist_tickers"] == []
    assert result["stocks"][TICKER]["price"] == 1504


def test_unavailable_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    for result in (
        load_local_context(None, AS_OF, [TICKER]),
        load_local_context(path, AS_OF, [TICKER]),
    ):
        assert result["status"] == "unavailable"
        assert result["universe_tickers"] is None
        assert result["stocks"][TICKER]["dividend_yield_ttm"] is None
    assert not path.exists()
