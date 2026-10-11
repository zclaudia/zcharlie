"""AG16 finite shortlist, real native selection and purpose budget boundaries."""
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from investment_agent.agent.budget import BudgetExceededError, BudgetService
from investment_agent.agent.contracts import ContextBinding, MessageInput
from investment_agent.agent.conversations import ConversationCreate, ConversationService
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.model import AgentModelConfig
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.common import Mutation, ObjectRef, Quantity
from investment_agent.investor.controls import CaseAction
from investment_agent.investor.discovery import DiscoveryOutput, DiscoveryService, LeadDecision
from investment_agent.investor.foundations import InvestorService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, BudgetEntry, ExecutionGrantRow
from investment_agent.storage.models.discovery import DiscoveryBatch
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.test_agent_conversations import base
from tests.test_investor_controls import execute
from tests.test_investor_publication import fixture
from tests.test_investor_publication import publication_db as publication_db


def priced(**overrides):
    selected = base(investor_agent_background_enabled=True, investor_agent_price_version="verified-fixture",
        investor_agent_price_model="fixture-model", investor_agent_input_usd_per_million=Decimal("1"),
        investor_agent_output_usd_per_million=Decimal("1"), **overrides)
    return selected.model_copy(update={"investor_agent_price_config_version": AgentModelConfig.from_settings(selected, 0).version})


async def setup(count=1, **auth_overrides):
    candidate, _, subject = await fixture()
    async with session_scope() as session:
        subjects = [subject]
        for i in range(1, count):
            company = Company(canonical_name=f"Fixture {i}")
            session.add(company)
            await session.flush()
            listing = Listing(company_id=company.id, market_code="SH", ticker=f"{600000+i}.SH", is_primary=True)
            session.add(listing)
            await session.flush()
            subjects.append(ObjectRef(kind="listing", id=listing.id, revision=1))
        foundation = InvestorService(session)
        mandate = await foundation.get_mandate("local_owner")
        values = dict(background_enabled=True, exploration_scope_refs=tuple(subjects), daily_budget=Quantity(value="10", unit="USD"),
            per_task_budget=Quantity(value="1", unit="USD"), exploration_daily_budget=Quantity(value="2", unit="USD"),
            review_daily_budget=Quantity(value="3", unit="USD"), max_leads_per_batch=100, max_new_tasks_per_batch=100)
        values.update(auth_overrides)
        auth = mandate.authorization.model_copy(update=values)
        await foundation.revise_mandate("local_owner", mandate.model_copy(update={"revision": 2, "scope_refs": tuple(subjects), "authorization": auth}), Mutation(expected_revision=1, idempotency_key=uuid4()))
        for ref in subjects:
            await FinancialSnapshotRepository(session).insert(listing_id=ref.id, report_period=date(2025,12,31), period_kind="annual",
                fields={"net_profit": "42"}, provider="offline-fixture", captured_at=datetime.now(UTC), availability="actual", available_from=date(2026,4,1))
    return candidate, tuple(subjects)


def output(batch, selected=0, **change):
    leads = [lead for lead in batch.payload["leads"] if lead["status"] == "unreviewed"]
    decisions = tuple(LeadDecision(lead_id=lead["id"], version=lead["version"], listing_ref=ObjectRef.model_validate(lead["listing_ref"]),
        selected=i < selected, question="Is the reported profit recurring?" if i < selected else None,
        reason="New reported data gives an answerable cash-quality question" if i < selected else "No sufficiently important unresolved question",
        reevaluate_when="A new cash-flow report or correction is committed") for i,lead in enumerate(leads))
    if change:
        decisions = (decisions[0].model_copy(update=change), *decisions[1:])
    return DiscoveryOutput(decisions=decisions, summary="Finite research questions selected; no return ranking")


