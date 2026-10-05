from datetime import UTC, datetime, timedelta

import pytest

from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)


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


async def test_enqueue_rejects_same_key_with_different_input(db_session):
    repository = PipelineRunRepository(db_session)
    await repository.enqueue(
        kind="ifind_research",
        idempotency_key="same-key",
        triggered_by="api",
        request_json={"report_year": 2025},
    )

    with pytest.raises(IdempotencyPayloadMismatchError):
        await repository.enqueue(
            kind="ifind_research",
            idempotency_key="same-key",
            triggered_by="api",
            request_json={"report_year": 2024},
        )


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


async def test_claim_next_reserves_oldest_available_run(db_session):
    repository = PipelineRunRepository(db_session)
    first, _ = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="first",
        triggered_by="api",
        request_json={"ticker": "600406.SH"},
    )
    second, _ = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="second",
        triggered_by="api",
    )

    claimed = await repository.claim_next(worker_id="worker-1", kind="ifind_research")

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status == "running"
    assert claimed.stage == "claimed"
    assert claimed.worker_id == "worker-1"
    assert claimed.attempt_count == 1
    assert claimed.request_json == {"ticker": "600406.SH"}
    next_claimed = await repository.claim_next(
        worker_id="worker-2", kind="ifind_research"
    )
    assert next_claimed is not None
    assert next_claimed.id == second.id


async def test_recover_stale_run_requeues_then_exhausts_retry_budget(db_session):
    repository = PipelineRunRepository(db_session)
    now = datetime.now(UTC)
    run, _ = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="recoverable",
        triggered_by="api",
        max_attempts=2,
        available_at=now - timedelta(minutes=11),
    )
    first_claim = await repository.claim_next(
        worker_id="dead-worker",
        kind="ifind_research",
        now=now - timedelta(minutes=10),
    )
    assert first_claim is not None

    requeued, failed = await repository.recover_stale(
        kind="ifind_research",
        stale_before=now - timedelta(minutes=5),
        now=now,
    )
    assert requeued == [run.id]
    assert failed == []
    assert run.status == "queued"

    second_claim = await repository.claim_next(
        worker_id="replacement-worker",
        kind="ifind_research",
        now=now,
    )
    assert second_claim is not None
    assert second_claim.id == run.id
    assert second_claim.attempt_count == 2
    second_claim.heartbeat_at = now - timedelta(minutes=10)
    await db_session.flush()

    requeued, failed = await repository.recover_stale(
        kind="ifind_research",
        stale_before=now - timedelta(minutes=5),
        now=now,
    )
    assert requeued == []
    assert failed == [run.id]
    assert run.status == "failed"
    assert run.stage == "failed"
