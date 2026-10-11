"""AG23 whole-account CAS, exact action authority and real native tools offline."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
from investment_agent.agent.tasks import TaskService, fingerprint
from investment_agent.api.app import create_app
from investment_agent.api.auth import get_bootstrap_settings, get_settings
from investment_agent.config import Settings
from investment_agent.investor.common import Mutation, ObjectRef
from investment_agent.investor.contracts import NumericNode, PortfolioConstraints, TextNode
from investment_agent.investor.controls import (
    DecisionAction,
    ExecuteRequest,
    UserControlService,
    authenticated_request,
)
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.portfolio.calculation_contracts import PortfolioCalculationRequest, Scenario
from investment_agent.portfolio.calculations import PortfolioCalculationService, TrustedTradingRule
from investment_agent.portfolio.plan_contracts import (
    OptionInput,
    PlanInput,
    PlanSemanticReview,
    PortfolioAction,
)
from investment_agent.portfolio.plans import PortfolioPlanService, maintain_portfolio_plans
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioEvent
from investment_agent.storage.models.portfolio_plan import (
    PortfolioPlanArtifact,
    PortfolioPlanEvent,
    PortfolioPlanHead,
    PortfolioPlanOutbox,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.test_investor_foundations import mandate
from tests.test_portfolio_calculations import action, rule
from tests.test_portfolio_ledger import append, balance, trade
from tests.test_portfolio_ledger import ledger as ledger_fixture

ledger = ledger_fixture
D = Decimal


def base(**kw):
    # Native-agent requests are reserved at wire utf8 bytes, a deliberately
    # conservative estimate; the synthetic fixture flow sits near the default
    # task token cap, so give it headroom (real budget behaviour has its own
    # dedicated tests).
    return Settings(_env_file=None, api_key="ci-inert-api-key", investor_agent_enabled=True, portfolio_ledger_enabled=True, investor_agent_task_seconds=600, investor_agent_task_tokens=400000, **kw)


class Rules:
    async def resolve(self, ref, as_of):
        return TrustedTradingRule(rule=rule(ref), market_code="SH", valid_from=(as_of - timedelta(days=30)).date(), available_from=as_of - timedelta(days=30), trusted_source="fixture verified exchange seam", source_version="fixture")


async def setup(ledger, *, cash="1000", coverage="complete", quantity="100", fee="1", movement=True, trusted=True):
    account, ref = ledger
    now = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as session:
        session.add(MarketSnapshot(listing_id=ref.id, trade_date=now.date(), open=D(10), high=D(10), low=D(10), close=D(10), volume=D(1), adjust="none", provider="fixture", captured_at=now, version=1))
        session.add(IndustryClassification(listing_id=ref.id, system="CSRC", code="fixture-sector", name="Fixture", valid_from=now.date(), source="fixture", captured_at=now))
        m = await InvestorService(session).create_mandate("local_owner", mandate().model_copy(update={"scope_refs": (ref,), "hard_constraints": (), "portfolio_constraints": PortfolioConstraints(account_id=account, max_single_weight=D("0.3"), max_sector_weight=D("0.6"), minimum_cash=D(0))}), uuid4())
    snapshot = (await append(account, balance(ref, cash=cash, coverage=coverage, quantity=quantity), 0)).snapshot
    req = PortfolioCalculationRequest(operation_id=uuid4(), account_id=account, snapshot_ref=snapshot.reference, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=m.revision), scenarios=(Scenario(name="reduce_risk" if movement else "wait", actions=(action(ref, direction="sell", quantity=D(10), fixed_fee=D(fee) if fee is not None else None),) if movement else ()),))
    async with session_scope() as session:
        calc = await PortfolioCalculationService(session, rules=Rules() if trusted else None).save(req)
    a = PortfolioAction(id=uuid4(), listing_ref=ref, stance="reduce" if movement else "wait", basis="portfolio_constraint", constraint="max_single_weight", research_gap="Company durability has not been researched", target_ref=ObjectRef(kind="calculation", id=calc.id, revision=1, field_path="results.scenario_1_action_0_quantity") if movement else None, content=(TextNode(type="text", text="Reduce confirmed concentration"),))
    candidate = PlanInput(account_id=account, snapshot_ref=snapshot.reference, mandate_ref=req.mandate_ref, calculation_ref=ObjectRef(kind="calculation", id=calc.id, revision=1), previous_ref=None, options=(OptionInput(id=uuid4(), scenario_index=1, actions=(a,)),), question_scope="Whole confirmed account alternatives")
    return candidate, ref, calc


def semantic(candidate, **updates):
    actions = [a for o in candidate.options for a in o.actions]
    value = PlanSemanticReview(candidate_hash=fingerprint(candidate.model_dump(mode="json")), assessments={str(a.id): ("Confirmed exposure exceeds exact user limit", "Missing business evidence remains a research gap", "Risk reduction improves concentration; residual violations remain explicit") for a in actions}, checked_sources=(candidate.snapshot_ref, candidate.mandate_ref, candidate.calculation_ref, *(r for a in actions for r in a.company_view_refs)), unresolved_questions=(), conclusion="pass")
    return value.model_copy(update=updates)


async def prepared(candidate, *, review=None, permission=True, scope=None):
    review = review or semantic(candidate)
    async with session_scope() as session:
        refs = scope or (candidate.snapshot_ref, *(a.listing_ref for o in candidate.options for a in o.actions))
        grant = ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Recheck portfolio risk", tool_names=("publish_portfolio_plan",) if permission else (), scope_refs=refs, mandate_id=candidate.mandate_ref.id, research_kind="portfolio_action", portfolio_account_id=candidate.account_id, portfolio_snapshot_ref=candidate.snapshot_ref)
        task = await admit(session, base=base(), input_key=str(uuid4()), binding=ContextBinding(context_refs=(candidate.snapshot_ref,), base_refs=refs, as_of=datetime.now(UTC)), grant=grant)
        run = await PipelineRunRepository(session).claim_next(worker_id=str(task.id), kinds=["agent_turn"])
        claim = await TaskService(session).claim(task.id, RunLease(run.id, str(task.id), run.attempt_count))
        operation = await TaskService(session).prepare_action(claim, intent_key="publish", parameters={"candidate": candidate.model_dump(mode="json"), "semantic": review.model_dump(mode="json")})
    return claim, UUID(operation.operation_id), review


async def publish(candidate, prepared_action=None, *, trusted=True):
    claim, operation, review = prepared_action or await prepared(candidate)
    async with session_scope() as session:
        return await PortfolioPlanService(session, rules=Rules() if trusted else None).publish(claim, operation, candidate, review, base=base())


@pytest.mark.asyncio
async def test_constraint_reduce_residual_violation_no_company_and_server_totals(ledger):
    candidate, _, calc = await setup(ledger)
    result = await publish(candidate)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        view = await PortfolioPlanService(session, rules=Rules()).get(result.plan_ref)
        assert view.status == "published"
        assert view.plan.no_action.violations == ("mandate_constraint_violation:max_single_weight",)
        option = view.plan.options[0]
        assert option.executable and option.constraint_improvements == ("max_single_weight",)
        assert option.violations == view.plan.no_action.violations
        assert option.totals == {p["field_path"][8:]: calc.results[p["field_path"][8:]] for p in calc.details["result_paths"] if p["scenario_index"] == 1}
        assert not view.plan.acceptance_reserves_cash
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
        assert (await session.get(PortfolioPlanArtifact, str(result.plan_ref.id))).account_id == str(candidate.account_id)


@pytest.mark.asyncio
async def test_constraint_wait_explicit_limits_without_rules_or_cash(ledger):
    candidate, _, _ = await setup(ledger, movement=False, trusted=False)
    result = await publish(candidate, trusted=False)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert not view.plan.options[0].executable
        assert view.plan.options[0].violations and view.plan.options[0].actions[0].research_gap


@pytest.mark.parametrize("change", ["buy_exception", "exit_partial", "reduce_exit", "hold_trade", "target_scenario", "target_holding", "numeric_prose", "no_company", "bad_semantic", "wrong_source", "unknown_fee", "untrusted_rule", "partial", "unknown_cash"])
@pytest.mark.asyncio
async def test_action_and_publication_gates(ledger, change):
    candidate, _, _ = await setup(ledger, fee=None if change == "unknown_fee" else "1", trusted=change != "untrusted_rule", coverage="partial" if change == "partial" else "complete", cash=None if change == "unknown_cash" else "1000")
    option = candidate.options[0]
    a = option.actions[0]
    updates = {
        "buy_exception": {"stance": "buy"},
        "exit_partial": {"stance": "exit"},
        "reduce_exit": {"target_ref": a.target_ref.model_copy(update={"field_path": "results.scenario_0_holding_0_quantity"})},
        "hold_trade": {"stance": "hold"},
        "target_scenario": {"target_ref": a.target_ref.model_copy(update={"field_path": "results.scenario_0_holding_0_quantity"})},
        "target_holding": {"target_ref": a.target_ref.model_copy(update={"field_path": "results.scenario_1_holding_0_quantity"})},
        "numeric_prose": {"content": (TextNode(type="text", text="Sell 10 shares"),)},
        "no_company": {"basis": "company_thesis", "constraint": None},
    }.get(change, {})
    candidate = candidate.model_copy(update={"options": (option.model_copy(update={"actions": (a.model_copy(update=updates),)}),)})
    review = semantic(candidate)
    if change == "bad_semantic":
        review = review.model_copy(update={"candidate_hash": "other"})
    if change == "wrong_source":
        review = review.model_copy(update={"checked_sources": ()})
    prepared_action = await prepared(candidate, review=review)
    result = await publish(candidate, prepared_action, trusted=change != "untrusted_rule")
    assert not result.published and result.check.failures
    async with session_scope() as session:
        assert await PortfolioPlanService(session).current(candidate.account_id) is None
        assert result.draft_ref and await session.get(PortfolioPlanArtifact, str(result.draft_ref.id))


@pytest.mark.asyncio
async def test_atomic_replace_two_sqlite_sessions_only_one_wins(ledger):
    candidate, _, _ = await setup(ledger)
    first = await publish(candidate)
    replacement = candidate.model_copy(update={"previous_ref": first.plan_ref})
    p1, p2 = await prepared(replacement), await prepared(replacement)
    results = await asyncio.gather(publish(replacement, p1), publish(replacement, p2), return_exceptions=True)
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, InvestorError)]
    assert len(winners) == 1 and winners[0].published and len(losers) == 1
    assert losers[0].detail.code == "revision_conflict"
    async with session_scope() as session:
        svc = PortfolioPlanService(session, rules=Rules())
        assert (await svc.current(candidate.account_id)).plan.id == winners[0].plan_ref.id
        assert (await svc.get(first.plan_ref)).status == "superseded"
        assert await session.scalar(select(func.count()).select_from(PortfolioPlanHead)) == 1
        assert await session.scalar(select(func.count()).select_from(PortfolioPlanArtifact).where(PortfolioPlanArtifact.kind == "portfolio_plan")) == 2


@pytest.mark.asyncio
async def test_publication_replay_conflict_and_outer_rollback(ledger):
    candidate, _, _ = await setup(ledger)
    p = await prepared(candidate)
    async with session_scope() as session:
        result = await PortfolioPlanService(session, rules=Rules()).publish(*p[:2], candidate, p[2], base=base())
        assert result.published
        await session.rollback()
    async with session_scope() as session:
        assert await PortfolioPlanService(session).current(candidate.account_id) is None
        assert (await session.get(ToolAction, str(p[1]))).state == "prepared"
    result = await publish(candidate, p)
    assert await publish(candidate, p) == result
    different = candidate.model_copy(update={"question_scope": "Changed exact body"})
    with pytest.raises(InvestorError, match="parameters differ"):
        await publish(different, p)


@pytest.mark.asyncio
async def test_partial_actual_trade_immediate_review_and_durable_bounded_dispatch(ledger):
    candidate, ref, _ = await setup(ledger)
    result = await publish(candidate)
    await append(candidate.account_id, trade(ref, occurred_at=datetime.now(UTC) - timedelta(hours=1), direction="sell", quantity="3"), 1)
    async with session_scope() as session:
        svc = PortfolioPlanService(session, rules=Rules())
        view = await svc.get(result.plan_ref)
        assert view.status == "review_required" and "actual_snapshot_changed" in view.limitation_reasons
        assert view.plan.snapshot_ref == candidate.snapshot_ref
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 2
        await maintain_portfolio_plans(session, base(), limit=10)
    async with session_scope() as session:
        await maintain_portfolio_plans(session, base(), limit=10)
        rows = list(await session.scalars(select(PortfolioPlanEvent).where(PortfolioPlanEvent.plan_id == str(result.plan_ref.id))))
        assert len(rows) == 2
        outboxes = list(await session.scalars(select(PortfolioPlanOutbox).where(PortfolioPlanOutbox.plan_id == str(result.plan_ref.id))))
        assert any(o.payload.get("blocked") == "background_disabled" for o in outboxes)
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 2


@pytest.mark.asyncio
async def test_decision_exact_option_auth_only_no_ledger_or_reservation(ledger):
    candidate, _, _ = await setup(ledger)
    result = await publish(candidate)
    async with session_scope() as session:
        svc = UserControlService(session)
        parameters = DecisionAction(portfolio_plan_ref=result.plan_ref, option_id=candidate.options[0].id, action_id=candidate.options[0].actions[0].id, decision="accept", user_text="Accept as a proposal")
        decision = await svc.execute(ExecuteRequest(idempotency_key=uuid4(), parameters=parameters), authenticated_request(parameters))
        assert decision.portfolio_plan_ref == result.plan_ref and decision.option_id == candidate.options[0].id
        assert decision.actor == "user"
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
        current = await PortfolioPlanService(session).current(candidate.account_id)
        assert current.status == "published" and current.plan.acceptance_reserves_cash is False
        with pytest.raises(InvestorError):
            await svc.execute(ExecuteRequest(idempotency_key=uuid4(), parameters=parameters.model_copy(update={"action_id": uuid4()})), authenticated_request(parameters.model_copy(update={"action_id": uuid4()})))


@pytest.mark.asyncio
async def test_authenticated_read_api_default_off_and_no_private_history(ledger):
    candidate, _, _ = await setup(ledger)
    result = await publish(candidate)
    app = create_app(settings=Settings(_env_file=None, api_key="ci-inert-api-key"))
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, api_key="ci-inert-api-key")
    app.dependency_overrides[get_bootstrap_settings] = app.dependency_overrides[get_settings]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/portfolio/plans/{result.plan_ref.id}?account_id={candidate.account_id}"
        assert (await client.get(path)).status_code == 401
        response = await client.get(path, headers={"X-API-Key": "ci-inert-api-key"})
        assert response.status_code == 200, response.text
        assert response.json()["plan"]["snapshot_ref"] == candidate.snapshot_ref.model_dump(mode="json")
        assert "prompt" not in response.text


@pytest.mark.asyncio
async def test_relevant_mandate_change_immediate_but_explanation_budget_not_invalidation(ledger):
    candidate, _, _ = await setup(ledger)
    result = await publish(candidate)
    async with session_scope() as session:
        foundation = InvestorService(session)
        old = await foundation.get_mandate("local_owner")
        changed = old.model_copy(update={"revision": old.revision + 1, "unknowns": ("User explanation preference only",)})
        await foundation.revise_mandate("local_owner", changed, Mutation(expected_revision=old.revision, idempotency_key=uuid4()))
        assert (await PortfolioPlanService(session).get(result.plan_ref)).status == "published"
    async with session_scope() as session:
        foundation = InvestorService(session)
        old = await foundation.get_mandate("local_owner")
        changed = old.model_copy(update={"revision": old.revision + 1, "portfolio_constraints": old.portfolio_constraints.model_copy(update={"max_single_weight": D("0.2")})})
        await foundation.revise_mandate("local_owner", changed, Mutation(expected_revision=old.revision, idempotency_key=uuid4()))
        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert view.status == "review_required" and "mandate_changed" in view.limitation_reasons


@pytest.mark.asyncio
async def test_native_conversation_grant_tools_completion_archived_holding_offline(ledger):
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
    from investment_agent.investor.controls import CaseAction
    from investment_agent.investor.foundations import CreateCase
    from investment_agent.portfolio.plan_contracts import PlanSemanticInput
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.storage.models.agent import ExecutionGrantRow

    candidate, ref, calc = await setup(ledger, movement=False, trusted=False)
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
            return ModelResponse([ToolCallPart("calculate_portfolio_options", {"business": business}, tool_call_id="calc")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        returned = next(p for p in returns if p.tool_name == "calculate_portfolio_options")
        built = candidate.model_copy(update={"calculation_ref": ObjectRef.model_validate(returned.content["calculation_ref"])})
        if "prepare_portfolio_plan" not in names:
            return ModelResponse([ToolCallPart("prepare_portfolio_plan", {"business": built.model_dump(mode="json")}, tool_call_id="prepare")], usage=RequestUsage(input_tokens=20, output_tokens=10))
        prepared_return = next(p for p in returns if p.tool_name == "prepare_portfolio_plan")
        review = semantic(built)
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
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
    assert observed["calls"] >= 5
    assert "publish_portfolio_plan" not in observed["tools"] and "prepare_company_view" not in observed["tools"]


async def authorize_background(ref):
    async with session_scope() as session:
        foundation = InvestorService(session)
        m = await foundation.get_mandate("local_owner")
        auth = m.authorization.model_copy(update={"background_enabled": True, "exploration_scope_refs": (ref,)})
        return await foundation.revise_mandate("local_owner", m.model_copy(update={"revision": m.revision + 1, "authorization": auth}), Mutation(expected_revision=m.revision, idempotency_key=uuid4()))


@pytest.mark.asyncio
async def test_background_archive_pause_denied_interactive_risk_admitted(ledger):
    from investment_agent.agent.execution import live_policy
    from investment_agent.investor.controls import CaseAction
    from investment_agent.investor.foundations import CreateCase

    candidate, ref, _ = await setup(ledger, movement=False, trusted=False)
    await authorize_background(ref)
    async with session_scope() as session:
        foundation = InvestorService(session)
        case = await foundation.create_case("local_owner", CreateCase(listing_ref=ref, question="Portfolio held case", idempotency_key=uuid4()))
        action_ = CaseAction(case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision), action="pause", reason="User pauses background")
        await UserControlService(session).execute(ExecuteRequest(parameters=action_, idempotency_key=uuid4()), authenticated_request(action_))
        grant = ExecutionGrant(mode="background", source="authorized_background", prompt="Risk", scope_refs=(ref, candidate.snapshot_ref), mandate_id=candidate.mandate_ref.id, research_kind="portfolio_action", portfolio_account_id=candidate.account_id, portfolio_snapshot_ref=candidate.snapshot_ref)
        with pytest.raises(ExecutionDeniedError, match="background_paused"):
            await live_policy(session, grant, base(investor_agent_background_enabled=True))
        interactive = grant.model_copy(update={"mode": "interactive", "source": "authenticated_message"})
        await live_policy(session, interactive, base())
        current = await foundation.get_case("local_owner", case.id)
        action_ = action_.model_copy(update={"case_ref": ObjectRef(kind="case", id=case.id, revision=current.revision), "action": "archive"})
        await UserControlService(session).execute(ExecuteRequest(parameters=action_, idempotency_key=uuid4()), authenticated_request(action_))
        with pytest.raises(ExecutionDeniedError, match="scope_archived"):
            await live_policy(session, grant, base(investor_agent_background_enabled=True))
        await live_policy(session, interactive, base())


@pytest.mark.asyncio
async def test_one_background_review_task_per_plan_no_paid_retry_reset_on_more_actuals(ledger):
    from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow

    candidate, ref, _ = await setup(ledger)
    result = await publish(candidate)
    await authorize_background(ref)
    await append(candidate.account_id, trade(ref, direction="sell", quantity="3", occurred_at=datetime.now(UTC) - timedelta(minutes=1)), 1)
    selected = base(investor_agent_background_enabled=True)
    async with session_scope() as session:
        await maintain_portfolio_plans(session, selected, limit=50)
        outbox = await session.scalar(select(PortfolioPlanOutbox).where(PortfolioPlanOutbox.plan_id == str(result.plan_ref.id), PortfolioPlanOutbox.payload["task_id"].as_string().is_not(None)))
        assert outbox and outbox.payload["attempted"]
        task_id = outbox.payload["task_id"]
        grant = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, task_id)).payload)
        assert grant.purpose == "review" and grant.portfolio_snapshot_ref != candidate.snapshot_ref
    await append(candidate.account_id, trade(ref, direction="sell", quantity="2", occurred_at=datetime.now(UTC) - timedelta(seconds=30)), 2)
    async with session_scope() as session:
        await maintain_portfolio_plans(session, selected, limit=50)
        await maintain_portfolio_plans(session, selected, limit=50)
        assert await session.scalar(select(func.count()).select_from(AgentTask)) == 2  # Original publication task + one review.
        events = list(await session.scalars(select(PortfolioPlanEvent).where(PortfolioPlanEvent.plan_id == str(result.plan_ref.id))))
        assert len(events) == 3
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 3


@pytest.mark.asyncio
async def test_actual_price_date_freshness_not_valuation_clock(ledger, monkeypatch):
    import investment_agent.portfolio.plans as module

    candidate, _, calc = await setup(ledger)
    async with session_scope() as session:
        # Today snapshot valuation does not magically refresh yesterday's trade_date.
        from investment_agent.portfolio.plans import price_limits
        price_date = datetime.fromisoformat(calc.details["sources"][0]["price_date"]).replace(tzinfo=UTC)
        assert price_limits(calc, price_date + timedelta(days=7), 7) == ()
        assert price_limits(calc, price_date + timedelta(days=8), 7)
    result = await publish(candidate)
    now = datetime.now(UTC)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(days=8)
    monkeypatch.setattr(module, "datetime", Later)
    async with session_scope() as session:
        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert view.status == "review_required" and any(r.startswith("price_basis_stale:") for r in view.limitation_reasons)


async def company_view(candidate, ref):
    from investment_agent.investor.contracts import (
        DIMENSIONS,
        CompanyRecommendation,
        DimensionAssessment,
    )
    from investment_agent.investor.foundations import CreateCase
    from investment_agent.investor.publication import FIXED_METHOD, PublicationService

    async with session_scope() as session:
        foundation = InvestorService(session)
        case = await foundation.create_case("local_owner", CreateCase(listing_ref=ref, question="Existing valid company view fixture", idempotency_key=uuid4()))
        now = datetime.now(UTC)
        recommendation = CompanyRecommendation(id=uuid4(), revision=1, created_at=now, actor="research_service", origin_ref=None, scope="company_view", case_ids=(case.id,), thesis_revision_refs=(ObjectRef(kind="thesis", id=uuid4(), revision=1),), as_of=now - timedelta(hours=1), question_scope="Fixture bounded view", assessed_dimensions=tuple(DimensionAssessment(dimension=d, status="missing", applicable=True, reason="Fixture limited evidence", refs=()) for d in DIMENSIONS), sufficiency="bounded", claims=(), counter_case=(), assumptions=(), gaps=("Business evidence remains limited",), conditions=(), valid_until=now + timedelta(days=14), next_review_at=now + timedelta(days=7), monitor_refs=(), dependency_refs=(), mandate_revision_id=candidate.mandate_ref, method_revision_id=FIXED_METHOD, provenance=(), business_view="undetermined", price_view="undetermined", conditional_actions=())
        view_ref = PublicationService(session)._save(case.id, "recommendation", recommendation)
        row = await foundation.repository.case("local_owner", case.id)
        updated = case.model_copy(update={"revision": case.revision + 1, "current_recommendation_ref": view_ref})
        assert await foundation.repository.advance_case(row, case.revision, updated.model_dump(mode="json"))
        return view_ref


@pytest.mark.asyncio
async def test_company_exact_current_view_and_immediate_restriction(ledger):
    from investment_agent.investor.publication import PublicationService

    candidate, ref, _ = await setup(ledger, movement=False, trusted=False)
    view_ref = await company_view(candidate, ref)
    action_ = candidate.options[0].actions[0].model_copy(update={"basis": "company_thesis", "constraint": None, "stance": "hold", "company_view_refs": (view_ref,)})
    candidate = candidate.model_copy(update={"options": (candidate.options[0].model_copy(update={"actions": (action_,)}),)})
    result = await publish(candidate, trusted=False)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        assert (await PortfolioPlanService(session).get(result.plan_ref)).status == "published"
        await PublicationService(session).restrict(view_ref, reason="Critical evidence changed")
        view = await PortfolioPlanService(session).get(result.plan_ref)
        assert view.status == "review_required" and any(r.startswith("company_view_unavailable:") for r in view.limitation_reasons)
    replacement = candidate.model_copy(update={"previous_ref": result.plan_ref})
    result = await publish(replacement, trusted=False)
    assert not result.published


@pytest.mark.asyncio
async def test_company_wrong_listing_and_unknown_absence_not_zero(ledger):
    candidate, ref, _ = await setup(ledger, movement=False, trusted=False, coverage="partial", quantity="0")
    view_ref = await company_view(candidate, ref)
    option = candidate.options[0]
    action_ = option.actions[0].model_copy(update={"basis": "company_thesis", "constraint": None, "stance": "hold", "company_view_refs": (view_ref,)})
    candidate = candidate.model_copy(update={"options": (option.model_copy(update={"actions": (action_,)}),)})
    result = await publish(candidate, trusted=False)
    assert not result.published  # A confirmed zero is never an actual positive holding.
    async with session_scope() as session:
        from investment_agent.storage.models.reference import Company, Listing
        company = Company(canonical_name="Other listing")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SH", ticker="600001.SH")
        session.add(listing)
        await session.flush()
        other = ObjectRef(kind="listing", id=listing.id, revision=1)
        action_ = action_.model_copy(update={"listing_ref": other, "stance": "buy"})
        with pytest.raises(InvestorError, match="another listing"):
            await PortfolioPlanService(session)._company(action_)
        changed = candidate.model_copy(update={"options": (option.model_copy(update={"actions": (action_,)}),)})
        check, _ = await PortfolioPlanService(session).check(changed, semantic(changed), as_of=datetime.now(UTC), task_id=uuid4())
        assert any("unknown absence is not zero" in error for error in check.field_errors.values())


@pytest.mark.asyncio
async def test_unused_alternative_price_does_not_limit_selected_whole_plan(ledger):
    from investment_agent.portfolio.plans import price_limits

    _, _, record = await setup(ledger)
    record = record.model_copy(deep=True)
    other = ObjectRef(kind="listing", id=999, revision=1).model_dump(mode="json")
    record.details["scenarios"].append({"actions": [{"listing_ref": other}]})
    record.details["sources"].append({"listing_ref": other, "price_date": "2020-01-01"})
    assert not price_limits(record, datetime.now(UTC), 7, {1})
    assert price_limits(record, datetime.now(UTC), 7, {2}) == ("price_basis_stale:999",)


@pytest.mark.asyncio
async def test_public_portfolio_failure_artifacts_exact_owner_account_kind(ledger):
    candidate, _, _ = await setup(ledger, fee=None)
    result = await publish(candidate)
    assert not result.published and result.draft_ref and result.check_ref
    app = create_app(settings=Settings(_env_file=None, api_key="ci-inert-api-key"))
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, api_key="ci-inert-api-key")
    app.dependency_overrides[get_bootstrap_settings] = app.dependency_overrides[get_settings]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-API-Key": "ci-inert-api-key"}
        for ref in (result.draft_ref, result.check_ref):
            path = f"/portfolio/artifacts/{ref.kind}/{ref.id}?account_id={candidate.account_id}"
            assert (await client.get(path)).status_code == 401
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, response.text
            assert "prompt" not in response.text and "api_key" not in response.text
            assert (await client.get(path.replace(str(candidate.account_id), str(uuid4())), headers=headers)).status_code == 404
            wrong = "review" if ref.kind != "review" else "draft"
            assert (await client.get(path.replace(f"/{ref.kind}/", f"/{wrong}/"), headers=headers)).status_code == 404
        assert response.json()["field_errors"]
        forbidden = f"/portfolio/artifacts/preparation/{result.check_ref.id}?account_id={candidate.account_id}"
        assert (await client.get(forbidden, headers=headers)).status_code == 422
    async with session_scope() as session:
        foreign = PortfolioPlanService(session, owner="other_owner")
        with pytest.raises(InvestorError):
            await foreign.artifact(result.check_ref)


@pytest.mark.asyncio
async def test_native_portfolio_answer_without_published_plan_remains_partial(ledger):
    from pydantic_ai.messages import ModelResponse, TextPart
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
    from investment_agent.research.worker import ResearchWorker

    candidate, _, _ = await setup(ledger, movement=False, trusted=False)
    selected = base(agent_api_key="fixture-offline-key", agent_base_url="https://fixture.invalid/v1", agent_model="fixture", agent_max_tokens=100)
    async with session_scope() as session:
        conversation = await ConversationService(session, selected).create(ConversationCreate(title="Portfolio deliverable required"))
        receipt = await ConversationService(session, selected).submit(conversation.id, MessageInput(client_message_id=uuid4(), text="Review the confirmed account", context_refs=(candidate.snapshot_ref,), research_kind="portfolio_action"))
    progress = ResearchProgress(question="Review the confirmed account", kind="portfolio_action", answered=True, content=(TextNode(type="text",text="Framework prose is not a published portfolio plan."),))
    output = ResearchOutput(progress=progress)
    model = FunctionModel(lambda messages,info: ModelResponse([TextPart(output.model_dump_json())], usage=RequestUsage(input_tokens=20,output_tokens=10)))
    executor = AgentExecutor(selected, model=model, registrations=registrations(), specification=ResearchPolicy(selected).specification())
    worker = ResearchWorker(settings=selected,worker_id="ag24-deliverable",handlers=[AgentJobHandler("agent_turn",executor)])
    await worker.run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        assert task.status == "partial" and task.stop_reason == "evidence_blocked"
        assert await PortfolioPlanService(session).current(candidate.account_id) is None
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