async def native_worker(monkeypatch, response, selected):
    original = AgentExecutor.__init__
    def constructor(self, *args, **kwargs):
        kwargs["model"] = FunctionModel(lambda messages,info: ModelResponse([TextPart(response.model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=10)))
        original(self, *args, **kwargs)
    monkeypatch.setattr(AgentExecutor,"__init__",constructor)
    worker = ResearchWorker(settings=selected, worker_id="native-discovery")
    return worker


@pytest.mark.asyncio
async def test_bounded_scan_native_selection_real_queue_restart_no_second_charge(publication_db, monkeypatch):
    _, subjects = await setup(23)
    selected = priced()
    async with session_scope() as session:
        batch = await DiscoveryService(session, selected).scan()
        assert batch and len(batch.payload["leads"]) == 20
        first_id = batch.id
        response = output(batch, 3)
        assert batch.payload["max_new_tasks"] == 3
        assert all(lead["sources"][0]["provider"] == "offline-fixture" for lead in batch.payload["leads"])
    worker = await native_worker(monkeypatch,response,selected)
    assert await worker.run_once() is not None
    async with session_scope() as session:
        batch = await session.scalar(select(DiscoveryBatch).where(DiscoveryBatch.id == first_id))
        assert batch.state == "selection_completed", (await TaskService(session).get_task(UUID(batch.task_id))).model_dump()
        records = batch.payload["leads"]
        assert sum(lead["status"] == "queued" for lead in records) == 3
        assert all(lead["status"] == "not_selected" and lead["reason"] and lead["reevaluate_when"] for lead in records[3:])
        assert await session.scalar(select(func.count()).select_from(InvestorCase)) == 3
        charges = (await session.scalars(select(BudgetEntry))).all()
        assert len(charges) == 1 and charges[0].purpose == "exploration"
        for lead in records[:3]:
            row = await session.get(ExecutionGrantRow, lead["research_task_id"])
            assert row.payload["purpose"] == "exploration" and row.payload["research_kind"] == "company_research"
        assert {lead["listing_ref"]["id"] for lead in records} <= {ref.id for ref in subjects}
    # Worker maintenance discovers the remaining three, but never reopens or
    # repeats the already charged selection. No further native run is executed.
    for _ in range(5):
        async with session_scope() as session:
            await DiscoveryService(session,selected).maintain()
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(DiscoveryBatch)) == 2
        assert await session.scalar(select(func.count()).select_from(BudgetEntry)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0,1,2])
async def test_live_mandate_and_application_caps(publication_db,cap):
    await setup(4,max_leads_per_batch=cap,max_new_tasks_per_batch=cap)
    async with session_scope() as session:
        batch = await DiscoveryService(session,priced()).scan()
        if not cap:
            assert batch is None
        else:
            assert len(batch.payload["leads"]) == cap and batch.payload["max_new_tasks"] == cap


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["overlimit", "subject", "version", "id", "question"])
async def test_native_rejected_output_never_creates_research(publication_db,monkeypatch,bad):
    await setup(4)
    selected = priced()
    async with session_scope() as session:
        batch = await DiscoveryService(session,selected).scan()
        response = output(batch,4 if bad=="overlimit" else 1, **({"listing_ref": ObjectRef(kind="listing",id=999,revision=1)} if bad=="subject" else {"version":"fabricated"} if bad=="version" else {"lead_id":uuid4()} if bad=="id" else {"question":None} if bad=="question" else {}))
    worker = await native_worker(monkeypatch,response,selected)
    await worker.run_once()
    async with session_scope() as session:
        await DiscoveryService(session,selected).maintain()
        batch = await session.scalar(select(DiscoveryBatch))
        assert batch.state == "stopped"
        assert all(lead["status"]=="unreviewed" for lead in batch.payload["leads"])
        assert await session.scalar(select(func.count()).select_from(AgentTask)) == 1
        assert await session.scalar(select(func.count()).select_from(InvestorCase)) == 1


