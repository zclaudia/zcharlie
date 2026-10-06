"""Tests for the PipelineRun repository lifecycle helpers."""

import pytest

from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository


@pytest.mark.asyncio
async def test_pipeline_run_create_returns_running(db_session):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="ifind_research", triggered_by="cli")
    assert run.id is not None
    assert run.status == "running"
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.metadata_json == {}


@pytest.mark.asyncio
async def test_pipeline_run_mark_success_merges_metadata(db_session):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="ifind_research")
    run.metadata_json = {"existing": "value"}
    await db_session.flush()

    await repo.mark_success(run.id, metadata={"new": "data"})
    refreshed = await repo.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "success"
    assert refreshed.finished_at is not None
    assert refreshed.metadata_json == {"existing": "value", "new": "data"}


@pytest.mark.asyncio
async def test_pipeline_run_mark_failed_sets_error(db_session):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="ifind_research")

    await repo.mark_failed(run.id, "ifind-proxy timeout")
    refreshed = await repo.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error_summary == "ifind-proxy timeout"
    assert refreshed.finished_at is not None


@pytest.mark.asyncio
async def test_pipeline_run_mark_partial(db_session):
    repo = PipelineRunRepository(db_session)
    run = await repo.create(kind="ifind_research")

    await repo.mark_partial(run.id, "explanation unavailable")
    refreshed = await repo.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "partial"
    assert refreshed.error_summary == "explanation unavailable"
