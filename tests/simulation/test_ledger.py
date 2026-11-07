from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_agent.simulation.contracts import AccountState
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.ledger import CorporateAction, apply_corporate_action, settle_order
from investment_agent.simulation.risk import fee_for, reserve_decision, stable_id, validated_update
from tests.simulation.test_execution import match
from tests.simulation.test_risk import (
    CONFIRMED,
    OPEN,
    account,
    decision,
    intent,
    locked,
    lot,
    round_,
    rules,
    terms,
)


def bought():
    reservation = locked()
    return settle_order(
        reservation.account,
        reservation.orders[0],
        match().fill,
        rules=rules(),
        confirmed_at=CONFIRMED,
        source_ref="bar1",
        expected_revision=1,
        next_trading_day=date(2026, 11, 10),
    )


def test_buy_fixture_then_sell_next_verified_day():
    buy = bought()
    assert buy.account.state.cash == Decimal("98995")
    assert buy.account.state.reserved_cash == 0
    assert buy.account.state.lots[0].quantity == Decimal(100)
    assert buy.account.state.lots[0].available_on == date(2026, 11, 10)
    assert buy.events[0].cash_delta == Decimal(-1005)
    assert buy.events[0].quantity_delta == Decimal(100)
    assert buy == bought()
    # Calendar maps Monday acquisition to Tuesday. The prior Friday round maps
    # to Monday explicitly; no calendar-day arithmetic is used by the engine.
    tuesday = OPEN + timedelta(days=1)
    r = validated_update(
        round_(),
        id="round2",
        observation_date=OPEN.date(),
        cutoff=CONFIRMED,
        decision_deadline=tuesday - timedelta(hours=1),
        execution_date=tuesday.date(),
        execution_at=tuesday,
        manifest=validated_update(round_().manifest, cutoff=CONFIRMED),
    )
    d = decision(
        [intent(side="sell", protection_price="11")],
        id="decision2",
        round_id="round2",
        account_revision=2,
        submitted_at=CONFIRMED,
    )
    reserved = reserve_decision(
        buy.account,
        d,
        r,
        rules(),
        {
            1: terms(
                trade_date=tuesday.date(), known_at=CONFIRMED, historical_available_at=CONFIRMED
            )
        },
    )
    o = reserved.orders[0]
    fill = validated_update(
        match().fill,
        id=stable_id("fill", stable_id("execution", o.id, o.execution_date, "r1"), 1),
        execution_key=stable_id("execution", o.id, o.execution_date, "r1"),
        order_id=o.id,
        round_id=o.round_id,
        side="sell",
        price=Decimal(11),
        economic_at=tuesday,
        confirmed_at=tuesday + timedelta(hours=9),
    )
    sold = settle_order(
        reserved.account,
        o,
        fill,
        rules=rules(),
        confirmed_at=fill.confirmed_at,
        source_ref="bar2",
        expected_revision=3,
    )
    assert sold.account.state.cash == Decimal("100090")
    assert not sold.account.state.lots and not sold.account.state.reserved_positions


def test_expiry_and_cancel_release_exact_reservations():
    reserved = locked()
    result = settle_order(
        reserved.account,
        reserved.orders[0],
        None,
        rules=rules(),
        confirmed_at=CONFIRMED,
        source_ref="bar1",
        expected_revision=1,
    )
    assert result.order.status == "expired"
    assert result.account.state.cash == Decimal(100000)
    assert result.account.state.reserved_cash == 0
    assert result.events[0].kind == "release"
    cancelled = settle_order(
        reserved.account,
        reserved.orders[0],
        None,
        rules=rules(),
        confirmed_at=OPEN - timedelta(hours=1),
        source_ref="pause",
        expected_revision=1,
        cancelled=True,
    )
    assert cancelled.order.status == "cancelled"


def test_t_plus_one_requires_verified_next_trading_date_and_no_repeat_settlement():
    reserved = locked()
    with pytest.raises(SimulationError, match="next verified"):
        settle_order(
            reserved.account,
            reserved.orders[0],
            match().fill,
            rules=rules(),
        confirmed_at=CONFIRMED,
            source_ref="bar1",
            expected_revision=1,
        )
    with pytest.raises(SimulationError, match="next verified"):
        settle_order(
            reserved.account,
            reserved.orders[0],
            match().fill,
            rules=rules(),
        confirmed_at=CONFIRMED,
            source_ref="bar1",
            expected_revision=1,
            next_trading_day=OPEN.date(),
        )
    result = bought()
    with pytest.raises(SimulationError, match="already settled"):
        settle_order(
            result.account,
            result.order,
            match().fill,
            rules=rules(),
        confirmed_at=CONFIRMED,
            source_ref="bar1",
            expected_revision=2,
            next_trading_day=date(2026, 11, 10),
        )


def test_fill_cannot_change_quantity_scope_rule_or_protection():
    reserved = locked()
    for patch in (
        {"quantity": Decimal(200)},
        {"account_id": "other"},
        {"rule_version": "other"},
        {"price": Decimal(11)},
    ):
        fill = validated_update(match().fill, **patch)
        with pytest.raises(SimulationError):
            settle_order(
                reserved.account,
                reserved.orders[0],
                fill,
                rules=rules(),
        confirmed_at=CONFIRMED,
                source_ref="bar1",
                expected_revision=1,
                next_trading_day=date(2026, 11, 10),
            )