@pytest.mark.asyncio
async def test_archive_pause_mixed_batch_does_not_poison_new_listing_or_move_pin(publication_db,monkeypatch):
    candidate,_ = await setup(2)
    async with session_scope() as session:
        foundation=InvestorService(session)
        case=await foundation.get_case("local_owner",candidate.thesis.case_ref.id)
        case=await foundation.set_pinned("local_owner",case.id, True, Mutation(expected_revision=case.revision,idempotency_key=uuid4()))
        await execute(session,CaseAction(case_ref=ObjectRef(kind="case",id=case.id,revision=case.revision),action="archive",reason="User archived"))
    async with session_scope() as session:
        batch=await DiscoveryService(session,priced()).scan()
        assert batch.state=="selecting"
        assert batch.payload["leads"][0]["status"]=="blocked"
        response=output(batch,1)
    worker=await native_worker(monkeypatch,response,priced())
    await worker.run_once()
    async with session_scope() as session:
        batch=await session.scalar(select(DiscoveryBatch))
        assert batch.state=="selection_completed"
        case=await InvestorService(session).get_case("local_owner",candidate.thesis.case_ref.id)
        assert case.status=="archived" and case.user_archived and case.pinned and case.episode==1
        assert sum(lead["status"]=="queued" for lead in batch.payload["leads"])==1


@pytest.mark.asyncio
async def test_missing_exploration_bucket_legacy_review_still_works(publication_db):
    await setup(exploration_daily_budget=None,review_daily_budget=None)
    async with session_scope() as session:
        batch=await DiscoveryService(session,priced()).scan()
        assert batch.state=="blocked" and batch.payload["pending_reason"]=="exploration_budget_unavailable"
        assert batch.task_id is None


@pytest.mark.asyncio
async def test_two_workers_scan_merge_history_and_future_acquisition(publication_db):
    _,subjects=await setup(2)
    async with session_scope() as session:
        repository=FinancialSnapshotRepository(session)
        for i in range(30):
            await repository.insert(listing_id=subjects[0].id, report_period=date(2010,12,31),period_kind="annual",fields={"net_profit":str(i)},provider="fixture",captured_at=datetime.now(UTC),availability="actual",available_from=date(2011,4,1),is_backfill=True)
        await repository.insert(listing_id=subjects[1].id,report_period=date(2025,12,31),period_kind="annual",fields={"net_profit":"FUTURE"},provider="fixture",captured_at=datetime.now(UTC)+timedelta(days=2),availability="actual",available_from=date(2026,4,1))
    async def scan():
        async with session_scope() as session:
            batch=await DiscoveryService(session,priced()).scan()
            return batch.id if batch else None
    ids=await asyncio.gather(scan(),scan())
    assert sum(value is not None for value in ids)==1
    async with session_scope() as session:
        batch=await session.scalar(select(DiscoveryBatch))
        assert len(batch.payload["leads"])==2
        assert "FUTURE" not in str(batch.payload)
        assert await session.scalar(select(func.count()).select_from(AgentTask))==1


async def claimed(subject,purpose,selected):
    async with session_scope() as session:
        mandate=await InvestorService(session).get_mandate("local_owner")
        task=await admit(session,base=selected,input_key=str(uuid4()),binding=ContextBinding(context_refs=(subject,),base_refs=(subject,),as_of=datetime.now(UTC)),
            grant=ExecutionGrant(mode="background",source="authorized_background",purpose=purpose,mandate_id=mandate.id,scope_refs=(subject,),prompt="Finite budget fixture",research_kind="review" if purpose=="review" else "company_research"),kind="agent_review" if purpose=="review" else "agent_research")
        row=await session.get(AgentTask,str(task.id))
        # Budget ledger stress uses the historical internal FIFO seam: it needs
        # two hypothetical concurrent exploration reservations, above AG28's
        # managed-worker exploration=1 cap. Real leases/CAS still apply; queue
        # fairness/caps are independently tested in test_agent_operations.py.
        run=await PipelineRunRepository(session).claim_next(worker_id="budget-fixture")
        assert run is not None and run.id == row.run_id
        claim=await TaskService(session).claim(task.id,RunLease(run.id,"budget-fixture",run.attempt_count))
        return claim


