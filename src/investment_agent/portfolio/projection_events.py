"""Per-fact projection handlers dispatched by exact fact type.

Each handler mutates the shared :class:`ProjectionState` and returns nothing.
The branch order inside a handler is the historical order; the dispatch table
replaces the ``isinstance`` chain, and the cross-fact row tail (negative cash,
unknown history, trace) stays with the engine.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, cast

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.contracts import (
    Balance,
    CashFact,
    CorporateAction,
    DividendRecord,
    EventRecord,
    HoldingComparison,
    RealizedPnL,
    ReconciliationComparison,
    Trade,
)
from investment_agent.portfolio.projection_state import CostState, ProjectionState

FactHandler = Callable[[ProjectionState, EventRecord], None]


def apply_balance(state: ProjectionState, record: EventRecord) -> None:
    """A confirmed balance: rebuild quantities, cash, coverage and the cost basis."""

    fact = cast(Balance, record.request.fact)
    if fact.kind == "reconciliation":
        before_cash = state.cash if not state.cash_unknown else None
        confirmed_holdings = {h.listing_ref: h.quantity for h in fact.holdings}
        holding_comparisons: list[HoldingComparison] = []
        for ref in sorted(state.quantities.keys() | confirmed_holdings.keys() | state.restricted, key=lambda r: int(r.id)):
            before_qty = state.quantities.get(ref, Decimal(0)) if ref not in state.restricted and (ref in state.quantities or state.coverage == "complete") else None
            after_qty = confirmed_holdings.get(ref, Decimal(0)) if ref in confirmed_holdings or fact.coverage == "complete" else None
            delta = after_qty - before_qty if before_qty is not None and after_qty is not None else None
            holding_comparisons.append(HoldingComparison(listing_ref=ref, before=before_qty, confirmed=after_qty, delta=delta, unknown_reason="before_or_confirmed_quantity_unknown" if delta is None else None))
        cash_delta = fact.cash - before_cash if fact.cash is not None and before_cash is not None else None
        comparison = ReconciliationComparison(event_id=record.id, occurred_at=fact.occurred_at, before_cash=before_cash, confirmed_cash=fact.cash, cash_delta=cash_delta, cash_unknown_reason="before_or_confirmed_cash_unknown" if cash_delta is None else None, holdings=tuple(holding_comparisons), user_difference_explanation=fact.unexplained_difference or "none")
        state.comparisons.append(comparison)
        if cash_delta is None or cash_delta != 0 or any(h.delta is None or h.delta != 0 for h in holding_comparisons):
            state.conflicts.append(f"reconciliation_difference:{record.id}")
    if state.has_balance:
        state.history.update(state.cash_unknown)
        state.history.update(f"unknown_quantity:{ref.id}" for ref in state.restricted)
        if state.coverage == "partial":
            state.history.add("unlisted_assets_unknown")
    prior_quantities = dict(state.quantities)
    prior_costs = dict(state.costs)
    prior_restricted = set(state.restricted)
    state.quantities = {h.listing_ref: h.quantity for h in fact.holdings}
    state.restricted = set()
    state.cash = fact.cash
    state.cash_unknown = set()
    if state.cash is None:
        state.mark_unknown(f"unknown_cash:{record.id}")
    state.coverage = fact.coverage
    state.scope = fact.asset_scope
    state.has_balance = True
    state.history.update(state.cash_unknown)
    if state.coverage == "partial":
        state.history.add("unlisted_assets_unknown")
    # Plan 2.7: balance holdings carry optional confirmed average cost and cycle
    # start; without a confirmation the cost stays unknown unless this is the
    # same unchanged position as before.
    costs: dict[ObjectRef, CostState] = {}
    for holding in fact.holdings:
        ref = holding.listing_ref
        if holding.quantity == 0:
            closed = prior_costs.get(ref)
            if closed is not None and closed.reason:
                state.cost_history.add(closed.reason)
            continue
        prior = prior_costs.get(ref)
        prior_quantity = prior_quantities.get(ref)
        if prior_quantity is None and prior is not None:
            prior_quantity = prior.quantity
        same_quantity = ref not in prior_restricted and prior_quantity is not None and prior_quantity == holding.quantity
        remaining: Decimal | None
        reason: str | None
        if holding.average_cost is not None:
            remaining = holding.quantity * holding.average_cost
            reason = None
        elif prior is not None and same_quantity and prior.remaining is not None:
            remaining = prior.remaining
            reason = None
        else:
            remaining = None
            reason = "balance_cost_not_confirmed"
        if prior is not None and prior.reason and prior.reason != reason and not same_quantity:
            state.cost_history.add(prior.reason)
        opened_on = holding.opened_on if holding.opened_on is not None else (prior.opened_on if prior is not None and same_quantity else None)
        costs[ref] = CostState(quantity=holding.quantity, remaining=remaining, opened_on=opened_on, reason=reason)
    state.costs = costs


def apply_trade(state: ProjectionState, record: EventRecord) -> None:
    """A buy or sell: quantity, cash, remaining cost and realized record."""

    fact = cast(Trade, record.request.fact)
    ref = fact.holding.listing_ref
    basis_unknown = ref in state.restricted
    if ref not in state.quantities and state.coverage == "partial":
        state.restricted.add(ref)
        basis_unknown = True
    direction = Decimal(1) if fact.direction == "buy" else Decimal(-1)
    state.quantities[ref] = state.quantities.get(ref, Decimal(0)) + direction * fact.holding.quantity
    if state.quantities[ref] < 0:
        state.mark_conflict(f"negative_quantity:{record.id}:{ref.id}")
        # Plan 2.8: a sell beyond a *confirmed* basis is rejected at commit; an
        # unknown basis is recorded, never pretended.
        if not basis_unknown:
            state.mark_deficit(record.id, ref)
        state.restricted.add(ref)
    cost = state.costs.get(ref)
    remaining: Decimal | None
    reason: str | None
    opened_on: date | None
    if fact.direction == "buy":
        if cost is None or cost.quantity == 0:
            # A buy from flat opens a new cycle (plan 2.7).
            opened_on = fact.occurred_at.astimezone(SHANGHAI_TZ).date()
            remaining = Decimal(0)
            reason = None
        else:
            opened_on = cost.opened_on
            remaining = cost.remaining
            reason = cost.reason
        if remaining is None:
            remaining, reason = None, reason or "prior_cost_unknown"
        elif fact.fee is None:
            remaining, reason = None, "buy_fee_unknown"
        else:
            remaining = remaining + fact.holding.quantity * fact.actual_price + fact.fee
            reason = None
        if basis_unknown:
            remaining, reason, opened_on = None, "quantity_basis_unknown", None
        state.costs[ref] = CostState(
            quantity=(cost.quantity if cost is not None else Decimal(0)) + fact.holding.quantity,
            remaining=remaining,
            opened_on=opened_on,
            reason=reason,
        )
    else:
        carried: Decimal | None = None
        opened_on = cost.opened_on if cost is not None else None
        cycle_opened_on = opened_on
        cycle_reason = cost.reason if cost is not None else None
        if cost is not None and cost.remaining is not None and cost.quantity > 0:
            # Carry the pre-sale average out of the sold portion; the remaining
            # average stays unchanged (plan 2.7).
            carried = cost.remaining * fact.holding.quantity / cost.quantity
            remaining = cost.remaining - carried if fact.holding.quantity < cost.quantity else Decimal(0)
            reason = None
        else:
            remaining, reason = None, cycle_reason or "sell_cost_basis_unknown"
        new_quantity = state.quantities[ref]
        if new_quantity == 0:
            # Cycle closed: remaining cost resets, cycle reference ends.
            remaining, reason, opened_on = Decimal(0), None, None
            if cycle_reason:
                state.cost_history.add(cycle_reason)
        state.costs[ref] = CostState(quantity=new_quantity, remaining=remaining, opened_on=opened_on, reason=reason)
        gross = fact.holding.quantity * fact.actual_price
        state.realized_records.append(
            RealizedPnL(
                event_id=record.id,
                listing_ref=ref,
                quantity=fact.holding.quantity,
                gross=gross,
                fee=fact.fee,
                cost_carried=carried,
                realized=(gross - fact.fee - carried) if (fact.fee is not None and carried is not None) else None,
                cycle_opened_on=cycle_opened_on,
            )
        )
    if state.cash is not None:
        state.cash -= direction * fact.holding.quantity * fact.actual_price
        if fact.fee is not None:
            state.cash -= fact.fee
    if fact.fee is None:
        issue = f"unknown_fee:{record.id}"
        state.mark_unknown(issue)
        state.history.add(issue)


def apply_cash(state: ProjectionState, record: EventRecord) -> None:
    """A cash transfer or dividend; dividends never reduce the acquisition cost."""

    fact = cast(CashFact, record.request.fact)
    if state.cash is not None:
        state.cash += fact.amount
    if fact.kind == "dividend":
        # Dividends never reduce the acquisition cost (plan 2.7).
        state.dividends.append(DividendRecord(event_id=record.id, listing_ref=fact.listing_ref, amount=fact.amount))


def apply_corporate_action(state: ProjectionState, record: EventRecord) -> None:
    """A split (quantity scales, total cost unchanged) or a pending action."""

    fact = cast(CorporateAction, record.request.fact)
    ref = fact.listing_ref
    if fact.action == "split":
        if ref not in state.quantities and state.coverage == "partial":
            state.restricted.add(ref)
        state.quantities[ref] = state.quantities.get(ref, Decimal(0)) * (fact.ratio or Decimal(1))
        cost = state.costs.get(ref)
        if cost is not None:
            # Quantity scales, total cost is unchanged, the cycle continues.
            state.costs[ref] = CostState(quantity=state.quantities[ref], remaining=cost.remaining, opened_on=cost.opened_on, reason=cost.reason)
    else:
        issue = f"pending_corporate_action:{record.id}"
        state.history.add(issue)
        if "quantity" in fact.affects:
            state.restricted.add(ref)
            state.quantities.setdefault(ref, Decimal(0))
            # An unconfirmed action keeps the cost unknown until the user
            # confirms the actual fact (plan 2.7).
            cost = state.costs.get(ref)
            if cost is not None and cost.reason:
                state.cost_history.add(cost.reason)
            state.costs[ref] = CostState(
                quantity=cost.quantity if cost is not None else state.quantities.get(ref, Decimal(0)),
                remaining=None,
                opened_on=cost.opened_on if cost is not None else None,
                reason="pending_corporate_action",
            )
        if "cash" in fact.affects:
            state.mark_unknown(issue)


HANDLERS: dict[type[Any], FactHandler] = {
    Balance: apply_balance,
    Trade: apply_trade,
    CashFact: apply_cash,
    CorporateAction: apply_corporate_action,
}
