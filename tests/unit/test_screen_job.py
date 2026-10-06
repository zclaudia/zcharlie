"""P1-C3: screen_run job (worker + persistence), scheduler fire and ``ia screen`` CLI."""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.config import ScreenScheduleConfig, Settings
from investment_agent.factors.view import SnapshotView
from investment_agent.research.worker import ResearchWorker, default_job_handlers
from investment_agent.rules import screen_job
from investment_agent.rules.screen_job import (
    SCREEN_RUN,
    ScreenRunHandler,
    ScreenRunPayload,
    enqueue_screen_run,
    screen_idempotency_key,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.models.rules import RuleTemplateRecord, ScreenResultRecord
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.universe import UniverseRepository
from tests.unit._factor_support import baostock_view

AS_OF = date(2026, 9, 30)
TICKERS = {"600519.SH": "600519", "601398.SH": "601398"}

TEMPLATE = {
    "id": "job_test",
    "name": "Job test",
    "applicable_groups": ["consumer", "banking"],
    "screen": {
        "all": [
            {"factor": "roe", "op": ">=", "value": "$roe_min"},
            {"factor": "gross_margin", "op": ">=", "value": 0.5},
        ]
    },
    "candidate_zones": {
        "buy_zone": {"all": [{"factor": "pe_percentile_5y", "op": "<=", "value": 0.3}]},
        "drop": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
    },
    "holding_zones": {
        "add": {"all": [{"factor": "position_weight", "op": "<", "value": 0.1}]},
        "trim": {"any": [{"factor": "gain_vs_cost", "op": ">", "value": 0.5}]},
        "exit_review": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
    },
    "params": {"roe_min": 0.1},
    "param_ranges": {"roe_min": {"min": 0.05, "max": 0.3}},
}


@pytest.fixture
def screen_db(tmp_path, monkeypatch):
    db_path = tmp_path / "screen.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    for key, value in {
        "API_KEY": "secret",
        "LLM_TIER3_API_KEY": "x",
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": url,
        "COLUMNS": "200",
    }.items():
        monkeypatch.setenv(key, value)

    def fake_view(session, ticker: str, as_of: date, **_: object) -> SnapshotView:
        return baostock_view(TICKERS[ticker], as_of)

    async def fake_load(session, ticker, as_of, **kwargs):  # noqa: ANN001
        return fake_view(session, ticker, as_of)

    monkeypatch.setattr(screen_job, "load_snapshot_view", fake_load)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "job_test.yaml").write_text(yaml.safe_dump(TEMPLATE), encoding="utf-8")
    monkeypatch.setattr(screen_job, "load_templates", _templates_from(templates))

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            await UniverseRepository(session).ensure_universe("CSI300")
            for ticker in TICKERS:
                listing = await ListingRepository(session).ensure_a_share_listing(ticker)
                session.add(
                    UniverseMembership(
                        universe_code="CSI300",
                        listing_id=listing.id,
                        valid_from=date(2020, 1, 1),
                        source="test",
                    )
                )

    asyncio.run(create())
    return url


def _templates_from(directory: Path):  # noqa: ANN202
    from investment_agent.rules.templates import load_templates

    def load(_: Path | None = None, **kwargs):  # noqa: ANN001, ANN202
        return load_templates(directory, **kwargs)

    return load


async def _run_job(payload: ScreenRunPayload) -> int:
    settings = Settings()  # type: ignore[call-arg]
    async with session_scope() as session:
        run, created = await enqueue_screen_run(session, payload, triggered_by="test")
        run_id = run.id
    assert created
    worker = ResearchWorker(
        settings=settings,
        worker_id=f"test:{socket.gethostname()}:{os.getpid()}",
        handlers=[ScreenRunHandler()],
    )
    await worker.run_once(run_id=run_id)
    return run_id


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
            assert "rule templates" in (run.error_summary or "")

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
