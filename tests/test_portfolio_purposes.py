"""One actual cash pool: immutable user allocations and independent comparisons."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from investment_agent.api.app import create_app
from investment_agent.api.auth import get_settings
from investment_agent.config import Settings
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculation_contracts import Scenario
from investment_agent.portfolio.calculations import calculate
from investment_agent.portfolio.contracts import CashFact, CreateAccount
from investment_agent.portfolio.purposes import (
    Purpose,
    PurposeRequest,
    PurposeService,
    check_scenarios,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.portfolio import PortfolioEvent
from tests.test_portfolio_calculations import action, request
from tests.test_portfolio_ledger import T, append, balance
from tests.test_portfolio_ledger import ledger as ledger_fixture

ledger = ledger_fixture
D = Decimal


def purpose(**changes):
    return Purpose(id=uuid4(), name="近期生活需要", amount="600", available_budget="100", required_cash="500", horizon="short_term", required_on=T.date(), source="用户确认的资金安排", source_version="账单一版", **changes)


def confirmation(snapshot, purposes=None, revision=0):
    return PurposeRequest(account_id=snapshot.account_id, idempotency_key=uuid4(), expected_revision=revision, snapshot_ref=snapshot.reference, purposes=purposes or (purpose(),), confirmation="user_confirmed")


async def confirm(req, enabled=True):
    async with session_scope() as session:
        return await PurposeService(session).confirm(req, enabled=enabled)


@pytest.mark.asyncio
async def test_cash_authority_cas_replay_off_changed_body_and_immutable_versions(ledger):
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    req = confirmation(snap)
    version = await confirm(req)
    assert version.confirmed_cash == D(1000) and version.unallocated == D(400)
    assert await confirm(req, enabled=False) == version
    with pytest.raises(InvestorError, match="标识"):
        await confirm(req.model_copy(update={"expected_revision": 1}), enabled=False)
    with pytest.raises(InvestorError, match="版本"):
        await confirm(confirmation(snap))
    renamed = req.purposes[0].model_copy(update={"name": "改个名称"})
    changed = await confirm(confirmation(snap, (renamed,), 1))
    assert changed.semantic_hash == version.semantic_hash and changed.reference != version.reference
    async with session_scope() as session:
        assert await PurposeService(session).exact(account, version.reference) == version
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
    excessive = renamed.model_copy(update={"amount": D(1001)})
    with pytest.raises(InvestorError, match="覆盖"):
        await confirm(confirmation(snap, (excessive,), 2))
    with pytest.raises(ValidationError):
        Purpose.model_validate({**renamed.model_dump(mode="json"), "amount": "550"})


@pytest.mark.asyncio
async def test_unknown_cash_not_subtotal_partial_known_cash_and_no_cross_account(ledger):
    account, ref = ledger
    unknown = (await append(account, balance(ref, cash=None, coverage="partial"), 0)).snapshot
    with pytest.raises(InvestorError, match="未知现金"):
        await confirm(confirmation(unknown))
    snap = (await append(account, balance(ref, kind="reconciliation", coverage="partial"), 1)).snapshot
    version = await confirm(confirmation(snap))
    assert snap.total_assets is None and version.confirmed_cash == D(1000)
    async with session_scope() as session:
        other = await PortfolioService(session).create_account(CreateAccount(idempotency_key=uuid4(), name="另一个账户"))
        with pytest.raises(InvestorError, match="不存在"):
            await PurposeService(session).exact(other.id, version.reference)
    with pytest.raises(InvestorError, match="版本"):
        await confirm(confirmation(unknown, revision=1))


@pytest.mark.asyncio
async def test_actual_cash_drop_recorded_and_purposes_overallocated_without_history_rewrite(ledger):
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    version = await confirm(confirmation(snap))
    fact = CashFact(kind="cash_transfer", occurred_at=T + timedelta(days=1), source="实际银行转出", user_confirmed=True, amount=D(-500))
    result = await append(account, fact, 1)
    assert result.snapshot.known_cash == D(500)
    async with session_scope() as session:
        view = await PurposeService(session).current(account)
        assert view.status == "overallocated" and view.unallocated == D(-100)
        assert await PurposeService(session).exact(account, version.reference) == version


@pytest.mark.asyncio
async def test_fees_multiple_purposes_mutually_exclusive_and_short_term_protection(ledger):
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    p1 = purpose()
    p2 = p1.model_copy(update={"id": uuid4(), "name": "长期研究", "horizon": "long_term", "amount": D(400), "available_budget": D(400), "required_cash": D(0)})
    version = await confirm(confirmation(snap, (p1, p2)))
    a = action(ref, quantity=D(10), purpose_id=p1.id)
    b = action(ref, quantity=D(39), purpose_id=p2.id)
    req = request(snap, ref, purpose_ref=version.reference, scenarios=(Scenario(name="shared", actions=(a,b)), Scenario(name="alternative", actions=(b,)), Scenario(name="reserve", actions=(action(ref, quantity=D(60), purpose_id=p2.id),))))
    details = calculate(snap, req, {ref:D(10)}, {ref:"sector"})
    details.update(request=req.model_dump(mode="json"), snapshot=snap.model_dump(mode="json"))
    check_scenarios(details, version)
    baseline, shared, alternative, reserve = details["scenarios"]
    assert baseline["purpose_checks"]["remaining_cash"] == "1000"
    assert shared["cash"] == "508" and shared["fees"] == "2"
    assert f"purpose_budget_exceeded:{p1.id}" in shared["violations"]  # fees cross cap
    assert alternative["purpose_checks"]["purposes"][1]["spent"] == "391"
    assert alternative["purpose_checks"]["purposes"][1]["remaining_budget"] == "9"
    assert "purpose_required_cash_shortfall" in reserve["violations"]
    assert reserve["purpose_checks"]["reserve_shortfall"] == "101"
    assert shared["purpose_checks"]["purposes"][1]["remaining_budget"] == "9"  # options never pool spend


@pytest.mark.asyncio
async def test_authenticated_api_disabled_replay_and_owner_boundary(ledger, monkeypatch):
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    monkeypatch.setenv("API_KEY", "fixture-purpose-key")
    selected = Settings(api_key="fixture-purpose-key", portfolio_ledger_enabled=True)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda:selected
    req = confirmation(snap)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post('/portfolio/purposes', json=req.model_dump(mode='json'))).status_code == 401
        headers = {'X-API-Key':'fixture-purpose-key'}
        response = await client.post('/portfolio/purposes', json=req.model_dump(mode='json'),headers=headers)
        assert response.status_code == 200, response.text
        selected = selected.model_copy(update={'portfolio_ledger_enabled':False})
        assert (await client.post('/portfolio/purposes',json=req.model_dump(mode='json'),headers=headers)).status_code == 200
        new = confirmation(snap, revision=1)
        assert (await client.post('/portfolio/purposes',json=new.model_dump(mode='json'),headers=headers)).status_code == 403
        view = (await client.get(f'/portfolio/purposes/current?account_id={account}',headers=headers)).json()
        assert view['known_cash'] == '1000' and view['required_cash'] == '500'


@pytest.mark.asyncio
async def test_user_message_native_purposes_calculation_plan_and_adoption_no_actual(ledger):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    from investment_agent.agent.contracts import MessageInput
    from investment_agent.agent.conversations import ConversationCreate, ConversationService
    from investment_agent.agent.execution import ExecutionGrant
    from investment_agent.agent.executor import AgentExecutor
    from investment_agent.agent.job import AgentJobHandler
    from investment_agent.agent.research import (
        ResearchOutput,
        ResearchPolicy,
        ResearchProgress,
        registrations,
    )
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.common import ObjectRef
    from investment_agent.investor.contracts import NumericNode, TextNode
    from investment_agent.investor.controls import (
        CaseAction,
        ExecuteRequest,
        UserControlService,
        authenticated_request,
    )
    from investment_agent.investor.foundations import CreateCase, InvestorService
    from investment_agent.portfolio.plan_contracts import PlanSemanticInput
    from investment_agent.portfolio.plans import PortfolioPlanService
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.storage.models.agent import ExecutionGrantRow, ToolAction
    from tests.test_portfolio_plans import base, semantic, setup
    candidate, ref, calc = await setup(ledger, movement=False, trusted=False)
    async with session_scope() as session:
        snap = await PortfolioService(session).snapshot(candidate.account_id)
    purpose_version = await confirm(confirmation(snap))
    selected = base(agent_api_key="fixture-offline-key", agent_base_url="https://fixture.invalid/v1", agent_model="fixture", agent_max_tokens=100)
    async with session_scope() as session:
        case = await InvestorService(session).create_case("local_owner", CreateCase(listing_ref=ref, question="Archived company research", idempotency_key=uuid4()))
        archive = CaseAction(case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision), action="archive", reason="Keep company research archived")
        await UserControlService(session).execute(ExecuteRequest(parameters=archive, idempotency_key=uuid4()), authenticated_request(archive))
        conversations = ConversationService(session, selected)
        conversation = await conversations.create(ConversationCreate(title="Explicit portfolio risk"))
        receipt = await conversations.submit(conversation.id, MessageInput(client_message_id=uuid4(), text="Review the confirmed portfolio risk", context_refs=(candidate.snapshot_ref,), research_kind="portfolio_action"))
        saved = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, str(receipt.task.id))).payload)
        assert saved.portfolio_account_id == candidate.account_id and saved.portfolio_snapshot_ref == candidate.snapshot_ref
    progress = ResearchProgress(question="Review confirmed account constraints", kind="portfolio_action", content=(TextNode(type="text", text="Business evidence remains unavailable; wait without increasing risk."),), answered=True, gaps=("Company evidence unavailable",), completed_steps=("Confirmed actual exposure and typed user constraint checked",))
    observed = {"tools": set(), "calls": 0}
    def model(messages, info):
        observed["calls"] += 1
        observed["tools"].update(t.name for t in info.function_tools)
        returns = [p for m in messages for p in getattr(m, "parts", ()) if isinstance(p, ToolReturnPart)]
        names = [p.tool_name for p in returns]
        if "get_portfolio_context" not in names:
            return ModelResponse([ToolCallPart("get_portfolio_context", {"business": {"snapshot_ref": candidate.snapshot_ref.model_dump(mode="json"), "account_id": str(candidate.account_id)}}, tool_call_id="read")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        if "save_research_progress" not in names:
            assert "prepare_portfolio_plan" not in [t.name for t in info.function_tools]
            return ModelResponse([ToolCallPart("save_research_progress", {"business": progress.model_dump(mode="json")}, tool_call_id="progress")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        if "calculate_portfolio_options" not in names:
            business = dict(calc.details["request"])
            business.pop("operation_id")
            business["purpose_ref"] = purpose_version.reference.model_dump(mode="json")
            return ModelResponse([ToolCallPart("calculate_portfolio_options", {"business": business}, tool_call_id="calc")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        returned = next(p for p in returns if p.tool_name == "calculate_portfolio_options")
        built = candidate.model_copy(update={"calculation_ref": ObjectRef.model_validate(returned.content["calculation_ref"])})
        if "prepare_portfolio_plan" not in names:
            return ModelResponse([ToolCallPart("prepare_portfolio_plan", {"business": built.model_dump(mode="json")}, tool_call_id="prepare")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        prepared_return = next(p for p in returns if p.tool_name == "prepare_portfolio_plan")
        review = semantic(built)
        review = review.model_copy(update={"checked_sources": (*review.checked_sources, purpose_version.reference)})
        final_progress = progress.model_copy(update={"fact_refs": (built.calculation_ref,), "content": (*progress.content, NumericNode(type="numeric_claim", role="calculation_output", ref=built.calculation_ref.model_copy(update={"field_path": "results.scenario_0_cash"}), display_format="decimal"))})
        output = ResearchOutput(progress=final_progress, portfolio_review=PlanSemanticInput(preparation_ref=ObjectRef.model_validate(prepared_return.content["preparation_ref"]), assessments=review.assessments, checked_sources=review.checked_sources, conclusion="pass"))
        return ModelResponse([TextPart(output.model_dump_json())], usage=RequestUsage(input_tokens=20, output_tokens=10))
    executor = AgentExecutor(selected, model=FunctionModel(model), registrations=registrations(), specification=ResearchPolicy(selected).specification())
    worker = ResearchWorker(settings=selected, worker_id="portfolio-fixture", handlers=[AgentJobHandler("agent_turn", executor)])
    await worker.run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        from investment_agent.storage.models.agent import PublicTaskEvent
        assert task.status == "succeeded", [e.payload["payload"] for e in await session.scalars(select(PublicTaskEvent).where(PublicTaskEvent.task_id == str(task.id))) if e.payload.get("type") == "error"]
        view = await PortfolioPlanService(session).current(candidate.account_id)
        assert view and view.status == "published" and not view.plan.options[0].executable
        actions = list(await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id))))
        assert {a.parameters.get("tool") for a in actions if a.state == "committed"} >= {"get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan", "save_research_progress"}
        record = await PortfolioPlanService(session).calculations.get(candidate.account_id, view.plan.calculation_ref.id)
        assert record.details["purposes"]["reference"] == purpose_version.reference.model_dump(mode="json")
        assert record.details["scenarios"][1]["purpose_checks"]["required_cash"] == "500"
        assert "confirm_purposes" not in observed["tools"]
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
    assert observed["calls"] >= 5
    assert "publish_portfolio_plan" not in observed["tools"] and "prepare_company_view" not in observed["tools"]


@pytest.mark.asyncio
async def test_user_clear_all_purposes_releases_only_planning_allocation(ledger):
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    first = await confirm(confirmation(snap))
    req = PurposeRequest(account_id=account, snapshot_ref=snap.reference, expected_revision=1, idempotency_key=uuid4(), purposes=(), confirmation="user_confirmed")
    cleared = await confirm(req)
    assert cleared.unallocated == D(1000) and cleared.semantic_hash != first.semantic_hash
    async with session_scope() as session:
        assert (await PurposeService(session).current(account)).required_cash == 0
        assert await PurposeService(session).exact(account, first.reference) == first
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1


def test_purpose_budget_boundary_uses_exact_decimal_not_default_context():
    p = purpose().model_dump(mode="json")
    p.update(amount="9999999999999999999999",available_budget="9999999999999999999998.999999999999999999",required_cash="0.000000000000000002")
    with pytest.raises(ValidationError):
        Purpose.model_validate(p)


@pytest.mark.asyncio
async def test_unknown_outlay_stays_unknown_in_purpose_outputs(ledger):
    from investment_agent.portfolio.calculations import reference_outputs
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    version = await confirm(confirmation(snap))
    req = request(snap, ref, purpose_ref=version.reference, scenarios=(Scenario(name="unknown_fee", actions=(action(ref, quantity=D(1), fixed_fee=None, purpose_id=version.purposes[0].id),)),))
    details = calculate(snap, req, {ref:D(10)}, {ref:"sector"})
    details.update(request=req.model_dump(mode="json"), snapshot=snap.model_dump(mode="json"))
    check_scenarios(details, version)
    result = details["scenarios"][1]
    assert result["purpose_checks"]["purposes"][0]["spent"] is None
    assert result["purpose_checks"]["purposes"][0]["remaining_budget"] is None
    outputs = reference_outputs(details)
    assert "scenario_1_purpose_0_spent" not in outputs
    assert outputs["scenario_0_purpose_required_cash"].value == D(500)
    paths = [p for p in details["result_paths"] if p["metric"] == "purpose_required_cash"]
    assert paths[0]["purpose_ref"] == version.reference.model_dump(mode="json")


@pytest.mark.asyncio
async def test_two_writers_purpose_cas_and_actual_cash_cas(ledger):
    import asyncio
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    first, second = confirmation(snap), confirmation(snap)
    results = await asyncio.gather(confirm(first), confirm(second), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, InvestorError) for r in results) == 1
    newer = (await append(account, balance(ref, kind="reconciliation", cash="900"), 1)).snapshot
    async with session_scope() as session:
        current = await PurposeService(session).current(account)
    with pytest.raises(InvestorError, match="版本"):
        await confirm(confirmation(snap, revision=current.version.reference.revision))
    version = await confirm(confirmation(newer, revision=current.version.reference.revision))
    assert version.confirmed_cash == D(900)


@pytest.mark.asyncio
async def test_pre_purpose_plan_limited_and_review_outbox_coalesces(ledger):
    from investment_agent.portfolio.plans import PortfolioPlanService
    from investment_agent.storage.models.portfolio_plan import PortfolioPlanOutbox
    from tests.test_portfolio_plans import publish, setup
    candidate, ref, _ = await setup(ledger, movement=False)
    result = await publish(candidate)
    async with session_scope() as session:
        snap = await PortfolioService(session).snapshot(candidate.account_id)
    await confirm(confirmation(snap))
    async with session_scope() as session:
        service = PortfolioPlanService(session)
        view = await service.current(candidate.account_id)
        assert view.status == "review_required"
        assert "purpose_constraints_changed" in view.limitation_reasons
        assert await service.reconcile() == 1
        assert await service.reconcile() == 1
        rows = list(await session.scalars(select(PortfolioPlanOutbox).where(PortfolioPlanOutbox.plan_id == str(result.plan_ref.id))))
        assert len(rows) == 2  # original published + one coalesced review event
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["after_admission", "historical"])
async def test_native_purpose_knowledge_cutoff_requires_fresh_user_message(ledger, timing):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    from investment_agent.agent.contracts import MessageInput
    from investment_agent.agent.conversations import ConversationCreate, ConversationService
    from investment_agent.agent.executor import AgentExecutor
    from investment_agent.agent.job import AgentJobHandler
    from investment_agent.agent.research import (
        ResearchOutput,
        ResearchPolicy,
        ResearchProgress,
        registrations,
    )
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.contracts import TextNode
    from investment_agent.portfolio.plans import PortfolioPlanService
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.storage.models.agent import ToolAction
    from tests.test_portfolio_plans import base, setup

    candidate, _, _ = await setup(ledger, movement=False)
    async with session_scope() as session:
        snap = await PortfolioService(session).snapshot(candidate.account_id)
    pv = await confirm(confirmation(snap))
    selected = base(agent_api_key="fixture-offline-key", agent_base_url="https://fixture.invalid/v1", agent_model="fixture", agent_max_tokens=100)
    async with session_scope() as session:
        service = ConversationService(session, selected)
        chat = await service.create(ConversationCreate(title="Exact purpose knowledge window"))
        receipt = await service.submit(chat.id, MessageInput(client_message_id=uuid4(),text="Review account purposes", context_refs=(candidate.snapshot_ref,),research_kind="portfolio_action",research_at=pv.confirmed_at - timedelta(microseconds=1) if timing == "historical" else None))
    if timing == "after_admission":
        changed = pv.purposes[0].model_copy(update={"required_cash":D(600),"available_budget":D(0)})
        await confirm(confirmation(snap, (changed,), 1))
    progress = ResearchProgress(question="Need new user message",kind="portfolio_action",content=(TextNode(type="text",text="Confirmed purpose knowledge needs a fresh research window."),),answered=False,gaps=("New user confirmation",),completed_steps=())
    def model(messages, info):
        returns = [p for m in messages for p in getattr(m, "parts", ()) if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse([ToolCallPart("get_portfolio_context", {"business":{"account_id":str(candidate.account_id),"snapshot_ref":candidate.snapshot_ref.model_dump(mode="json")}},tool_call_id="read")],usage=RequestUsage(input_tokens=20,output_tokens=10))
        return ModelResponse([TextPart(ResearchOutput(progress=progress).model_dump_json())],usage=RequestUsage(input_tokens=20,output_tokens=10))
    executor = AgentExecutor(selected,model=FunctionModel(model),registrations=registrations(),specification=ResearchPolicy(selected).specification())
    await ResearchWorker(settings=selected,worker_id="purpose-cutoff",handlers=[AgentJobHandler("agent_turn",executor)]).run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        assert task.status in {"partial", "failed"}
        assert await PortfolioPlanService(session).current(candidate.account_id) is None
        actions = list(await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id))))
        assert not any(a.state == "committed" and a.parameters.get("tool") == "get_portfolio_context" for a in actions)


@pytest.mark.asyncio
async def test_sell_fee_above_proceeds_consumes_purpose_budget(ledger):
    account, ref = ledger
    snap = (await append(account, balance(ref), 0)).snapshot
    p = purpose().model_copy(update={"available_budget":D(0)})
    version = await confirm(confirmation(snap, (p,)))
    req = request(snap, ref, purpose_ref=version.reference, scenarios=(Scenario(name="costly_sale",actions=(action(ref,direction="sell",quantity=D(1),fixed_fee=D(11),purpose_id=p.id),)),))
    details = calculate(snap, req, {ref:D(10)}, {ref:"sector"})
    details.update(request=req.model_dump(mode="json"),snapshot=snap.model_dump(mode="json"))
    check_scenarios(details, version)
    result = details["scenarios"][1]
    assert result["cash"] == "999"
    assert result["purpose_checks"]["purposes"][0]["spent"] == "1"
    assert f"purpose_budget_exceeded:{p.id}" in result["violations"]
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1


@pytest.mark.asyncio
async def test_serialized_pre_ag29_calculation_replay_rebuild_and_plan_compatibility(ledger):
    from investment_agent.evidence.contracts import CalculationRecord
    from investment_agent.portfolio.calculation_contracts import (
        PortfolioCalculationRequest,
        PortfolioScenarioInput,
    )
    from investment_agent.portfolio.calculations import PortfolioCalculationService
    from investment_agent.portfolio.service import digest
    from investment_agent.storage.models.evidence import SavedCalculation
    from tests.test_portfolio_plans import setup
    candidate, _, record = await setup(ledger, movement=True, trusted=False)
    raw = record.model_dump(mode="json")
    legacy_request = raw["details"]["request"]
    legacy_request.pop("purpose_ref", None)
    for scenario in legacy_request["scenarios"]:
        for movement in scenario["actions"]:
            movement.pop("purpose_id", None)
    legacy_id = uuid4()
    raw["id"] = str(legacy_id)
    legacy_request["operation_id"] = str(legacy_id)
    candidate = candidate.model_copy(update={"calculation_ref": candidate.calculation_ref.model_copy(update={"id":legacy_id})})
    old_input = {"request": legacy_request, "snapshot": raw["details"]["snapshot"], "mandate": raw["details"]["mandate"], "sources": raw["details"]["sources"], "trusted_rules": raw["details"]["trusted_rules"]}
    raw["input_hash"] = digest(old_input)  # exact original AG22 hash shape, no purposes key
    old = CalculationRecord.model_validate(raw)
    request_model = PortfolioCalculationRequest.model_validate(legacy_request)
    assert request_model.model_dump(mode="json") == legacy_request
    business = dict(legacy_request)
    business.pop("operation_id")
    assert PortfolioScenarioInput.model_validate(business).model_dump(mode="json") == business
    async with session_scope() as session:
        session.add(SavedCalculation(id=str(legacy_id), revision=1, owner_id="local_owner", payload=raw))  # insert historical serialized fixture; never mutate a row
    async with session_scope() as session:
        service = PortfolioCalculationService(session)
        replay = await service.save(request_model)
        assert replay == old
        rebuilt = await service.rebuild(request_model)
        assert rebuilt.input_hash == old.input_hash
        assert rebuilt.results == old.results and rebuilt.details == old.details
        from investment_agent.portfolio.plans import PortfolioPlanService
        assert await PortfolioPlanService(session).verified_calculation(candidate) == old
