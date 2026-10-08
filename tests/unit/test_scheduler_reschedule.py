"""Scheduler follows the runtime schedule in the settings store (settings-plan S3)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from investment_agent.config import Settings
from investment_agent.scheduler import runner
from investment_agent.settings.loader import ensure_imported, load_app_config
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base

NO_YAML = Path("does-not-exist.yaml")
SHANGHAI = "Asia/Shanghai"


def _settings(tmp_path: Path, **values: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, api_key="test", llm_tier3_api_key="k", data_dir=tmp_path, **values
    )


async def _apply(values: dict[str, object]) -> None:
    async with session_scope() as session:
        await SettingsStore(session).apply(values, actor="ui")


def _expected_next(cron: str) -> datetime:
    trigger = runner._parse_cron(cron, timezone=SHANGHAI)
    next_fire = trigger.get_next_fire_time(None, datetime.now(ZoneInfo(SHANGHAI)))
    assert next_fire is not None
    return next_fire


async def _wait_for(predicate) -> None:  # noqa: ANN001
    """Poll ``predicate`` (log captures / counters have no event to await)."""

    async with asyncio.timeout(3.0):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.fixture
async def file_db(tmp_path):
    """A disk SQLite: unlike the shared in-memory connection, a concurrent
    reader never sees another session's uncommitted (flushed) writes."""

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
async def started(file_db, tmp_path):
    """A running scheduler built from the store, plus its settings and revision."""

    settings = _settings(tmp_path)
    async with session_scope() as session:
        await ensure_imported(session, settings, NO_YAML)
    await _apply(
        {"schedule.screen.cron": "0 20 * * sun", "schedule.verification.dispatch_cron": None}
    )
    async with session_scope() as session:
        config, revision = await load_app_config(session, settings, NO_YAML)
    sched = runner.build_scheduler(settings=settings, app_config=config)
    sched.start()
    yield sched, settings, revision
    sched.shutdown(wait=False)


async def _watch(sched, settings, revision):  # noqa: ANN001, ANN202
    return asyncio.create_task(
        runner.watch_settings(sched, settings, NO_YAML, revision=revision, poll_seconds=0.01)
    )


async def test_reschedules_when_the_revision_moves(started) -> None:
    sched, settings, revision = started
    assert "verification_dispatch" not in {job.id for job in sched.get_jobs()}
    assert sched.get_job("screen_run").next_run_time == _expected_next("0 20 * * sun")

    with capture_logs() as logs:
        task = await _watch(sched, settings, revision)
        await _apply(
            {
                "schedule.screen.cron": "15 6 * * mon",
                "schedule.verification.dispatch_cron": "*/30 * * * *",
                "schedule.candidates.expiry_sweep_cron": None,
            }
        )
        await _wait_for(lambda: any(e["event"] == "scheduler.rescheduled" for e in logs))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    (event,) = [entry for entry in logs if entry["event"] == "scheduler.rescheduled"]
    assert event["revision"] == revision + 1
    ids = {job.id for job in sched.get_jobs()}
    assert {"screen_run", "verification_dispatch"} <= ids
    assert "candidate_expiry" not in ids
    assert set(event["jobs"]) == ids
    screen = sched.get_job("screen_run")
    assert screen.next_run_time == _expected_next("15 6 * * mon")
    assert event["jobs"]["screen_run"] == screen.next_run_time.isoformat()
    assert sched.get_job("screen_run").args[0].cron == "15 6 * * mon"


