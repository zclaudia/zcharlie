"""Verification schedule (P1-A13): dispatcher idempotency, calibration sampling,
scheduler wiring, ``ia verify`` CLI."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.config import AppYamlConfig, SchedulerConfig, VerificationScheduleConfig
from investment_agent.scheduler.runner import _verification_triggers, build_scheduler
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.verification import VerificationRequest
from investment_agent.verification import enqueue_verification
from investment_agent.verification.job import IFIND_VERIFY, dispatch_slot
from investment_agent.verification.schedule import (
    calibration_sample,
    calibration_week,
    dispatch_verification,
    enqueue_calibration,
)
from tests.unit._snapshot_support import seed_watchlist, settings_for

NOW = datetime(2026, 10, 7, 1, 10, tzinfo=UTC)
TICKERS = [f"{600000 + i:06d}.SH" for i in range(30)]


async def _runs() -> list[PipelineRun]:
    async with session_scope() as session:
        stmt = select(PipelineRun).where(PipelineRun.kind == IFIND_VERIFY).order_by(PipelineRun.id)
        return list((await session.execute(stmt)).scalars())


def test_dispatch_slot_floors_to_half_hours():
    assert dispatch_slot(NOW) == "2026-10-07T01:00Z"
    assert dispatch_slot(NOW + timedelta(minutes=25)) == "2026-10-07T01:30Z"
    assert dispatch_slot(datetime(2026, 10, 7, 9, 59, tzinfo=UTC)) == "2026-10-07T09:30Z"


async def test_dispatcher_is_idempotent_per_slot_and_skips_when_idle(db_session):
    del db_session
    assert (await dispatch_verification(now=NOW)).run_id is None  # nothing due
    ids = await seed_watchlist(["600519.SH"])
    await enqueue_verification(listing_id=ids["600519.SH"], ticker="600519.SH", trigger="promote")

    first = await dispatch_verification(now=NOW)
    assert first.created and first.due == 1
    again = await dispatch_verification(now=NOW + timedelta(minutes=5))
    assert not again.created and again.run_id == first.run_id
    # Next slot while the run is still queued: no second run piles up.
    queued = await dispatch_verification(now=NOW + timedelta(minutes=40))
    assert not queued.created and queued.run_id == first.run_id
    assert len(await _runs()) == 1

    async with session_scope() as session:
        run = await session.get(PipelineRun, first.run_id)
        assert run is not None
        run.status = "success"
    # Same slot after the run finished: the slot key returns the finished run.
    assert not (await dispatch_verification(now=NOW + timedelta(minutes=10))).created
    nxt = await dispatch_verification(now=NOW + timedelta(minutes=40))
    assert nxt.created and nxt.run_id != first.run_id
    runs = await _runs()
    assert [r.idempotency_key for r in runs] == [
        "ifind_verify:2026-10-07T01:00Z",
        "ifind_verify:2026-10-07T01:30Z",
    ]


def test_calibration_sample_is_deterministic_per_week():
    week = calibration_week(date(2026, 10, 11))  # Sunday of ISO week 41
    assert week == "2026-W41"
    first = calibration_sample(TICKERS, week=week, count=10)
    assert first == calibration_sample(list(reversed(TICKERS)), week=week, count=10)
    assert len(first) == 10 and first == sorted(first) and set(first) <= set(TICKERS)
    assert first != calibration_sample(TICKERS, week="2026-W42", count=10)
    assert calibration_sample(TICKERS[:4], week=week, count=10) == sorted(TICKERS[:4])
    assert calibration_sample(TICKERS, week=week, count=0) == []


async def test_calibration_enqueues_the_weekly_sample_once(db_session):
    del db_session
    await seed_watchlist(TICKERS[:12], disabled=(TICKERS[0],))
    sunday = date(2026, 10, 11)
    sample = await enqueue_calibration(today=sunday, count=10)
    assert len(sample) == 10 and TICKERS[0] not in sample
    assert sample == calibration_sample(TICKERS[1:12], week="2026-W41", count=10)
    assert await enqueue_calibration(today=sunday, count=10) == sample  # re-fire: merged
    async with session_scope() as session:
        rows = list((await session.execute(select(VerificationRequest))).scalars())
    assert len(rows) == 10
    assert {(r.trigger, r.fields is None, r.report_period) for r in rows} == {
        ("calibration", True, None)
    }
    assert rows[0].detail == {"week": "2026-W41"}


async def test_calibration_with_an_empty_watchlist_is_a_no_op(db_session):
    del db_session
    assert await enqueue_calibration(today=date(2026, 10, 11), count=10) == []


def test_scheduler_registers_verification_jobs_with_crontab_weekdays(tmp_path):
    cfg = VerificationScheduleConfig()
    triggers = _verification_triggers(cfg)
    assert set(triggers) == {"dispatch", "calibration"}
    friday = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    fire = triggers["calibration"].get_next_fire_time(None, friday)
    assert fire is not None
    assert (fire.date(), fire.hour, fire.minute) == (date(2026, 10, 11), 21, 0)
    assert fire.weekday() == 6  # crontab 0 = Sunday, not Monday
    dispatch = triggers["dispatch"].get_next_fire_time(None, friday + timedelta(minutes=1))
    assert dispatch is not None and dispatch.minute in (0, 30)

    app_config = AppYamlConfig(scheduler=SchedulerConfig(watchlist_refresh_cron=None), verification=cfg)
    sched = build_scheduler(settings=settings_for(tmp_path), app_config=app_config)
    assert {job.id for job in sched.get_jobs()} >= {
        "verification_dispatch",
        "verification_calibration",
    }
    disabled = VerificationScheduleConfig(dispatch_cron=None, calibration_cron=None)
    assert _verification_triggers(disabled) == {}


def test_cli_request_and_status(tmp_path, monkeypatch):
    import asyncio

    from investment_agent.cli.main import app
    from investment_agent.storage.database import init_engine
    from investment_agent.storage.models.base import Base

    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    async def setup() -> None:
        engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await seed_watchlist(["600519.SH"])
        await engine.dispose()

    asyncio.run(setup())
    runner = CliRunner()
    queued = runner.invoke(
        app,
        ["verify", "request", "--ticker", "600519.SH", "--trigger", "conflict",
         "--fields", "debt_ratio,roe", "--period", "2025-12-31"],
    )
    assert queued.exit_code == 0, queued.output
    assert "Queued" in queued.output
    merged = runner.invoke(
        app, ["verify", "request", "--ticker", "600519.SH", "--trigger", "conflict",
              "--fields", "revenue_total", "--period", "2025-12-31"],
    )
    assert "Merged" in merged.output
    assert runner.invoke(app, ["verify", "request", "--ticker", "999999.SH"]).exit_code == 1
    assert runner.invoke(
        app, ["verify", "request", "--ticker", "600519.SH", "--period", "2025-12-01"]
    ).exit_code != 0
    status = runner.invoke(app, ["verify", "status"])
    assert status.exit_code == 0, status.output
    assert "used 0/150" in status.output and "Due now: 1" in status.output