@pytest.mark.asyncio
async def test_atomic_two_worker_exploration_bucket_review_no_borrow_and_live_reduction(publication_db):
    _,subjects=await setup(exploration_daily_budget=Quantity(value=".000150",unit="USD"),review_daily_budget=Quantity(value=".0002",unit="USD"))
    selected=priced()
    claims=[await claimed(subjects[0],"exploration",selected) for _ in range(2)]
    async def reserve(claim):
        try:
            async with session_scope() as session:
                return await BudgetService(session,selected).reserve(claim,estimated_tokens=100)
        except BudgetExceededError:
            return None
    results=await asyncio.gather(*[reserve(claim) for claim in claims])
    assert sum(result is not None for result in results)==1
    review=await claimed(subjects[0],"review",selected)
    async with session_scope() as session:
        review_receipt=await BudgetService(session,selected).reserve(review,estimated_tokens=100)
        assert review_receipt
        mandate=await InvestorService(session).get_mandate("local_owner")
        auth=mandate.authorization.model_copy(update={"review_daily_budget":Quantity(value=".00005",unit="USD")})
        await InvestorService(session).revise_mandate("local_owner",mandate.model_copy(update={"revision":mandate.revision+1,"authorization":auth}),Mutation(expected_revision=mandate.revision,idempotency_key=uuid4()))
    async with session_scope() as session:
        with pytest.raises(BudgetExceededError):
            await BudgetService(session,selected).sent(review,review_receipt)
        assert {row.purpose for row in (await session.scalars(select(BudgetEntry))).all()}=={"review","exploration"}


@pytest.mark.asyncio
async def test_user_nomination_uses_existing_authenticated_direct_path_without_template(publication_db):
    _,subjects=await setup(exploration_daily_budget=None)
    selected=base(investor_agent_background_enabled=False)
    async with session_scope() as session:
        service=ConversationService(session,selected)
        conversation=await service.create(ConversationCreate())
        accepted=await service.submit(conversation.id,MessageInput(client_message_id=uuid4(),text="Research cash durability",context_refs=(subjects[0],),research_kind="company_research"))
        grant=await session.get(ExecutionGrantRow,str(accepted.task.id))
        assert grant.payload["mode"]=="interactive"
        assert await session.scalar(select(func.count()).select_from(DiscoveryBatch))==0
        with pytest.raises(ExecutionDeniedError,match="scope_revoked"):
            await admit(session,base=selected,input_key="unauthorized",binding=accepted.task.binding,grant=ExecutionGrant(mode="interactive",source="authenticated_message",scope_refs=(ObjectRef(kind="listing",id=999,revision=1),),mandate_id=UUID(grant.payload["mandate_id"]),prompt="Not authorized"))


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["succeeded","partial","failed","cancelled"])
async def test_pending_case_only_merges_question_source_and_one_bounded_followup(publication_db,monkeypatch,terminal):
    candidate,subjects=await setup()
    selected=priced()
    async with session_scope() as session:
        case=await InvestorService(session).get_case("local_owner",candidate.thesis.case_ref.id)
        ref=ObjectRef(kind="case",id=case.id,revision=case.revision)
        mandate=await InvestorService(session).get_mandate("local_owner")
        predecessor=await admit(session,base=selected,input_key="existing-research",binding=ContextBinding(context_refs=(ref,),base_refs=(ref,),as_of=datetime.now(UTC)),grant=ExecutionGrant(mode="background",source="authorized_background",purpose="exploration",scope_refs=(ref,),mandate_id=mandate.id,prompt="Previous distinct research question",research_kind="company_research"),kind="agent_research")
        predecessor_row=await session.get(AgentTask,str(predecessor.id))
        predecessor_run=predecessor_row.run_id
        batch=await DiscoveryService(session,selected).scan()
        selection_row=await session.get(AgentTask,batch.task_id)
        selection_run=selection_row.run_id
        response=output(batch,1)
    worker=await native_worker(monkeypatch,response,selected)
    await worker.run_once(run_id=selection_run)
    async with session_scope() as session:
        batch=await session.scalar(select(DiscoveryBatch))
        assert batch.state=="research_pending"
        lead=batch.payload["leads"][0]
        assert lead["merged_task_id"]==str(predecessor.id) and lead["question"] and lead["sources"]
        assert await session.scalar(select(func.count()).select_from(AgentTask))==2
        # Certify a terminal predecessor through the real task lease/checkpoint.
        run=await PipelineRunRepository(session).claim_next(worker_id="finish-predecessor",run_id=predecessor_run)
        claim=await TaskService(session).claim(predecessor.id,RunLease(run.id,"finish-predecessor",run.attempt_count))
        from investment_agent.investor.enums import StopReason
        await TaskService(session).checkpoint(claim,checkpoint={},terminal=terminal,stop_reason=StopReason.ANSWERED if terminal=="succeeded" else StopReason.FAILED)
    for _ in range(3):
        async with session_scope() as session:
            await DiscoveryService(session,selected).maintain()
    async with session_scope() as session:
        batch=await session.scalar(select(DiscoveryBatch))
        lead=batch.payload["leads"][0]
        assert lead["status"]==("queued" if terminal=="succeeded" else "stopped")
        assert await session.scalar(select(func.count()).select_from(AgentTask))==(3 if terminal=="succeeded" else 2)
        assert await session.scalar(select(func.count()).select_from(BudgetEntry))==1
        if terminal=="succeeded":
            grant=await session.get(ExecutionGrantRow,lead["research_task_id"])
            task=await TaskService(session).get_task(UUID(lead["research_task_id"]))
            assert grant.payload["purpose"]=="exploration" and task.parent_task_id==UUID(batch.task_id)
        assert len(batch.payload["leads"])==1 and lead["listing_ref"]["id"]==subjects[0].id


