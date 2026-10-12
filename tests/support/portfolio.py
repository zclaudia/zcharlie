"""Portfolio ledger/calculation/plan fixtures and constructors (moved from
tests/test_portfolio_ledger.py, tests/test_investor_foundations.py,
tests/test_portfolio_calculations.py and tests/test_portfolio_plans.py)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.tasks import TaskService, fingerprint
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import PortfolioConstraints, TextNode
from investment_agent.investor.foundations import InvestorService
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    Scenario,
    ScenarioAction,
    ScenarioConstraints,
    TradingRule,
)
from investment_agent.portfolio.calculations import PortfolioCalculationService, TrustedTradingRule
from investment_agent.portfolio.contracts import (
    AppendEvent,
    Balance,
    CreateAccount,
    Holding,
    Trade,
)
from investment_agent.portfolio.plan_contracts import (
    OptionInput,
    PlanInput,
    PlanSemanticReview,
    PortfolioAction,
)
from investment_agent.portfolio.plans import PortfolioPlanService
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.support.investor import mandate

T = datetime(2025, 1, 1, tzinfo=UTC)
D = Decimal


# -- ledger (tests/test_portfolio_ledger.py) --------------------------------- #


@pytest.fixture
async def ledger(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'ledger.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            session.add(Market(code="SH", name="Fixture", timezone="UTC", currency="CNY"))
            company = Company(canonical_name="Fixture")
            session.add(company)
            await session.flush()
            listing = Listing(company_id=company.id, market_code="SH", ticker="600000.SH")
            session.add(listing)
            await session.flush()
            ref = ObjectRef(kind="listing", id=listing.id, revision=1)
            session.add(MarketSnapshot(listing_id=listing.id, trade_date=T.date(), open=D(10), high=D(10), low=D(10), close=D(10), volume=D(1), adjust="none", provider="fixture", captured_at=T, version=1))
            account = await PortfolioService(session).create_account(CreateAccount(idempotency_key=uuid4(), name="Actual confirmed account"))
        yield account.id, ref


def balance(ref, *, occurred_at=T, kind="opening_balance", coverage="complete", cash="1000", quantity="10"):
    return Balance(kind=kind, occurred_at=occurred_at, source="user statement", user_confirmed=True, coverage=coverage, asset_scope="This explicit brokerage account", holdings=(Holding(listing_ref=ref, quantity=D(quantity)),), cash=D(cash) if cash is not None else None, prior_history="included_in_confirmed_balance", unexplained_difference="unexplained: 12 CNY; no synthetic transfer" if kind == "reconciliation" else None)


def trade(ref, *, occurred_at=T + timedelta(days=1), fee="1", direction="buy", quantity="3"):
    return Trade(kind="trade", occurred_at=occurred_at, source="actual execution statement", user_confirmed=True, holding=Holding(listing_ref=ref, quantity=D(quantity)), direction=direction, actual_price=D(10), fee=D(fee) if fee is not None else None)


async def append(account, fact, revision, **kw):
    request = AppendEvent(account_id=account, expected_revision=revision, idempotency_key=kw.pop("idempotency_key", uuid4()), fact=fact, **kw)
    async with session_scope() as session:
        return await PortfolioService(session).append(request)


# -- calculations (tests/test_portfolio_calculations.py) --------------------- #


def rule(ref, **updates):
    return TradingRule(listing_ref=ref, source="explicit fixture assumption; no exchange certification", version="fixture-v1", nature="user_scenario", buy_minimum=D(1), buy_increment=D(1), sell_minimum=D(1), sell_increment=D(1), full_odd_lot_sell=True).model_copy(update=updates)


def action(ref, **updates):
    return ScenarioAction(listing_ref=ref, direction="buy", quantity=D(60), execution_price=D(10), fixed_fee=D(1), fee_rate=D(0), assumption_source="user_scenario fixture").model_copy(update=updates)


def request(snapshot, ref, **updates):
    return PortfolioCalculationRequest(operation_id=uuid4(), account_id=snapshot.account_id, snapshot_ref=snapshot.reference, mandate_ref=ObjectRef(kind="mandate", id=uuid4(), revision=1), constraints=ScenarioConstraints(nature="agent_scenario", source="fixture only", max_single_weight=D("0.5"), max_sector_weight=D("0.5"), minimum_cash=D(0)), rules=(rule(ref),), scenarios=(Scenario(name="buy", actions=(action(ref),)),)).model_copy(update=updates)


# -- plans (tests/test_portfolio_plans.py) ----------------------------------- #


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
