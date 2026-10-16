"""account_fit_checks v1: financial checks, never a company recommendation."""

from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid5

from investment_agent.evidence.contracts import CalculationAssumption, CalculationRecord
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    Scenario,
    ScenarioAction,
)
from investment_agent.portfolio.calculations import calculate, reference_outputs
from investment_agent.portfolio.purposes import check_scenarios
from investment_agent.portfolio.service import digest
from investment_agent.portfolio.suitability_contracts import (
    AccountFitAssessment,
    FitCheck,
    FitRequestItem,
    FrozenAccountInputs,
)

METHOD = "account_fit_checks"
METHOD_VERSION = "1"
KNOWN_TTL_SECONDS = 300
UNKNOWN_TTL_SECONDS = 30


class FitItemEvaluationError(Exception):
    """An explicitly classified local item failure; system errors propagate."""


def purpose_key(inputs: FrozenAccountInputs) -> str:
    return digest(inputs.purpose_binding.model_dump(mode="json"))


def calculation_basis_key(item: FitRequestItem, inputs: FrozenAccountInputs) -> str:
    return digest({"item": item.model_dump(mode="json"), "purpose": inputs.purpose_binding.model_dump(mode="json"), "method": METHOD, "version": METHOD_VERSION, "valuation_policy": "exact_daily_valuation"})


def fit_inputs(inputs: FrozenAccountInputs, item: FitRequestItem) -> FrozenAccountInputs:
    held = {h.listing_ref for h in inputs.snapshot.holdings}
    relevant = tuple(s for s in inputs.listing_sources if s.listing_ref in held or s.listing_ref == item.listing_ref)
    frozen = inputs.model_copy(update={"listing_sources": relevant})
    return frozen.model_copy(update={"manifest_hash": digest(frozen.model_dump(mode="json", exclude={"manifest_hash", "source_fingerprint"}))})


def fit_input_hash(inputs: FrozenAccountInputs, item: FitRequestItem) -> str:
    return digest({"frozen_inputs": inputs.model_dump(mode="json"), "item": item.model_dump(mode="json"), "method": METHOD, "version": METHOD_VERSION})


