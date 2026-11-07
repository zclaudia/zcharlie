"""Pure lock-time validation and reservations; no sale proceeds finance purchases."""

from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import wraps
from hashlib import sha256
from typing import Any, Self

from pydantic import BaseModel, model_validator

from investment_agent.investor.common import Contract
from investment_agent.simulation.contracts import (
    Decision,
    Identifier,
    NonnegativeMoney,
    PositiveInt,
    PositiveMoney,
    RulePackage,
    SimulationAccount,
    SimulationOrder,
    SimulationRound,
)
from investment_agent.simulation.errors import SimulationError

CENT = Decimal("0.01")


def exact_decimal[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with localcontext() as ctx:
            ctx.prec = 100
            return function(*args, **kwargs)

    return wrapped


def stable_id(kind: str, *parts: object) -> str:
    """Length-delimited inputs avoid ambiguous concatenation and replay randomness."""
    encoded = "".join(f"{len(str(p))}:{p}" for p in parts)
    return f"{kind}-{sha256(encoded.encode()).hexdigest()}"


def validated_update[T: BaseModel](model: T, **changes: Any) -> T:
    """model_copy(update=...) bypasses validation, including on frozen contracts."""
    return type(model).model_validate({**model.model_dump(), **changes})


class ReservationTerms(Contract):
    listing_id: PositiveInt
    trade_date: date
    rule_version: Identifier
    source_ref: Identifier
    verified: bool
    tick_size: PositiveMoney
    lot_size: PositiveInt
    known_at: datetime
    historical_turnover: NonnegativeMoney | None = None
    historical_available_at: datetime | None = None
    corporate_actions_complete: bool = False
    unsupported_corporate_actions: tuple[Identifier, ...] = ()
    security_scope: str = "cn_mainboard_common_a"

    @model_validator(mode="after")
    def capacity_metadata(self) -> Self:
        if (self.historical_turnover is None) != (self.historical_available_at is None):
            raise ValueError("historical capacity needs both value and availability proof")
        return self


class ExecutionTerms(ReservationTerms):
    """Later execution-session proof, never supplied to the deciding agent."""

    upper_limit: PositiveMoney
    lower_limit: PositiveMoney

    @model_validator(mode="after")
    def legal_limits(self) -> Self:
        if self.lower_limit > self.upper_limit:
            raise ValueError("lower limit exceeds upper limit")
        with localcontext() as ctx:
            ctx.prec = 100
            if self.lower_limit % self.tick_size or self.upper_limit % self.tick_size:
                raise ValueError("legal price limits must conform to tick")
        return self


class ReservationResult(Contract):
    account: SimulationAccount
    orders: tuple[SimulationOrder, ...]


def fee_for(quantity: Decimal, price: Decimal, side: str, rules: RulePackage) -> Decimal:
    """Experimental frozen schedule, component-wise nearest-cent half-up rounding."""
    if side not in {"buy", "sell"}:
        raise SimulationError("invalid_decision", "invalid fee side")
    if not all(isinstance(v, Decimal) and v.is_finite() and v > 0 for v in (quantity, price)):
        raise SimulationError(
            "invalid_decision", "fee quantity and price must be positive finite Decimal"
        )
    schedule = rules.fee_schedule
    with localcontext() as ctx:
        ctx.prec = 140
        notional = quantity * price
        commission = max(notional * schedule.commission_rate, schedule.minimum_commission)
        tax = notional * schedule.sell_tax_rate if side == "sell" else Decimal(0)
        transfer = notional * schedule.transfer_rate
        return sum(
            (v.quantize(CENT, rounding=ROUND_HALF_UP) for v in (commission, tax, transfer)),
            Decimal(0),
        )


def check_terms(terms: ExecutionTerms, listing_id: int, day: date, rules: RulePackage) -> None:
    if terms.listing_id != listing_id or terms.trade_date != day:
        raise SimulationError("scope_mismatch", "execution proof is for another listing or session")
    if terms.rule_version != rules.version or terms.security_scope != rules.security_scope:
        raise SimulationError(
            "unsupported_rule", "execution proof does not match frozen rule package"
        )
    if not terms.verified:
        raise SimulationError("incomplete_data", "unverified execution rules", retryable=True)
    if not terms.corporate_actions_complete or terms.unsupported_corporate_actions:
        raise SimulationError("settlement_blocked", "unknown or unsupported corporate action")


@exact_decimal
def reserve_decision(
    account: SimulationAccount,
    decision: Decision,
    round_: SimulationRound,
    rules: RulePackage,
    terms_by_listing: dict[int, ReservationTerms],
    *,
    position_prices: dict[int, Decimal] | None = None,
) -> ReservationResult:
    """Validate the entire fixed order set atomically or raise an explicit failure.

    position_prices are the frozen observation marks used for position-weight
    checks; every held listing needs one when a buy is being evaluated.
    """
    for field in ("experiment_id", "participant_id", "account_id"):
        if getattr(account, field) != getattr(decision, field):
            raise SimulationError("scope_mismatch", "decision/account scope differs")
    if round_.experiment_id != account.experiment_id or decision.round_id != round_.id:
        raise SimulationError("scope_mismatch", "decision/round scope differs")
    if decision.account_revision != account.revision:
        raise SimulationError("revision_conflict", "account changed since decision")
    if decision.status not in {"submitted", "locked"} or round_.status not in {
        "collecting_decisions",
        "locked",
    }:
        raise SimulationError("invalid_transition", "only a submitted decision can lock")
    if decision.submitted_at is None or decision.submitted_at > round_.decision_deadline:
        raise SimulationError("deadline_passed", "decision arrived after execution deadline")
    if decision.submitted_at < round_.cutoff:
        raise SimulationError(
            "future_evidence", "decision submission precedes frozen observation cutoff"
        )
    if not round_.manifest.complete:
        raise SimulationError("incomplete_data", "common manifest is incomplete")
    state = account.state
    cash_reserve = state.reserved_cash
    positions = list(state.reserved_positions)
    orders = []
    prices = position_prices or {}
    if any(o.side == "buy" for o in decision.orders):
        if any(lot.listing_id not in prices for lot in state.lots):
            raise SimulationError(
                "incomplete_data", "position controls require frozen holding marks"
            )
        if any(not isinstance(p, Decimal) or not p.is_finite() or p <= 0 for p in prices.values()):
            raise SimulationError("incomplete_data", "invalid frozen position mark")
    equity = (
        state.cash
        + state.receivables
        - state.payables
        + sum(
            (lot.quantity * prices.get(lot.listing_id, Decimal(0)) for lot in state.lots),
            Decimal(0),
        )
    )
    scope = {
        field: getattr(account, field)
        for field in ("experiment_id", "participant_id", "account_id")
    }
    projected_equity = equity - sum(
        (
            fee_for(o.quantity, o.protection_price, "buy", rules)
            for o in decision.orders
            if o.side == "buy"
        ),
        Decimal(0),
    )
    for sequence, intent in enumerate(decision.orders, 1):
        if intent.listing_id not in round_.manifest.universe:
            raise SimulationError("unsupported_security", "listing is outside the frozen universe")
        terms = terms_by_listing.get(intent.listing_id)
        if terms is None:
            raise SimulationError("incomplete_data", "missing execution rules")
        if terms.listing_id != intent.listing_id or terms.trade_date != round_.execution_date:
            raise SimulationError(
                "scope_mismatch", "reservation rules cover another listing/session"
            )
        if terms.rule_version != rules.version or terms.security_scope != rules.security_scope:
            raise SimulationError(
                "unsupported_rule", "reservation rules differ from frozen package"
            )
        if not terms.verified or terms.known_at > round_.cutoff:
            raise SimulationError(
                "incomplete_data", "reservation rules were not verified at cutoff"
            )
        if not terms.corporate_actions_complete or terms.unsupported_corporate_actions:
            raise SimulationError("settlement_blocked", "unknown or unsupported corporate action")
        if intent.protection_price % terms.tick_size:
            raise SimulationError("invalid_decision", "protection price does not conform to tick")
        if intent.quantity != intent.quantity.to_integral_value():
            raise SimulationError("invalid_decision", "share quantity must be integral")
        if (terms.historical_turnover is None or terms.historical_available_at is None
                or terms.historical_available_at > round_.cutoff):
            raise SimulationError(
                "incomplete_data", "capacity proof must be known at observation cutoff"
            )
        notional = intent.quantity * intent.protection_price
        if notional > terms.historical_turnover * rules.max_historical_turnover_ratio:
            raise SimulationError("capacity_exceeded", "order exceeds frozen historical capacity")
        reserved_cash = Decimal(0)
        reserved_quantity = Decimal(0)
        held = sum(
            (lot.quantity for lot in state.lots if lot.listing_id == intent.listing_id), Decimal(0)
        )
        if intent.side == "buy":
            if intent.quantity % terms.lot_size:
                raise SimulationError("invalid_decision", "buy quantity does not conform to lot")
            reserved_cash = notional + fee_for(
                intent.quantity, intent.protection_price, "buy", rules
            )
            cash_reserve += reserved_cash
            if cash_reserve > state.cash:
                raise SimulationError(
                    "insufficient_cash", "protected notional and fee exceed available cash"
                )
            if state.reserved_cash:
                raise SimulationError(
                    "invalid_transition", "settle outstanding buy reservations before a new decision"
                )
            if (
                projected_equity <= 0
                or (held * prices.get(intent.listing_id, intent.protection_price) + notional)
                > projected_equity * rules.max_position_weight
            ):
                raise SimulationError("invalid_decision", "buy exceeds maximum position weight")
        else:
            sellable = sum(
                (
                    lot.quantity
                    for lot in state.lots
                    if lot.listing_id == intent.listing_id
                    and lot.available_on <= round_.execution_date
                ),
                Decimal(0),
            )
            already = sum(
                (o.quantity for o in positions if o.listing_id == intent.listing_id), Decimal(0)
            )
            if intent.quantity > sellable - already:
                raise SimulationError(
                    "insufficient_sellable", "sell exceeds unreserved T+1 sellable quantity"
                )
            if intent.quantity % terms.lot_size and intent.quantity != held - already:
                raise SimulationError(
                    "invalid_decision", "odd lot sell must liquidate remaining holding"
                )
            reserved_quantity = intent.quantity
            positions.append(intent)
        order_id = stable_id("order", decision.id, sequence)
        orders.append(
            SimulationOrder(
                **scope,
                id=order_id,
                round_id=round_.id,
                decision_id=decision.id,
                sequence=sequence,
                account_revision=account.revision + 1,
                listing_id=intent.listing_id,
                side=intent.side,
                quantity=intent.quantity,
                protection_price=intent.protection_price,
                execution_date=round_.execution_date,
                valid_from=round_.execution_at,
                valid_until=round_.execution_at,
                reserved_cash=reserved_cash,
                reserved_quantity=reserved_quantity,
                rule_version=rules.version,
                idempotency_key=stable_id("lock", decision.id, sequence),
            )
        )
    new_state = validated_update(
        state, reserved_cash=cash_reserve, reserved_positions=tuple(positions)
    )
    return ReservationResult(
        account=validated_update(
            account, revision=account.revision + bool(orders), state=new_state
        ),
        orders=tuple(orders),
    )
