"""Validated immutable account projections and append-only event payloads.

Persistence must compare expected revision and deduplicate event/fill IDs in one
transaction. These functions never perform I/O or claim to own that transaction.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Self

from pydantic import model_validator

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.simulation.contracts import (
    AccountScope,
    AccountState,
    Identifier,
    LedgerEvent,
    NonnegativeMoney,
    PositionLot,
    PositiveInt,
    PositiveMoney,
    RulePackage,
    SimulationAccount,
    SimulationFill,
    SimulationOrder,
)
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import exact_decimal, fee_for, stable_id, validated_update

SCOPE = ("experiment_id", "participant_id", "account_id")


class SettlementResult(Contract):
    account: SimulationAccount
    order: SimulationOrder
    events: tuple[LedgerEvent, ...]


def _scope(account: SimulationAccount, other: AccountScope) -> None:
    if any(getattr(account, key) != getattr(other, key) for key in SCOPE):
        raise SimulationError("scope_mismatch", "ledger object belongs to another account")


def _release(state: AccountState, order: SimulationOrder) -> AccountState:
    if order.side == "buy":
        if state.reserved_cash < order.reserved_cash:
            raise SimulationError("reconciliation_failed", "cash reservation is missing")
        return validated_update(state, reserved_cash=state.reserved_cash - order.reserved_cash)
    remaining = order.reserved_quantity
    positions = []
    for reservation in state.reserved_positions:
        if reservation.listing_id == order.listing_id and remaining:
            consumed = min(remaining, reservation.quantity)
            remaining -= consumed
            if reservation.quantity > consumed:
                positions.append(
                    validated_update(reservation, quantity=reservation.quantity - consumed)
                )
        else:
            positions.append(reservation)
    if remaining:
        raise SimulationError("reconciliation_failed", "share reservation is missing")
    return validated_update(state, reserved_positions=tuple(positions))


@exact_decimal
def settle_order(
    account: SimulationAccount,
    order: SimulationOrder,
    fill: SimulationFill | None,
    *,
    rules: RulePackage,
    confirmed_at: datetime,
    source_ref: str,
    expected_revision: int,
    next_trading_day: date | None = None,
    event_sequence: int = 1,
    expiry_reason: str = "execution_opportunity_expired",
    cancelled: bool = False,
) -> SettlementResult:
    """Release reserves and apply a full fill, or release an expired/cancelled order.

    next_trading_day must come from the verified, versioned trading calendar;
    weekends are never guessed with a timedelta. It is required for buy fills.
    """
    confirmed_at = utc_timestamp(confirmed_at)
    _scope(account, order)
    if account.revision != expected_revision or account.revision < order.account_revision:
        raise SimulationError("revision_conflict", "unexpected settlement account revision")
    if order.status != "locked":
        raise SimulationError("invalid_transition", "order was already settled")
    if order.rule_version != rules.version:
        raise SimulationError("unsupported_rule", "order differs from frozen rule package")
    if cancelled and confirmed_at >= order.valid_from:
        raise SimulationError("invalid_transition", "cannot cancel an already occurred execution event")
    if not cancelled and confirmed_at < order.valid_from:
        raise SimulationError("invalid_transition", "cannot settle before economic execution event")
    state = _release(account.state, order)
    cash_delta = Decimal(0)
    quantity_delta = Decimal(0)
    if fill is not None:
        if cancelled:
            raise SimulationError("invalid_transition", "cannot cancel an existing economic fill")
        _scope(account, fill)
        fields = ("round_id", "listing_id", "side", "quantity", "rule_version")
        if fill.order_id != order.id or any(
            getattr(fill, field) != getattr(order, field) for field in fields
        ):
            raise SimulationError(
                "reconciliation_failed", "fill does not match entire locked order"
            )
        execution_key = stable_id("execution", order.id, order.execution_date, rules.version)
        if (fill.execution_key != execution_key or fill.sequence != 1
                or fill.id != stable_id("fill", execution_key, 1)):
            raise SimulationError("reconciliation_failed", "fill identity differs from deterministic execution")
        if fill.fee != fee_for(fill.quantity, fill.price, fill.side, rules):
            raise SimulationError("reconciliation_failed", "fill fee differs from frozen fee schedule")
        if fill.economic_at != order.valid_from or fill.confirmed_at > confirmed_at:
            raise SimulationError(
                "reconciliation_failed", "fill timestamps differ from execution proof"
            )
        if (order.side == "buy" and fill.price > order.protection_price) or (
            order.side == "sell" and fill.price < order.protection_price
        ):
            raise SimulationError("price_protection", "fill breaches protection price")
        notional = fill.price * fill.quantity
        lots = list(state.lots)
        if order.side == "buy":
            if next_trading_day is None or next_trading_day <= order.execution_date:
                raise SimulationError("incomplete_data", "buy requires next verified trading day")
            cash_delta = -notional - fill.fee
            if -cash_delta > order.reserved_cash:
                raise SimulationError("insufficient_cash", "buy fill exceeds its own reservation")
            quantity_delta = fill.quantity
            lots.append(
                PositionLot(
                    id=stable_id("lot", fill.id),
                    listing_id=fill.listing_id,
                    quantity=fill.quantity,
                    available_on=next_trading_day,
                    unit_cost=((notional + fill.fee) / fill.quantity).quantize(
                        Decimal("1e-18"), rounding=ROUND_HALF_UP
                    ),
                    acquired_on=order.execution_date,
                )
            )
        else:
            cash_delta = notional - fill.fee
            quantity_delta = -fill.quantity
            remaining = fill.quantity
            lots = []
            for lot in sorted(state.lots, key=lambda item: (item.acquired_on, item.id)):
                if (
                    lot.listing_id == fill.listing_id
                    and lot.available_on <= order.execution_date
                    and remaining
                ):
                    used = min(lot.quantity, remaining)
                    remaining -= used
                    if lot.quantity > used:
                        lots.append(validated_update(lot, quantity=lot.quantity - used))
                else:
                    lots.append(lot)
            if remaining:
                raise SimulationError("insufficient_sellable", "fill exceeds T+1 sellable lots")
        if state.cash + cash_delta < state.reserved_cash:
            raise SimulationError(
                "insufficient_cash", "fill consumes another order's reserved cash"
            )
        state = validated_update(state, cash=state.cash + cash_delta, lots=tuple(lots))
    new_account = validated_update(account, revision=account.revision + 1, state=state)
    status = "filled" if fill else "cancelled" if cancelled else "expired"
    settled_order = validated_update(
        order, status=status, rejection_reason=None if fill else expiry_reason
    )
    scope = {key: getattr(account, key) for key in SCOPE}
    event = LedgerEvent(
        **scope,
        id=stable_id("event", order.id, "settle"),
        sequence=event_sequence,
        account_revision=new_account.revision,
        idempotency_key=stable_id("settle", order.id),
        kind="fill" if fill else "release",
        economic_at=fill.economic_at if fill else min(confirmed_at, order.valid_from),
        confirmed_at=confirmed_at,
        cash_delta=cash_delta,
        quantity_delta=quantity_delta,
        listing_id=order.listing_id,
        fill_id=fill.id if fill else None,
        source_ref=fill.market_data_ref if fill else source_ref,
        state_after=state,
    )
    return SettlementResult(account=new_account, order=settled_order, events=(event,))


class CorporateAction(Contract):
    id: Identifier
    listing_id: PositiveInt
    kind: Literal["cash_entitlement", "cash_payment", "split", "unsupported"]
    economic_at: datetime
    confirmed_at: datetime
    source_ref: Identifier
    verified: bool
    amount: NonnegativeMoney = Decimal(0)
    split_ratio: PositiveMoney | None = None
    # A record-date entitlement amount is established by the verified historical
    # ownership projection, not inferred from current lots after intervening sales.
    entitlement_ref: Identifier | None = None

    @model_validator(mode="after")
    def supported_metadata(self) -> Self:
        if self.confirmed_at < self.economic_at:
            raise ValueError("corporate action confirmation precedes economic event")
        if self.kind == "split" and (self.split_ratio is None or self.amount):
            raise ValueError("split requires ratio and no cash amount")
        if self.kind in {"cash_entitlement", "cash_payment"} and (
            self.amount <= 0 or self.split_ratio is not None or self.entitlement_ref is None
        ):
            raise ValueError("cash actions require positive proven entitlement amount")
        return self


class CorporateActionResult(Contract):
    account: SimulationAccount
    event: LedgerEvent


@exact_decimal
def apply_corporate_action(
    account: SimulationAccount,
    action: CorporateAction,
    *,
    expected_revision: int,
    event_sequence: int = 1,
) -> CorporateActionResult:
    if account.revision != expected_revision:
        raise SimulationError("revision_conflict", "unexpected corporate action revision")
    if not action.verified or action.kind == "unsupported":
        raise SimulationError("settlement_blocked", "unverified or unsupported corporate action")
    state = account.state
    if state.reserved_cash or state.reserved_positions:
        raise SimulationError(
            "settlement_blocked", "cancel affected outstanding orders before corporate action"
        )
    cash_delta = receivable_delta = quantity_delta = Decimal(0)
    if action.kind == "cash_entitlement":
        receivable_delta = action.amount
        state = validated_update(state, receivables=state.receivables + action.amount)
    elif action.kind == "cash_payment":
        if action.amount > state.receivables:
            raise SimulationError("reconciliation_failed", "payment exceeds recorded receivables")
        cash_delta = action.amount
        receivable_delta = -action.amount
        state = validated_update(
            state, cash=state.cash + action.amount, receivables=state.receivables - action.amount
        )
    else:
        assert action.split_ratio is not None
        lots = []
        for lot in state.lots:
            if (lot.listing_id == action.listing_id
                    and lot.acquired_on < action.economic_at.astimezone(SHANGHAI_TZ).date()):
                quantity = lot.quantity * action.split_ratio
                if quantity != quantity.to_integral_value():
                    raise SimulationError("settlement_blocked", "fractional corporate-action shares need a verified allocation rule")
                unit_cost = (lot.unit_cost / action.split_ratio).quantize(
                    Decimal("1e-18"), rounding=ROUND_HALF_UP
                )
                quantity_delta += quantity - lot.quantity
                lots.append(validated_update(lot, quantity=quantity, unit_cost=unit_cost))
            else:
                lots.append(lot)
        state = validated_update(state, lots=tuple(lots))
    account_after = validated_update(account, revision=account.revision + 1, state=state)
    event = LedgerEvent(
        **{key: getattr(account, key) for key in SCOPE},
        id=stable_id("event", account.account_id, action.id),
        sequence=event_sequence,
        account_revision=account_after.revision,
        idempotency_key=stable_id("ca", account.account_id, action.id),
        kind="corporate_action",
        economic_at=action.economic_at,
        confirmed_at=action.confirmed_at,
        cash_delta=cash_delta,
        quantity_delta=quantity_delta,
        receivable_delta=receivable_delta,
        listing_id=action.listing_id,
        source_ref=action.source_ref,
        state_after=state,
    )
    return CorporateActionResult(account=account_after, event=event)