def evaluate_fit(inputs: FrozenAccountInputs, item: FitRequestItem, *, operation_id: UUID) -> tuple[AccountFitAssessment, CalculationRecord | None]:
    """Use old pure arithmetic only for actions whose quantity is known.

    Missing buy rules never become one share or a no-trade buy simulation.
    The no_action scenario remains independently useful baseline arithmetic.
    """
    source = next((s for s in inputs.listing_sources if s.listing_ref == item.listing_ref), None)
    if source is None:
        raise FitItemEvaluationError("listing_source_missing")
    frozen = fit_inputs(inputs, item)
    relevant = frozen.listing_sources
    assumptions = item.trade_assumptions
    action = None
    if item.action != "hold" and source.trusted_rule is not None:
        assert assumptions is not None
        action = ScenarioAction(listing_ref=item.listing_ref, direction="buy", quantity=source.trusted_rule.rule.buy_minimum, execution_price=assumptions.execution_price, fixed_fee=assumptions.fixed_fee, fee_rate=assumptions.fee_rate, purpose_id=inputs.purpose_binding.purpose_id, assumption_source=assumptions.assumption_source, nature="user_scenario")
    can_simulate = item.action == "hold" or action is not None
    request = PortfolioCalculationRequest(operation_id=uuid5(operation_id, f"calculation:{item.listing_ref.id}"), account_id=inputs.account_id, snapshot_ref=inputs.snapshot.reference, mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=inputs.mandate.revision), scenarios=(Scenario(name="hold" if item.action == "hold" else "minimum_increment" if action else "simulation_unavailable", actions=(action,) if action else ()),), purpose_ref=inputs.purpose_version.reference if inputs.purpose_version else None)
    formal = inputs.mandate.portfolio_constraints
    if formal is not None and formal.account_id != inputs.account_id:
        formal = None
    # Missing valuation cannot acquire a complete NAV from the old snapshot.
    valuation_complete = inputs.valuation is not None and inputs.valuation.coverage == "complete" and inputs.valuation.total_assets is not None
    snapshot = inputs.snapshot if valuation_complete else inputs.snapshot.model_copy(update={"coverage": "partial"})
    prices = {s.listing_ref: s.price.price if s.price.unknown_reason is None else None for s in relevant}
    sectors = {s.listing_ref: s.sector.code for s in relevant}
    trusted = {s.listing_ref: s.trusted_rule for s in relevant if s.trusted_rule is not None}
    details = calculate(snapshot, request, prices, sectors, formal_constraints=formal, unstructured_constraints=bool(inputs.mandate.hard_constraints), trusted_rules=trusted)
    details.update(request=request.model_dump(mode="json"), snapshot=snapshot.model_dump(mode="json"), account_id=str(inputs.account_id), listing_ref=item.listing_ref.model_dump(mode="json"), fit_item=item.model_dump(mode="json"), frozen_inputs=frozen.model_dump(mode="json"), method=METHOD, method_version=METHOD_VERSION)
    if inputs.purpose_version is not None:
        check_scenarios(details, inputs.purpose_version)
    extra_unknowns: set[str] = set()
    extra_unknowns.update(s.price.unknown_reason for s in relevant if s.price.unknown_reason is not None and (s.listing_ref == item.listing_ref or any(h.listing_ref == s.listing_ref and h.confirmed_quantity != 0 for h in inputs.snapshot.holdings)))
    if inputs.valuation is None:
        extra_unknowns.add("valuation_missing")
    elif not valuation_complete:
        extra_unknowns.add("valuation_incomplete")
    if inputs.purpose_version is not None and inputs.purpose_version.snapshot_ref != inputs.snapshot.reference:
        extra_unknowns.add("purpose_snapshot_changed")
    if inputs.purpose_binding.state == "unknown" or (inputs.purpose_binding.state == "unselected" and item.action != "hold"):
        extra_unknowns.add("purpose_binding_unknown" if inputs.purpose_binding.state == "unknown" else "purpose_unselected")
    if not can_simulate:
        extra_unknowns.add("trading_rule_unavailable")
        # There is no numerical trade scenario. Only retain real baseline.
        details["scenarios"] = details["scenarios"][:1]
    for scenario in details["scenarios"]:
        if "purpose_association_required" in scenario["violations"] and inputs.purpose_binding.state != "selected":
            scenario["violations"].remove("purpose_association_required")
            scenario["unknowns"].append("purpose_unselected")
        scenario["unknowns"] = sorted(set(scenario["unknowns"]) | extra_unknowns)
        scenario["publication_ready"] = False
        scenario["status"] = "violated" if scenario["violations"] else "needs_data" if scenario["unknowns"] else "ok"
    checks: list[FitCheck] = []
    for index, scenario in enumerate(details["scenarios"]):
        scope: Literal["baseline", "scenario"] = "baseline" if index == 0 else "scenario"
        checks.extend(FitCheck(code=code, state="blocked", scope=scope) for code in scenario["violations"])
        checks.extend(FitCheck(code=code, state="unknown", scope=scope) for code in scenario["unknowns"])
        checks.extend(FitCheck(code=f"mandate:{code}", state="blocked" if status == "fail" else status, scope=scope) for code, status in scenario["mandate_checks"].items())
        checks.append(FitCheck(code="cash_nonnegative", state="blocked" if scenario["checks"]["cash_nonnegative"] == "fail" else scenario["checks"]["cash_nonnegative"], scope=scope))
    if not can_simulate:
        checks.append(FitCheck(code="minimum_increment_unavailable", state="unknown", scope="scenario"))
    checks.append(FitCheck(code="soft_limits", state="not_configured", scope="scenario"))
    blocked = any(check.state == "blocked" for check in checks)
    unknown = any(check.state == "unknown" for check in checks)
    status: Literal["blocked", "unknown", "pass"] = "blocked" if blocked else "unknown" if unknown else "pass"
    outputs = reference_outputs(details)
    # A constant zero fee on an empty no_action baseline does not constitute
    # an evaluated amount. Fully unavailable input has no OK calculation.
    if not any(path["metric"] != "fees" for path in details["result_paths"]):
        outputs = {}
        details["result_paths"] = []
    input_hash = fit_input_hash(frozen, item)
    calculation = None
    if outputs:
        numeric_assumptions = []
        if assumptions is not None:
            for name, unit in (("execution_price", "CNY/share"), ("fixed_fee", "CNY"), ("fee_rate", "ratio")):
                value = getattr(assumptions, name)
                if value is not None:
                    numeric_assumptions.append(CalculationAssumption(name=name, value=Quantity(value=value, unit=unit, currency="CNY" if unit.startswith("CNY") else None), provenance="user_scenario", rationale=assumptions.assumption_source))
        calculation = CalculationRecord(id=request.operation_id, revision=1, created_at=inputs.assessment_as_of, actor="account_fit_calculator", origin_ref=inputs.snapshot.reference, method=METHOD, method_version=METHOD_VERSION, fact_refs=(inputs.snapshot.reference, request.mandate_ref, *((inputs.valuation.reference,) if inputs.valuation else ()), *((inputs.purpose_version.reference,) if inputs.purpose_version else ())), assumption_refs=(), input_hash=input_hash, assumptions=tuple(numeric_assumptions), as_of=inputs.assessment_as_of, applicable=True, status=CalculationStatus.OK, results=outputs, quality=tuple(sorted({code for scenario in details["scenarios"] for code in scenario["unknowns"]})), details=details)
    result_id = uuid5(operation_id, f"fit:{item.listing_ref.id}")
    assessment = AccountFitAssessment(id=result_id, reference=ObjectRef(kind="account_fit", id=result_id, revision=1), operation_id=operation_id, input_hash=input_hash, account_id=inputs.account_id, listing_ref=item.listing_ref, action=item.action, request_item=item, purpose_binding_key=purpose_key(inputs), calculation_basis_key=calculation_basis_key(item, inputs), assessment_as_of=inputs.assessment_as_of, price_basis_at=inputs.price_basis_at, expires_at=inputs.assessment_as_of + timedelta(seconds=UNKNOWN_TTL_SECONDS if unknown else KNOWN_TTL_SECONDS), frozen_inputs=frozen, financial_status=status, checks=tuple(checks), calculation_refs=(ObjectRef(kind="calculation", id=calculation.id, revision=1),) if calculation else (), ranking_bucket="blocked" if blocked else "incomplete", coverage="partial" if unknown else "complete")
    return assessment, calculation
