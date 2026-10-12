"""P1-D1 wiring: screen_run creates candidates + drafting jobs; daily expiry sweep."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from investment_agent.candidates.lifecycle import RULE_DRAFT
from investment_agent.config import (
    AppYamlConfig,
    CandidateScheduleConfig,
    SchedulerConfig,
    Settings,
)
from investment_agent.rules import screen_job
from investment_agent.rules.screen_job import ScreenRunPayload
from investment_agent.scheduler.runner import (
    _candidate_trigger,
    build_scheduler,
    run_candidate_expiry_job,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from tests.support.screen_job import AS_OF, _run_job, screen_db  # noqa: F401 - fixture

pytestmark = pytest.mark.usefixtures("screen_db")


def test_screen_run_opens_candidates_and_queues_drafts() -> None:
    payload = ScreenRunPayload(universe_code="CSI300", as_of=AS_OF)

    async def scenario() -> None:
        run_id = await _run_job(payload)
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None and run.status == "success", run.error_summary if run else None
            meta = run.metadata_json["candidates"]
            assert [item["ticker"] for item in meta["created"]] == ["600519.SH"]
            assert meta["drafts_enqueued"] == 1 and meta["merged"] == []
            candidate = (await session.execute(select(CandidateRecord))).scalar_one()
            assert candidate.status == "open" and candidate.draft_state == "pending"
            assert candidate.sources[0]["screen_run_id"] == run_id
            draft = (
                await session.execute(select(PipelineRun).where(PipelineRun.kind == RULE_DRAFT))
            ).scalar_one()
            assert draft.status == "queued"
            assert draft.request_json["template_id"] == "job_test"
            assert draft.request_json["as_of"] == AS_OF.isoformat()

        # A re-executed run neither duplicates candidates nor drafting jobs.
        await screen_job.execute_screen_run(payload, run_id=run_id)
        async with session_scope() as session:
            assert len(list((await session.execute(select(CandidateRecord))).scalars())) == 1
            drafts = (
                await session.execute(select(PipelineRun).where(PipelineRun.kind == RULE_DRAFT))
            ).scalars()
            assert len(list(drafts)) == 1
            nominations = (
                await session.execute(
                    select(DecisionRecord).where(DecisionRecord.kind == "nominate")
                )
            ).scalars()
            assert len(list(nominations)) == 1

    asyncio.run(scenario())


def test_expiry_job_and_schedule() -> None:
    async def scenario() -> None:
        run_id = await _run_job(ScreenRunPayload(universe_code="CSI300", as_of=AS_OF))
        assert run_id
        async with session_scope() as session:
            candidate = (await session.execute(select(CandidateRecord))).scalar_one()
            candidate.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        assert await run_candidate_expiry_job() == 1
        assert await run_candidate_expiry_job() == 0
        async with session_scope() as session:
            candidate = (await session.execute(select(CandidateRecord))).scalar_one()
            assert candidate.status == "expired"

    asyncio.run(scenario())
    cfg = CandidateScheduleConfig()
    trigger = _candidate_trigger(cfg)
    assert trigger is not None
    fire = trigger.get_next_fire_time(None, datetime(2026, 10, 7, tzinfo=UTC))
    assert fire is not None and fire.strftime("%H:%M") == "07:00"
    sched = build_scheduler(
        settings=Settings(),  # type: ignore[call-arg]
        app_config=AppYamlConfig(scheduler=SchedulerConfig(), candidates=cfg),
    )
    assert [job.id for job in sched.get_jobs()] == ["candidate_expiry"]
    off = AppYamlConfig(
        scheduler=SchedulerConfig(), candidates=CandidateScheduleConfig(expiry_sweep_cron=None)
    )
    assert build_scheduler(settings=Settings(), app_config=off).get_jobs() == []  # type: ignore[call-arg]
