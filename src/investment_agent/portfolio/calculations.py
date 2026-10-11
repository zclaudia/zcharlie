"""Deterministic immutable scenario accounting; no ledger writes or reservations.

Monetary arithmetic preserves finite input amounts; ratios always use 40.
Actions within a scenario share cash and quantities. Alternatives never do.
"""
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationAssumption, CalculationRecord
from investment_agent.investor.common import Contract, ObjectRef, Quantity, Text
from investment_agent.investor.contracts import Mandate, PortfolioConstraints
from investment_agent.investor.enums import CalculationStatus
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    Scenario,
    TradingRule,
)
from investment_agent.portfolio.contracts import Snapshot
from investment_agent.portfolio.service import PortfolioService, digest
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.investor import MandateRevision
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.reference import Listing, Market


class TrustedTradingRule(Contract):
    """Server resolver output, never part of the request schema."""
    rule: TradingRule
    market_code: Text
    valid_from: date
    valid_to: date | None = None
    available_from: datetime
    trusted_source: Text
    source_version: Text
    verification: Literal["verified"] = "verified"

    @model_validator(mode="after")
    def interval(self) -> "TrustedTradingRule":
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("invalid trading rule validity interval")
        return self


class TradingRuleResolver(Protocol):
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule | None: ...


class UnavailableTradingRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule | None:
        return None


D = Decimal


def ratio(value: Decimal, total: Decimal | None) -> Decimal | None:
    if total is None or total <= 0:
        return None
    with localcontext() as context:
        context.prec = 40
        return value / total


def floor_quantity(cap: Decimal, rule: TradingRule) -> Decimal:
    """Minimum plus N increments; floor so fees-inclusive budget is not exceeded."""
    if cap < rule.buy_minimum:
        return D(0)
    return rule.buy_minimum + ((cap - rule.buy_minimum) / rule.buy_increment).to_integral_value(rounding=ROUND_FLOOR) * rule.buy_increment


def calculate(snapshot: Snapshot, request: PortfolioCalculationRequest, prices: dict[ObjectRef, Decimal | None], sectors: dict[ObjectRef, str | None], source_unknowns: tuple[str, ...] = (), formal_constraints: PortfolioConstraints | None = None, unstructured_constraints: bool = True, trusted_rules: dict[ObjectRef, TrustedTradingRule] | None = None) -> dict[str, Any]:
    """Pure kernel. Production inputs are rebuilt by PortfolioCalculationService."""
    with localcontext() as context:
        amounts = [snapshot.known_cash, *[h.confirmed_quantity for h in snapshot.holdings], *prices.values()]
        finite = [v for v in amounts if v is not None]
        # Snapshots can contain long exact split chains. Amount precision adapts
        # to their integer/fractional span; only ratio precision stays fixed.
        context.prec = max(128, max((v.adjusted() for v in finite), default=0) - min((int(v.as_tuple().exponent) for v in finite), default=0) + 160)
        return _calculate(snapshot, request, prices, sectors, source_unknowns, formal_constraints, unstructured_constraints, trusted_rules or {})