@pytest.mark.asyncio
async def test_cancel_selection_never_claims_researched_and_zero_selection_no_case(publication_db,monkeypatch):
    await setup(2)
    selected=priced()
    async with session_scope() as session:
        batch=await DiscoveryService(session,selected).scan()
        row=await session.get(AgentTask,batch.task_id)
        run_id=row.run_id
        response=output(batch,0)
        task=await TaskService(session).get_task(UUID(batch.task_id))
        await TaskService(session).cancel(task.id)
    worker=await native_worker(monkeypatch,response,selected)
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        await DiscoveryService(session,selected).maintain()
        batch=await session.scalar(select(DiscoveryBatch))
        assert batch.state=="stopped"
        assert all(lead["status"]=="unreviewed" for lead in batch.payload["leads"])
        assert await session.scalar(select(func.count()).select_from(BudgetEntry))==0


@pytest.mark.asyncio
async def test_authenticated_query_pagination_current_states_no_private_data(publication_db):
    import httpx

    from investment_agent.api.app import create_app
    from investment_agent.api.auth import get_bootstrap_settings
    from investment_agent.web.session import SESSION_COOKIE_NAME, create_session_token
    await setup()
    selected=priced()
    async with session_scope() as session:
        batch=await DiscoveryService(session,selected).scan()
        batch_id=batch.id
    app=create_app(settings=selected)
    app.dependency_overrides[get_bootstrap_settings]=lambda:selected
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
        path="/investor/discovery/batches"
        assert (await client.get(path)).status_code==401
        client.cookies.set(SESSION_COOKIE_NAME,create_session_token(api_key=selected.api_key,ttl_minutes=15))
        rows=(await client.get(path+"?limit=1")).json()
        assert len(rows)==1 and rows[0]["id"]==batch_id
        assert (await client.get(path+f'?after={rows[0]["sequence"]}')).json()==[]
        result=await client.get(path+f"/{batch_id}")
        assert result.status_code==200 and result.json()["current_task_states"]
        assert not any(key in result.text for key in ('"prompt"','"history"','fixture-model-key','"checkpoint"'))
        assert (await client.get(path+f"/{uuid4()}")).status_code==404


@pytest.mark.asyncio
async def test_native_can_select_zero_and_new_version_reuses_case_without_score_mutation(publication_db,monkeypatch):
    candidate,subjects=await setup(2)
    selected=priced()
    async with session_scope() as session:
        batch=await DiscoveryService(session,selected).scan()
        row=await session.get(AgentTask,batch.task_id)
        run_id=row.run_id
        response=output(batch,0)
    worker=await native_worker(monkeypatch,response,selected)
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        batch=await session.scalar(select(DiscoveryBatch))
        assert batch.state=="selection_completed" and all(lead["status"]=="not_selected" for lead in batch.payload["leads"])
        assert await session.scalar(select(func.count()).select_from(InvestorCase))==1
        assert await DiscoveryService(session,selected).scan() is None
        await FinancialSnapshotRepository(session).insert(listing_id=subjects[0].id,report_period=date(2025,12,31),period_kind="annual",fields={"net_profit":"corrected"},provider="offline-fixture",captured_at=datetime.now(UTC),availability="actual",available_from=date(2026,4,1))
        newer=await DiscoveryService(session,selected).scan()
        assert len(newer.payload["leads"])==1 and newer.payload["leads"][0]["previous_sources"]
        previous=[lead for lead in batch.payload["leads"] if lead["listing_ref"]["id"]==subjects[0].id][0]
        assert newer.payload["leads"][0]["version"]!=previous["version"]
        assert (await InvestorService(session).get_case("local_owner",candidate.thesis.case_ref.id)).episode==1


