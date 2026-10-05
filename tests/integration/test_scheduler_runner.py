"""Tests for scheduler.runner (no actual pipeline execution)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from investment_agent.config import (
    AppYamlConfig,
    AuxiliaryTrackingConfig,
    PipelineConfig,
    SchedulerConfig,
    Settings,
    UniverseEntry,
)
from investment_agent.scheduler.runner import (
    _enqueue_scheduled_watchlist_refresh,
    _parse_cron,
    build_scheduler,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository


def test_parse_cron_valid() -> None:
    t = _parse_cron("30 16 * * 1-5")
    assert t is not None


def test_parse_cron_invalid_raises() -> None:
    with pytest.raises(ValueError, match="5 fields"):
        _parse_cron("30 16 *")


def test_build_scheduler_registers_job_per_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    cfg = AppYamlConfig(
        universes=[UniverseEntry(code="NASDAQ_100"), UniverseEntry(code="TEST_U")],
        pipeline=PipelineConfig(),
        scheduler=SchedulerConfig(
            discovery_cron="30 16 * * 1-5", discovery_timezone="UTC",
        ),
        auxiliary_tracking=AuxiliaryTrackingConfig(),
    )
    sched = build_scheduler(settings=settings, app_config=cfg)
    job_ids = {j.id for j in sched.get_jobs()}
    assert "discovery_NASDAQ_100" in job_ids
    assert "discovery_TEST_U" in job_ids
    assert "watchlist_refresh" not in job_ids
    # Scheduler was never started, so no shutdown call is needed.


def test_build_scheduler_registers_a_share_watchlist_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    cfg = AppYamlConfig(
        universes=[],
        pipeline=PipelineConfig(),
        scheduler=SchedulerConfig(
            discovery_cron="30 16 * * 1-5",
            discovery_timezone="America/New_York",
            watchlist_refresh_cron="30 15 * * 1-5",
            watchlist_refresh_timezone="Asia/Shanghai",
            a_share_calendar_path="config/calendars/a_share.yaml",
        ),
        auxiliary_tracking=AuxiliaryTrackingConfig(),
    )

    sched = build_scheduler(settings=settings, app_config=cfg)
    jobs = {job.id: job for job in sched.get_jobs()}

    assert set(jobs) == {"watchlist_refresh"}
    assert str(jobs["watchlist_refresh"].trigger.timezone) == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_scheduled_watchlist_refresh_skips_holiday_and_is_idempotent(
    db_session,
    tmp_path,
) -> None:
    del db_session  # initializes the shared in-memory engine and schema
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier2_api_key="test",
        llm_tier3_api_key="test",
        ifind_proxy_api_key="proxy-test",
        data_dir=tmp_path,
    )
    calendar = load_trading_calendar(Path("config/calendars/a_share.yaml"))
    async with session_scope() as session:
        session.add(
            Market(
                code="SSE",
                name="Shanghai Stock Exchange",
                timezone="Asia/Shanghai",
                currency="CNY",
            )
        )
        await session.flush()
        listing = await ListingRepository(session).upsert_listing(
            market_code="SSE",
            ticker="600406",
            company_name="国电南瑞",
        )
        await WatchlistRepository(session).add(
            listing.id,
            industry="电力设备",
            provider="ifind",
            report_year=2025,
        )

    skipped = await _enqueue_scheduled_watchlist_refresh(
        settings=settings,
        calendar=calendar,
        trading_date=date(2026, 10, 5),
    )
    created = await _enqueue_scheduled_watchlist_refresh(
        settings=settings,
        calendar=calendar,
        trading_date=date(2026, 10, 8),
    )
    replayed = await _enqueue_scheduled_watchlist_refresh(
        settings=settings,
        calendar=calendar,
        trading_date=date(2026, 10, 8),
    )

    assert skipped is None
    assert created == 1
    assert replayed == 0
    async with session_scope() as session:
        runs = list(
            (
                await session.execute(
                    select(PipelineRun).where(
                        PipelineRun.triggered_by == "scheduler"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 1
        assert runs[0].request_json["as_of"] == "2026-10-08"