def _calculate(snapshot: Snapshot, request: PortfolioCalculationRequest, prices: dict[ObjectRef, Decimal | None], sectors: dict[ObjectRef, str | None], source_unknowns: tuple[str, ...], formal_constraints: PortfolioConstraints | None, unstructured_constraints: bool, trusted_rules: dict[ObjectRef, TrustedTradingRule]) -> dict[str, Any]:
    initial = {h.listing_ref: h.confirmed_quantity for h in snapshot.holdings}
    rules = {r.listing_ref: r for r in request.rules}
    results: list[dict[str, Any]] = []
    for scenario in (Scenario(name="no_action", actions=()), *request.scenarios):
        quantities = dict(initial)
        cash = snapshot.known_cash
        unknowns: set[str] = set()
        cash_upper_bound = snapshot.known_cash
        failures = set(snapshot.conflicts)
        if cash is None:
            unknowns.add("known_cash_missing")
        if snapshot.coverage != "complete":
            unknowns.add("partial_asset_scope")
        actions: list[dict[str, Any]] = []
        total_fees: Decimal | None = D(0)
        for index, action in enumerate(scenario.actions):
            ref = action.listing_ref
            key = f"action.{index}"
            quantity = action.quantity
            price = action.execution_price
            trusted = trusted_rules.get(ref)
            rule = trusted.rule if trusted is not None else rules.get(ref)
            if rule is not None and trusted is None:
                unknowns.add(f"{key}.trading_rule_is_scenario_assumption")
            fee_known = action.fixed_fee is not None and action.fee_rate is not None
            if rule is None:
                unknowns.add(f"{key}.trading_rule_missing")
                # Requested shares are assumptions; no executable shares are generated.
                quantity = None
            if price is None:
                unknowns.add(f"{key}.execution_price_missing")
            if not fee_known:
                unknowns.add(f"{key}.fee_missing")
                total_fees = None
            if action.budget is not None:
                quantity = None
                if rule is not None and price is not None and fee_known:
                    assert action.fixed_fee is not None and action.fee_rate is not None
                    cap = max(D(0), (action.budget - action.fixed_fee) / (price * (1 + action.fee_rate)))
                    quantity = floor_quantity(cap, rule)
                    # Guard division rounding at a unit boundary.
                    while quantity > 0 and quantity * price * (1 + action.fee_rate) + action.fixed_fee > action.budget:
                        quantity = max(D(0), quantity - rule.buy_increment)
                    if quantity < rule.buy_minimum:
                        quantity = D(0)
                        failures.add(f"{key}.below_minimum")
            before = quantities.get(ref, D(0) if snapshot.coverage == "complete" else None)
            if action.direction == "sell" and action.quantity is not None and before is not None and action.quantity > before:
                failures.add(f"{key}.insufficient_holding")
            if quantity is not None and rule is not None and quantity > 0:
                minimum = rule.buy_minimum if action.direction == "buy" else rule.sell_minimum
                increment = rule.buy_increment if action.direction == "buy" else rule.sell_increment
                odd_exit = action.direction == "sell" and rule.full_odd_lot_sell and before is not None and quantity == before
                if not odd_exit and (quantity < minimum or (quantity - minimum) % increment != 0):
                    failures.add(f"{key}.trading_unit_violation")
                if action.direction == "sell":
                    if before is None:
                        unknowns.add(f"{key}.confirmed_holding_missing")
                    elif quantity > before:
                        failures.add(f"{key}.insufficient_holding")
                if before is None:
                    unknowns.add(f"{key}.confirmed_holding_missing")
                    quantities[ref] = None
                else:
                    quantities[ref] = before + quantity * (1 if action.direction == "buy" else -1)
            elif quantity == 0:
                quantities[ref] = before
            else:
                quantities[ref] = None
            fee = None
            gross = quantity * price if quantity is not None and price is not None else None
            if gross is not None and fee_known:
                assert action.fixed_fee is not None and action.fee_rate is not None
                fee = D(0) if quantity == 0 else action.fixed_fee + gross * action.fee_rate
                if total_fees is not None:
                    total_fees += fee
            if gross is None:
                cash_upper_bound = None
            elif cash_upper_bound is not None:
                cash_upper_bound += gross * (-1 if action.direction == "buy" else 1) - (fee if fee is not None else D(0))
                if cash_upper_bound < 0:
                    failures.add(f"{key}.cash_shortfall")
            if gross is None or fee is None:
                cash = None
                total_fees = None
            elif cash is not None:
                cash += gross * (-1 if action.direction == "buy" else 1) - fee
                # Sequential action order is intentional: no unfunded pre-sale buys.
                if cash < 0:
                    failures.add(f"{key}.cash_shortfall")
            actions.append({"listing_ref": ref.model_dump(mode="json"), "quantity_authority": "verified_rule_simulation" if trusted is not None else "scenario_rule_simulation" if rule is not None else "unavailable", "rule_source": trusted.model_dump(mode="json") if trusted is not None else rule.model_dump(mode="json") if rule is not None else None, "requested_quantity": str(action.quantity) if action.quantity is not None else None, "quantity": str(quantity) if quantity is not None else None, "execution_price": str(price) if price is not None else None, "gross": str(gross) if gross is not None else None, "fee": str(fee) if fee is not None else None})
        values: dict[ObjectRef, Decimal | None] = {}
        for ref, quantity in quantities.items():
            price = prices.get(ref)
            values[ref] = D(0) if quantity == 0 else quantity * price if quantity is not None and price is not None else None
            if quantity is None:
                unknowns.add(f"quantity_missing:{ref.id}")
            if quantity != 0 and price is None:
                unknowns.add(f"valuation_price_missing:{ref.id}")
            if quantity is not None and quantity < 0:
                failures.add(f"negative_holding:{ref.id}")
        required = {ref for ref, quantity in quantities.items() if quantity != 0}
        unknowns.update(u for u in source_unknowns if any(u.endswith(f":{ref.id}") for ref in required))
        nav = cash + sum((v for v in values.values() if v is not None), D(0)) if cash is not None and snapshot.coverage == "complete" and all(v is not None for v in values.values()) else None
        if nav is None:
            unknowns.add("complete_nav_missing")
        elif nav <= 0:
            unknowns.add("nonpositive_nav_ratio_undefined")
        sector_values: dict[str, Decimal] = {}
        sector_complete = True
        for ref, value in values.items():
            if value == 0:
                continue
            sector = sectors.get(ref)
            if sector is None or value is None:
                sector_complete = False
                unknowns.add(f"sector_coverage_missing:{ref.id}")
            else:
                sector_values[sector] = sector_values.get(sector, D(0)) + value
        weights = {str(ref.id): ratio(value, nav) if value is not None else None for ref, value in values.items()}
        sector_weights = {key: ratio(value, nav) for key, value in sector_values.items()}
        checks: dict[str, str] = {"cash_nonnegative": "unknown" if cash is None else "fail" if cash < 0 else "pass"}
        if cash is not None and cash < 0:
            failures.add("cash_shortfall")
        formal_checks: dict[str, str] = {}
        for authority, constraints in (("scenario", request.constraints), ("mandate", formal_constraints)):
            if constraints is None:
                if authority == "mandate":
                    unknowns.add("mandate_constraints_missing")
                continue
            comparisons: list[tuple[str, bool | None]] = []
            if constraints.minimum_cash is not None:
                comparisons.append(("minimum_cash", None if cash is None else cash >= constraints.minimum_cash))
            if constraints.minimum_cash_weight is not None:
                cash_weight = ratio(cash, nav) if cash is not None else None
                comparisons.append(("minimum_cash_weight", None if cash_weight is None else cash_weight >= constraints.minimum_cash_weight))
            if constraints.max_single_weight is not None:
                comparisons.append(("max_single_weight", False if any(w is not None and w > constraints.max_single_weight for w in weights.values()) else None if nav is None or nav <= 0 else True))
            if constraints.max_sector_weight is not None:
                comparisons.append(("max_sector_weight", False if any(w is not None and w > constraints.max_sector_weight for w in sector_weights.values()) else None if not sector_complete or nav is None or nav <= 0 else True))
            for name, passed in comparisons:
                target_checks = checks if authority == "scenario" else formal_checks
                target_checks[name] = "unknown" if passed is None else "pass" if passed else "fail"
                if passed is None:
                    unknowns.add(f"{authority}_constraint_unknown:{name}")
                elif not passed:
                    failures.add(f"{authority}_constraint_violation:{name}")
        if unstructured_constraints:
            unknowns.add("formal_mandate_constraints_not_structured")
        results.append({"name": scenario.name, "status": "violated" if failures else "needs_data" if unknowns else "ok", "cash": str(cash) if cash is not None else None, "cash_upper_bound": str(cash_upper_bound) if cash_upper_bound is not None else None, "nav": str(nav) if nav is not None else None, "fees": str(total_fees) if total_fees is not None else None, "actions": actions, "holdings": [{"listing_ref": ref.model_dump(mode="json"), "quantity": str(quantities[ref]) if quantities[ref] is not None else None, "market_value": str(value) if value is not None else None, "weight": str(weights[str(ref.id)]) if weights[str(ref.id)] is not None else None} for ref, value in values.items()], "sector_weights": {key: str(value) if value is not None else None for key, value in sector_weights.items()}, "sector_coverage": "complete" if sector_complete else "partial", "checks": checks, "mandate_checks": formal_checks, "publication_ready": not failures and not unknowns, "violations": sorted(failures), "unknowns": sorted(unknowns)})
    return {"scenarios": results, "constraint_authority": "exact_mandate_version_and_separate_scenario_assumptions", "mutually_exclusive": True, "currency": "CNY", "ratio_precision": 40}


