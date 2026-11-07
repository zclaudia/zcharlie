"""Deterministic baselines compile fixed quantities before the opening event."""

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, localcontext

from investment_agent.simulation.contracts import (
    Decision,
    OrderIntent,
    Participant,
    RulePackage,
    SimulationAccount,
    SimulationRound,
)
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import ReservationTerms, fee_for, stable_id


def baseline_decision(
    participant: Participant,
    account: SimulationAccount,
    round_: SimulationRound,
    rules: RulePackage,
    terms: dict[int, ReservationTerms],
    prices: dict[int, Decimal],
    *,
    submitted_at: datetime,
    already_attempted: bool = False,
) -> Decision:
    if participant.config.kind not in {"cash", "equal_weight"}:
        raise SimulationError("invalid_config", "participant is not a deterministic baseline")
    if (
        participant.experiment_id != account.experiment_id
        or participant.id != account.participant_id
        or participant.account_id != account.account_id
        or round_.experiment_id != account.experiment_id
    ):
        raise SimulationError("scope_mismatch", "baseline participant, account and round differ")
    if any(
        not isinstance(price, Decimal) or not price.is_finite() or price <= 0
        for price in prices.values()
    ):
        raise SimulationError("incomplete_data", "baseline prices must be finite positive Decimal")
    orders = []
    if participant.config.kind == "equal_weight" and not already_attempted:
        with localcontext() as context:
            context.prec = 100
            allocation = (account.state.cash - account.state.reserved_cash) / len(
                round_.manifest.universe
            )
            for listing_id in sorted(round_.manifest.universe):
                term = terms.get(listing_id)
                price = prices.get(listing_id)
                if term is None or price is None:
                    raise SimulationError(
                        "incomplete_data", "baseline requires frozen prices and reservation rules"
                    )
                # Protection is derived from known close and frozen adverse slippage.
                protection = (
                    (price * (1 + rules.slippage.adverse_rate)) / term.tick_size
                ).to_integral_value(rounding="ROUND_CEILING") * term.tick_size
                cap = max(
                    Decimal(0),
                    min(
                        allocation,
                        (term.historical_turnover or Decimal(0))
                        * rules.max_historical_turnover_ratio,
                        (account.initial_cash - rules.fee_schedule.minimum_commission)
                        * rules.max_position_weight,
                    ),
                )
                lots = int(
                    (cap / protection / term.lot_size).to_integral_value(rounding=ROUND_FLOOR)
                )
                # Monotone fee schedules permit logarithmic search, including
                # large balances or extreme experimental rates.
                low, high = 0, lots
                while low < high:
                    middle = (low + high + 1) // 2
                    quantity = Decimal(middle * term.lot_size)
                    if (
                        quantity * protection + fee_for(quantity, protection, "buy", rules)
                        <= allocation
                    ):
                        low = middle
                    else:
                        high = middle - 1
                lots = low
                quantity = Decimal(lots * term.lot_size)
                if lots:
                    orders.append(
                        OrderIntent(
                            listing_id=listing_id,
                            side="buy",
                            quantity=quantity,
                            protection_price=protection,
                        )
                    )
    return Decision(
        experiment_id=account.experiment_id,
        participant_id=participant.id,
        account_id=account.account_id,
        id=stable_id("decision", participant.id, round_.id, "baseline"),
        round_id=round_.id,
        account_revision=account.revision,
        status="submitted",
        action="trade" if orders else "hold",
        orders=tuple(orders),
        rationale="deterministic equal-weight initial acquisition"
        if orders
        else "deterministic buy-and-hold / cash policy",
        submitted_at=submitted_at,
        idempotency_key=stable_id("baseline", participant.id, round_.id),
    )
