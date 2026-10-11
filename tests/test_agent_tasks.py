"""S08/S10 deterministic durable execution, without model or HTTP calls."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from investment_agent.agent.contracts import ContextBinding, WaitRequirement
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.tasks import (
    ReconciliationRequiredError,
    TaskBoundaryError,
    TaskService,
)
from investment_agent.config import Settings
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investor import MandateHead
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)

NOW = datetime(2026, 10, 10, tzinfo=UTC)
BINDING = ContextBinding(context_refs=(), base_refs=(), as_of=NOW)


async def claimed(session):
    service = TaskService(session)
    task = await service.create(input_key=str(uuid4()), binding=BINDING)
    run = await PipelineRunRepository(session).claim_next(worker_id="worker", kinds=["agent_turn"])
    claim = await service.claim(task.id, RunLease(run.id, "worker", run.attempt_count))
    await session.commit()
    return service, task, claim


@pytest.mark.asyncio
async def test_commit_then_return_failure_replays_receipt_across_sessions(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(
        claim, intent_key="mandate:create:v1", parameters={"scope": "company"}
    )
    operation_id = UUID(action.operation_id)
    await db_session.commit()
    calls = []

    async def write(session, operation):
        calls.append(operation)
        session.add(MandateHead(owner_id="local_owner", mandate_id=str(operation), revision=1))
        return {"mandate_id": str(operation)}

    result = await service.settle_action(claim, operation_id, write)
    await db_session.commit()  # process dies here, before executor receives tool result
    async with session_scope() as recovered:
        saved = await TaskService(recovered).recovery_actions(task.id)
        assert saved[0].result == result
        assert await TaskService(recovered).settle_action(claim, operation_id, write) == result
    assert calls == [operation_id]
    with pytest.raises(IdempotencyPayloadMismatchError):
        await service.prepare_action(
            claim, intent_key="mandate:create:v1", parameters={"scope": "other"}
        )


@pytest.mark.asyncio
async def test_callback_and_receipt_rollback_on_lease_loss(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(claim, intent_key="write", parameters={})
    await db_session.commit()

    async def write(session, operation):
        session.add(MandateHead(owner_id="local_owner", mandate_id=str(operation), revision=1))
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == claim.lease.run_id)
            .values(worker_id="other")
        )
        return {"saved": True}

    with pytest.raises(LeaseLostError):
        await service.settle_action(claim, UUID(action.operation_id), write)
    assert await db_session.get(MandateHead, "local_owner") is None
    assert (await service.recovery_actions(task.id))[0].state == "prepared"


@pytest.mark.asyncio
async def test_wait_releases_slot_and_dependencies_resume_once(db_session):
    service, task, claim = await claimed(db_session)
    first, second = uuid4(), uuid4()
    waiting = WaitRequirement(
        required_input="data", dependency_ids=(first, second), deadline=NOW + timedelta(days=1)
    )
    await service.checkpoint(claim, checkpoint={"step": "waiting"}, waiting=waiting)
    await db_session.commit()
    assert (await db_session.get(PipelineRun, claim.lease.run_id)).status == "success"
    assert (await service.get_task(task.id)).status == TaskStatus.WAITING_DATA
    _, token = await service.get_checkpoint(task.id)
    first_event, second_event = uuid4(), uuid4()
    assert not await service.resume(
        task.id, first, wait_token=token, completion_event_id=first_event, now=NOW
    )
    assert not await service.resume(
        task.id, first, wait_token=token, completion_event_id=first_event, now=NOW
    )
    assert await service.resume(
        task.id, second, wait_token=token, completion_event_id=second_event, now=NOW
    )
    assert not await service.resume(
        task.id, second, wait_token=token, completion_event_id=second_event, now=NOW
    )
    assert len((await db_session.scalars(select(PipelineRun))).all()) == 2
    events = await service.get_events(task.id)
    assert events[0].attempt_id is None
    assert events[1].attempt_id == claim.attempt_id
    assert [e.event_id for e in events] == sorted({e.event_id for e in events})


@pytest.mark.asyncio
@pytest.mark.parametrize("has_artifact", [False, True])
async def test_deadline_true_terminal_and_late_input_requires_followup(db_session, has_artifact):
    from investment_agent.investor.common import ObjectRef

    service, task, claim = await claimed(db_session)
    dependency = uuid4()
    artifacts = (ObjectRef(kind="evidence", id=uuid4(), revision=1),) if has_artifact else ()
    await service.checkpoint(
        claim,
        checkpoint={},
        artifact_refs=artifacts,
        waiting=WaitRequirement(required_input="data", dependency_ids=(dependency,), deadline=NOW),
    )
    _, token = await service.get_checkpoint(task.id)
    assert await service.expire_due(now=NOW) == 1
    expired = await service.get_task(task.id)
    assert expired.status == (TaskStatus.PARTIAL if has_artifact else TaskStatus.FAILED)
    assert not await service.resume(
        task.id, dependency, wait_token=token, completion_event_id=uuid4(), now=NOW
    )
    followup = await service.create(
        input_key="late-result", binding=BINDING, parent_task_id=task.id
    )
    assert followup.parent_task_id == task.id and followup.id != task.id
    assert (await service.get_task(task.id)).status == expired.status


@pytest.mark.asyncio
async def test_cancel_preserves_committed_result_and_stops_new_writes(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(claim, intent_key="write", parameters={})

    async def write(session, operation):
        return {"saved": True}

    await service.settle_action(claim, UUID(action.operation_id), write)
    await db_session.commit()
    assert (await service.cancel(task.id)).cancel_requested
    assert (await service.cancel(task.id)).status == TaskStatus.RUNNING
    await service.ack_cancel(claim)
    assert (await service.get_task(task.id)).status == TaskStatus.CANCELLED
    with pytest.raises((TaskBoundaryError, LeaseLostError)):
        await service.prepare_action(claim, intent_key="next", parameters={})
    assert (await service.recovery_actions(task.id))[0].result == {"saved": True}


@pytest.mark.asyncio
async def test_duplicate_input_and_terminal_cannot_reopen(db_session):
    service, task, claim = await claimed(db_session)
    assert (
        await service.create(input_key=(await service._row(task.id)).input_key, binding=BINDING)
    ).id == task.id
    with pytest.raises(IdempotencyPayloadMismatchError):
        await service.create(
            input_key=(await service._row(task.id)).input_key,
            binding=BINDING.model_copy(update={"as_of": NOW + timedelta(days=1)}),
        )
    await service.checkpoint(
        claim, checkpoint={}, terminal="succeeded", stop_reason=StopReason.ANSWERED
    )
    with pytest.raises((LeaseLostError, TaskBoundaryError)):
        await service.checkpoint(claim, checkpoint={})
    assert (await service.cancel(task.id)).status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_external_uncertainty_explicit_reconciliation_after_cancel(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(
        claim, intent_key="provider-write", parameters={}, effect="external"
    )
    operation = UUID(action.operation_id)
    await service.start_external(claim, operation, expected_revision=action.revision)
    await db_session.commit()
    with pytest.raises(ReconciliationRequiredError):
        await service.start_external(claim, operation, expected_revision=action.revision)
    await service.cancel(task.id)
    await service.reconcile_external(
        task.id, operation, expected_revision=action.revision, outcome="unknown"
    )
    assert (await service.recovery_actions(task.id))[0].state == "uncertain"
    await service.reconcile_external(
        task.id,
        operation,
        expected_revision=action.revision,
        outcome="committed",
        result={"provider_receipt": "safe-id"},
    )
    assert (await service.recovery_actions(task.id))[0].state == "committed"
    await service.ack_cancel(claim)
    assert (await service.get_task(task.id)).status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_outer_rollback_also_rolls_back_business_and_action(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(claim, intent_key="write", parameters={})
    operation = UUID(action.operation_id)
    await db_session.commit()

    async def write(session, operation):
        session.add(MandateHead(owner_id="local_owner", mandate_id=str(operation), revision=1))
        return {"saved": True}

    await service.settle_action(claim, operation, write)
    await db_session.rollback()
    assert await db_session.get(MandateHead, "local_owner") is None
    assert (await db_session.get(ToolAction, str(operation))).state == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_real_worker_claim_execute_and_cancel_race(db_session, cancelled):
    service = TaskService(db_session)
    task = await service.create(input_key="worker", binding=BINDING)
    if cancelled:
        await service.cancel(task.id)
    await db_session.commit()
    calls = []

    async def execute(claim, ctx):
        calls.append(claim)
        async with session_scope() as session:
            await TaskService(session).checkpoint(
                claim,
                checkpoint={"done": True},
                terminal="succeeded",
                stop_reason=StopReason.ANSWERED,
            )
        return "completed"

    handler = AgentJobHandler("agent_turn", execute)
    worker = ResearchWorker(
        settings=Settings(), worker_id="worker", handlers=[handler], kinds=["agent_turn"]
    )
    assert (await worker.run_once() is None) == cancelled
    assert len(calls) == (0 if cancelled else 1)
    assert (await service.get_task(task.id)).status == (
        TaskStatus.CANCELLED if cancelled else TaskStatus.SUCCEEDED
    )
    assert await worker.run_once() is None


@pytest.mark.asyncio
async def test_old_completion_event_cannot_unlock_next_wait(db_session):
    service, task, claim = await claimed(db_session)
    dependency, event = uuid4(), uuid4()
    waiting = WaitRequirement(
        required_input="data", dependency_ids=(dependency,), deadline=NOW + timedelta(days=1)
    )
    await service.checkpoint(claim, checkpoint={}, waiting=waiting)
    _, first_token = await service.get_checkpoint(task.id)
    assert await service.resume(
        task.id, dependency, wait_token=first_token, completion_event_id=event, now=NOW
    )
    run = await PipelineRunRepository(db_session).claim_next(
        worker_id="worker", kinds=["agent_turn"]
    )
    next_claim = await service.claim(task.id, RunLease(run.id, "worker", run.attempt_count))
    await service.checkpoint(next_claim, checkpoint={}, waiting=waiting)
    _, next_token = await service.get_checkpoint(task.id)
    assert next_token != first_token
    assert not await service.resume(
        task.id, dependency, wait_token=first_token, completion_event_id=uuid4(), now=NOW
    )
    assert not await service.resume(
        task.id, dependency, wait_token=next_token, completion_event_id=event, now=NOW
    )
    assert (await service.get_task(task.id)).status == TaskStatus.WAITING_DATA
    assert await service.resume(
        task.id, dependency, wait_token=next_token, completion_event_id=uuid4(), now=NOW
    )


@pytest.mark.asyncio
async def test_cancellation_during_blocked_external_call_waits_for_ack(db_session):
    import asyncio

    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(
        claim, intent_key="call", parameters={}, effect="external"
    )
    operation = UUID(action.operation_id)
    await service.start_external(claim, operation, expected_revision=action.revision)
    await db_session.commit()
    started, finish = asyncio.Event(), asyncio.Event()

    async def provider_call():
        started.set()
        await finish.wait()
        return {"provider_receipt": "confirmed"}

    running = asyncio.create_task(provider_call())
    await started.wait()
    stopping = await service.cancel(task.id)
    await db_session.commit()
    assert stopping.status == TaskStatus.RUNNING and stopping.cancel_requested
    assert not running.done()
    with pytest.raises(TaskBoundaryError):
        await service.prepare_action(claim, intent_key="forbidden-next", parameters={})
    finish.set()
    result = await running
    await service.reconcile_external(
        task.id, operation, expected_revision=action.revision, outcome="committed", result=result
    )
    await service.ack_cancel(claim)
    assert (await service.get_task(task.id)).status == TaskStatus.CANCELLED
    assert (await service.recovery_actions(task.id))[0].result == result


@pytest.mark.asyncio
async def test_false_attempt_cannot_write(db_session):
    from dataclasses import replace

    service, task, claim = await claimed(db_session)
    with pytest.raises(TaskBoundaryError):
        await service.prepare_action(
            replace(claim, attempt_id=uuid4()), intent_key="bad", parameters={}
        )


@pytest.mark.asyncio
async def test_new_lease_recovery_reuses_stable_business_action(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(
        claim, intent_key="stable-stage:write", parameters={"expected_revision": 1}
    )
    operation = UUID(action.operation_id)
    calls = []

    async def write(session, operation):
        calls.append(operation)
        return {"saved": True}

    await service.settle_action(claim, operation, write)
    await db_session.commit()
    runs = PipelineRunRepository(db_session)
    await runs.recover_stale(
        stale_before=datetime.now(UTC) + timedelta(days=1), kinds=["agent_turn"]
    )
    run = await runs.claim_next(worker_id="replacement", kinds=["agent_turn"])
    replacement = await service.claim(task.id, RunLease(run.id, "replacement", run.attempt_count))
    same = await service.prepare_action(
        replacement, intent_key="stable-stage:write", parameters={"expected_revision": 1}
    )
    assert same.operation_id == str(operation)
    assert await service.settle_action(replacement, operation, write) == {"saved": True}
    assert calls == [operation]
    with pytest.raises(LeaseLostError):
        await service.prepare_action(claim, intent_key="old-worker", parameters={})


@pytest.mark.asyncio
async def test_actual_foundation_service_and_artifact_receipt_atomic(db_session):
    from investment_agent.agent.tasks import ActionResult
    from investment_agent.investor.common import ObjectRef, Quantity
    from investment_agent.investor.contracts import Mandate, ResearchAuthorization
    from investment_agent.investor.foundations import InvestorService
    from investment_agent.storage.models.investor import MandateRevision

    unknown = Quantity(value=None, unit="CNY", unknown_reason="not provided")
    candidate = Mandate(
        id=uuid4(),
        revision=1,
        created_at=NOW,
        actor="client",
        origin_ref=None,
        goals=(),
        scope_refs=(),
        hard_constraints=(),
        unknowns=(),
        authorization=ResearchAuthorization(
            daily_budget=unknown,
            per_task_budget=unknown,
            max_leads_per_batch=0,
            max_new_tasks_per_batch=0,
            source_refs=(),
        ),
    )
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(
        claim, intent_key="mandate", parameters=candidate.model_dump(mode="json")
    )
    operation = UUID(action.operation_id)
    await db_session.commit()

    async def write(session, operation):
        saved = await InvestorService(session).create_mandate("local_owner", candidate, operation)
        ref = ObjectRef(kind="mandate", id=saved.id, revision=saved.revision)
        return ActionResult({"ref": ref.model_dump(mode="json")}, (ref,))

    await service.settle_action(claim, operation, write)
    assert len((await service.get_task(task.id)).artifact_refs) == 1
    assert (await service.get_events(task.id))[-1].type == "artifact_saved"
    await db_session.rollback()
    assert not (await db_session.scalars(select(MandateRevision))).all()
    assert not (await service.get_task(task.id)).artifact_refs
    assert (await service.get_events(task.id))[-1].type == "task_state"
    await service.settle_action(claim, operation, write)
    await db_session.commit()
    assert (await service.recovery_actions(task.id))[0].state == "committed"


@pytest.mark.asyncio
async def test_worker_failure_settles_business_task(db_session):
    service = TaskService(db_session)
    task = await service.create(input_key="worker-failure", binding=BINDING)
    await db_session.commit()

    async def execute(claim, ctx):
        raise ValueError("deterministic failure")

    worker = ResearchWorker(
        settings=Settings(),
        worker_id="worker",
        handlers=[AgentJobHandler("agent_turn", execute)],
        kinds=["agent_turn"],
    )
    assert await worker.run_once() is not None
    assert (await service.get_task(task.id)).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_external_generation_rejects_stale_independent_reconciliation(db_session):
    service, task, claim = await claimed(db_session)
    action = await service.prepare_action(
        claim, intent_key="external", parameters={}, effect="external"
    )
    operation = UUID(action.operation_id)
    first_generation = await service.start_external(claim, operation, expected_revision=1)
    await db_session.commit()
    async with session_scope() as stale:
        old = await TaskService(stale).recovery_actions(task.id)
        assert old[0].revision == first_generation
        # End read transaction, preserve the stale generation a provider call carries.
        await stale.commit()
        prepared = await service.reconcile_external(
            task.id, operation, expected_revision=first_generation, outcome="not_applied"
        )
        second_generation = await service.start_external(
            claim, operation, expected_revision=prepared
        )
        await db_session.commit()
        with pytest.raises(ReconciliationRequiredError):
            await TaskService(stale).reconcile_external(
                task.id, operation, expected_revision=first_generation, outcome="not_applied"
            )
        await stale.rollback()
        await service.reconcile_external(
            task.id,
            operation,
            expected_revision=second_generation,
            outcome="committed",
            result={"receipt": "second"},
        )
        await db_session.commit()
        with pytest.raises(IdempotencyPayloadMismatchError):
            await TaskService(stale).reconcile_external(
                task.id, operation, expected_revision=first_generation, outcome="unknown"
            )
        await stale.rollback()
    assert (await service.recovery_actions(task.id))[0].result == {"receipt": "second"}
