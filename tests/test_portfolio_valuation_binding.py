"""Plans bind the daily valuation explicitly (watch-holding-plan 2.5 / 7.2 WP3).

The v2 calculation contract freezes the bound valuation's prices and knowledge
cutoff instead of the snapshot's older frozen prices; the plan carries the same
``valuation_ref``; a newer valuation only marks limits on reads - it never
silently re-prices the old plan.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
from investment_agent.agent.portfolio_tools import _require_usable_valuation
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import PortfolioConstraints, TextNode
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.portfolio.calculation_contracts import PortfolioCalculationRequest, Scenario
from investment_agent.portfolio.calculations import PortfolioCalculationService
from investment_agent.portfolio.plan_contracts import OptionInput, PlanInput, PortfolioAction
from investment_agent.portfolio.plans import PortfolioPlanService
from investment_agent.portfolio.valuation import ValuationService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from tests.test_investor_foundations import mandate
from tests.test_portfolio_calculations import action
from tests.test_portfolio_ledger import append, balance
from tests.test_portfolio_ledger import ledger as ledger_fixture
from tests.test_portfolio_plans import Rules, base, publish

# Anchored near the real clock: publication-price staleness is measured
# against "now", so fixtures must not sit a year in the past.
T0 = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=3)
DAY = T0.date()
ledger = ledger_fixture


async def _seed(ledger, *, later_close="12"):
    """Opening 100 shares with a frozen price of 10, plus a later valuation at 12."""

    account, ref = ledger
    snapshot = (await append(account, balance(ref, cash="1000", quantity="100", occurred_at=T0), 0)).snapshot
    async with session_scope() as session:
        later = T0 + timedelta(days=1)
        session.add(MarketSnapshot(listing_id=ref.id, trade_date=later.date(), open=Decimal(later_close), high=Decimal(later_close), low=Decimal(later_close), close=Decimal(later_close), volume=Decimal(1), adjust="none", provider="fixture", captured_at=later, version=1))
        session.add(IndustryClassification(listing_id=ref.id, system="CSRC", code="fixture-sector", name="Fixture", valid_from=later.date(), source="fixture", captured_at=later))
        m = await InvestorService(session).create_mandate("local_owner", mandate().model_copy(update={"scope_refs": (ref,), "hard_constraints": (), "portfolio_constraints": PortfolioConstraints(account_id=account, max_single_weight=Decimal("0.3"), max_sector_weight=Decimal("0.6"), minimum_cash=Decimal(0))}), uuid4())
    async with session_scope() as session:
        valuation = await ValuationService(session).ensure(account, valuation_date=DAY + timedelta(days=1), now=T0 + timedelta(days=1), is_trading_day=lambda day: True)
    assert valuation is not None
    assert valuation.holdings[0].price == Decimal(later_close)
    assert Decimal(snapshot.holdings[0].price) == Decimal(10)  # the snapshot's older frozen basis
    return snapshot, ref, valuation, m


def _request(snapshot, ref, m, *, valuation_ref):
    return PortfolioCalculationRequest(
        operation_id=uuid4(),
        account_id=snapshot.account_id,
        snapshot_ref=snapshot.reference,
        valuation_ref=valuation_ref,
        mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=m.revision),
        scenarios=(Scenario(name="reduce_risk", actions=(action(ref, direction="sell", quantity=Decimal(10), fixed_fee=Decimal(1)),)),),
    )


def _candidate(snapshot, ref, calc, *, valuation_ref):
    a = PortfolioAction(
        id=uuid4(),
        listing_ref=ref,
        stance="reduce",
        basis="portfolio_constraint",
        constraint="max_single_weight",
        research_gap="Company durability has not been researched",
        target_ref=ObjectRef(kind="calculation", id=calc.id, revision=1, field_path="results.scenario_1_action_0_quantity"),
        content=(TextNode(type="text", text="Reduce confirmed concentration"),),
    )
    return PlanInput(
        account_id=snapshot.account_id,
        snapshot_ref=snapshot.reference,
        valuation_ref=valuation_ref,
        mandate_ref=ObjectRef.model_validate(calc.details["request"]["mandate_ref"]),
        calculation_ref=ObjectRef(kind="calculation", id=calc.id, revision=1),
        previous_ref=None,
        options=(OptionInput(id=uuid4(), scenario_index=1, actions=(a,)),),
        question_scope="Whole confirmed account alternatives",
    )


@pytest.mark.asyncio
async def test_valuation_bound_calculation_freezes_the_valuation_price_basis(ledger):
    snapshot, ref, valuation, m = await _seed(ledger)
    req = _request(snapshot, ref, m, valuation_ref=valuation.reference)
    async with session_scope() as session:
        calc = await PortfolioCalculationService(session, rules=Rules()).save(req)

    assert calc.method_version == "ag22-v2"
    assert valuation.reference in calc.fact_refs
    assert calc.as_of == valuation.knowledge_cutoff
    assert calc.details["valuation"]["ref"]["id"] == str(valuation.id)
    assert calc.details["valuation"]["knowledge_cutoff"] == valuation.knowledge_cutoff.isoformat()
    # The frozen snapshot (price 10) stays untouched; the calculation is priced
    # on the valuation (12) and says so.
    assert Decimal(calc.details["snapshot"]["holdings"][0]["price"]) == Decimal(10)
    assert Decimal(calc.details["sources"][0]["price"]) == Decimal(12)
    assert calc.details["sources"][0]["price_date"] == (DAY + timedelta(days=1)).isoformat()
    assert Decimal(calc.details["scenarios"][0]["nav"]) == Decimal(2200)
    assert calc.details["price_basis_at"] == valuation.knowledge_cutoff.isoformat()


@pytest.mark.asyncio
async def test_plan_binds_the_same_valuation_and_marks_a_newer_one(ledger):
    snapshot, ref, valuation, m = await _seed(ledger)
    req = _request(snapshot, ref, m, valuation_ref=valuation.reference)
    async with session_scope() as session:
        calc = await PortfolioCalculationService(session, rules=Rules()).save(req)
    candidate = _candidate(snapshot, ref, calc, valuation_ref=valuation.reference)

    result = await publish(candidate)
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        view = await PortfolioPlanService(session, rules=Rules()).get(result.plan_ref)
        assert view.status == "published"
        assert view.plan.valuation_ref == valuation.reference
        assert view.plan.price_basis_at == valuation.knowledge_cutoff

    # A newer trading day's price lands: the plan keeps its own frozen
    # reference and is marked limited, never silently re-priced.
    async with session_scope() as session:
        later = T0 + timedelta(days=2)
        session.add(MarketSnapshot(listing_id=ref.id, trade_date=later.date(), open=Decimal(13), high=Decimal(13), low=Decimal(13), close=Decimal(13), volume=Decimal(1), adjust="none", provider="fixture", captured_at=later, version=1))
    async with session_scope() as session:
        newer = await ValuationService(session).ensure(snapshot.account_id, valuation_date=DAY + timedelta(days=2), now=T0 + timedelta(days=2), is_trading_day=lambda day: True)
    assert newer is not None and newer.reference != valuation.reference
    async with session_scope() as session:
        view = await PortfolioPlanService(session, rules=Rules()).get(result.plan_ref)
        assert view.status == "review_required"
        assert "valuation_updated" in view.limitation_reasons
        assert view.plan.valuation_ref == valuation.reference
        # The plan's frozen totals are unchanged.
        assert view.plan.options[0].totals == {p["field_path"][8:]: calc.results[p["field_path"][8:]] for p in calc.details["result_paths"] if p["scenario_index"] == 1}


@pytest.mark.asyncio
async def test_calculation_rejects_a_valuation_of_another_ledger_version(ledger):
    snapshot, ref, valuation, m = await _seed(ledger)
    from tests.test_portfolio_ledger import trade

    moved = (await append(snapshot.account_id, trade(ref), 1)).snapshot
    assert moved.revision == 2
    req = _request(moved, ref, m, valuation_ref=valuation.reference)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="估值不属于该事实版本"):
            await PortfolioCalculationService(session, rules=Rules()).save(req)


@pytest.mark.asyncio
async def test_legacy_contract_stays_v1_and_rejects_a_valuation_claim(ledger):
    snapshot, ref, valuation, m = await _seed(ledger)
    req = _request(snapshot, ref, m, valuation_ref=None)
    async with session_scope() as session:
        calc = await PortfolioCalculationService(session, rules=Rules()).save(req)
    assert calc.method_version == "ag22-v1"
    assert calc.as_of == snapshot.valuation_at
    assert "valuation" not in calc.details
    assert Decimal(calc.details["sources"][0]["price"]) == Decimal(10)  # the snapshot's frozen basis

    candidate = _candidate(snapshot, ref, calc, valuation_ref=valuation.reference)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="Plan valuation differs"):
            await PortfolioPlanService(session, rules=Rules()).verified_calculation(candidate)


@pytest.mark.asyncio
async def test_publication_requires_the_calculations_exact_valuation(ledger):
    snapshot, ref, valuation, m = await _seed(ledger)
    req = _request(snapshot, ref, m, valuation_ref=valuation.reference)
    async with session_scope() as session:
        calc = await PortfolioCalculationService(session, rules=Rules()).save(req)
    # Another valuation of the same ledger version, but a different basis
    # identity (older date, stale price): publication must refuse the swap
    # instead of publishing numbers priced on something else.
    async with session_scope() as session:
        other = await ValuationService(session).ensure(snapshot.account_id, valuation_date=DAY, now=T0, is_trading_day=lambda day: True)
    assert other is not None and other.reference != valuation.reference

    candidate = _candidate(snapshot, ref, calc, valuation_ref=other.reference)
    result = await publish(candidate)
    assert not result.published
    assert "calculation_ref" in result.check.field_errors
    assert "Plan valuation differs" in result.check.field_errors["calculation_ref"]


@pytest.mark.asyncio
async def test_calculate_tool_requires_the_current_or_pit_usable_valuation(ledger):
    snapshot, ref, valuation, m = await _seed(ledger)
    req = _request(snapshot, ref, m, valuation_ref=valuation.reference)

    async def granted_operation(*, current_research: bool, as_of: datetime) -> tuple[UUID, ExecutionGrant]:
        async with session_scope() as session:
            task = await admit(
                session,
                base=base(),
                input_key=str(uuid4()),
                binding=ContextBinding(context_refs=(snapshot.reference,), base_refs=(snapshot.reference,), as_of=as_of),
                grant=ExecutionGrant(
                    mode="interactive",
                    source="authenticated_message",
                    prompt="Review the whole account",
                    tool_names=("calculate_portfolio_options",),
                    scope_refs=(snapshot.reference,),
                    mandate_id=m.id,
                    research_kind="portfolio_action",
                    portfolio_account_id=snapshot.account_id,
                    portfolio_snapshot_ref=snapshot.reference,
                    current_research=current_research,
                ),
            )
            operation = uuid4()
            session.add(
                ToolAction(
                    operation_id=str(operation),
                    task_id=str(task.id),
                    intent_key="calc",
                    request_hash="fixture",
                    parameters={},
                    revision=1,
                    effect="write",
                    state="committed",
                )
            )
            from investment_agent.storage.models.agent import ExecutionGrantRow

            row = await session.get(ExecutionGrantRow, str(task.id))
            assert row is not None
            return operation, ExecutionGrant.model_validate(row.payload)

    operation, grant = await granted_operation(current_research=True, as_of=datetime.now(UTC))
    async with session_scope() as session:
        await _require_usable_valuation(session, operation, grant, req)  # current: passes

    # A newer valuation invalidates the binding for live research: the agent
    # must refresh the context instead of consuming the older prices.
    async with session_scope() as session:
        later = T0 + timedelta(days=2)
        session.add(MarketSnapshot(listing_id=ref.id, trade_date=later.date(), open=Decimal(13), high=Decimal(13), low=Decimal(13), close=Decimal(13), volume=Decimal(1), adjust="none", provider="fixture", captured_at=later, version=1))
    async with session_scope() as session:
        await ValuationService(session).ensure(snapshot.account_id, valuation_date=DAY + timedelta(days=2), now=T0 + timedelta(days=2), is_trading_day=lambda day: True)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="valuation_updated_reload_portfolio_context"):
            await _require_usable_valuation(session, operation, grant, req)

    # A historical task cannot bind knowledge after its research cutoff.
    operation_past, grant_past = await granted_operation(current_research=False, as_of=T0)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="valuation_after_research_cutoff"):
            await _require_usable_valuation(session, operation_past, grant_past, req)
