"""AG09 atomic admission, authentic HTTP, recovery and trusted deliverables."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select, update

from investment_agent.agent.contracts import WaitRequirement
from investment_agent.agent.conversations import (
    ConversationService,
    InvalidContextError,
)
from investment_agent.agent.execution import ExecutionDeniedError, check_boundary
from investment_agent.agent.executor import AgentExecutor, RunSpecification, TaskDisposition
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.tasks import EventCursorExpiredError, TaskService
from investment_agent.investor.common import Mutation, ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.investor.foundations import CreateCase, InvestorService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import (
    AgentTask,
    ConversationRow,
    ExecutionGrantRow,
    MessageRow,
    PublicTaskEvent,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)
from tests.support.conversations import (  # noqa: F401
    base,
    claim,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)


@pytest.mark.asyncio
async def test_admission_receipt_normalization_concurrent_and_feature_off(conversation_db):
    conversation = await new_conversation()
    body = message(text="  e\u0301\r\nquery  ")
    receipts = await asyncio.gather(*(submit(conversation, body) for _ in range(5)))
    assert len({receipt.task.id for receipt in receipts}) == 1
    assert receipts[0].message.text == "é\nquery"
    assert receipts[0].task.message_ref.id == receipts[0].message.id
    repeat = await submit(conversation, body.model_copy(update={"text": "é\nquery"}),
        base(investor_agent_enabled=False))
    assert repeat.task.id == receipts[0].task.id
    with pytest.raises(IdempotencyPayloadMismatchError):
        await submit(conversation, body.model_copy(update={"text": "different"}))
    with pytest.raises(ExecutionDeniedError):
        await submit(conversation, message(), base(investor_agent_enabled=False))
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentTask)) == 1
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 1
        grant = (await session.get(ExecutionGrantRow, str(repeat.task.id))).payload
        assert grant["tool_names"] == ["save_research_progress", "prepare_user_operation"] and grant["scope_refs"] == []


@pytest.mark.asyncio
async def test_outer_rollback_removes_message_task_queue_grant_events(conversation_db):
    conversation = await new_conversation()
    async with session_scope() as session:
        await ConversationService(session, base()).submit(conversation.id, message())
        await session.rollback()
    async with session_scope() as session:
        for model in (MessageRow, AgentTask, PipelineRun, ExecutionGrantRow, PublicTaskEvent):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (await session.get(ConversationRow, str(conversation.id))).next_sequence == 1


async def listings(session):
    session.add(Market(code="SH", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Fixture Company")
    session.add(company)
    await session.flush()
    rows = [Listing(company_id=company.id, market_code="SH", ticker=ticker, is_primary=True) for ticker in ("600001", "600002")]
    session.add_all(rows)
    await session.flush()
    return tuple(ObjectRef(kind="listing", id=row.id, revision=1) for row in rows)


@pytest.mark.asyncio
async def test_fixed_multi_object_binding_and_cross_conversation_reply(conversation_db):
    async with session_scope() as session:
        refs = await listings(session)
    conversation = await new_conversation()
    body = message(context_refs=refs)
    receipt = await submit(conversation, body)
    second = await submit(conversation, message(context_refs=(refs[1],)))
    assert receipt.task.binding.context_refs == refs
    async with session_scope() as session:
        loaded = await TaskService(session).get_task(receipt.task.id)
        assert loaded.binding.context_refs == refs
        grant = (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        assert grant["watchlist"] is False and "request_data_refresh" not in grant["tool_names"]
    with pytest.raises(IdempotencyPayloadMismatchError):
        await submit(conversation, body.model_copy(update={"context_refs": (refs[1],)}))
    with pytest.raises(InvalidContextError):
        await submit(conversation, message(context_refs=(refs[0].model_copy(update={"revision": 2}),)))
    other = await new_conversation()
    with pytest.raises(InvalidContextError):
        await submit(other, message(reply_to=ObjectRef(kind="message", id=second.message.id, revision=1)))
    answered = await submit(conversation, message(reply_to=ObjectRef(kind="message", id=receipt.message.id, revision=1)))
    assert answered.message.reply_to.id == receipt.message.id


@pytest.mark.asyncio
async def test_current_mandate_scope_revalidation_and_exact_case_version(conversation_db):
    async with session_scope() as session:
        refs = await listings(session)
        unknown = Quantity(value=None, unit="USD", unknown_reason="not set")
        mandate = await InvestorService(session).create_mandate("local_owner", Mandate(id=uuid4(),
            revision=1, created_at=datetime.now(UTC), actor="user", origin_ref=None, goals=(),
            scope_refs=(refs[0],), hard_constraints=(), unknowns=(), authorization=ResearchAuthorization(
                daily_budget=unknown, per_task_budget=unknown, max_leads_per_batch=0,
                max_new_tasks_per_batch=0, source_refs=())), uuid4())
        case = await InvestorService(session).create_case("local_owner", CreateCase(
            listing_ref=refs[0], question="Fixture", idempotency_key=uuid4()))
    conversation = await new_conversation()
    case_ref = ObjectRef(kind="case", id=case.id, revision=1)
    accepted = await submit(conversation, message(context_refs=(case_ref, refs[0])))
    assert ObjectRef(kind="mandate", id=mandate.id, revision=1) in accepted.task.binding.base_refs
    execution = await claim(accepted.task)
    async with session_scope() as session:
        permission, _ = await check_boundary(session, execution, base())
        assert permission.scope_refs == (refs[0], case_ref)
        await InvestorService(session).revise_mandate("local_owner",
            mandate.model_copy(update={"revision": 2, "scope_refs": (refs[1],)}),
            Mutation(expected_revision=1, idempotency_key=uuid4()))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="scope_revoked"):
            await check_boundary(session, execution, base())
    # Personal scope revocation remains enforced above; a new, independently
    # authorized company study is not restricted to the portfolio's universe.
    company = await submit(conversation, message(context_refs=(refs[0],), research_kind="company_research"))
    assert all(ref.kind != "mandate" for ref in company.task.binding.base_refs)
    company_claim = await claim(company.task)
    async with session_scope() as session:
        permission, _ = await check_boundary(session, company_claim, base())
        assert permission.mandate_id is None
    with pytest.raises(ExecutionDeniedError):
        await submit(conversation, message(context_refs=(case_ref, ObjectRef(kind="mandate", id=mandate.id, revision=1))))


@pytest.mark.asyncio
async def test_personal_allocation_requires_mandate_at_admission(conversation_db):
    conversation = await new_conversation()
    with pytest.raises(InvalidContextError, match="个人配置建议需要先设置投资约定"):
        await submit(conversation, message(text="给出我的仓位配置", research_kind="portfolio_action"))
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(AgentTask)) == 0


@pytest.mark.asyncio
async def test_retention_empty_table_preserves_id_and_expired_watermark(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    async with session_scope() as session:
        old = (await TaskService(session).get_events(receipt.task.id))[0].event_id
        await session.execute(update(PublicTaskEvent).values(occurred_at=datetime.now(UTC) - timedelta(days=31)))
        assert await TaskService(session).retain_events(limit=1) == 1
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PublicTaskEvent)) == 0
        with pytest.raises(EventCursorExpiredError):
            await TaskService(session).get_events(receipt.task.id, old)
        new = await TaskService(session).append_event(receipt.task.id, None, "progress", {"summary": "Public progress"})
        assert new.event_id > old
        assert [event.event_id for event in await TaskService(session).get_events(receipt.task.id, 0)] == [new.event_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "waiting", "running", "backoff", "terminal"])
async def test_cancel_queue_wait_running_backoff_terminal(conversation_db, state):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    execution = None
    if state != "queued":
        execution = await claim(receipt.task)
    async with session_scope() as session:
        service = TaskService(session)
        if state == "waiting":
            await service.checkpoint(execution, checkpoint={}, waiting=WaitRequirement(required_input="Wait",
                dependency_ids=(uuid4(),), deadline=datetime.now(UTC) + timedelta(minutes=1)))
        elif state == "terminal":
            await service.checkpoint(execution, checkpoint={}, terminal="failed", stop_reason=StopReason.EVIDENCE_BLOCKED)
        elif state == "backoff":
            await session.execute(update(PipelineRun).where(PipelineRun.id == execution.lease.run_id)
                .values(status="queued", worker_id=None, available_at=datetime.now(UTC)+timedelta(hours=1)))
        cancelled = await service.cancel(receipt.task.id)
        assert cancelled == await service.cancel(receipt.task.id)
        assert cancelled.status == (TaskStatus.RUNNING if state == "running" else TaskStatus.FAILED if state == "terminal" else TaskStatus.CANCELLED)
        if state == "running":
            assert cancelled.cancel_requested and cancelled.stop_reason is None
            assert (await service.ack_cancel(execution)).status == TaskStatus.CANCELLED
    async with session_scope() as session:
        if state in {"queued", "backoff"}:
            assert await PipelineRunRepository(session).claim_next(worker_id="other") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("certified", [False, True])
async def test_real_worker_only_trusted_completion_saves_deliverable(conversation_db, certified):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    async def complete(session, execution, outcome):
        assert outcome.output.summary == "private draft"
        await ConversationService(session, base()).save_assistant(execution, text="Certified fixture answer")
        return TaskDisposition("succeeded", StopReason.ANSWERED)
    model = FunctionModel(lambda messages, info: ModelResponse([TextPart('{"summary":"private draft"}')],
        usage=RequestUsage(input_tokens=10, output_tokens=2)))
    executor = AgentExecutor(base(), model=model, specification=RunSpecification(completion=complete if certified else None))
    worker = ResearchWorker(settings=base(), worker_id="fixture", handlers=[AgentJobHandler("agent_turn", executor)])
    await worker.run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        saved = await ConversationService(session, base()).messages(conversation.id)
        assert len(saved) == (2 if certified else 1)
        assert task.status == (TaskStatus.SUCCEEDED if certified else TaskStatus.FAILED)
        if certified:
            assert saved[1].text == "Certified fixture answer" and saved[1].context_refs == receipt.message.context_refs
        assert "private draft" not in str([item.model_dump() for item in saved])


@pytest.mark.asyncio
async def test_assistant_and_terminal_checkpoint_rollback_together(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    execution = await claim(receipt.task)
    async with session_scope() as session:
        svc = ConversationService(session, base())
        saved = await svc.save_assistant(execution, text="Deliverable")
        assert saved == await svc.save_assistant(execution, text="Deliverable")
        with pytest.raises(IdempotencyPayloadMismatchError):
            await svc.save_assistant(execution, text="Changed")
        await TaskService(session).checkpoint(execution, checkpoint={}, terminal="succeeded", stop_reason=StopReason.ANSWERED)
        await session.rollback()
    async with session_scope() as session:
        assert len(await ConversationService(session, base()).messages(conversation.id)) == 1
        assert (await TaskService(session).get_task(receipt.task.id)).status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_default_worker_runs_bounded_event_maintenance(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    async with session_scope() as session:
        await TaskService(session).cancel(receipt.task.id)
        await session.execute(update(PublicTaskEvent).values(occurred_at=datetime.now(UTC)-timedelta(days=31)))
    worker = ResearchWorker(settings=base(investor_agent_enabled=False), worker_id="maintenance")
    assert await worker.run_once() is None
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PublicTaskEvent)) == 0
        assert (await TaskService(session).get_task(receipt.task.id)).status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_idle_polls_throttle_maintenance_until_interval_elapsed(conversation_db, monkeypatch):
    from investment_agent.agent.tasks import TaskService as Service

    worker = ResearchWorker(settings=base(investor_agent_enabled=False), worker_id="throttle")
    monotonic = [1000.0]
    monkeypatch.setattr("investment_agent.research.worker.time.monotonic", lambda: monotonic[0])
    calls = []
    original = Service.retain_events
    async def counted(self, *, now=None, limit=500):
        calls.append(1)
        return await original(self, now=now, limit=limit)
    monkeypatch.setattr(Service, "retain_events", counted)
    assert await worker.run_once() is None  # first poll maintains immediately
    assert await worker.run_once() is None  # idle poll inside the interval skips it
    assert len(calls) == 1
    monotonic[0] += 30.0
    assert await worker.run_once() is None  # interval elapsed: maintained again
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_research_kind_keyword_classification_is_documented_and_overridable(conversation_db):
    conversation = await new_conversation()
    auto = await submit(conversation, message(text="比较这两家公司的现金流"))
    override = await submit(conversation, message(text="比较这两家公司的现金流", research_kind="local_check"))
    async with session_scope() as session:
        auto_grant = (await session.get(ExecutionGrantRow, str(auto.task.id))).payload
        override_grant = (await session.get(ExecutionGrantRow, str(override.task.id))).payload
        assert auto_grant["research_kind"] == "comparison"  # server-side keyword default
        assert override_grant["research_kind"] == "local_check"  # explicit input wins


@pytest.mark.asyncio
async def test_snapshot_real_read_transaction_cannot_skip_concurrent_terminal(conversation_db, monkeypatch):
    from investment_agent.api.routes.agent import snapshot
    conversation = await new_conversation()
    receipt = await submit(conversation)
    original = TaskService.get_task
    written = False
    async def task_then_writer(self, task_id):
        nonlocal written
        task = await original(self, task_id)
        if not written:
            written = True
            async with session_scope() as writer:
                await TaskService(writer).cancel(task_id)
        return task
    monkeypatch.setattr(TaskService, "get_task", task_then_writer)
    async with session_scope() as reader:
        saved = await snapshot(receipt.task.id, ConversationService(reader, base()))
    assert saved["task"]["status"] == "queued"
    async with session_scope() as session:
        events = await TaskService(session).get_events(receipt.task.id, saved["event_cursor"])
        assert len(events) == 1 and events[0].payload.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_sse_real_read_snapshot_cannot_close_on_unread_terminal(conversation_db, monkeypatch):
    from investment_agent.api.routes.agent import _active_streams, event_stream, read_events
    conversation = await new_conversation()
    receipt = await submit(conversation)
    original = TaskService.get_events
    written = False
    async def events_then_writer(self, task_id, after_event_id=0):
        nonlocal written
        events = await original(self, task_id, after_event_id)
        if not written:
            written = True
            async with session_scope() as writer:
                await TaskService(writer).cancel(task_id)
        return events
    monkeypatch.setattr(TaskService, "get_events", events_then_writer)
    task, events = await read_events(receipt.task.id, 0)
    assert task.status == TaskStatus.QUEUED and len(events) == 1
    class Connected:
        async def is_disconnected(self):
            return False
    token = uuid4()
    _active_streams.add(token)
    stream = event_stream(Connected(), receipt.task.id, events[-1]["event_id"], token)
    output = [frame async for frame in stream]
    assert any('"status": "cancelled"' in frame for frame in output)
    assert token not in _active_streams


@pytest.mark.asyncio
async def test_evidence_and_snapshot_exact_versions_derive_trusted_subject(conversation_db):
    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.storage.models.evidence import SavedEvidence
    from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
    async with session_scope() as session:
        refs = await listings(session)
        now = datetime.now(UTC)
        record = EvidenceRecord(id=uuid4(), revision=1, created_at=now, actor="user", origin_ref=None,
            source_ref=refs[0], source_kind="user_report", locator="submission", content_hash="ignored",
            subject_ref=refs[0], period=None, published_at=now, acquired_at=now, available_from=now,
            verification="user_report", conflict="none", text="User report")
        evidence = await EvidenceService(session).save_evidence("local_owner", record, uuid4())
        snapshot = FinancialSnapshot(listing_id=refs[1].id, report_period=datetime(2025,12,31).date(),
            period_kind="annual", available_from=now.date(), availability="actual", fields={},
            provider="fixture", captured_at=now, version=2)
        session.add(snapshot)
        await session.flush()
        snapshot_ref = ObjectRef(kind="financial_snapshot", id=snapshot.id, revision=2)
        private_id = uuid4()
        session.add(SavedEvidence(id=str(private_id), revision=1, owner_id="another-owner", payload=record.model_dump(mode="json")))
    conversation = await new_conversation()
    body = message(context_refs=(evidence, snapshot_ref))
    receipt = await submit(conversation, body)
    async with session_scope() as session:
        grant = (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        assert {ref["id"] for ref in grant["scope_refs"]} == {refs[0].id, refs[1].id}
        assert evidence in receipt.task.binding.base_refs
        assert snapshot_ref in receipt.task.binding.base_refs
    for invalid in (evidence.model_copy(update={"revision":2}), snapshot_ref.model_copy(update={"revision":1}),
        ObjectRef(kind="evidence", id=private_id, revision=1)):
        with pytest.raises(InvalidContextError):
            await submit(conversation, message(context_refs=(invalid,)))
    answer = await submit(conversation, message(context_refs=(evidence.model_copy(update={"field_path":"text"}),)))
    assert answer.message.context_refs[0].field_path == "text"


@pytest.mark.asyncio
async def test_archive_hides_conversation_blocks_new_messages_and_restores(conversation_db):
    from investment_agent.agent.conversations import ConversationArchivedError

    conversation = await new_conversation()
    async with session_scope() as session:
        service = ConversationService(session, base())
        state = await service.archive(conversation.id)
        assert state.archived_at is not None
        assert (await service.archive(conversation.id)).archived_at == state.archived_at  # idempotent
        assert [c.id for c in await service.list()] == []
        assert (await service.summaries()).items == ()
        archived = (await service.summaries(archived=True)).items
        assert [item.id for item in archived] == [conversation.id] and archived[0].archived_at is not None
        assert archived[0].purge_at == archived[0].archived_at + timedelta(days=30)
        assert await service.get(conversation.id)  # still readable
    with pytest.raises(ConversationArchivedError):
        await submit(conversation)
    async with session_scope() as session:
        service = ConversationService(session, base())
        assert (await service.unarchive(conversation.id)).archived_at is None
        assert [c.id for c in await service.list()] == [conversation.id]
        assert (await service.summaries(archived=True)).items == ()
    assert (await submit(conversation)).task.id


@pytest.mark.asyncio
async def test_archive_refused_while_a_task_is_unfinished(conversation_db):
    from investment_agent.agent.conversations import ConversationActiveError

    conversation = await new_conversation()
    receipt = await submit(conversation)
    async with session_scope() as session:
        with pytest.raises(ConversationActiveError):
            await ConversationService(session, base()).archive(conversation.id)
    async with session_scope() as session:
        await TaskService(session).cancel(receipt.task.id)
    async with session_scope() as session:
        assert (await ConversationService(session, base()).archive(conversation.id)).archived_at is not None


@pytest.mark.asyncio
async def test_purge_removes_only_conversations_archived_over_thirty_days(conversation_db):
    old, recent, active = await new_conversation(), await new_conversation(), await new_conversation()
    receipt = await submit(old)
    async with session_scope() as session:
        await TaskService(session).cancel(receipt.task.id)
    now = datetime.now(UTC)
    async with session_scope() as session:
        service = ConversationService(session, base())
        await service.archive(old.id, now=now - timedelta(days=31))
        await service.archive(recent.id, now=now - timedelta(days=29))
    async with session_scope() as session:
        assert await ConversationService(session, base()).purge_archived(now=now) == 1
    async with session_scope() as session:
        service = ConversationService(session, base())
        with pytest.raises(LookupError):
            await service.get(old.id)
        assert await session.scalar(select(func.count()).select_from(MessageRow).where(MessageRow.conversation_id == str(old.id))) == 0
        assert await service.get(recent.id) and await service.get(active.id)
        # The research task outlives its conversation.
        assert (await TaskService(session).get_task(receipt.task.id)).status == TaskStatus.CANCELLED
        assert await service.purge_archived(now=now) == 0
        assert await service.purge_archived(now=now + timedelta(days=2)) == 1


@pytest.mark.asyncio
async def test_default_worker_maintenance_purges_expired_archives(conversation_db):
    conversation = await new_conversation()
    async with session_scope() as session:
        await ConversationService(session, base()).archive(conversation.id, now=datetime.now(UTC) - timedelta(days=45))
    worker = ResearchWorker(settings=base(investor_agent_enabled=False), worker_id="archive-purge")
    assert await worker.run_once() is None
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationRow)) == 0
