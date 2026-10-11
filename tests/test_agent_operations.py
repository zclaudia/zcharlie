"""Actual file-SQLite queue claims and independent writer contention, no model IO."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.operations import AGENT_KINDS, CAPACITY, admission, metrics
from investment_agent.agent.tasks import TaskBoundaryError, TaskService
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.monitoring import OperationsCursor
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.test_investor_publication import publication_db as publication_db

NOW = datetime(2026, 10, 11, 8, tzinfo=UTC)


async def enqueue(kind, *, scope=None):
    async with session_scope() as session:
        task = await TaskService(session).create(
            input_key=str(uuid4()),
            kind=kind,
            binding=ContextBinding(
                context_refs=(ObjectRef(kind="case", id=scope, revision=1),) if scope else (),
                base_refs=(),
                as_of=NOW,
            ),
        )
        row = await session.get(AgentTask, str(task.id))
        run = await session.get(PipelineRun, row.run_id)
        run.available_at = NOW
        run.started_at = NOW
        return run.id, task.id


async def claim(*, run_id=None):
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(
            worker_id=str(uuid4()),
            kinds=AGENT_KINDS if run_id is None else None,
            run_id=run_id,
            now=NOW,
        )
        if not run:
            return None
        value = (run.id, run.kind)
        await PipelineRunRepository(session).mark_success(
            run.id, lease=RunLease(run.id, run.worker_id, run.attempt_count)
        )
        return value


async def test_weighted_priority_and_constant_interactive_background_progress(publication_db):
    exploration, _ = await enqueue("agent_discovery")
    review, _ = await enqueue("agent_review")
    interactive = [await enqueue("agent_turn") for _ in range(8)]
    first = await claim()
    assert first[0] == interactive[0][0]
    claimed = [first] + [await claim() for _ in range(4)]
    assert [kind for _, kind in claimed] == ["agent_turn"] * 3 + ["agent_review", "agent_discovery"]
    assert exploration in [r for r, _ in claimed] and review in [r for r, _ in claimed]
    # New interactive arrivals cannot reset persisted weighted slots.
    for _ in range(12):
        await enqueue("agent_turn")
    other, _ = await enqueue("agent_discovery")
    assert other in [(await claim())[0] for _ in range(5)]


async def test_scope_fairness_and_cursor_survives_new_sessions(publication_db):
    case_a, case_b = uuid4(), uuid4()
    first, _ = await enqueue("agent_review", scope=case_a)
    backlog = [await enqueue("agent_review", scope=case_a) for _ in range(9)]
    second, _ = await enqueue("agent_review", scope=case_b)
    assert (await claim())[0] == first
    assert (await claim())[0] == second  # fresh session/worker, not FIFO starvation
    assert (await claim())[0] == backlog[0][0]
    async with session_scope() as session:
        cursor = await session.get(OperationsCursor, 1)
        assert cursor.payload["count"] == 3


async def test_independent_writers_global_caps_explicit_id_and_reserved_interactive(publication_db):
    exploration = [await enqueue("agent_discovery") for _ in range(3)]
    review = [await enqueue("agent_review") for _ in range(4)]
    interactive = [await enqueue("agent_turn") for _ in range(4)]

    async def keep(run_id):
        async with session_scope() as session:
            row = await PipelineRunRepository(session).claim_next(
                worker_id=str(uuid4()), run_id=run_id, now=NOW
            )
            return row.id if row else None

    results = await asyncio.gather(*(keep(run) for run, _ in exploration + review))
    assert sum(r is not None for r in results) == 3
    async with session_scope() as session:
        running = list(
            await session.scalars(select(PipelineRun).where(PipelineRun.status == "running"))
        )
        assert sum(r.kind == "agent_discovery" for r in running) == 1
        assert sum(r.kind == "agent_review" for r in running) == 2
    assert await keep(interactive[0][0]) == interactive[0][0]
    assert await keep(interactive[1][0]) is None
    async with session_scope() as session:
        assert (await metrics(session, now=NOW))["purposes"]["interactive"]["running"] == 1


async def test_atomic_admission_capacity_and_idempotent_repeat(publication_db, monkeypatch):
    monkeypatch.setitem(CAPACITY, "exploration", 2)
    inputs = [await enqueue("agent_discovery") for _ in range(2)]
    async with session_scope() as session:
        assert not await admission(session, "exploration")
    with pytest.raises(TaskBoundaryError, match="queue_capacity"):
        await enqueue("agent_discovery")
    async with session_scope() as session:
        existing = await session.get(AgentTask, str(inputs[0][1]))
        result = await TaskService(session).create(
            input_key=existing.input_key,
            kind="agent_discovery",
            binding=ContextBinding.model_validate(existing.payload["binding"]),
        )
        assert str(result.id) == existing.id
    await claim()
    await enqueue("agent_discovery")


async def test_competing_admissions_never_overfill(publication_db, monkeypatch):
    monkeypatch.setitem(CAPACITY, "interactive", 1)
    results = await asyncio.gather(
        enqueue("agent_turn"), enqueue("agent_turn"), return_exceptions=True
    )
    assert sum(isinstance(r, tuple) for r in results) == 1
    assert sum(isinstance(r, TaskBoundaryError) for r in results) == 1


async def test_retry_recovery_availability_cancel_preserve_business_identity(publication_db):
    run_id, task_id = await enqueue("agent_turn")
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(
            worker_id="first", run_id=run_id, now=NOW
        )
        service = TaskService(session)
        lease = RunLease(run_id, "first", run.attempt_count)
        task_claim = await service.claim(task_id, lease)
        assert task_claim.task_id == task_id
        await PipelineRunRepository(session).schedule_retry(
            run_id, error_summary="fixture", available_at=NOW + timedelta(days=1), lease=lease
        )
    assert await claim(run_id=run_id) is None
    async with session_scope() as session:
        repo = PipelineRunRepository(session)
        run = await repo.claim_next(
            worker_id="next-day", run_id=run_id, now=NOW + timedelta(days=1)
        )
        assert run.request_json["task_id"] == str(task_id) and run.attempt_count == 2
        claim2 = await TaskService(session).claim(task_id, RunLease(run.id, "next-day", 2))
        await TaskService(session).cancel(task_id)
        await TaskService(session).ack_cancel(claim2)
    assert await claim(run_id=run_id) is None
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task_id)).status == "cancelled"


async def test_mixed_legacy_background_saturation_reserves_interactive(publication_db):
    kinds = [*AGENT_KINDS, "ifind_research"]
    native = await enqueue("agent_turn")
    async with session_scope() as session:
        repo = PipelineRunRepository(session)
        legacy = [
            await repo.enqueue(
                kind="ifind_research",
                idempotency_key=str(uuid4()),
                triggered_by="fixture",
                available_at=NOW,
            )
            for _ in range(5)
        ]
        for item, _ in legacy[:3]:
            assert await repo.claim_next(
                worker_id=str(uuid4()), kinds=kinds, run_id=item.id, now=NOW
            )
        assert (
            await repo.claim_next(
                worker_id="no-more-background", kinds=kinds, run_id=legacy[3][0].id, now=NOW
            )
            is None
        )
        assert await repo.claim_next(
            worker_id="interactive", kinds=kinds, run_id=native[0], now=NOW
        )


async def test_persisted_reduced_capacity_runtime_knobs(publication_db):
    from investment_agent.settings.loader import store_for
    from tests.test_agent_conversations import base

    async with session_scope() as session:
        await store_for(session, base()).apply(
            {"agent_queue_max_running": 1, "agent_queue_interactive_capacity": 2}, actor="api"
        )
    first, _ = await enqueue("agent_turn")
    await enqueue("agent_turn")
    with pytest.raises(TaskBoundaryError, match="queue_capacity"):
        await enqueue("agent_turn")
    async with session_scope() as session:
        repo = PipelineRunRepository(session)
        assert await repo.claim_next(worker_id="one", kinds=AGENT_KINDS, run_id=first, now=NOW)
        assert await repo.claim_next(worker_id="two", kinds=AGENT_KINDS, now=NOW) is None
        view = await metrics(session, now=NOW)
        assert view["max_concurrency"] == 1 and view["purposes"]["interactive"]["capacity"] == 2


async def test_aging_bounds_new_account_arrival_starvation(publication_db):
    account = uuid4()
    first, _ = await enqueue("agent_review", scope=account)
    assert (await claim())[0] == first
    old, _ = await enqueue("agent_review", scope=account)
    for _ in range(5):
        await enqueue("agent_review", scope=uuid4())
    async with session_scope() as session:
        row = await session.get(PipelineRun, old)
        row.started_at = NOW - timedelta(seconds=300)
    assert (await claim())[0] == old