@pytest.mark.asyncio
async def test_pending_coverage_reads_real_ag13_committed_progress_and_versioned_evidence(publication_db,monkeypatch):
    from investment_agent.agent.research import research_artifacts, save_progress
    from investment_agent.evidence.snapshots import materialize
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.enums import StopReason
    from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
    from tests.test_investor_monitoring import full_progress
    candidate,subjects=await setup()
    selected=priced()
    async with session_scope() as session:
        mandate=await InvestorService(session).get_mandate("local_owner")
        ref=subjects[0]
        case_ref_value=ObjectRef(kind="case",id=candidate.thesis.case_ref.id,revision=1)
        await FinancialSnapshotRepository(session).insert(listing_id=ref.id,report_period=date(2025,12,31),period_kind="annual",fields={"net_profit":42, "verification":{"net_profit_parent":"verified"}},provider="ifind",captured_at=datetime.now(UTC),availability="actual",available_from=date(2026,4,1))
        sources=await DiscoveryService(session,selected)._sources(ref.id,datetime.now(UTC))
        source=sources[0]
        # Real snapshot adapter, immutable evidence service, AG13 validation and
        # committed action result prove the actual coverage data path.
        snapshot=await session.get(FinancialSnapshot,source["id"])
        record=materialize(snapshot,"net_profit_parent")
        assert record.verification=="verified"
        evidence=await EvidenceService(session).save_evidence("local_owner",record,uuid4())
        predecessor=await admit(session,base=selected,input_key="covered-predecessor",binding=ContextBinding(context_refs=(ref,case_ref_value),base_refs=(ref,case_ref_value),as_of=datetime.now(UTC)),grant=ExecutionGrant(mode="background",source="authorized_background",purpose="exploration",scope_refs=(ref,case_ref_value),mandate_id=mandate.id,prompt="Is the reported profit recurring?",research_kind="review"),kind="agent_review")
        original=await session.get(AgentTask,str(predecessor.id))
        original_run=original.run_id
        batch=await DiscoveryService(session,selected).scan()
        selection=await session.get(AgentTask,batch.task_id)
        selection_run=selection.run_id
        response=output(batch,1)
    worker=await native_worker(monkeypatch,response,selected)
    await worker.run_once(run_id=selection_run)
    async with session_scope() as session:
        run=await PipelineRunRepository(session).claim_next(worker_id="covering",run_id=original_run)
        claim=await TaskService(session).claim(predecessor.id,RunLease(run.id,"covering",run.attempt_count))
        progress=full_progress(candidate,evidence).model_copy(update={"question":response.decisions[0].question})
        service=TaskService(session)
        action=await service.prepare_action(claim,intent_key="coverage-progress",parameters={"tool":"save_research_progress","business":progress.model_dump(mode="json")},effect="database")
        await service.settle_action(claim,UUID(action.operation_id),lambda s,op:save_progress(s,op,progress))
        artifacts=await research_artifacts(session,predecessor.id)
        assert artifacts[0]["progress"]["question"]==response.decisions[0].question
        await service.checkpoint(claim,checkpoint={},terminal="succeeded",stop_reason=StopReason.ANSWERED)
    async with session_scope() as session:
        await DiscoveryService(session,selected).maintain()
        batch=await session.scalar(select(DiscoveryBatch))
        lead=batch.payload["leads"][0]
        assert batch.state=="selection_completed" and lead["status"]=="covered"
        assert lead["research_task_id"]==str(predecessor.id)
        assert await session.scalar(select(func.count()).select_from(AgentTask))==2
        assert await session.scalar(select(func.count()).select_from(BudgetEntry))==1
