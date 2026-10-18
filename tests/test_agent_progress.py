"""AIB2 task progress projection: plan states, current-policy limits, usage."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text, update

from investment_agent.agent.budget import BudgetService
from investment_agent.agent.contracts import MessageInput, WaitRequirement
from investment_agent.agent.conversations import ConversationCreate, ConversationService
from investment_agent.agent.progress import task_progress
from investment_agent.agent.research import ResearchProgress, save_progress
from investment_agent.agent.tasks import TaskClaim, TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization
from investment_agent.investor.enums import StopReason
from investment_agent.investor.foundations import InvestorService
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.agent import (
    AgentTask,
    BudgetEntry,
    PublicTaskEvent,
)
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investor import MandateHead
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

STEPS = ("读取同口径历史现金流", "核对存货披露口径")


@pytest_asyncio.fixture
async def progress_db(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'progress.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield


def base(**overrides):
    values = dict(api_key="fixture-app", investor_agent_enabled=True,
        agent_api_key="fixture-model-key", agent_base_url="https://fixture.invalid/v1",
        agent_model="fixture-model", agent_max_tokens=100, investor_agent_task_tokens=100000,
        investor_agent_daily_tokens=500000, investor_agent_task_seconds=600)
    values.update(overrides)
    return Settings(_env_file=None, **values)


def message(**overrides):
    values = dict(client_message_id=uuid4(), text="研究这家公司的现金流", context_refs=(),
        research_kind="comparison")
    values.update(overrides)
    return MessageInput(**values)


def research_progress(kind: str, *, question: str, steps, completed=()) -> ResearchProgress:
    if kind == "company_research":
        from investment_agent.agent.research import ResearchHypothesis
        from investment_agent.investor.contracts import DIMENSIONS, DimensionAssessment
        from investment_agent.investor.enums import DimensionStatus
        return ResearchProgress(kind=kind, question=question,
            hypotheses=(ResearchHypothesis(question="现金流为何走弱",
                explanation="经营收现质量下降", alternative="一次性支出被误读",
                falsification_condition="多期经营现金流持续为负", evidence_priority="先看现金流量表"),),
            evidence_steps=tuple(steps), completed_steps=tuple(completed),
            completion_criteria=("每个维度均有明确结论",), stop_conditions=("关键材料不可得",),
            dimensions=tuple(DimensionAssessment(dimension=dimension, status=DimensionStatus.MISSING,
                applicable=True) for dimension in DIMENSIONS))
    return ResearchProgress(kind=kind, question=question, evidence_steps=tuple(steps),
        completed_steps=tuple(completed))


async def new_conversation():
    async with session_scope() as session:
        return await ConversationService(session, base()).create(ConversationCreate())


async def submit(conversation, body=None):
    async with session_scope() as session:
        return await ConversationService(session, base()).submit(conversation.id, body or message())


async def claim(task_id: UUID) -> TaskClaim:
    async with session_scope() as session:
        row = await session.get(AgentTask, str(task_id))
        run = await PipelineRunRepository(session).claim_next(worker_id="fixture", run_id=row.run_id)
        return await TaskService(session).claim(task_id, RunLease(run.id, "fixture", run.attempt_count))


async def save_plan(execution: TaskClaim, *, kind="comparison", question="现金流是否恶化",
                    steps=STEPS, completed=()) -> None:
    progress = research_progress(kind, question=question, steps=steps, completed=completed)
    async with session_scope() as session:
        service = TaskService(session)
        parameters = {"tool": "save_research_progress", "business": progress.model_dump(mode="json")}
        action = await service.prepare_action(execution, intent_key=f"progress:{uuid4()}",
            parameters=parameters)
        await service.settle_action(execution, UUID(action.operation_id),
            lambda session, operation: save_progress(session, operation, progress))


async def progress_for(task_id: UUID, settings=None):
    async with session_scope() as session:
        return await task_progress(session, task_id, settings or base())


@pytest.mark.asyncio
async def test_plan_states_extra_completed_and_replay(progress_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    execution = await claim(receipt.task.id)
    before = await progress_for(receipt.task.id)
    assert before.plan is None and before.research_kind == "comparison"
    assert before.usage.cost.value is None and before.usage.cost.unknown_reason == "not settled"
    assert before.limits.cost_usd is None and before.limits.cost_limit_reason == "unbounded"
    assert before.limits.model_requests == 20 and before.limits.tool_calls == 50
    assert before.limits.tokens == 100000 and before.limits.seconds == 600
    assert before.elapsed_seconds is not None and before.elapsed_unknown_reason is None
    assert before.deadline > before.as_of

    await save_plan(execution, completed=(STEPS[0], "额外完成的核对"))
    view = await progress_for(receipt.task.id)
    assert view.plan is not None
    assert view.plan.question == "现金流是否恶化"
    assert [(step.text, step.state) for step in view.plan.steps] == [(STEPS[0], "done"), (STEPS[1], "next")]
    assert view.plan.extra_completed == ("额外完成的核对",)
    assert view.plan.saved_at is not None and view.plan.saved_at_unknown_reason is None
    assert view.plan.ref.kind == "research_progress"

    # Idempotent replay: no second artifact ref, no second artifact_saved event.
    async with session_scope() as session:
        service = TaskService(session)
        task = await service.get_task(receipt.task.id)
        operation = next(ref.id for ref in task.artifact_refs if ref.kind == "research_progress")
        events_before = len(await service.get_events(receipt.task.id))
        progress = ResearchProgress(kind="comparison", question="现金流是否恶化",
            evidence_steps=STEPS, completed_steps=(STEPS[0], "额外完成的核对"))
        again = await service.settle_action(execution, operation,
            lambda session, op: save_progress(session, op, progress))
        assert again["progress"]["question"] == "现金流是否恶化"
        assert len((await service.get_task(receipt.task.id)).artifact_refs) == len(task.artifact_refs)
        assert len(await service.get_events(receipt.task.id)) == events_before

    async with session_scope() as session:
        await TaskService(session).cancel(receipt.task.id)
    cancelling = await progress_for(receipt.task.id)
    assert cancelling.plan.steps[1].state == "pending"

    async with session_scope() as session:
        await TaskService(session).ack_cancel(execution)
    done = await progress_for(receipt.task.id)
    assert done.plan.steps[1].state == "pending"
    assert done.limits.finish_only is False
    assert done.elapsed_seconds is not None


@pytest.mark.asyncio
async def test_latest_plan_wins_and_pruned_events_degrade(progress_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    execution = await claim(receipt.task.id)
    await save_plan(execution, question="第一版问题", steps=("步骤一", "步骤二"))
    await save_plan(execution, question="第二版问题", steps=("新的步骤",))
    view = await progress_for(receipt.task.id)
    assert view.plan.question == "第二版问题"
    assert [step.text for step in view.plan.steps] == ["新的步骤"]

    async with session_scope() as session:
        await TaskService(session).checkpoint(execution, checkpoint={}, terminal="partial",
            stop_reason=StopReason.EVIDENCE_BLOCKED)
    frozen = await progress_for(receipt.task.id)
    assert frozen.elapsed_seconds is not None and frozen.elapsed_unknown_reason is None

    # Retention prunes the events: save time and duration become explicit unknowns.
    async with session_scope() as session:
        await session.execute(update(PublicTaskEvent).values(occurred_at=datetime.now(UTC) - timedelta(days=31)))
        assert await TaskService(session).retain_events() >= 1
    pruned = await progress_for(receipt.task.id)
    assert pruned.plan.saved_at is None
    assert pruned.plan.saved_at_unknown_reason == "event_history_pruned"
    assert pruned.elapsed_seconds is None
    assert pruned.elapsed_unknown_reason == "event_history_pruned"
    # A disabled Agent must still serve saved plans and usage.
    off = await progress_for(receipt.task.id, base(investor_agent_enabled=False))
    assert off.plan.question == "第二版问题"


@pytest.mark.asyncio
async def test_waiting_tasks_show_no_next(progress_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    execution = await claim(receipt.task.id)
    await save_plan(execution, steps=("只差一步",))
    async with session_scope() as session:
        await TaskService(session).checkpoint(execution, checkpoint={},
            waiting=WaitRequirement(required_input="补充投资期限", dependency_ids=(uuid4(),),
                deadline=datetime.now(UTC) + timedelta(minutes=5)), wait_status="waiting_user")
    view = await progress_for(receipt.task.id)
    assert [step.state for step in view.plan.steps] == ["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("research_kind", ["company_research", "local_check"])
async def test_finish_only_matches_headroom_and_unknown_usage(progress_db, research_kind):
    conversation = await new_conversation()
    receipt = await submit(conversation, message(research_kind=research_kind))
    execution = await claim(receipt.task.id)
    async with session_scope() as session:
        session.add(BudgetEntry(id=str(uuid4()), task_id=str(receipt.task.id),
            attempt_id=str(execution.attempt_id), owner_id="local_owner", day=BudgetService.day(),
            state="reserved", kind="model", purpose="interactive", calls=1, estimated_tokens=99000,
            estimated_cost_usd=Decimal("0.01"), price_version=None, input_price=None, output_price=None,
            input_tokens=None, output_tokens=None, cost_usd=None, unknown_reason=None))
    view = await progress_for(receipt.task.id)
    assert view.limits.finish_only is True
    async with session_scope() as session:
        headroom = await BudgetService(session, base()).research_headroom(execution)
    assert headroom["finish_only"] is True

    # comparison tasks never advertise the finishing envelope
    other = await new_conversation()
    other_receipt = await submit(other)
    other_execution = await claim(other_receipt.task.id)
    async with session_scope() as session:
        session.add(BudgetEntry(id=str(uuid4()), task_id=str(other_receipt.task.id),
            attempt_id=str(other_execution.attempt_id), owner_id="local_owner", day=BudgetService.day(),
            state="reserved", kind="model", purpose="interactive", calls=1, estimated_tokens=99000,
            estimated_cost_usd=Decimal("0.01"), price_version=None, input_price=None, output_price=None,
            input_tokens=None, output_tokens=None, cost_usd=None, unknown_reason=None))
    assert (await progress_for(other_receipt.task.id)).limits.finish_only is False

    async with session_scope() as session:
        service = BudgetService(session, base())
        reservation = await service.reserve(execution, estimated_tokens=10)
        await service.settle(reservation, None, sent=True)
    unknown = await progress_for(receipt.task.id)
    assert unknown.usage.input_tokens is None and unknown.usage.output_tokens is None
    assert unknown.usage.cost.value is None
    assert unknown.usage.cost.unknown_reason == "price_or_usage_unknown"

    async with session_scope() as session:
        await TaskService(session).checkpoint(execution, checkpoint={}, terminal="failed",
            stop_reason=StopReason.FAILED)
    terminal = await progress_for(receipt.task.id)
    assert terminal.limits.finish_only is False
    assert terminal.usage.input_tokens is None


@pytest.mark.asyncio
async def test_mandate_cost_projection_degrades_without_failure(progress_db):
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="公司甲")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600001", is_primary=True)
        session.add(listing)
        await session.flush()
        subject = ObjectRef(kind="listing", id=listing.id, revision=1)
        unknown = Quantity(value=None, unit="USD", unknown_reason="not set")
        await InvestorService(session).create_mandate("local_owner", Mandate(id=uuid4(),
            revision=1, created_at=datetime.now(UTC), actor="user", origin_ref=None, goals=(),
            scope_refs=(subject,), hard_constraints=(), unknowns=(), authorization=ResearchAuthorization(
                daily_budget=unknown,
                per_task_budget=Quantity(value=Decimal("1.00"), unit="USD"),
                max_leads_per_batch=0, max_new_tasks_per_batch=0, source_refs=())), uuid4())
    conversation = await new_conversation()
    receipt = await submit(conversation)
    view = await progress_for(receipt.task.id)
    assert view.limits.cost_usd == Decimal("1.00") and view.limits.cost_limit_reason is None

    async with session_scope() as session:
        await session.execute(delete(MandateHead).where(MandateHead.owner_id == "local_owner"))
    cleared = await progress_for(receipt.task.id)
    assert cleared.limits.cost_usd is None
    assert cleared.limits.cost_limit_reason == "mandate_unavailable"


@pytest.mark.asyncio
async def test_progress_routes_snapshot_contract_and_missing_grant(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    key = {"X-API-Key": "secret"}
    created = api_client.post("/agent/conversations", headers=key, json={})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    body = {"client_message_id": str(uuid4()), "text": "研究这家公司", "context_refs": [],
        "research_kind": "comparison"}
    receipt = api_client.post(f"/agent/conversations/{conversation_id}/messages", headers=key, json=body)
    assert receipt.status_code == 202, receipt.text
    task_id = receipt.json()["task"]["id"]

    default = api_client.get(f"/agent/tasks/{task_id}/snapshot", headers=key).json()
    assert "progress" not in default
    enriched = api_client.get(f"/agent/tasks/{task_id}/snapshot", headers=key,
        params={"include_progress": "true"}).json()
    assert enriched["task"] == default["task"] and enriched["event_cursor"] == default["event_cursor"]
    assert enriched["messages"] == default["messages"]
    assert enriched["progress"]["plan"] is None
    assert enriched["progress"]["limits"]["cost_limit_reason"] == "unbounded"
    progress = api_client.get(f"/agent/tasks/{task_id}/progress", headers=key).json()
    assert progress["task_id"] == task_id and progress["task_revision"] == default["task"]["revision"]

    async with session_scope() as session:
        await session.execute(text("DELETE FROM agent_execution_grant WHERE task_id = :id"), {"id": task_id})
    assert api_client.get(f"/agent/tasks/{task_id}/progress", headers=key).status_code == 404
    kept = api_client.get(f"/agent/tasks/{task_id}/snapshot", headers=key,
        params={"include_progress": "true"}).json()
    assert kept["progress"] is None and kept["task"]["id"] == task_id
    assert api_client.get(f"/agent/tasks/{uuid4()}/progress", headers=key).status_code == 404
