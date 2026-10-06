from datetime import UTC, datetime, timedelta

import pytest

from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
    lease_scope,
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


async def _claim_with_lease(repository, *, worker_id, now=None):
    claimed = await repository.claim_next(
        worker_id=worker_id, kind="ifind_research", now=now
    )
    assert claimed is not None
    return claimed, RunLease(
        run_id=claimed.id, worker_id=worker_id, attempt=claimed.attempt_count
    )


async def test_stale_worker_cannot_clobber_newer_attempt(db_session):
    repository = PipelineRunRepository(db_session)
    now = datetime.now(UTC)
    run, _ = await repository.enqueue(
        kind="ifind_research",
        idempotency_key="fenced",
        triggered_by="api",
        available_at=now - timedelta(minutes=11),
    )
    _, old_lease = await _claim_with_lease(
        repository, worker_id="slow-worker", now=now - timedelta(minutes=10)
    )
    requeued, _ = await repository.recover_stale(
        kind="ifind_research", stale_before=now - timedelta(minutes=5), now=now
    )
    assert requeued == [run.id]
    _, new_lease = await _claim_with_lease(repository, worker_id="fresh-worker", now=now)
    assert new_lease.attempt == old_lease.attempt + 1

    with pytest.raises(LeaseLostError):
        await repository.set_stage(run.id, "scoring", lease=old_lease)
    with pytest.raises(LeaseLostError):
        await repository.heartbeat(run.id, lease=old_lease)
    with pytest.raises(LeaseLostError):
        await repository.mark_success(run.id, {"x": 1}, lease=old_lease)
    with pytest.raises(LeaseLostError):
        await repository.mark_failed(run.id, "late failure", lease=old_lease)
    # A late retry decision must not flip the live claim back to queued.
    with pytest.raises(LeaseLostError):
        await repository.schedule_retry(
            run.id,
            error_summary="late retry",
            available_at=now,
            lease=old_lease,
        )
    with lease_scope(old_lease), pytest.raises(LeaseLostError):
        # Implicit fencing via the active lease scope (chain code path).
        await repository.mark_partial(run.id, "late partial")

    current = await repository.get(run.id)
    assert current is not None
    assert current.status == "running"
    assert current.worker_id == "fresh-worker"
    assert current.error_summary is None

    await repository.heartbeat(run.id, lease=new_lease)
    with lease_scope(new_lease):
        await repository.mark_success(run.id, {"x": 2})
    assert current.status == "success"
    assert current.metadata_json == {"x": 2}


async def test_mark_failed_never_overwrites_terminal_status(db_session):
    repository = PipelineRunRepository(db_session)
    success = await repository.create(kind="ifind_research")
    await repository.mark_success(success.id)
    partial = await repository.create(kind="ifind_research")
    await repository.mark_partial(partial.id, "explanation failed")

    assert await repository.mark_failed(success.id, "late failure") is False
    assert await repository.mark_failed(partial.id, "late failure") is False
    assert success.status == "success"
    assert partial.status == "partial"
    assert partial.error_summary == "explanation failed"


async def test_schedule_retry_returns_false_when_budget_exhausted(db_session):
    repository = PipelineRunRepository(db_session)
    await repository.enqueue(
        kind="ifind_research",
        idempotency_key="one-shot",
        triggered_by="api",
        max_attempts=1,
    )
    run, lease = await _claim_with_lease(repository, worker_id="w")

    assert (
        await repository.schedule_retry(
            run.id,
            error_summary="retry",
            available_at=datetime.now(UTC),
            lease=lease,
        )
        is False
    )
    assert run.status == "running"
    assert await repository.mark_failed(run.id, "terminal", lease=lease) is True
    assert run.status == "failed"


async def test_recover_stale_ignores_unclaimed_cli_runs(db_session):
    repository = PipelineRunRepository(db_session)
    run = await repository.create(kind="ifind_research", triggered_by="cli")
    run.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    requeued, failed = await repository.recover_stale(
        kind="ifind_research",
        stale_before=datetime.now(UTC) - timedelta(minutes=5),
    )

    assert (requeued, failed) == ([], [])
    assert run.status == "running"
