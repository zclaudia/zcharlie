from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from investment_agent.simulation.contracts import (
    AccountState,
    DataManifest,
    Decision,
    OrderIntent,
    PositionLot,
    RulePackage,
    SimulationAccount,
    SimulationRound,
)
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import (
    ExecutionTerms,
    ReservationTerms,
    fee_for,
    reserve_decision,
    validated_update,
)

D = Decimal
SCOPE = dict(experiment_id="e", participant_id="p", account_id="a")
CUTOFF = datetime(2026, 11, 6, 10, tzinfo=UTC)  # Friday after close
OPEN = datetime(2026, 11, 9, 1, 30, tzinfo=UTC)  # Monday
CONFIRMED = datetime(2026, 11, 9, 10, tzinfo=UTC)


def rules(**changes):
    return RulePackage.model_validate(
        dict(
            version="r1",
            content_hash="a" * 64,
            calendar_version="cal1",
            fee_schedule=dict(
                version="fee1", commission_rate="0", minimum_commission="5", sell_tax_rate="0"
            ),
            slippage=dict(version="slip1", adverse_rate="0"),
        )
        | changes
    )


def account(**state):
    return SimulationAccount(
        **SCOPE, initial_cash=D("100000"), state=AccountState(cash=D("100000"), **state)
    )


def round_():
    return SimulationRound(
        id="round1",
        experiment_id="e",
        observation_date=date(2026, 11, 6),
        cutoff=CUTOFF,
        decision_deadline=OPEN - timedelta(hours=1),
        execution_date=OPEN.date(),
        execution_at=OPEN,
        status="collecting_decisions",
        manifest=DataManifest(
            version="m1",
            content_hash="b" * 64,
            cutoff=CUTOFF,
            universe=(1, 2),
            artifact_path="fixture",
            complete=True,
        ),
    )


def decision(orders=None, **changes):
    return Decision.model_validate(
        dict(
            **SCOPE,
            id="decision1",
            round_id="round1",
            account_revision=0,
            status="submitted",
            action="trade",
            orders=orders or [intent()],
            rationale="fixture",
            submitted_at=CUTOFF,
            idempotency_key="decision-key",
        )
        | changes
    )


def intent(**changes):
    return OrderIntent.model_validate(
        dict(listing_id=1, side="buy", quantity="100", protection_price="10") | changes
    )


def terms(**changes):
    return ReservationTerms.model_validate(
        dict(
            listing_id=1,
            trade_date=OPEN.date(),
            rule_version="r1",
            source_ref="rules-proof",
            verified=True,
            corporate_actions_complete=True,
            tick_size="0.01",
            lot_size=100,
            known_at=CUTOFF,
            historical_turnover="10000000",
            historical_available_at=CUTOFF,
        )
        | changes
    )


def execution_terms(**changes):
    return ExecutionTerms.model_validate(
        terms().model_dump() | dict(upper_limit="11", lower_limit="9", known_at=CONFIRMED) | changes
    )


def locked():
    return reserve_decision(account(), decision(), round_(), rules(), {1: terms()})


def test_unknown_corporate_actions_and_existing_buy_reserves_cannot_lock():
    for proof in (terms(corporate_actions_complete=False),
                  terms(unsupported_corporate_actions=("rights",))):
        with pytest.raises(SimulationError, match="corporate action"):
            reserve_decision(account(), decision(), round_(), rules(), {1: proof})
    reserved = locked()
    with pytest.raises(SimulationError, match="outstanding buy"):
        reserve_decision(reserved.account, decision(account_revision=1, id="second"),
                         round_(), rules(), {1: terms()})


def lot(**changes):
    return PositionLot.model_validate(
        dict(
            id="lot1",
            listing_id=1,
            quantity="100",
            available_on=OPEN.date(),
            unit_cost="10",
            acquired_on=date(2026, 11, 6),
        )
        | changes
    )


def test_buy_reserves_protection_notional_and_fee_replay():
    result = locked()
    assert result.account.state.cash == D("100000")
    assert result.account.state.reserved_cash == D("1005")
    assert result == locked()
    assert result.orders[0].reserved_cash == D("1005")
    assert result.orders[0].account_revision == result.account.revision == 1
    # Actual Monday execution proof need not be available Friday.
    assert execution_terms().known_at > round_().cutoff


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"historical_turnover": None, "historical_available_at": None}, "incomplete_data"),
        ({"historical_available_at": CUTOFF + timedelta(seconds=1)}, "incomplete_data"),
        ({"historical_turnover": "99999"}, "capacity_exceeded"),
        ({"known_at": CONFIRMED}, "incomplete_data"),
        ({"verified": False}, "incomplete_data"),
        ({"rule_version": "other"}, "unsupported_rule"),
        ({"listing_id": 2}, "scope_mismatch"),
    ],
)
def test_reservation_rejects_invalid_or_future_rules(patch, code):
    with pytest.raises(SimulationError) as exc:
        reserve_decision(account(), decision(), round_(), rules(), {1: terms(**patch)})
    assert exc.value.code == code


