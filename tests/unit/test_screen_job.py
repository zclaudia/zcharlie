"""P1-C3: screen_run job (worker + persistence), scheduler fire and ``ia screen`` CLI."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.config import ScreenScheduleConfig, Settings
from investment_agent.research.worker import default_job_handlers
from investment_agent.rules import screen_job
from investment_agent.rules.screen_job import (
    SCREEN_RUN,
    ScreenRunPayload,
    screen_idempotency_key,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.rules import RuleTemplateRecord, ScreenResultRecord
from tests.support.screen_job import (  # noqa: F401
    AS_OF,
    TEMPLATE,
    TICKERS,
    _run_job,
)
from tests.support.screen_job import (
    screen_db as screen_db,
)


def test_screen_run_job_persists_results_and_summary(screen_db) -> None:
    payload = ScreenRunPayload(universe_code="CSI300", as_of=AS_OF)

    async def scenario() -> None:
        run_id = await _run_job(payload)
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None and run.status == "success", run.error_summary if run else None
            assert run.idempotency_key == screen_idempotency_key(payload)
            meta = run.metadata_json
            assert meta["member_count"] == 2
            assert meta["template_sync"] == [
                {"template": "job_test", "version": 1, "action": "created"}
            ]
            counts = meta["templates"]["job_test@1"]["counts"]
            rows = list(
                (
                    await session.execute(
                        select(ScreenResultRecord).order_by(ScreenResultRecord.ticker)
                    )
                ).scalars()
            )
            assert [row.ticker for row in rows] == ["600519.SH", "601398.SH"]
            moutai, icbc = rows
            # Moutai: ROE and gross margin high -> hit. ICBC: gross margin is not
            # applicable (banking) -> skipped; its ~9% ROE alone decides -> miss.
            assert moutai.outcome == "hit" and moutai.rank == 1
            assert moutai.context["industry_group"] == "consumer"
            assert {r["factor"] for r in moutai.reasons} == {"roe", "gross_margin"}
            assert all(r["snapshot_ref"] for r in moutai.reasons)
            assert icbc.outcome == "miss" and icbc.context["industry_group"] == "banking"
            assert [(r["factor"], r["outcome"]) for r in icbc.reasons] == [("roe", "not_met")]
            assert counts == {"hit": 1, "truncated": 0, "miss": 1, "skipped": 0, "excluded": 0}
            assert sum(1 for row in rows if row.hit) == counts["hit"]

        # A retried / re-executed run replaces its rows instead of duplicating them.
        await screen_job.execute_screen_run(payload, run_id=run_id)
        async with session_scope() as session:
            rows = list((await session.execute(select(ScreenResultRecord))).scalars())
            assert len(rows) == 2
            versions = list((await session.execute(select(RuleTemplateRecord))).scalars())
            assert [v.version for v in versions] == [1]  # sync idempotent

    asyncio.run(scenario())


def test_screen_job_fails_without_templates(screen_db, monkeypatch) -> None:
    payload = ScreenRunPayload(universe_code="CSI300", as_of=AS_OF, sync_templates=False)

    async def scenario() -> None:
        run_id = await _run_job(payload)
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            assert run.status in {"failed", "queued"}
            assert run.error_summary == "RuntimeError"

    asyncio.run(scenario())


def test_worker_serves_screen_run_by_default() -> None:
    settings = Settings(api_key="x" * 32, llm_tier3_api_key="x")  # type: ignore[call-arg]
    kinds = [handler.kind for handler in default_job_handlers(settings)]
    assert SCREEN_RUN in kinds


def test_scheduler_fire_enqueues_idempotently(screen_db) -> None:
    from investment_agent.config import AppYamlConfig, SchedulerConfig
    from investment_agent.scheduler.runner import (
        _screen_trigger,
        build_scheduler,
        run_screen_schedule_job,
    )

    cfg = ScreenScheduleConfig()
    trigger = _screen_trigger(cfg)
    assert trigger is not None
    fire = trigger.get_next_fire_time(None, datetime(2026, 10, 7, tzinfo=UTC))
    assert fire is not None and fire.strftime("%a %H:%M") == "Sun 20:00"

    async def scenario() -> None:
        assert await run_screen_schedule_job(cfg, today=date(2026, 10, 11)) == 1
        assert await run_screen_schedule_job(cfg, today=date(2026, 10, 11)) == 0
        async with session_scope() as session:
            run = (
                await session.execute(select(PipelineRun).where(PipelineRun.kind == SCREEN_RUN))
            ).scalar_one()
            assert run.request_json["as_of"] == "2026-10-11"
            assert run.idempotency_key == "screen_run:CSI300:2026-10-11"

    asyncio.run(scenario())
    sched = build_scheduler(
        settings=Settings(),  # type: ignore[call-arg]
        app_config=AppYamlConfig(scheduler=SchedulerConfig(), screen=cfg),
    )
    assert [job.id for job in sched.get_jobs()] == ["screen_run"]
    disabled = AppYamlConfig(scheduler=SchedulerConfig(), screen=ScreenScheduleConfig(cron=None))
    assert build_scheduler(settings=Settings(), app_config=disabled).get_jobs() == []  # type: ignore[call-arg]


def test_cli_screen_run_now_and_show(screen_db) -> None:
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["screen", "run", "--as-of", "2026-09-30", "--now"])
    assert result.exit_code == 0, result.output
    assert "finished: status=success" in result.output
    assert "job_test@1" in result.output
    again = runner.invoke(app, ["screen", "run", "--as-of", "2026-09-30"])
    assert "Existing" in again.output
    rerun = runner.invoke(app, ["screen", "run", "--as-of", "2026-09-30", "--rerun"])
    assert "Queued" in rerun.output

    shown = runner.invoke(app, ["screen", "show", "1", "--reasons"])
    assert shown.exit_code == 0, shown.output
    assert "600519.SH" in shown.output
    assert '"rule_path": "screen.all[0]"' in shown.output
    assert runner.invoke(app, ["screen", "show", "99"]).exit_code == 1
    assert runner.invoke(app, ["screen", "run", "--universe", "NOPE"]).exit_code != 0
