"""Tests for scheduler.runner (no actual pipeline execution)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from investment_agent.config import (
    AppYamlConfig,
    SchedulerConfig,
    Settings,
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


def test_build_scheduler_without_watchlist_cron_has_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    cfg = AppYamlConfig(scheduler=SchedulerConfig())
    sched = build_scheduler(settings=settings, app_config=cfg)
    assert sched.get_jobs() == []
    # Scheduler was never started, so no shutdown call is needed.


def test_build_scheduler_registers_a_share_watchlist_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    cfg = AppYamlConfig(
        scheduler=SchedulerConfig(
            watchlist_refresh_cron="30 15 * * 1-5",
            watchlist_refresh_timezone="Asia/Shanghai",
            a_share_calendar_path="config/calendars/a_share.yaml",
        ),
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


def _watchlist_trigger():
    return _parse_cron("30 15 * * 1-5", timezone="Asia/Shanghai")


@pytest.mark.parametrize(
    ("now_utc", "expected"),
    [
        # 2026-10-08 (Thu, trading day) 16:00 Shanghai: after 15:30 -> catch up.
        ("2026-10-08T08:00:00+00:00", date(2026, 10, 8)),
        # 23:59 Shanghai, still the same trading day.
        ("2026-10-08T15:59:00+00:00", date(2026, 10, 8)),
        # 15:00 Shanghai: before the scheduled time -> the cron will fire.
        ("2026-10-08T07:00:00+00:00", None),
        # Exactly the scheduled time counts as passed.
        ("2026-10-08T07:30:00+00:00", date(2026, 10, 8)),
        # 2026-10-07 is a weekday holiday (National Day).
        ("2026-10-07T09:00:00+00:00", None),
        # Saturday: no cron fire at all.
        ("2026-10-10T09:00:00+00:00", None),
        # 2026-10-08T20:00Z is already 2026-10-09 04:00 in Shanghai.
        ("2026-10-08T20:00:00+00:00", None),
        # Unreviewed year fails closed.
        ("2027-01-04T09:00:00+00:00", None),
    ],
)
def test_watchlist_catch_up_date(now_utc: str, expected: date | None) -> None:
    from datetime import datetime

    from investment_agent.scheduler.runner import watchlist_catch_up_date

    calendar = load_trading_calendar(Path("config/calendars/a_share.yaml"))
    assert (
        watchlist_catch_up_date(
            now=datetime.fromisoformat(now_utc),
            trigger=_watchlist_trigger(),
            calendar=calendar,
        )
        == expected
    )


def test_build_scheduler_sets_misfire_grace_and_coalesce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_agent.scheduler.runner import MISFIRE_GRACE_SECONDS

    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    cfg = AppYamlConfig(
        scheduler=SchedulerConfig(
            watchlist_refresh_cron="30 15 * * 1-5",
        ),
    )

    sched = build_scheduler(settings=settings, app_config=cfg)
    jobs = sched.get_jobs()

    assert {job.id for job in jobs} == {"watchlist_refresh"}
    for job in jobs:
        assert job.misfire_grace_time == MISFIRE_GRACE_SECONDS
        assert job.coalesce is True


@pytest.mark.asyncio
async def test_startup_check_warns_on_expiring_calendar_and_catches_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from structlog.testing import capture_logs

    from investment_agent.scheduler import runner

    calls: list[str] = []

    async def fake_job(calendar_path: str) -> None:
        calls.append(calendar_path)

    monkeypatch.setattr(runner, "_run_watchlist_refresh_job", fake_job)
    cfg = AppYamlConfig(
        scheduler=SchedulerConfig(
            watchlist_refresh_cron="30 15 * * 1-5",
        ),
    )

    with capture_logs() as logs:
        # 2026-11-03 (Tue) 17:00 Shanghai: 58 days of coverage left.
        await runner._startup_watchlist_check(
            cfg, datetime.fromisoformat("2026-11-03T09:00:00+00:00")
        )

    warnings = [e for e in logs if e["event"] == "scheduler.calendar.expiring_soon"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["coverage_end"] == "2026-12-31"
    assert warnings[0]["days_remaining"] == 58
    assert calls == ["config/calendars/a_share.yaml"]
    assert any(e["event"] == "scheduler.watchlist.catch_up" for e in logs)


@pytest.mark.asyncio
async def test_startup_check_does_not_catch_up_before_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from structlog.testing import capture_logs

    from investment_agent.scheduler import runner

    calls: list[str] = []

    async def fake_job(calendar_path: str) -> None:
        calls.append(calendar_path)

    monkeypatch.setattr(runner, "_run_watchlist_refresh_job", fake_job)
    cfg = AppYamlConfig(
        scheduler=SchedulerConfig(
            watchlist_refresh_cron="30 15 * * 1-5",
        ),
    )

    with capture_logs() as logs:
        await runner._startup_watchlist_check(
            cfg, datetime.fromisoformat("2026-06-01T01:00:00+00:00")
        )

    assert calls == []
    assert [e["log_level"] for e in logs if e["event"].startswith("scheduler.calendar")] == [
        "info"
    ]


@pytest.mark.asyncio
async def test_watchlist_tick_logs_expired_calendar_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from freezegun import freeze_time
    from structlog.testing import capture_logs

    from investment_agent.scheduler import runner

    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    with capture_logs() as logs, freeze_time("2027-01-04T07:30:00Z"):
        await runner._run_watchlist_refresh_job("config/calendars/a_share.yaml")

    events = [e["event"] for e in logs]
    assert "scheduler.calendar.expired" in events
    assert "scheduler.watchlist.calendar_unknown" in events