@pytest.mark.parametrize(
    "patch", [{"quantity": "101"}, {"quantity": "100.5"}, {"protection_price": "10.001"}]
)
def test_lot_tick_quantity_conformity(patch):
    with pytest.raises(SimulationError):
        reserve_decision(account(), decision([intent(**patch)]), round_(), rules(), {1: terms()})


def test_fees_prevent_full_notional_spend():
    a = validated_update(account(), state=AccountState(cash="1000"))
    with pytest.raises(SimulationError, match="fee"):
        reserve_decision(a, decision(), round_(), rules(), {1: terms()})


def test_same_session_sale_does_not_finance_buy():
    a = validated_update(account(lots=(lot(),)), state=AccountState(cash="5", lots=(lot(),)))
    d = decision([intent(side="sell"), intent(listing_id=2)])
    with pytest.raises(SimulationError) as exc:
        reserve_decision(
            a,
            d,
            round_(),
            rules(),
            {1: terms(), 2: terms(listing_id=2)},
            position_prices={1: D("10")},
        )
    assert exc.value.code == "insufficient_cash"
    assert a.state.cash == D("5") and not a.state.reserved_positions


def test_t_plus_one_and_existing_reservation():
    a = account(lots=(lot(available_on=date(2026, 11, 10)),))
    with pytest.raises(SimulationError, match="T\\+1"):
        reserve_decision(a, decision([intent(side="sell")]), round_(), rules(), {1: terms()})
    a = account(lots=(lot(),), reserved_positions=(intent(side="sell"),))
    with pytest.raises(SimulationError):
        reserve_decision(a, decision([intent(side="sell")]), round_(), rules(), {1: terms()})


def test_passive_overweight_holding_can_sell_but_not_add():
    a = account(lots=(lot(quantity="20000"),))
    constrained = rules(max_position_weight="0.5")
    with pytest.raises(SimulationError, match="weight"):
        reserve_decision(
            a, decision(), round_(), constrained, {1: terms()}, position_prices={1: D("10")}
        )
    result = reserve_decision(
        a, decision([intent(side="sell")]), round_(), constrained, {1: terms()}
    )
    assert result.orders[0].side == "sell"


def test_frozen_model_update_revalidates_and_duplicate_lots_fail():
    with pytest.raises(ValidationError):
        validated_update(account().state, cash=D("-1"))
    with pytest.raises(ValidationError, match="duplicate lot"):
        account(lots=(lot(), lot()))


def test_fee_rounding_components_and_decimal_context_independence():
    r = rules(
        fee_schedule=dict(
            version="fees",
            commission_rate="0.000005",
            minimum_commission="0",
            sell_tax_rate="0.000005",
            transfer_rate="0.000005",
        )
    )
    assert fee_for(D(100), D(10), "sell", r) == D("0.03")
    with localcontext() as ctx:
        ctx.prec = 6
        assert fee_for(D("1000000000000000000000"), D("0.01"), "buy", rules()) == D("5.00")
    for bad in (D("NaN"), D("Infinity"), D(-1), 1.0):
        with pytest.raises(SimulationError):
            fee_for(bad, D(10), "buy", rules())


def test_scope_and_revision_checks():
    for patch, code in [
        ({"account_id": "other"}, "scope_mismatch"),
        ({"account_revision": 1}, "revision_conflict"),
    ]:
        with pytest.raises(SimulationError) as exc:
            reserve_decision(account(), decision(**patch), round_(), rules(), {1: terms()})
        assert exc.value.code == code


def test_order_must_be_in_frozen_universe_and_submission_after_cutoff():
    with pytest.raises(SimulationError) as exc:
        reserve_decision(
            account(), decision([intent(listing_id=3)]), round_(), rules(), {3: terms(listing_id=3)}
        )
    assert exc.value.code == "unsupported_security"
    with pytest.raises(SimulationError) as exc:
        reserve_decision(
            account(),
            decision(submitted_at=CUTOFF - timedelta(seconds=1)),
            round_(),
            rules(),
            {1: terms()},
        )
    assert exc.value.code == "future_evidence"


def test_reservation_respects_fees_in_position_weight_and_prior_frozen_cash():
    with pytest.raises(SimulationError, match="weight"):
        reserve_decision(
            account(),
            decision([intent(quantity="5000")]),
            round_(),
            rules(max_position_weight="0.5"),
            {1: terms()},
        )
    a = account(reserved_cash="99000")
    with pytest.raises(SimulationError) as exc:
        reserve_decision(a, decision(), round_(), rules(), {1: terms()})
    assert exc.value.code == "insufficient_cash"
