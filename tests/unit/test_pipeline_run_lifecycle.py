import pytest

from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository


async def test_enqueue_is_idempotent_within_pipeline_kind(db_session):
    repository = PipelineRunRepository(db_session)

    first, first_created = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="research:600406:2026-10-05",
        triggered_by="api",
    )
    second, second_created = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="research:600406:2026-10-05",
        triggered_by="api",
    )

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert second.status == "queued"
    assert second.stage == "queued"


async def test_run_lifecycle_tracks_stage_attempt_and_heartbeat(db_session):
    repository = PipelineRunRepository(db_session)
    run, _ = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="fixture",
        triggered_by="api",
    )

    await repository.mark_running(run.id, stage="fetching")
    assert run.status == "running"
    assert run.stage == "fetching"
    assert run.attempt_count == 1
    assert run.heartbeat_at is not None

    first_heartbeat = run.heartbeat_at
    await repository.set_stage(run.id, "scoring")
    assert run.stage == "scoring"
    assert run.heartbeat_at is not None
    assert run.heartbeat_at >= first_heartbeat

    await repository.mark_success(run.id, {"business_score": 55.22})
    assert run.status == "success"
    assert run.stage == "completed"
    assert run.metadata_json["business_score"] == 55.22


async def test_enqueue_rejects_invalid_idempotency_keys(db_session):
    repository = PipelineRunRepository(db_session)

    with pytest.raises(ValueError, match="1 to 128"):
        await repository.enqueue(
            kind="ifind_research",
            idempotency_key=" ",
            triggered_by="api",
        )

    with pytest.raises(ValueError, match="1 to 128"):
        await repository.enqueue(
            kind="ifind_research",
            idempotency_key="x" * 129,
            triggered_by="api",
        )