def ca(**changes):
    return CorporateAction.model_validate(
        dict(
            id="div1",
            listing_id=1,
            kind="cash_entitlement",
            economic_at=CONFIRMED,
        confirmed_at=CONFIRMED,
            source_ref="ca-proof",
            verified=True,
            amount="100",
            entitlement_ref="record-date-proof",
        )
        | changes
    )


def test_dividend_receivable_then_payment_not_spendable_until_paid():
    original = account(lots=(lot(),))
    accrued = apply_corporate_action(original, ca(), expected_revision=0)
    assert accrued.account.state.cash == Decimal(100000)
    assert accrued.account.state.receivables == Decimal(100)
    paid = apply_corporate_action(
        accrued.account, ca(id="pay1", kind="cash_payment"), expected_revision=1
    )
    assert paid.account.state.cash == Decimal(100100)
    assert paid.account.state.receivables == 0
    assert paid.event.cash_delta == Decimal(100)
    assert paid.event.receivable_delta == Decimal(-100)
    with pytest.raises(SimulationError, match="exceeds"):
        apply_corporate_action(
            paid.account, ca(id="overpay", kind="cash_payment"), expected_revision=2
        )


def test_split_preserves_cost_and_sellability_unknown_action_blocks():
    original = account(lots=(lot(),))
    action = ca(kind="split", amount="0", split_ratio="2", entitlement_ref=None)
    result = apply_corporate_action(original, action, expected_revision=0)
    adjusted_lot = result.account.state.lots[0]
    assert adjusted_lot.quantity == Decimal(200) and adjusted_lot.unit_cost == Decimal(5)
    assert adjusted_lot.available_on == original.state.lots[0].available_on
    assert result.event.quantity_delta == Decimal(100)
    for action in (ca(verified=False), ca(kind="unsupported")):
        with pytest.raises(SimulationError, match="unsupported"):
            apply_corporate_action(original, action, expected_revision=0)
    with pytest.raises(SimulationError, match="cancel"):
        apply_corporate_action(locked().account, ca(), expected_revision=1)


def test_cash_and_share_invariants_and_action_metadata():
    with pytest.raises(ValidationError):
        AccountState(cash="1", reserved_cash="2")
    with pytest.raises(ValidationError):
        ca(entitlement_ref=None)
    with pytest.raises(ValidationError):
        ca(kind="split", amount="0", split_ratio=None)


@pytest.mark.parametrize("patch", [
    {"id": "another-fill"}, {"execution_key": "another-execution"}, {"sequence": 2},
    {"fee": Decimal("0")},
])
def test_settlement_rejects_forged_identity_or_fee_without_mutating_reserves(patch):
    reserved = locked()
    before = reserved.account.model_dump_json()
    with pytest.raises(SimulationError, match="identity|fee"):
        settle_order(reserved.account, reserved.orders[0], validated_update(match().fill, **patch),
            rules=rules(), confirmed_at=CONFIRMED, source_ref="bar1", expected_revision=1,
            next_trading_day=date(2026, 11, 10))
    assert reserved.account.model_dump_json() == before


def test_settlement_enforces_nonzero_sell_tax_and_cannot_cancel_after_open():
    frozen_rules = rules(fee_schedule={"version": "taxed", "commission_rate": "0",
        "minimum_commission": "5", "sell_tax_rate": "0.0005"})
    reserved = reserve_decision(account(lots=(lot(),)), decision([intent(side="sell")]),
        round_(), frozen_rules, {1: terms()})
    order = reserved.orders[0]
    from investment_agent.simulation.execution import match_daily_open
    from tests.simulation.test_execution import bar
    from tests.simulation.test_risk import execution_terms

    fill = match_daily_open(order, bar(), execution_terms(), frozen_rules, confirmed_at=CONFIRMED).fill
    assert fill.fee == fee_for(Decimal(100), Decimal(10), "sell", frozen_rules) == Decimal("5.50")
    with pytest.raises(SimulationError, match="fee"):
        settle_order(reserved.account, order, validated_update(fill, fee=Decimal(5)),
            rules=frozen_rules, confirmed_at=CONFIRMED, source_ref="bar1", expected_revision=1)
    settled = settle_order(reserved.account, order, fill, rules=frozen_rules,
        confirmed_at=CONFIRMED, source_ref="bar1", expected_revision=1)
    assert settled.account.state.cash == Decimal("100994.50")
    with pytest.raises(SimulationError, match="already occurred"):
        settle_order(reserved.account, order, None, rules=frozen_rules,
            confirmed_at=OPEN, source_ref="pause", expected_revision=1, cancelled=True)


def test_split_does_not_rewrite_ex_date_purchases_or_guess_fractional_allocation():
    same_day = lot(acquired_on=CONFIRMED.date(), available_on=CONFIRMED.date() + timedelta(days=1))
    original = account(lots=(same_day,))
    action = ca(kind="split", amount="0", split_ratio="2", entitlement_ref=None)
    result = apply_corporate_action(original, action, expected_revision=0)
    assert result.account.state.lots == original.state.lots
    assert result.event.quantity_delta == 0
    with pytest.raises(SimulationError, match="fractional"):
        apply_corporate_action(account(lots=(lot(quantity="101"),)),
            ca(kind="split", amount="0", split_ratio="1.1", entitlement_ref=None), expected_revision=0)
