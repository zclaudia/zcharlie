"""AG08 real native framework/worker and independent file-SQLite boundaries."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select, update

from investment_agent.agent.budget import BudgetExceededError, BudgetService
from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionDeniedError, check_boundary
from investment_agent.agent.executor import AgentExecutor, deliver_deferred_result
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.tasks import ReconciliationRequiredError, TaskService
from investment_agent.agent.tools import ExecutionContext, ToolRegistration, TrustedTools
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization
from investment_agent.investor.enums import TaskStatus
from investment_agent.research.worker import ResearchWorker, default_job_handlers
from investment_agent.settings.loader import ensure_imported
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentModelRun, BudgetEntry, ToolAction
from investment_agent.storage.models.investor import InvestorCase, MandateHead, MandateRevision
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.support.execution import (  # noqa: F401
    Value,
    admitted,
    grant,
    http_result,
    no_scope,
    priced,
    run_worker,
    settings,
)
from tests.support.execution import (
    execution_db as execution_db,
)


class TimedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: datetime


class DatedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: date


@pytest.mark.parametrize("time_case,allowed", [
    ("future", False), ("naive", False), ("past", True),
    ("future_market_date", False), ("past_market_date", True),
])
async def test_native_tool_time_is_bounded_by_trusted_task_cutoff(execution_db, time_case, allowed):
    from zoneinfo import ZoneInfo

    base = settings()
    task = await admitted(base, grant(tool_names=("fetch",)), claim=False)
    cutoff = task.binding.as_of
    inputs = {
        "future": (cutoff + timedelta(days=1)).isoformat(),
        "naive": cutoff.replace(tzinfo=None).isoformat(),
        "past": (cutoff - timedelta(days=1)).isoformat(),
        "future_market_date": (cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat(),
        "past_market_date": (cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat(),
    }
    called = []

    async def fetch(session, operation, business):
        called.append(business.as_of)
        return {"observed": True}

    requests = []

    def response(messages, info):
        requests.append(messages)
        if len(requests) == 1:
            return ModelResponse([ToolCallPart("fetch", {"business": {"as_of": inputs[time_case]}}, tool_call_id="pit-call")],
                usage=RequestUsage(input_tokens=10, output_tokens=2))
        return ModelResponse([TextPart('{"summary":"historical material"}')], usage=RequestUsage(input_tokens=10, output_tokens=2))

    arguments = DatedValue if "market_date" in time_case else TimedValue
    await run_worker(base, FunctionModel(response), tools=(ToolRegistration("fetch", arguments, no_scope, database=fetch),))
    assert bool(called) is allowed
    async with session_scope() as session:
        persisted = await TaskService(session).get_task(task.id)
        assert persisted.usage.tool_calls == 1
        if not allowed:
            assert persisted.status == TaskStatus.FAILED


async def test_disabled_admission_and_missing_trusted_grant(execution_db):
    base = settings(investor_agent_enabled=False)
    with pytest.raises(ExecutionDeniedError, match="feature_disabled"):
        await admitted(base)
    async with session_scope() as session:
        task = await TaskService(session).create(input_key="no-grant", binding=ContextBinding(
            context_refs=(), base_refs=(), as_of=datetime.now(UTC)))
    calls = []
    await run_worker(settings(), FunctionModel(lambda messages, info: calls.append(1)))
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).status == TaskStatus.FAILED
    assert not calls


async def test_atomic_daily_reservation_across_file_connections(execution_db):
    base = settings(investor_agent_daily_tokens=100)
    _, first = await admitted(base)
    _, second = await admitted(base)

    async def reserve(claim):
        async with session_scope() as session:
            return await BudgetService(session, base).reserve(claim, estimated_tokens=60)
    results = await asyncio.gather(reserve(first), reserve(second), return_exceptions=True)
    assert sum(isinstance(result, UUID) for result in results) == 1
    assert sum(isinstance(result, BudgetExceededError) for result in results) == 1
    async with session_scope() as session:
        assert len((await session.scalars(select(BudgetEntry))).all()) == 1


async def test_task_lifetime_budget_does_not_reset_next_day(execution_db):
    base = settings(investor_agent_task_tokens=70)
    _, claim = await admitted(base)
    now = datetime.now(UTC)
    async with session_scope() as session:
        budget = BudgetService(session, base)
        reservation = await budget.reserve(claim, estimated_tokens=40, now=now)
        await budget.settle(reservation, {"input_tokens": 30, "output_tokens": 10}, sent=True)
    async with session_scope() as session:
        with pytest.raises(BudgetExceededError):
            await BudgetService(session, base).reserve(claim, estimated_tokens=40, now=now + timedelta(days=1))


@pytest.mark.parametrize("unknown", [False, True])
async def test_unknown_or_actual_overage_is_preserved_and_stops(execution_db, unknown):
    base = settings(investor_agent_task_tokens=50)
    task, claim = await admitted(base)
    async with session_scope() as session:
        budget = BudgetService(session, base)
        reservation = await budget.reserve(claim, estimated_tokens=20)
        await budget.settle(reservation, None if unknown else {"input_tokens": 50, "output_tokens": 20}, sent=True)
    async with session_scope() as session:
        actual = await TaskService(session).get_task(task.id)
        assert actual.usage.requests == 1
        assert actual.usage.input_tokens == (None if unknown else 50)
        assert actual.usage.cost.value is None
        with pytest.raises(BudgetExceededError):
            await BudgetService(session, base).check(claim)


async def test_unknown_budget_entries_settle_through_trusted_reconciliation(execution_db):
    base = priced()
    task, claim = await admitted(base)
    async with session_scope() as session:
        budget = BudgetService(session, base)
        reservation = await budget.reserve(claim, estimated_tokens=20)
        await budget.settle(reservation, None, sent=True)
    from investment_agent.agent.operations import reconcile_budget
    async with session_scope() as session:
        row = (await session.scalars(select(BudgetEntry).where(BudgetEntry.state == "unknown"))).one()
        budget = BudgetService(session, base)
        with pytest.raises(BudgetExceededError):
            await budget.check(claim)
        with pytest.raises(ValueError, match="actual provider usage"):
            await reconcile_budget(session, base=base, entry_id=UUID(row.id), action="settle",
                operator="ops", reason="console check")
        fixed = await reconcile_budget(session, base=base, entry_id=UUID(row.id), action="settle",
            usage={"input_tokens": 12, "output_tokens": 8}, operator="ops", reason="provider console")
        assert fixed.state == "settled" and fixed.input_tokens == 12 and fixed.output_tokens == 8
        assert fixed.cost_usd == Decimal("0.000028")
        await budget.check(claim)
        with pytest.raises(ValueError, match="only unknown"):
            await reconcile_budget(session, base=base, entry_id=UUID(row.id), action="release",
                operator="ops", reason="again")
    async with session_scope() as session:
        usage = (await TaskService(session).get_task(task.id)).usage
        assert usage.input_tokens == 12 and usage.output_tokens == 8
        events = await TaskService(session).get_events(task.id)
        assert any("provider console" in str(event.payload) and "ops" in str(event.payload) for event in events)


async def test_unknown_budget_entries_release_with_operator_audit(execution_db):
    base = settings()
    task, claim = await admitted(base)
    async with session_scope() as session:
        budget = BudgetService(session, base)
        reservation = await budget.reserve(claim, estimated_tokens=20)
        await budget.settle(reservation, None, sent=True)
    from investment_agent.agent.operations import reconcile_budget
    async with session_scope() as session:
        row = (await session.scalars(select(BudgetEntry).where(BudgetEntry.state == "unknown"))).one()
        with pytest.raises(BudgetExceededError):
            await BudgetService(session, base).check(claim)
        released = await reconcile_budget(session, base=base, entry_id=UUID(row.id), action="release",
            operator="ops", reason="request never reached the provider")
        assert released.state == "released"
    async with session_scope() as session:
        budget = BudgetService(session, base)
        await budget.check(claim)
        await budget.reserve(claim, estimated_tokens=50)  # release unblocks the day
        usage = (await TaskService(session).get_task(task.id)).usage
        assert usage.requests == 0  # the released reservation no longer counts
        events = await TaskService(session).get_events(task.id)
        assert any("never reached the provider" in str(event.payload) for event in events)


async def test_transient_connect_failure_releases_reservation_and_retries(execution_db):
    base = priced()
    task = await admitted(base, claim=False)
    calls = []
    def respond(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return http_result()
    executor = AgentExecutor(base, transport=httpx.MockTransport(respond))
    worker = ResearchWorker(settings=base, worker_id="fixture", retry_base_delay_seconds=0,
                            handlers=[AgentJobHandler("agent_turn", executor)])
    await worker.run_once()
    async with session_scope() as session:
        actual = await TaskService(session).get_task(task.id)
        assert actual.status == TaskStatus.RUNNING  # scheduled retry, not a permanent failure
        from investment_agent.storage.models.analysis import PipelineRun
        run = await session.scalar(select(PipelineRun))
        assert run.status == "queued" and run.attempt_count == 1
        entries = (await session.scalars(select(BudgetEntry))).all()
        assert len(entries) == 1 and entries[0].state == "released"  # nothing was consumed
    await worker.run_once()
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.status == TaskStatus.FAILED and final.usage.requests == 1
        assert final.usage.input_tokens == 20
        audit = await session.scalar(select(AgentModelRun).where(AgentModelRun.error_code.is_not(None)))
        assert audit.error_code and "refused" not in audit.error_code and "fixture.invalid" not in audit.error_code
        events = await TaskService(session).get_events(task.id)
        assert "refused" not in str(events) and "fixture.invalid" not in str(events)
    assert calls == [1, 1]


async def test_sent_rechecks_lowered_budget_after_reservation(execution_db):
    base = settings()
    task, claim = await admitted(base)
    async with session_scope() as session:
        await ensure_imported(session, base)
        reservation = await BudgetService(session, base).reserve(claim, estimated_tokens=500)
    async with session_scope() as session:
        await SettingsStore(session).apply({"investor_agent_task_tokens": 100}, actor="user", note="lower cap")
    async with session_scope() as session:
        with pytest.raises(BudgetExceededError):
            await BudgetService(session, base).sent(claim, reservation)
        await BudgetService(session, base).settle(reservation, None, sent=False)
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).usage.requests == 0


async def test_verified_cost_atomic_caps_snapshot_and_price_identity(execution_db):
    base = priced()
    permission = grant(task_cost_usd=Decimal(".0001"), daily_cost_usd=Decimal(".0001"))
    task, claim = await admitted(base, permission)
    async with session_scope() as session:
        reservation = await BudgetService(session, base).reserve(claim, estimated_tokens=50,
                                                                estimated_input=30, output_bound=20)
        await BudgetService(session, base).sent(claim, reservation)
    # Settlement uses the reserved quote even if settings are subsequently changed.
    changed = base.model_copy(update={"investor_agent_input_usd_per_million": Decimal(100)})
    async with session_scope() as session:
        actual = await BudgetService(session, changed).settle(reservation, {"input_tokens": 30, "output_tokens": 20}, sent=True)
        assert actual.cost.value == Decimal(".000070")
        with pytest.raises(BudgetExceededError):
            await BudgetService(session, base).reserve(claim, estimated_tokens=40)
    assert BudgetService.prices(base.model_copy(update={"agent_base_url": "https://other.invalid/v1"})) == (None, None)
    assert BudgetService.prices(base.model_copy(update={"agent_provider": "deepseek"})) == (None, None)
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).usage.cost.value == Decimal(".000070")


async def test_money_cap_without_verified_price_stops_before_request(execution_db):
    base = settings()
    _, claim = await admitted(base, grant(task_cost_usd=Decimal(1)))
    async with session_scope() as session:
        with pytest.raises(BudgetExceededError, match="cost_unknown"):
            await BudgetService(session, base).reserve(claim, estimated_tokens=50)


async def test_native_deferred_private_history_resumes_across_executor_instances(execution_db):
    base = settings()
    task = await admitted(base, grant(tool_names=("fetch",)), claim=False)
    calls = []

    async def fetch(session, operation, business):
        calls.append(operation)
        return {"status": "pending", "dependency_id": "provider-job", "value": business.value}
    spec = ToolRegistration("fetch", Value, no_scope, database=fetch)

    def response(messages, info):
        results = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        if not results:
            return ModelResponse([ToolCallPart("fetch", {"business": {"value": 7}}, tool_call_id="provider-call")],
                                 usage=RequestUsage(input_tokens=11, output_tokens=3))
        assert results[-1].content == {"ready": 7}
        return ModelResponse([TextPart('{"summary":"ready"}')], usage=RequestUsage(input_tokens=20, output_tokens=4))
    await run_worker(base, FunctionModel(response), tools=(spec,))
    async with session_scope() as session:
        service = TaskService(session)
        saved = await service.get_task(task.id)
        assert saved.status == TaskStatus.WAITING_DATA
        checkpoint, token = await service.get_checkpoint(task.id)
        assert checkpoint["data"]["history"]["framework_version"] == "2.31.1"
        dependency = saved.waiting.dependency_ids[0]
        assert str(dependency) == str(calls[0])
        assert await deliver_deferred_result(session, task.id, dependency, wait_token=token,
            completion_event_id=uuid4(), result={"ready": 7})
    await run_worker(base, FunctionModel(response), tools=(spec,))  # a new executor/toolset instance
    async with session_scope() as session:
        service = TaskService(session)
        final = await service.get_task(task.id)
        assert final.status == TaskStatus.FAILED  # native answer is not certified research completion
        assert final.usage.requests == 2 and final.usage.tool_calls == 1
        assert final.usage.input_tokens == 31 and final.usage.output_tokens == 7
        assert len((await session.scalars(select(ToolAction))).all()) == 1
        assert (await service.get_checkpoint(task.id))[0]["data"]["draft"] == {"summary": "ready"}
        assert not await deliver_deferred_result(session, task.id, dependency, wait_token=token,
            completion_event_id=uuid4(), result={"forged": True})
    assert len(calls) == 1


async def test_native_tools_cannot_accept_control_arguments_and_retry_is_charged(execution_db):
    base = settings()
    task = await admitted(base, grant(tool_names=("write",)), claim=False)
    calls = []

    async def write(session, operation, business):
        calls.append(operation)
        return {"value": business.value}
    spec = ToolRegistration("write", Value, no_scope, database=write)
    requests = []

    def response(messages, info):
        requests.append(messages)
        if len(requests) == 1:
            return ModelResponse([ToolCallPart("write", {"business": {"value": 1, "operation_id": str(uuid4()), "actor": "owner"}},
                tool_call_id="forged")], usage=RequestUsage(input_tokens=10, output_tokens=2))
        return ModelResponse([TextPart('{"summary":"invalid control parameters rejected"}')], usage=RequestUsage(input_tokens=14, output_tokens=3))
    await run_worker(base, FunctionModel(response), tools=(spec,))
    async with session_scope() as session:
        usage = (await TaskService(session).get_task(task.id)).usage
        assert usage.requests == 2 and usage.tool_calls == 1 and usage.input_tokens == 24
    assert not calls
    class Controls(BaseModel):
        model_config = ConfigDict(extra="forbid")
        actor: str
    with pytest.raises(ValueError, match="control fields"):
        ToolRegistration("bad", Controls, no_scope, database=write)


async def test_one_response_tool_batch_exhausts_persistent_limit_before_dispatch(execution_db):
    base = settings(investor_agent_task_tool_calls=2)
    task = await admitted(base, grant(tool_names=("write",)), claim=False)
    calls = []
    async def write(session, operation, business):
        calls.append(operation)
        return {"value": business.value}
    def response(messages, info):
        return ModelResponse([ToolCallPart("write", {"business": {"value": value}}, tool_call_id=str(value))
            for value in range(3)], usage=RequestUsage(input_tokens=10, output_tokens=10))
    await run_worker(base, FunctionModel(response), tools=(ToolRegistration("write", Value, no_scope, database=write),))
    async with session_scope() as session:
        task = await TaskService(session).get_task(task.id)
        assert task.status == TaskStatus.FAILED and task.stop_reason.value == "budget_exhausted"
        assert task.usage.requests == 1 and task.usage.tool_calls == 3
    assert not calls


@pytest.mark.parametrize("control", ["cancel", "disable"])
async def test_inflight_http_control_stops_tools_and_keeps_charge(execution_db, control):
    base = settings()
    task = await admitted(base, grant(tool_names=("write",)), claim=False)
    async with session_scope() as session:
        await ensure_imported(session, base)
    started, released = asyncio.Event(), asyncio.Event()
    calls = []
    async def write(session, operation, business):
        calls.append(operation)
        return {"value": business.value}
    async def respond(request):
        started.set()
        await released.wait()
        return http_result(tools=True)
    running = asyncio.create_task(run_worker(base, tools=(ToolRegistration("write", Value, no_scope, database=write),),
                                            transport=httpx.MockTransport(respond)))
    await asyncio.wait_for(started.wait(), 5)
    async with session_scope() as session:
        if control == "cancel":
            await TaskService(session).cancel(task.id)
            assert (await TaskService(session).get_task(task.id)).status == TaskStatus.RUNNING
        else:
            await SettingsStore(session).apply({"investor_agent_enabled": False}, actor="user", note="disable")
    released.set()
    await running
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.status == (TaskStatus.CANCELLED if control == "cancel" else TaskStatus.FAILED)
        assert final.usage.requests == 1 and final.usage.input_tokens == 20
        audits = (await session.scalars(select(AgentModelRun))).all()
        assert len(audits) == 1 and audits[0].usage == {"input_tokens": 20, "output_tokens": 4}
    assert not calls


async def test_http_missing_usage_stops_after_first_request_and_preserves_null(execution_db):
    base = settings()
    task = await admitted(base, grant(tool_names=("write",)), claim=False)
    network, callbacks = [], []
    def respond(request):
        network.append(1)
        return http_result(tools=True, usage=False)
    async def write(session, operation, business):
        callbacks.append(1)
        return {}
    await run_worker(base, tools=(ToolRegistration("write", Value, no_scope, database=write),),
                     transport=httpx.MockTransport(respond))
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.status == TaskStatus.FAILED and final.usage.input_tokens is None
        audit = await session.scalar(select(AgentModelRun))
        assert audit.usage is None and audit.http_attempts == 1
    assert network == [1] and not callbacks


async def test_two_native_deferred_dependencies_concurrent_delivery_keeps_both_results(execution_db):
    base = settings()
    task = await admitted(base, grant(tool_names=("fetch",)), claim=False)
    calls = []
    async def fetch(session, operation, business):
        calls.append(operation)
        return {"status": "pending", "dependency_id": str(business.value)}
    spec = ToolRegistration("fetch", Value, no_scope, database=fetch)
    def response(messages, info):
        returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse([ToolCallPart("fetch", {"business": {"value": value}}, tool_call_id=f"call-{value}")
                for value in (1, 2)], usage=RequestUsage(input_tokens=10, output_tokens=4))
        assert {r.tool_call_id for r in returns} == {"call-1", "call-2"}
        assert {r.content["ready"] for r in returns} == {1, 2}
        return ModelResponse([TextPart('{"summary":"both ready"}')], usage=RequestUsage(input_tokens=20, output_tokens=4))
    await run_worker(base, FunctionModel(response), tools=(spec,))
    async with session_scope() as session:
        service = TaskService(session)
        waiting = await service.get_task(task.id)
        _, token = await service.get_checkpoint(task.id)
        dependencies = waiting.waiting.dependency_ids
    async def complete(index):
        async with session_scope() as session:
            return await deliver_deferred_result(session, task.id, dependencies[index], wait_token=token,
                completion_event_id=uuid4(), result={"ready": index + 1})
    assert sorted(await asyncio.gather(complete(0), complete(1))) == [False, True]
    async with session_scope() as session:
        checkpoint, _ = await TaskService(session).get_checkpoint(task.id)
        assert checkpoint["data"]["deferred_results"] == {"call-1": {"ready": 1}, "call-2": {"ready": 2}}
        assert (await TaskService(session).get_task(task.id)).status == TaskStatus.QUEUED
    await run_worker(base, FunctionModel(response), tools=(spec,))
    assert len(calls) == 2
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).usage.requests == 2


async def seed_listing_scope(*, paused=False):
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="Fixture company")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600000")
        session.add(listing)
        await session.flush()
        ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        case = InvestorCase(id=str(uuid4()), owner_id="local_owner", listing_id=listing.id, revision=1,
            payload={"status": "researching", "background_paused": paused}, source_refs=[])
        session.add(case)
        budget = Quantity(value=Decimal(5), unit="currency", currency="USD")
        mandate = Mandate(id=uuid4(), revision=1, created_at=datetime.now(UTC), actor="user", origin_ref=None,
            goals=(), scope_refs=(ref,), hard_constraints=(), unknowns=(),
            authorization=ResearchAuthorization(background_enabled=True, exploration_scope_refs=(ref,),
                daily_budget=budget, per_task_budget=budget, max_leads_per_batch=20,
                max_new_tasks_per_batch=3, source_refs=()))
        session.add(MandateHead(owner_id="local_owner", mandate_id=str(mandate.id), revision=1))
        session.add(MandateRevision(mandate_id=str(mandate.id), revision=1, owner_id="local_owner",
                                   payload=mandate.model_dump(mode="json")))
    return ref, UUID(case.id), mandate


async def test_listing_reference_cannot_bypass_case_pause_but_interactive_remains_allowed(execution_db):
    ref, case, mandate = await seed_listing_scope()
    base = priced(investor_agent_background_enabled=True)
    _, background = await admitted(base, grant(mode="background", source="authorized_background",
        mandate_id=mandate.id, scope_refs=(ref,)))
    _, interactive = await admitted(base, grant(mandate_id=mandate.id, scope_refs=(ref,)))
    async with session_scope() as session:
        await session.execute(update(InvestorCase).where(InvestorCase.id == str(case))
                              .values(payload={"status": "researching", "background_paused": True}))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="background_paused"):
            await check_boundary(session, background, base)
        await check_boundary(session, interactive, base)
    async with session_scope() as session:
        await session.execute(update(InvestorCase).where(InvestorCase.id == str(case))
                              .values(payload={"status": "archived", "background_paused": False}))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="scope_archived"):
            await check_boundary(session, interactive, base)


async def test_whole_watchlist_membership_and_permission_rechecked(execution_db):
    ref, _, mandate = await seed_listing_scope()
    base = priced()
    async with session_scope() as session:
        session.add(WatchlistItem(listing_id=ref.id, added_at=datetime.now(UTC)))
    _, claim = await admitted(base, grant(mandate_id=mandate.id, scope_refs=(ref,), watchlist=True,
                                        tool_names=("refresh",)))
    async with session_scope() as session:
        await check_boundary(session, claim, base, tool="refresh", watchlist=True)
    async with session_scope() as session:
        company = Company(canonical_name="Second fixture company")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600001")
        session.add(listing)
        await session.flush()
        session.add(WatchlistItem(listing_id=listing.id, added_at=datetime.now(UTC)))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="whole_watchlist_scope_changed"):
            await check_boundary(session, claim, base, tool="refresh", watchlist=True)
    _, limited = await admitted(base, grant(scope_refs=(ref,), tool_names=("refresh",)))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="whole_watchlist_authorization_required"):
            await check_boundary(session, limited, base, tool="refresh", watchlist=True)


async def test_live_mandate_budget_unit_and_current_scope_are_enforced(execution_db):
    ref, _, mandate = await seed_listing_scope()
    base = priced()
    _, claim = await admitted(base, grant(mandate_id=mandate.id, scope_refs=(ref,)))
    modified = mandate.model_copy(update={"revision": 2, "authorization": mandate.authorization.model_copy(update={
        "per_task_budget": Quantity(value=1, unit="shares")})})
    async with session_scope() as session:
        session.add(MandateRevision(mandate_id=str(mandate.id), revision=2, owner_id="local_owner",
                                   payload=modified.model_dump(mode="json")))
        await session.execute(update(MandateHead).values(revision=2))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="budget_unit_invalid"):
            await BudgetService(session, base).reserve(claim, estimated_tokens=10)
    async with session_scope() as session:
        restricted = modified.model_copy(update={"revision": 3, "scope_refs": ()})
        session.add(MandateRevision(mandate_id=str(mandate.id), revision=3, owner_id="local_owner",
                                   payload=restricted.model_dump(mode="json")))
        await session.execute(update(MandateHead).values(revision=3))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="scope_revoked"):
            await check_boundary(session, claim, base)


async def test_external_uncertain_recovery_reconciles_before_any_model_or_replay(execution_db):
    base = settings()
    task, claim = await admitted(base, grant(tool_names=("external",)))
    sent, reconciled = [], []
    class External:
        async def send(self, operation_id, business):
            sent.append(operation_id)
            return {"saved": business.value}
        async def reconcile(self, operation_id, business):
            reconciled.append(operation_id)
            return "unknown", None
    spec = ToolRegistration("external", Value, no_scope, external=External())
    async with session_scope() as session:
        service = TaskService(session)
        action = await service.prepare_action(claim, intent_key="saved-external", effect="external",
            parameters={"tool": "external", "business": {"value": 7}, "phase": "draft:v1"})
        await service.start_external(claim, UUID(action.operation_id), expected_revision=action.revision)
    context = ExecutionContext(claim, session_scope, base)
    with pytest.raises(ReconciliationRequiredError):
        await TrustedTools(context, (spec,)).recover()
    assert reconciled == [UUID(action.operation_id)] and not sent
    async with session_scope() as session:
        saved = (await TaskService(session).recovery_actions(task.id))[0]
        assert saved.state == "uncertain" and saved.revision == 3


async def test_external_write_commit_before_io_and_receipt_replay(execution_db):
    base = settings()
    task, claim = await admitted(base, grant(tool_names=("external",)))
    calls = []
    class External:
        async def send(self, operation, business):
            async with session_scope() as session:
                action = await session.get(ToolAction, str(operation))
                assert action.state == "uncertain"  # independently visible before I/O
            calls.append(operation)
            return {"saved": business.value}
        async def reconcile(self, operation, business):
            return "committed", {"saved": business.value}
    spec = ToolRegistration("external", Value, no_scope, external=External())
    trusted = TrustedTools(ExecutionContext(claim, session_scope, base), (spec,))
    assert await trusted.call(spec, Value(value=7)) == {"saved": 7}
    assert await TrustedTools(ExecutionContext(claim, session_scope, base), (spec,)).call(spec, Value(value=7)) == {"saved": 7}
    assert len(calls) == 1


@pytest.mark.parametrize("sent", [False, True])
async def test_crashed_attempt_reservations_recover_conservatively(execution_db, sent):
    base = settings()
    task, old_claim = await admitted(base)
    async with session_scope() as session:
        reservation = await BudgetService(session, base).reserve(old_claim, estimated_tokens=100)
        if sent:
            await BudgetService(session, base).sent(old_claim, reservation)
        from investment_agent.storage.models.analysis import PipelineRun
        await session.execute(update(PipelineRun).where(PipelineRun.id == old_claim.lease.run_id)
                              .values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1)))
    handler = AgentJobHandler("agent_turn", AgentExecutor(base))
    worker = ResearchWorker(settings=base, worker_id="recovered", handlers=[handler])
    requeued, _ = await worker.recover_stale(stale_after_seconds=1)
    assert requeued == [old_claim.lease.run_id]
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(worker_id="recovered", kinds=["agent_turn"])
        new_claim = await TaskService(session).claim(task.id, RunLease(run.id, "recovered", run.attempt_count))
        await BudgetService(session, base).recover(new_claim)
    async with session_scope() as session:
        row = await session.get(BudgetEntry, str(reservation))
        assert row.state == ("unknown" if sent else "released")
        if sent:
            with pytest.raises(BudgetExceededError):
                await BudgetService(session, base).check(new_claim)
        else:
            await BudgetService(session, base).reserve(new_claim, estimated_tokens=100)


async def test_default_worker_registers_agent_and_expires_waiting_without_affecting_injected_handlers(execution_db):
    from investment_agent.agent.contracts import WaitRequirement
    base = settings(investor_agent_enabled=False)
    assert {h.kind for h in default_job_handlers(base)} >= {"agent_turn", "agent_research", "agent_review"}
    async with session_scope() as session:
        task = await TaskService(session).create(input_key="expiry", binding=ContextBinding(
            context_refs=(), base_refs=(), as_of=datetime.now(UTC)))
        run = await PipelineRunRepository(session).claim_next(worker_id="fixture", kinds=["agent_turn"])
        claim = await TaskService(session).claim(task.id, RunLease(run.id, "fixture", run.attempt_count))
        await TaskService(session).checkpoint(claim, checkpoint={}, waiting=WaitRequirement(
            required_input="data", dependency_ids=(uuid4(),), deadline=datetime.now(UTC) - timedelta(seconds=1)))
    injected = ResearchWorker(settings=base, worker_id="fixture", handlers=[AgentJobHandler("agent_turn", AgentExecutor(base))])
    assert await injected.run_once() is None
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).status == TaskStatus.WAITING_DATA
    default = ResearchWorker(settings=base, worker_id="fixture")
    assert await default.run_once() is None
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).status == TaskStatus.FAILED


async def test_verified_small_cost_accumulates_without_six_decimal_rounding(execution_db):
    base = priced().model_copy(update={"investor_agent_input_usd_per_million": Decimal("0.1"),
                                      "investor_agent_output_usd_per_million": Decimal("0.1")})
    task = await admitted(base, claim=False)
    calls = []
    def response(messages, info):
        calls.append(1)
        return ModelResponse([TextPart("invalid" if len(calls) == 1 else '{"summary":"fixed"}')],
                             usage=RequestUsage(input_tokens=1, output_tokens=1))
    await run_worker(base, FunctionModel(response))
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.usage.requests == 2
        assert final.usage.cost.value == Decimal("0.0000004")
        audit = await session.scalar(select(AgentModelRun))
        assert audit.cost_usd == Decimal("0.0000004") and audit.cost_unknown_reason is None
        receipts = (await session.scalars(select(BudgetEntry).where(BudgetEntry.kind == "model"))).all()
        assert [r.cost_usd for r in receipts] == [Decimal("0.0000002"), Decimal("0.0000002")]


async def test_cross_run_native_retry_cannot_reset_request_budget(execution_db):
    base = settings(investor_agent_task_requests=1)
    task = await admitted(base, grant(tool_names=("fetch",)), claim=False)
    observed = []
    async def fetch(session, operation, business):
        return {"status": "pending", "dependency_id": "fixture"}
    spec = ToolRegistration("fetch", Value, no_scope, database=fetch)
    def response(messages, info):
        observed.append(1)
        return ModelResponse([ToolCallPart("fetch", {"business": {"value": 1}}, tool_call_id="call")],
                             usage=RequestUsage(input_tokens=10, output_tokens=2))
    await run_worker(base, FunctionModel(response), tools=(spec,))
    async with session_scope() as session:
        service = TaskService(session)
        waiting = await service.get_task(task.id)
        _, token = await service.get_checkpoint(task.id)
        assert await deliver_deferred_result(session, task.id, waiting.waiting.dependency_ids[0], wait_token=token,
            completion_event_id=uuid4(), result={"ready": True})
    await run_worker(base, FunctionModel(response), tools=(spec,))
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.usage.requests == 1 and final.stop_reason.value == "budget_exhausted"
    assert observed == [1]


async def test_background_pause_while_model_inflight_stops_next_native_tool(execution_db):
    ref, case, mandate = await seed_listing_scope()
    base = priced(investor_agent_background_enabled=True)
    task = await admitted(base, grant(mode="background", source="authorized_background",
        mandate_id=mandate.id, scope_refs=(ref,), tool_names=("write",)), claim=False)
    started, released = asyncio.Event(), asyncio.Event()
    calls = []
    async def response(messages, info):
        started.set()
        await released.wait()
        return ModelResponse([ToolCallPart("write", {"business": {"value": 1}}, tool_call_id="call")],
                             usage=RequestUsage(input_tokens=10, output_tokens=2))
    async def write(session, operation, business):
        calls.append(1)
        return {}
    work = asyncio.create_task(run_worker(base, FunctionModel(response),
        tools=(ToolRegistration("write", Value, no_scope, database=write),)))
    await asyncio.wait_for(started.wait(), 5)
    async with session_scope() as session:
        await session.execute(update(InvestorCase).where(InvestorCase.id == str(case))
            .values(payload={"status": "researching", "background_paused": True}))
    released.set()
    await work
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.status == TaskStatus.FAILED and final.usage.input_tokens == 10
        audit = await session.scalar(select(AgentModelRun))
        assert audit.usage == {"input_tokens": 10, "output_tokens": 2}
    assert not calls


async def test_actual_failed_http_response_usage_is_charged_without_sdk_retry(execution_db):
    base = priced()
    task = await admitted(base, claim=False)
    from investment_agent.storage.models.analysis import PipelineRun
    async with session_scope() as session:
        await session.execute(update(PipelineRun).values(max_attempts=1))
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "fixture-secret PRIVATE"},
                                        "usage": {"prompt_tokens": 20, "completion_tokens": 4}})
    await run_worker(base, transport=httpx.MockTransport(respond))
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.status == TaskStatus.FAILED and final.usage.input_tokens == 20
        assert final.usage.cost.value == Decimal(".000028")
        events = await TaskService(session).get_events(task.id)
        assert "PRIVATE" not in str(events) and "fixture-secret" not in str(events)
    assert calls == [1]


async def test_task_timeout_persists_unknown_charge_and_no_later_actions(execution_db):
    base = settings(investor_agent_task_seconds=2.0)
    task = await admitted(base, claim=False)
    started = asyncio.Event()
    async def respond(request):
        started.set()
        await asyncio.Event().wait()
    await run_worker(base, transport=httpx.MockTransport(respond))
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert started.is_set()
        assert final.status == TaskStatus.FAILED and final.stop_reason.value == "budget_exhausted"
        assert final.usage.requests == 1 and final.usage.input_tokens is None
        audit = await session.scalar(select(AgentModelRun))
        assert audit.error_code == "TimeoutError" and audit.usage is None


async def test_completion_is_a_trusted_policy_distinct_from_model_success(execution_db):
    from investment_agent.agent.executor import RunSpecification, TaskDisposition
    from investment_agent.investor.enums import StopReason
    base = settings()
    task = await admitted(base, claim=False)
    certified = []
    async def complete(session, claim, outcome):
        assert outcome.output.summary == "fixture draft"
        certified.append(claim.task_id)
        return TaskDisposition("succeeded", StopReason.ANSWERED)
    model = FunctionModel(lambda messages, info: ModelResponse([TextPart('{"summary":"fixture draft"}')],
        usage=RequestUsage(input_tokens=10, output_tokens=2)))
    executor = AgentExecutor(base, model=model, specification=RunSpecification(completion=complete))
    worker = ResearchWorker(settings=base, worker_id="fixture", handlers=[AgentJobHandler("agent_turn", executor)])
    await worker.run_once()
    async with session_scope() as session:
        assert (await TaskService(session).get_task(task.id)).status == TaskStatus.SUCCEEDED
    assert certified == [task.id]


async def test_real_worker_retry_known_provider_failure_keeps_task_budget(execution_db):
    from investment_agent.storage.models.analysis import PipelineRun
    base = priced()
    task = await admitted(base, claim=False)
    calls = []
    def respond(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "PRIVATE"},
                "usage": {"prompt_tokens": 20, "completion_tokens": 4}})
        return http_result()
    executor = AgentExecutor(base, transport=httpx.MockTransport(respond))
    worker = ResearchWorker(settings=base, worker_id="fixture", retry_base_delay_seconds=0,
                            handlers=[AgentJobHandler("agent_turn", executor)])
    await worker.run_once()
    async with session_scope() as session:
        actual = await TaskService(session).get_task(task.id)
        assert actual.status == TaskStatus.RUNNING and actual.usage.requests == 1
        run = await session.scalar(select(PipelineRun))
        assert run.status == "queued" and run.attempt_count == 1
    await worker.run_once()
    async with session_scope() as session:
        actual = await TaskService(session).get_task(task.id)
        assert actual.status == TaskStatus.FAILED and actual.usage.requests == 2
        assert actual.usage.input_tokens == 40 and actual.usage.cost.value == Decimal(".000056")
        run = await session.scalar(select(PipelineRun))
        assert run.status == "success" and run.attempt_count == 2
        audits = (await session.scalars(select(AgentModelRun))).all()
        assert len(audits) == 2 and sum(a.cost_usd for a in audits) == actual.usage.cost.value
    assert calls == [1, 1]


async def test_conclusion_headroom_uses_remaining_lifetime_and_daily_caps(execution_db):
    base = settings(investor_agent_task_tokens=100000, investor_agent_daily_tokens=100000)
    task, trusted = await admitted(base, grant(research_kind="company_research"))
    async with session_scope() as session:
        budget = BudgetService(session, base)
        initial = await budget.research_headroom(trusted)
        assert not initial['finish_only']
        reservation = await budget.reserve(trusted, estimated_tokens=48000)
        await budget.settle(reservation, {'input_tokens': 6000, 'output_tokens': 1000}, sent=True)
        state = await budget.research_headroom(trusted)
        assert state == {'remaining_tokens': 93000, 'remaining_requests': initial['remaining_requests'] - 1,
                         'conclusion_reserve': 96000, 'finish_only': True}
        # This is a planning reserve, not a second charge or a budget increase.
        assert (await budget.usage(task.id)).input_tokens == 6000