def reference_outputs(details: dict[str, Any]) -> dict[str, Quantity]:
    """Stable index paths; known values only, with per-path diagnostic context."""
    outputs: dict[str, Quantity] = {}
    paths: list[dict[str, Any]] = []
    for index, scenario in enumerate(details["scenarios"]):
        prefix = f"scenario_{index}"
        def add(key: str, value: str | None, unit: str, scenario_index: int = index, scenario_data: dict[str, Any] = scenario, **identity: Any) -> None:
            if value is None:
                return
            outputs[key] = Quantity(value=D(value), unit=unit, currency="CNY" if unit == "CNY" else None)
            paths.append({"field_path": f"results.{key}", "scenario_index": scenario_index, "scenario_name": scenario_data["name"], "role": "calculation_output", "scenario_status": scenario_data["status"], "publication_ready": scenario_data["publication_ready"], **identity})
        for field in ("cash", "cash_upper_bound", "nav", "fees"):
            add(f"{prefix}_{field}", scenario[field], "CNY", metric=field)
        purpose_checks = scenario.get("purpose_checks")
        if purpose_checks:
            for field in ("required_cash", "remaining_cash", "reserve_shortfall"):
                add(f"{prefix}_purpose_{field}", purpose_checks[field], "CNY", metric=f"purpose_{field}", purpose_ref=purpose_checks["purpose_ref"])
            for purpose_index, purpose in enumerate(purpose_checks["purposes"]):
                for field in ("spent", "remaining_budget"):
                    add(f"{prefix}_purpose_{purpose_index}_{field}", purpose[field], "CNY", metric=f"purpose_{field}", purpose_id=purpose["id"], purpose_ref=purpose_checks["purpose_ref"])
        for holding_index, holding in enumerate(scenario["holdings"]):
            for field, unit in (("quantity", "shares"), ("market_value", "CNY"), ("weight", "ratio")):
                add(f"{prefix}_holding_{holding_index}_{field}", holding[field], unit, listing_ref=holding["listing_ref"], holding_index=holding_index, metric=field)
        for action_index, action in enumerate(scenario["actions"]):
            for field, unit in (("quantity", "shares"), ("gross", "CNY"), ("fee", "CNY")):
                add(f"{prefix}_action_{action_index}_{field}", action[field], unit, listing_ref=action["listing_ref"], action_index=action_index, metric=field, quantity_authority=action["quantity_authority"])
        for sector_index, (code, weight) in enumerate(sorted(scenario["sector_weights"].items())):
            add(f"{prefix}_sector_{sector_index}_weight", weight, "ratio", sector_code=code, metric="sector_weight")
    details["result_paths"] = paths
    return outputs