async def test_unchanged_revision_leaves_jobs_untouched(started, monkeypatch) -> None:
    sched, settings, revision = started
    before = {job.id: (job, job.next_run_time) for job in sched.get_jobs()}
    calls: list[int] = []
    reads: list[int] = []
    real_read = runner._read_revision

    async def counting_read(value: Settings) -> int:
        reads.append(1)
        return await real_read(value)

    monkeypatch.setattr(runner, "reschedule", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(runner, "_read_revision", counting_read)
    task = await _watch(sched, settings, revision)
    await _wait_for(lambda: len(reads) >= 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == []
    after = {job.id: (job, job.next_run_time) for job in sched.get_jobs()}
    assert after.keys() == before.keys()
    for job_id, (job, next_run) in before.items():
        assert after[job_id][1] == next_run
        assert after[job_id][0].trigger is job.trigger


async def test_failed_rebuild_keeps_the_current_jobs(started, monkeypatch) -> None:
    sched, settings, revision = started
    before = {job.id for job in sched.get_jobs()}

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("investment_agent.settings.loader.app_config_from", broken)
    with capture_logs() as logs:
        task = await _watch(sched, settings, revision)
        await _apply({"schedule.screen.cron": "15 6 * * mon"})
        await _wait_for(lambda: any(e["event"] == "scheduler.reschedule_failed" for e in logs))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert {job.id for job in sched.get_jobs()} == before
    assert sched.get_job("screen_run").next_run_time == _expected_next("0 20 * * sun")


async def test_run_scheduler_imports_and_builds_from_the_store(
    file_db, tmp_path, monkeypatch
) -> None:
    yaml_path = tmp_path / "app.yaml"
    yaml_path.write_text(
        "scheduler:\n  watchlist_refresh_cron: '30 19 * * 1-5'\n"
        "screen:\n  cron: '0 20 * * sun'\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    for name in (
        "_startup_watchlist_check",
        "_startup_snapshot_check",
        "_startup_screen_check",
        "_startup_verification_check",
    ):

        async def skip(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(runner, name, skip)
    startups: list[int] = []

    async def no_sweep() -> None:
        startups.append(1)

    monkeypatch.setattr(runner, "run_candidate_expiry_job", no_sweep)
    built: list[object] = []
    real_build = runner.build_scheduler

    def capture(**kwargs: object):  # noqa: ANN202
        sched = real_build(**kwargs)  # type: ignore[arg-type]
        built.append(sched)
        return sched

    monkeypatch.setattr(runner, "build_scheduler", capture)
    with capture_logs() as logs:
        task = asyncio.create_task(
            runner._run_scheduler(settings, str(yaml_path), poll_seconds=0.01)
        )
        await _wait_for(lambda: any(e["event"] == "scheduler.started" for e in logs))
        sched = built[0]
        # Imported from app.yaml: sections it omits import as disabled.
        assert {job.id for job in sched.get_jobs()} == {"watchlist_refresh", "screen_run"}  # type: ignore[attr-defined]
        await _apply({"schedule.screen.cron": "15 6 * * mon"})
        await _wait_for(lambda: any(e["event"] == "scheduler.rescheduled" for e in logs))
        assert sched.get_job("screen_run").next_run_time == _expected_next("15 6 * * mon")  # type: ignore[attr-defined]
        task.cancel()
        await task  # the runner swallows the cancellation and shuts down
    assert startups == []  # candidates section absent: no start-up sweep, none on reschedule
    assert [e["event"] for e in logs].count("scheduler.started") == 1


async def test_watchlist_refresh_reads_settings_from_the_store(
    file_db, tmp_path, monkeypatch
) -> None:
    from datetime import date

    settings = _settings(tmp_path, ifind_proxy_api_key="env-key")
    async with session_scope() as session:
        await ensure_imported(session, settings, NO_YAML)
    await _apply({"ifind_proxy_api_key": ""})
    seen: list[bool] = []

    async def fake_enqueue(session, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append(kwargs["ifind_available"])
        return []

    class TradingEveryDay:
        def is_trading_day(self, _day: date) -> bool:
            return True

    async def load() -> Settings:
        from investment_agent.settings.loader import load_settings

        return await load_settings(base=settings)

    monkeypatch.setattr(runner, "enqueue_watchlist_refresh", fake_enqueue)
    monkeypatch.setattr(runner, "load_settings", load)
    created = await runner._enqueue_scheduled_watchlist_refresh(
        calendar=TradingEveryDay(),  # type: ignore[arg-type]
        trading_date=date(2026, 10, 9),
    )
    assert created == 0
    assert seen == [False]  # the key cleared in the store wins over env
