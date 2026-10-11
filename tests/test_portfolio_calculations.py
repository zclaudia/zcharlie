"""S11/S12 alternative accounting, explicit unknowns and authenticated storage."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from investment_agent.api.app import create_app
from investment_agent.api.auth import get_settings
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    PriceSelection,
    Scenario,
    ScenarioAction,
    ScenarioConstraints,
    TradingRule,
)
from investment_agent.portfolio.calculations import PortfolioCalculationService, calculate
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioEvent
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.test_investor_foundations import mandate
from tests.test_portfolio_ledger import T, append, balance
from tests.test_portfolio_ledger import ledger as ledger_fixture

ledger = ledger_fixture

D = Decimal


def rule(ref, **updates):
    return TradingRule(listing_ref=ref, source="explicit fixture assumption; no exchange certification", version="fixture-v1", nature="user_scenario", buy_minimum=D(1), buy_increment=D(1), sell_minimum=D(1), sell_increment=D(1), full_odd_lot_sell=True).model_copy(update=updates)


def action(ref, **updates):
    return ScenarioAction(listing_ref=ref, direction="buy", quantity=D(60), execution_price=D(10), fixed_fee=D(1), fee_rate=D(0), assumption_source="user_scenario fixture").model_copy(update=updates)


def request(snapshot, ref, **updates):
    return PortfolioCalculationRequest(operation_id=uuid4(), account_id=snapshot.account_id, snapshot_ref=snapshot.reference, mandate_ref=ObjectRef(kind="mandate", id=uuid4(), revision=1), constraints=ScenarioConstraints(nature="agent_scenario", source="fixture only", max_single_weight=D("0.5"), max_sector_weight=D("0.5"), minimum_cash=D(0)), rules=(rule(ref),), scenarios=(Scenario(name="buy", actions=(action(ref),)),)).model_copy(update=updates)


async def setup(ledger):
    account, ref = ledger
    first = await append(account, balance(ref), 0)
    return first.snapshot, ref


def run(snapshot, req, ref):
    return calculate(snapshot, req, {ref: D(10)}, {ref: "sector"})["scenarios"]


@pytest.mark.asyncio
async def test_shared_cash_alternatives_and_fee_denominator(ledger):
    snapshot, ref = await setup(ledger)
    req = request(snapshot, ref, scenarios=(Scenario(name="shared", actions=(action(ref), action(ref))), Scenario(name="exclusive", actions=(action(ref),))))
    baseline, shared, exclusive = run(snapshot, req, ref)
    assert baseline["cash"] == "1000" and baseline["nav"] == "1100"
    assert shared["cash"] == "-202" and shared["nav"] == "1098"
    assert "cash_shortfall" in shared["violations"]
    assert exclusive["cash"] == "399" and exclusive["nav"] == "1099"
    assert exclusive["fees"] == "1"
    with localcontext() as context:
        context.prec = 40
        assert D(exclusive["holdings"][0]["weight"]) == D(700) / D(1099)
    assert "scenario_constraint_violation:max_single_weight" in exclusive["violations"]
    assert "formal_mandate_constraints_not_structured" in exclusive["unknowns"]


@pytest.mark.asyncio
async def test_unknown_is_not_zero_or_pass(ledger):
    snapshot, ref = await setup(ledger)
    req = request(snapshot, ref, scenarios=(Scenario(name="unknown_fee", actions=(action(ref, fixed_fee=None),)), Scenario(name="oversell", actions=(action(ref, direction="sell", quantity=D(11)),))))
    partial = snapshot.model_copy(update={"coverage": "partial", "total_assets": None})
    baseline, unknown, oversell = run(partial, req, ref)
    assert baseline["cash"] == "1000" and baseline["nav"] is None
    assert baseline["checks"]["cash_nonnegative"] == "pass"
    assert baseline["checks"]["max_single_weight"] == "unknown"
    assert unknown["cash"] is None and unknown["nav"] is None and unknown["fees"] is None
    assert "action.0.fee_missing" in unknown["unknowns"]
    assert "action.0.insufficient_holding" in oversell["violations"]
    assert oversell["unknowns"] and oversell["status"] == "violated"
    missing = run(snapshot.model_copy(update={"known_cash": None, "cash_subtotal": D(9999)}), request(snapshot, ref, rules=()), ref)[1]
    assert missing["cash"] is None and missing["actions"][0]["quantity"] is None
    assert missing["checks"]["cash_nonnegative"] == "unknown"


@pytest.mark.asyncio
async def test_rounding_fees_minimum_increment_and_odd_exit(ledger):
    snapshot, ref = await setup(ledger)
    custom_rule = rule(ref, buy_minimum=D(2), buy_increment=D(3), sell_minimum=D(2), sell_increment=D(3))
    req = request(snapshot, ref, rules=(custom_rule,), scenarios=(Scenario(name="budget", actions=(action(ref, quantity=None, budget=D(102), fixed_fee=D(2), fee_rate=D("0.1")),)), Scenario(name="exit", actions=(action(ref, direction="sell", quantity=D(10)),)), Scenario(name="unit_bad", actions=(action(ref, quantity=D(3)),))))
    _, budget, exit_result, invalid = run(snapshot, req, ref)
    assert budget["actions"][0]["quantity"] == "8"
    assert D(budget["actions"][0]["gross"]) + D(budget["actions"][0]["fee"]) <= D(102)
    assert exit_result["holdings"][0]["quantity"] == "0"
    assert not any("unit" in v for v in exit_result["violations"])
    assert "action.0.trading_unit_violation" in invalid["violations"]
    closed = calculate(snapshot, req.model_copy(update={"scenarios": (req.scenarios[1],)}), {ref: None}, {ref: None})["scenarios"][1]
    assert closed["holdings"][0]["market_value"] == "0" and closed["nav"] == "1099"


@pytest.mark.asyncio
async def test_execution_vs_valuation_and_missing_industry(ledger):
    snapshot, ref = await setup(ledger)
    req = request(snapshot, ref, scenarios=(Scenario(name="spread", actions=(action(ref, quantity=D(1), execution_price=D(12)),)),))
    result = calculate(snapshot, req, {ref: D(10)}, {ref: None})["scenarios"][1]
    assert result["nav"] == "1097" and result["cash"] == "987"
    assert result["sector_weights"] == {} and result["checks"]["max_sector_weight"] == "unknown"
    assert result["checks"]["max_single_weight"] == "pass"


@pytest.mark.asyncio
async def test_saved_replay_source_cutoff_owner_and_no_ledger_effect(ledger):
    snapshot, ref = await setup(ledger)
    async with session_scope() as session:
        saved_mandate = await InvestorService(session).create_mandate("local_owner", mandate(), uuid4())
    req = request(snapshot, ref, mandate_ref=ObjectRef(kind="mandate", id=saved_mandate.id, revision=1))
    async with session_scope() as session:
        record = await PortfolioCalculationService(session).save(req)
        assert record.method_version == "ag22-v1"
        assert record.results["scenario_1_cash"].value == D(399) and record.details["scenarios"][1]["nav"] is None  # stale 2025 quote
        assert any(v.startswith("stale_price") for v in record.details["scenarios"][1]["unknowns"])
    async with session_scope() as session:
        assert await PortfolioCalculationService(session).save(req) == record
        assert await session.scalar(select(func.count()).select_from(PortfolioEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(SavedCalculation)) == 1
        assert await PortfolioCalculationService(session).get(snapshot.account_id, record.id) == record
        with pytest.raises(InvestorError):
            await PortfolioCalculationService(session, "other").get(snapshot.account_id, record.id)
        with pytest.raises(InvestorError):
            await PortfolioCalculationService(session).save(req.model_copy(update={"max_price_age_days": 1}))
        row = await session.get(SavedCalculation, (str(record.id), 1))
        with pytest.raises(ValueError, match="immutable"):
            async with session.begin_nested():
                row.payload = {}  # type: ignore[union-attr]
                await session.flush()
    async with session_scope() as session:
        wrong = req.model_copy(update={"operation_id": uuid4(), "snapshot_ref": snapshot.reference.model_copy(update={"revision": 2})})
        with pytest.raises(InvestorError):
            await PortfolioCalculationService(session).save(wrong)


@pytest.mark.asyncio
async def test_api_auth_disabled_history_and_source_forgery(ledger, monkeypatch):
    monkeypatch.setenv("API_KEY", "fixture-key")
    snapshot, ref = await setup(ledger)
    async with session_scope() as session:
        m = await InvestorService(session).create_mandate("local_owner", mandate(), uuid4())
    req = request(snapshot, ref, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=1))
    settings = Settings(api_key="fixture-key", portfolio_ledger_enabled=False)
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/portfolio/calculations", json=req.model_dump(mode="json"))).status_code == 401
        headers = {"X-API-Key": "fixture-key"}
        assert (await client.post("/portfolio/calculations", headers=headers, json=req.model_dump(mode="json"))).status_code == 403
        settings.portfolio_ledger_enabled = True
        bad = req.model_dump(mode="json")
        bad["prices"] = [{"listing_ref": ref.model_dump(mode="json"), "price_ref": {"kind": "market_snapshot", "id": 1, "revision": 1}, "price": "1"}]
        assert (await client.post("/portfolio/calculations", headers=headers, json=bad)).status_code == 422
        response = await client.post("/portfolio/calculations", headers=headers, json=req.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        settings.portfolio_ledger_enabled = False
        url = f"/portfolio/calculations/{req.operation_id}?account_id={snapshot.account_id}"
        assert (await client.get(url, headers=headers)).status_code == 200
        assert (await client.get(url)).status_code == 401


@pytest.mark.asyncio
async def test_new_listing_server_price_and_pit_industry(ledger):
    snapshot, ref = await setup(ledger)
    async with session_scope() as session:
        m = await InvestorService(session).create_mandate("local_owner", mandate(), uuid4())
        company = Company(canonical_name="New")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SH", ticker="600001.SH")
        session.add(listing)
        await session.flush()
        new_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        p = MarketSnapshot(listing_id=listing.id, trade_date=snapshot.valuation_at.date(), open=D(20), high=D(20), low=D(20), close=D(20), volume=D(1), adjust="none", provider="fixture", captured_at=snapshot.valuation_at - timedelta(seconds=1), version=1)
        session.add(p)
        session.add(IndustryClassification(listing_id=listing.id, system="CSRC", code="A", name="Fixture", valid_from=T.date(), source="fixture", captured_at=snapshot.valuation_at - timedelta(seconds=1)))
        await session.flush()
        price_ref = ObjectRef(kind="market_snapshot", id=p.id, revision=1)
    req = request(snapshot, ref, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=1), rules=(rule(new_ref),), prices=(PriceSelection(listing_ref=new_ref, price_ref=price_ref),), scenarios=(Scenario(name="new", actions=(action(new_ref, quantity=D(1)),)),))
    async with session_scope() as session:
        result = await PortfolioCalculationService(session).save(req)
        new_source = next(s for s in result.details["sources"] if s["listing_ref"] == new_ref.model_dump(mode="json"))
        assert new_source["price"] == "20.000000" and new_source["industry"]["code"] == "A"
    for changes in ({"revision": 2}, {"id": 1}):
        bad = req.model_copy(update={"operation_id": uuid4(), "prices": (PriceSelection(listing_ref=new_ref, price_ref=price_ref.model_copy(update=changes)),)})
        async with session_scope() as session:
            with pytest.raises(InvestorError, match="Price"):
                await PortfolioCalculationService(session).save(bad)
    async with session_scope() as session:
        session.add(Market(code="USD", name="foreign", timezone="UTC", currency="USD"))
        foreign = Listing(company_id=company.id, market_code="USD", ticker="FOREIGN")
        session.add(foreign)
        await session.flush()
        foreign_ref = ObjectRef(kind="listing", id=foreign.id, revision=1)
        bad = req.model_copy(update={"operation_id": uuid4(), "prices": (), "scenarios": (Scenario(name="fx", actions=(action(foreign_ref),)),)})
        with pytest.raises(InvestorError, match="CNY"):
            await PortfolioCalculationService(session).save(bad)


@pytest.mark.asyncio
async def test_snapshot_weight_precision_independent_of_event_count(ledger):
    snapshot, ref = await setup(ledger)
    from investment_agent.portfolio.contracts import CashFact
    from investment_agent.portfolio.service import PortfolioService
    from tests.test_portfolio_ledger import balance as make_balance
    # Exercise real projection under the event-count monetary precision context.
    async with session_scope() as session:
        records = list((await PortfolioService(session).events(snapshot.account_id)).items)
        opening = records[0].model_copy(update={"request": records[0].request.model_copy(update={"fact": make_balance(ref, cash="200")})})
        records = [opening]
        for index in range(1, 12):
            records.append(opening.model_copy(update={"id": uuid4(), "revision": index + 1, "request": opening.request.model_copy(update={"fact": CashFact(kind="cash_transfer", occurred_at=T + timedelta(seconds=index), currency="CNY", source="fixture", user_confirmed=True, amount=D(0))})}))
        projected = await PortfolioService(session)._project(snapshot.account_id, records, datetime.now(UTC))
        assert len(str(projected.snapshot.holdings[0].weight)) <= 42
        with localcontext() as context:
            context.prec = 40
            assert projected.snapshot.holdings[0].weight == D(1) / D(3)


def test_wire_rejects_floats_fake_rules_and_unknown_currency():
    with pytest.raises(ValidationError):
        ScenarioConstraints(nature="user_scenario", source="fixture", max_single_weight=0.5)
    with pytest.raises(ValidationError):
        TradingRule.model_validate({"nature": "verified_exchange_rule"})


@pytest.mark.asyncio
async def test_typed_mandate_authority_and_verified_rules_are_separate(ledger):
    from investment_agent.investor.common import Mutation
    from investment_agent.investor.contracts import PortfolioConstraints
    from investment_agent.portfolio.calculations import TrustedTradingRule
    snapshot, ref = await setup(ledger)
    formal = PortfolioConstraints(account_id=snapshot.account_id, max_single_weight=D("0.1"), minimum_cash=D(900))
    async with session_scope() as session:
        m = await InvestorService(session).create_mandate("local_owner", mandate().model_copy(update={"portfolio_constraints": formal}), uuid4())
        assert m.actor == "user" and m.portfolio_constraints == formal
    trusted = TrustedTradingRule(rule=rule(ref), market_code="SH", valid_from=T.date(), available_from=T, trusted_source="fixture official-adapter boundary", source_version="1")
    req = request(snapshot, ref, constraints=None, scenarios=(Scenario(name="buy", actions=(action(ref, quantity=D(1)),)),))
    output = calculate(snapshot, req, {ref: D(10)}, {ref: "A"}, formal_constraints=formal, unstructured_constraints=False, trusted_rules={ref: trusted})["scenarios"]
    assert output[0]["status"] == "ok" and output[0]["publication_ready"]
    assert output[1]["status"] == "violated" and not output[1]["publication_ready"]
    assert output[1]["checks"] == {"cash_nonnegative": "pass"}
    assert output[1]["mandate_checks"]["max_single_weight"] == "fail"
    assert "mandate_constraint_violation:max_single_weight" in output[1]["violations"]
    scenario_req = req.model_copy(update={"constraints": ScenarioConstraints(nature="agent_scenario", source="model tries relaxation", max_single_weight=D(1), minimum_cash=D(0))})
    scenario_result = calculate(snapshot, scenario_req, {ref: D(10)}, {ref: "A"}, formal_constraints=formal, unstructured_constraints=False, trusted_rules={ref: trusted})["scenarios"][1]
    assert scenario_result["checks"]["max_single_weight"] == "pass"
    assert scenario_result["mandate_checks"]["max_single_weight"] == "fail"
    assumed = calculate(snapshot, req, {ref: D(10)}, {ref: "A"}, formal_constraints=formal, unstructured_constraints=False)["scenarios"][1]
    assert "action.0.trading_rule_is_scenario_assumption" in assumed["unknowns"]
    assert assumed["actions"][0]["quantity_authority"] == "scenario_rule_simulation"
    assert not assumed["publication_ready"]
    async with session_scope() as session:
        svc = InvestorService(session)
        revised = await svc.revise_mandate("local_owner", m.model_copy(update={"revision": 2, "portfolio_constraints": formal.model_copy(update={"minimum_cash": D(950)})}), Mutation(expected_revision=1, idempotency_key=uuid4()))
        assert (await svc.get_mandate("local_owner", 1)).portfolio_constraints.minimum_cash == D(900)
        assert revised.portfolio_constraints.minimum_cash == D(950)


@pytest.mark.asyncio
async def test_alternative_missing_price_does_not_pollute_baseline(ledger):
    snapshot, ref = await setup(ledger)
    from investment_agent.investor.contracts import PortfolioConstraints
    from investment_agent.portfolio.calculations import TrustedTradingRule
    new_ref = ObjectRef(kind="listing", id=999, revision=1)
    req = request(snapshot, ref, constraints=None, rules=(rule(ref), rule(new_ref)), scenarios=(Scenario(name="new", actions=(action(new_ref),)), Scenario(name="existing", actions=(action(ref, quantity=D(1)),))))
    formal = PortfolioConstraints(account_id=snapshot.account_id)
    trusted = {r: TrustedTradingRule(rule=rule(r), market_code="SH", valid_from=T.date(), available_from=T, trusted_source="fixture", source_version="1") for r in (ref, new_ref)}
    baseline, missing, existing = calculate(snapshot, req, {ref: D(10), new_ref: None}, {ref: "A", new_ref: None}, ("stale_price:999",), formal, False, trusted)["scenarios"]
    assert baseline["status"] == "ok" and existing["status"] == "ok"
    assert missing["status"] == "needs_data" and missing["nav"] is None
    assert "stale_price:999" in missing["unknowns"] and "stale_price:999" not in baseline["unknowns"]


@pytest.mark.asyncio
async def test_resolver_rejects_future_rules_and_pit_price(ledger):
    from investment_agent.portfolio.calculations import TrustedTradingRule
    snapshot, ref = await setup(ledger)
    async with session_scope() as session:
        m = await InvestorService(session).create_mandate("local_owner", mandate(), uuid4())
    req = request(snapshot, ref, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=1))
    class FutureResolver:
        async def resolve(self, listing_ref, as_of):
            return TrustedTradingRule(rule=rule(listing_ref), market_code="SH", valid_from=T.date(), available_from=as_of + timedelta(days=1), trusted_source="fixture", source_version="1")
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="Trading rule"):
            await PortfolioCalculationService(session, rules=FutureResolver()).save(req)
    async with session_scope() as session:
        company = Company(canonical_name="Future quote")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SH", ticker="600002.SH")
        session.add(listing)
        await session.flush()
        new_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        p = MarketSnapshot(listing_id=listing.id, trade_date=T.date(), open=D(1), high=D(1), low=D(1), close=D(1), volume=D(1), adjust="none", provider="fixture", captured_at=snapshot.valuation_at + timedelta(seconds=1), version=1)
        session.add(p)
        await session.flush()
        price_ref = ObjectRef(kind="market_snapshot", id=p.id, revision=1)
        bad = req.model_copy(update={"operation_id": uuid4(), "prices": (PriceSelection(listing_ref=new_ref, price_ref=price_ref),), "scenarios": (Scenario(name="future", actions=(action(new_ref),)),)})
        with pytest.raises(InvestorError, match="cutoff"):
            await PortfolioCalculationService(session).save(bad)


@pytest.mark.asyncio
async def test_unknown_fees_preserve_provable_cash_violation(ledger):
    snapshot, ref = await setup(ledger)
    req = request(snapshot, ref, scenarios=(Scenario(name="unknown_fee", actions=(action(ref, quantity=D(110), fixed_fee=None),)),))
    result = run(snapshot, req, ref)[1]
    assert result["cash"] is None and result["cash_upper_bound"] == "-100"
    assert "action.0.cash_shortfall" in result["violations"]
    assert "action.0.fee_missing" in result["unknowns"]
    assert result["checks"]["cash_nonnegative"] == "unknown"


@pytest.mark.asyncio
async def test_below_minimum_keeps_existing_holdings_and_cash(ledger):
    snapshot, ref = await setup(ledger)
    req = request(snapshot, ref, rules=(rule(ref, buy_minimum=D(200)),), scenarios=(Scenario(name="too_small", actions=(action(ref, quantity=None, budget=D(50)),)),))
    result = run(snapshot, req, ref)[1]
    assert result["actions"][0]["quantity"] == "0" and result["fees"] == "0"
    assert result["cash"] == "1000" and result["nav"] == "1100"
    assert result["holdings"][0]["quantity"] == "10"
    assert "action.0.below_minimum" in result["violations"]


@pytest.mark.asyncio
async def test_partial_unknown_holding_and_numeric_reference_paths(ledger):
    from investment_agent.portfolio.calculations import reference_outputs
    snapshot, ref = await setup(ledger)
    unknown_holding = snapshot.holdings[0].model_copy(update={"confirmed_quantity": None, "quantity_subtotal": D(999)})
    partial = snapshot.model_copy(update={"coverage": "partial", "holdings": (unknown_holding,), "total_assets": None})
    req = request(partial, ref, scenarios=(Scenario(name="sell", actions=(action(ref, direction="sell", quantity=D(1)),)),))
    details = calculate(partial, req, {ref: D(10)}, {ref: "A"})
    result = details["scenarios"][1]
    assert result["holdings"][0]["quantity"] is None and result["nav"] is None
    assert "action.0.confirmed_holding_missing" in result["unknowns"]
    assert "action.0.insufficient_holding" not in result["violations"]
    outputs = reference_outputs(details)
    assert "scenario_1_nav" not in outputs and "scenario_1_holding_0_quantity" not in outputs
    assert outputs["scenario_0_cash"].value == D(1000)
    assert outputs["scenario_1_action_0_quantity"].unit == "shares"
    path = next(p for p in details["result_paths"] if p["field_path"] == "results.scenario_1_action_0_quantity")
    assert path["listing_ref"] == ref.model_dump(mode="json") and not path["publication_ready"]


@pytest.mark.asyncio
async def test_saved_price_and_constraint_versions_remain_fixed_after_new_material(ledger):
    from investment_agent.investor.common import Mutation
    from investment_agent.investor.contracts import PortfolioConstraints
    snapshot, ref = await setup(ledger)
    async with session_scope() as session:
        m = await InvestorService(session).create_mandate("local_owner", mandate().model_copy(update={"portfolio_constraints": PortfolioConstraints(account_id=snapshot.account_id, minimum_cash=D(500))}), uuid4())
    req = request(snapshot, ref, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=1))
    async with session_scope() as session:
        record = await PortfolioCalculationService(session).save(req)
    async with session_scope() as session:
        session.add(MarketSnapshot(listing_id=int(ref.id), trade_date=T.date(), open=D(30), high=D(30), low=D(30), close=D(30), volume=D(1), adjust="none", provider="fixture", captured_at=datetime.now(UTC), version=2))
        await InvestorService(session).revise_mandate("local_owner", m.model_copy(update={"revision": 2, "portfolio_constraints": m.portfolio_constraints.model_copy(update={"minimum_cash": D(0)})}), Mutation(expected_revision=1, idempotency_key=uuid4()))
    async with session_scope() as session:
        assert await PortfolioCalculationService(session).get(snapshot.account_id, record.id) == record
        assert await PortfolioCalculationService(session).save(req) == record
        assert record.details["mandate"]["portfolio_constraints"]["minimum_cash"] == "500"
        assert record.details["sources"][0]["price_ref"]["revision"] == 1
        assert record.details["scenarios"][1]["mandate_checks"]["minimum_cash"] == "fail"


@pytest.mark.asyncio
async def test_account_scope_and_service_trusted_rule_boundary(ledger):
    from investment_agent.investor.contracts import PortfolioConstraints
    from investment_agent.portfolio.calculations import TrustedTradingRule
    snapshot, ref = await setup(ledger)
    async with session_scope() as session:
        m = await InvestorService(session).create_mandate("local_owner", mandate().model_copy(update={"portfolio_constraints": PortfolioConstraints(account_id=uuid4())}), uuid4())
    req = request(snapshot, ref, mandate_ref=ObjectRef(kind="mandate", id=m.id, revision=1))
    class FixtureResolver:
        async def resolve(self, listing_ref, as_of):
            return TrustedTradingRule(rule=rule(listing_ref), market_code="SH", valid_from=T.date(), available_from=T, trusted_source="fixture adapter", source_version="1")
    async with session_scope() as session:
        result = await PortfolioCalculationService(session, rules=FixtureResolver()).save(req)
        scenario = result.details["scenarios"][1]
        assert "mandate_constraints_missing" in scenario["unknowns"]
        assert scenario["actions"][0]["quantity_authority"] == "verified_rule_simulation"
        assert not scenario["publication_ready"]
        assert result.details["trusted_rules"][str(ref.id)]["verification"] == "verified"


@pytest.mark.asyncio
async def test_long_exact_amounts_have_fixed_ratio_precision(ledger):
    snapshot, ref = await setup(ledger)
    quantity = D("1" + "0" * 160 + ".123456789")
    holding = snapshot.holdings[0].model_copy(update={"confirmed_quantity": quantity})
    large = snapshot.model_copy(update={"holdings": (holding,)})
    req = request(large, ref, scenarios=(Scenario(name="hold", actions=()),))
    result = calculate(large, req, {ref: D(10)}, {ref: "A"})["scenarios"][0]
    with localcontext() as context:
        context.prec = 220
        assert D(result["nav"]) == quantity * 10 + 1000
    assert len(result["holdings"][0]["weight"]) <= 42