class PortfolioCalculationService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner", rules: TradingRuleResolver | None = None) -> None:
        self.session = session
        self.owner = owner
        self.rules = rules or UnavailableTradingRules()

    async def get(self, account_id: UUID, calculation_id: UUID) -> CalculationRecord:
        await PortfolioService(self.session, self.owner)._account(account_id)
        row = await self.session.get(SavedCalculation, (str(calculation_id), 1))
        if row is None or row.owner_id != self.owner or row.payload.get("method") != "portfolio_scenarios" or row.payload.get("details", {}).get("request", {}).get("account_id") != str(account_id):
            raise InvestorError("not_found", "Portfolio calculation not found")
        return CalculationRecord.model_validate(row.payload)

    @storage_conflicts
    async def save(self, request: PortfolioCalculationRequest) -> CalculationRecord:
        ledger = PortfolioService(self.session, self.owner)
        await ledger._reserve()
        await ledger._account(request.account_id)
        payload = request.model_dump(mode="json")
        row = await self.session.get(SavedCalculation, (str(request.operation_id), 1))
        if row is not None:
            if row.owner_id != self.owner:
                raise InvestorError("not_found", "Calculation not found")
            if row.payload.get("method") != "portfolio_scenarios" or row.payload.get("details", {}).get("request") != payload:
                raise InvestorError("idempotency_conflict", "Calculation key reused")
            return await self.get(request.account_id, request.operation_id)
        from investment_agent.portfolio.purposes import PurposeService
        current_purposes = await PurposeService(self.session, self.owner).current(request.account_id)
        if current_purposes is not None and request.purpose_ref != current_purposes.version.reference:
            raise InvestorError("invalid_reference", "计算须绑定该账户当前确认用途版本")
        record = await self.rebuild(request)
        self.session.add(SavedCalculation(id=str(record.id), revision=1, owner_id=self.owner, payload=record.model_dump(mode="json")))
        await self.session.flush()
        return record

    async def rebuild(self, request: PortfolioCalculationRequest) -> CalculationRecord:
        """Re-resolve exact source identities and trusted rules without persistence."""
        ledger = PortfolioService(self.session, self.owner)
        payload = request.model_dump(mode="json")
        snapshot = await ledger.snapshot(request.account_id, snapshot_id=UUID(str(request.snapshot_ref.id)), revision=request.snapshot_ref.revision)
        valuation = None
        if request.valuation_ref is not None:
            from investment_agent.portfolio.valuation import ValuationService

            valuation = await ValuationService(self.session, self.owner).get(
                request.account_id, UUID(str(request.valuation_ref.id))
            )
            if (
                valuation.snapshot_id != UUID(str(request.snapshot_ref.id))
                or valuation.ledger_revision != request.snapshot_ref.revision
            ):
                raise InvestorError(
                    "invalid_reference", "估值不属于该事实版本；按当前估值重新计算"
                )
        # The price basis and knowledge time: the bound valuation's cutoff for
        # the v2 contract, the frozen snapshot otherwise (plan 2.5).
        knowledge_at = valuation.knowledge_cutoff if valuation is not None else snapshot.valuation_at
        valuation_basis = (
            {h.listing_ref: h for h in valuation.holdings} if valuation is not None else {}
        )
        mandate = await self.session.get(MandateRevision, (str(request.mandate_ref.id), request.mandate_ref.revision))
        if mandate is None or mandate.owner_id != self.owner:
            raise InvestorError("not_found", "Mandate version not found")
        mandate_value = Mandate.model_validate(mandate.payload)
        formal = mandate_value.portfolio_constraints
        if formal is not None and formal.account_id != request.account_id:
            formal = None
        refs = {h.listing_ref for h in snapshot.holdings} | {a.listing_ref for s in request.scenarios for a in s.actions}
        selections = {p.listing_ref: p.price_ref for p in request.prices}
        frozen = {h.listing_ref: h for h in snapshot.holdings}
        if any(ref not in refs or ref in frozen for ref in selections):
            raise InvestorError("invalid_reference", "Price selections only extend new holdings")
        trusted_rules: dict[ObjectRef, TrustedTradingRule] = {}
        prices: dict[ObjectRef, Decimal | None] = {}
        sectors: dict[ObjectRef, str | None] = {}
        sources: list[dict[str, Any]] = []
        unknowns: list[str] = []
        for ref in sorted(refs, key=lambda item: int(item.id)):
            listing = await self.session.get(Listing, ref.id)
            market = await self.session.get(Market, listing.market_code) if listing else None
            if listing is None or market is None or market.currency != "CNY" or listing.market_code not in {"CN", "SH", "SZ", "BJ", "SSE", "SZSE", "BSE"}:
                raise InvestorError("invalid_reference", "Listing outside supported CNY scope")
            trusted = await self.rules.resolve(ref, knowledge_at)
            if trusted is not None:
                if trusted.rule.listing_ref != ref or trusted.market_code != listing.market_code or trusted.available_from > knowledge_at or trusted.valid_from > knowledge_at.date() or (trusted.valid_to is not None and trusted.valid_to <= knowledge_at.date()):
                    raise InvestorError("invalid_reference", "Trading rule identity or PIT validity mismatch")
                trusted_rules[ref] = trusted
            if valuation is not None:
                basis = valuation_basis.get(ref)
                if basis is not None:
                    # The bound valuation's own price, or explicitly unknown -
                    # never the snapshot's older frozen price (plan 2.5).
                    price_ref = basis.price_ref
                    if price_ref is None and basis.quantity != 0:
                        unknowns.append(f"valuation_price_unavailable:{ref.id}")
                else:
                    price_ref = selections.get(ref)
            else:
                price_ref = frozen[ref].price_ref if ref in frozen else selections.get(ref)
            price = await self.session.get(MarketSnapshot, price_ref.id) if price_ref else None
            if price_ref is not None and (price is None or price.version != price_ref.revision or price.listing_id != ref.id or price.adjust != "none" or price.close <= 0 or price.trade_date > knowledge_at.date() or (price.captured_at.replace(tzinfo=UTC) if price.captured_at.tzinfo is None else price.captured_at.astimezone(UTC)) > knowledge_at):
                raise InvestorError("invalid_reference", "Price identity, basis or knowledge cutoff mismatch")
            prices[ref] = price.close if price is not None else None
            if valuation is None and ref in frozen and price is not None and (frozen[ref].price != price.close or frozen[ref].price_date != price.trade_date):
                raise InvestorError("invalid_reference", "Frozen snapshot price mismatch")
            if price is not None and (knowledge_at.date() - price.trade_date).days > request.max_price_age_days:
                prices[ref] = None
                if ref not in frozen or frozen[ref].confirmed_quantity != 0:
                    unknowns.append(f"stale_price:{ref.id}")
            classifications = list(await self.session.scalars(select(IndustryClassification).where(IndustryClassification.listing_id == ref.id, IndustryClassification.system == request.industry_system, IndustryClassification.valid_from <= knowledge_at.date(), (IndustryClassification.valid_to.is_(None) | (IndustryClassification.valid_to > knowledge_at.date())), IndustryClassification.captured_at <= knowledge_at)))
            classification = classifications[0] if len(classifications) == 1 else None
            sectors[ref] = classification.code if classification else None
            sources.append({"listing_ref": ref.model_dump(mode="json"), "price_ref": price_ref.model_dump(mode="json") if price_ref else None, "price": str(price.close) if price else None, "price_date": str(price.trade_date) if price else None, "captured_at": price.captured_at.isoformat() if price else None, "provider": price.provider if price else None, "industry": {"id": classification.id, "code": classification.code, "source": classification.source, "system": classification.system, "valid_from": str(classification.valid_from), "valid_to": str(classification.valid_to) if classification.valid_to else None, "captured_at": classification.captured_at.isoformat()} if classification else None})
        details = calculate(snapshot, request, prices, sectors, tuple(unknowns), formal, bool(mandate_value.hard_constraints), trusted_rules)
        details.update({"request": payload, "snapshot": snapshot.model_dump(mode="json"), "mandate": mandate.payload, "sources": sources, "trusted_rules": {str(ref.id): rule.model_dump(mode="json") for ref, rule in trusted_rules.items()}, "price_basis_at": knowledge_at.isoformat(), "fact_as_of": snapshot.fact_as_of.isoformat()})
        if valuation is not None:
            details["valuation"] = {
                "ref": valuation.reference.model_dump(mode="json"),
                "valuation_date": valuation.valuation_date.isoformat(),
                "knowledge_cutoff": valuation.knowledge_cutoff.isoformat(),
                "algorithm_version": valuation.algorithm_version,
                "input_digest": valuation.input_digest,
            }
        from investment_agent.portfolio.purposes import PurposeService, check_scenarios
        if request.purpose_ref is not None:
            version = await PurposeService(self.session, self.owner).exact(request.account_id, request.purpose_ref)
            details["purposes"] = version.model_dump(mode="json")
            check_scenarios(details, version)
        outputs = reference_outputs(details)
        assumptions: list[CalculationAssumption] = []
        for scenario_index, scenario in enumerate(request.scenarios, 1):
            for action_index, action in enumerate(scenario.actions):
                for field, unit in (("quantity", "shares"), ("budget", "CNY"), ("execution_price", "CNY/share"), ("fixed_fee", "CNY"), ("fee_rate", "ratio")):
                    value = getattr(action, field)
                    if value is not None:
                        assumptions.append(CalculationAssumption(name=f"scenario_{scenario_index}_action_{action_index}_{field}", value=Quantity(value=value, unit=unit, currency="CNY" if unit.startswith("CNY") else None), provenance=action.nature, rationale=action.assumption_source))
        # OK means deterministic arithmetic completed, not constraint approval.
        # Each numeric path retains its scenario status; AG23 must gate it.
        method_version = "ag22-v2" if valuation is not None else "ag22-v1"
        record = CalculationRecord(id=request.operation_id, revision=1, created_at=datetime.now(UTC), actor="portfolio_calculator", origin_ref=snapshot.reference, method="portfolio_scenarios", method_version=method_version, fact_refs=(snapshot.reference, *((valuation.reference,) if valuation is not None else ()), request.mandate_ref, *((request.purpose_ref,) if request.purpose_ref else ())), assumption_refs=(), input_hash=digest({"request": payload, "snapshot": details["snapshot"], "mandate": mandate.payload, "sources": sources, "trusted_rules": details["trusted_rules"], **({"valuation": details["valuation"]} if "valuation" in details else {}), **({"purposes": details["purposes"]} if "purposes" in details else {})}), as_of=knowledge_at, applicable=True, status=CalculationStatus.OK, assumptions=tuple(assumptions), results=outputs, quality=tuple(sorted({u for s in details["scenarios"] for u in s["unknowns"]})), details=details)
        return record
