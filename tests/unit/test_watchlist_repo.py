"""Tests for WatchlistRepository."""

import pytest
from sqlalchemy import select

from investment_agent.storage.models.reference import Market
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
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
async def test_add_existing_updates_research_config_without_resetting_state(db_session):
    aapl, _ = await _seed_listings(db_session)
    repo = WatchlistRepository(db_session)
    first = await repo.add(
        aapl.id,
        industry="Technology",
        provider="auto",
        report_year=2024,
    )
    added_at = first.added_at
    first.last_refresh_status = "success"
    first.last_refresh_error = None
    await db_session.flush()

    second = await repo.add(
        aapl.id,
        industry="Information Technology",
        provider="ifind",
        report_year=2025,
    )

    assert second.id == first.id
    assert second.added_at == added_at
    assert second.industry == "Information Technology"
    assert second.provider == "ifind"
    assert second.report_year == 2025
    assert second.last_refresh_status == "success"


@pytest.mark.asyncio
async def test_older_run_cannot_overwrite_newer_refresh_state(db_session):
    aapl, _ = await _seed_listings(db_session)
    watchlist = WatchlistRepository(db_session)
    await watchlist.add(aapl.id)
    runs = PipelineRunRepository(db_session)
    older = await runs.create(kind="ifind_research", listing_id=aapl.id)
    newer = await runs.create(kind="ifind_research", listing_id=aapl.id)

    assert await watchlist.mark_refresh_queued(aapl.id, run_id=newer.id) is True
    assert (
        await watchlist.mark_refresh_queued(
            aapl.id,
            run_id=older.id,
            status="running",
        )
        is False
    )
    assert (
        await watchlist.mark_refresh_finished(
            aapl.id,
            run_id=older.id,
            status="failed",
            refreshed_at=older.started_at,
            error="stale failure",
        )
        is False
    )

    item = await watchlist.get_by_listing(aapl.id)
    assert item is not None
    assert item.last_research_run_id == newer.id
    assert item.last_refresh_status == "queued"
    assert item.last_refresh_error is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["queued", "running"])
async def test_enqueue_failure_does_not_clobber_live_run_state(db_session, live_status):
    aapl, _ = await _seed_listings(db_session)
    watchlist = WatchlistRepository(db_session)
    await watchlist.add(aapl.id)
    run = await PipelineRunRepository(db_session).create(
        kind="ifind_research", listing_id=aapl.id, status=live_status
    )
    assert await watchlist.mark_refresh_queued(
        aapl.id, run_id=run.id, status=live_status
    )

    updated = await watchlist.mark_refresh_enqueue_failed(
        aapl.id, error="enqueue failed: OperationalError: locked"
    )

    item = await watchlist.get_by_listing(aapl.id)
    assert updated is False
    assert item is not None
    assert item.last_refresh_status == live_status
    assert item.last_research_run_id == run.id
    assert item.last_refreshed_at is None
    assert item.last_refresh_error == "enqueue failed: OperationalError: locked"


@pytest.mark.asyncio
async def test_enqueue_failure_overwrites_terminal_run_state(db_session):
    aapl, _ = await _seed_listings(db_session)
    watchlist = WatchlistRepository(db_session)
    await watchlist.add(aapl.id)
    runs = PipelineRunRepository(db_session)
    run = await runs.create(kind="ifind_research", listing_id=aapl.id)
    await runs.mark_success(run.id)
    await watchlist.mark_refresh_finished(
        aapl.id,
        run_id=run.id,
        status="success",
        refreshed_at=run.started_at,
        error=None,
    )

    assert await watchlist.mark_refresh_enqueue_failed(aapl.id, error="boom") is True

    item = await watchlist.get_by_listing(aapl.id)
    assert item is not None
    assert item.last_refresh_status == "failed"
    assert item.last_refresh_error == "boom"


@pytest.mark.asyncio
async def test_enqueue_failure_without_previous_run_marks_failed(db_session):
    aapl, _ = await _seed_listings(db_session)
    watchlist = WatchlistRepository(db_session)
    await watchlist.add(aapl.id)

    assert await watchlist.mark_refresh_enqueue_failed(aapl.id, error="bad") is True

    item = await watchlist.get_by_listing(aapl.id)
    assert item is not None
    assert item.last_refresh_status == "failed"
    assert item.last_refreshed_at is not None
