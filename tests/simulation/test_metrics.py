from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.metrics import (
    ValuationMark,
    cumulative_return,
    maximum_drawdown,
    value_account,
)
from investment_agent.simulation.risk import validated_update
from tests.simulation.test_ledger import bought
from tests.simulation.test_risk import CONFIRMED, account, lot, rules


def mark(**changes):
    return ValuationMark.model_validate(
        dict(
            listing_id=1,
            price="11",
            price_date=CONFIRMED.date(),
            source_ref="close1",
            known_at=CONFIRMED,
            verified=True,
        )
        | changes
    )


def test_hand_calculated_fixture_and_reserves_are_equity():
    buy = bought()
    nav = value_account(buy.account, CONFIRMED.date(), (mark(),), rules(), created_at=CONFIRMED)
    assert nav.equity == Decimal(100095)
    assert nav.position_value == Decimal(1100)
    assert nav.cumulative_return == Decimal("0.00095")
    assert nav.max_drawdown == 0
    assert nav.quality == "complete"
    assert nav.price_refs == ("close1",)
    assert nav == value_account(
        buy.account, CONFIRMED.date(), (mark(),), rules(), created_at=CONFIRMED
    )


def test_empty_portfolio_and_reserves_do_not_reduce_nav():
    a = account(reserved_cash="100")
    nav = value_account(a, CONFIRMED.date(), (), rules(), created_at=CONFIRMED)
    assert nav.equity == Decimal(100000) and nav.cumulative_return == 0
    assert nav.quality == "complete" and nav.max_drawdown == 0


@pytest.mark.parametrize(
    "marks,reasons",
    [
        ((), ()),
        ((mark(verified=False),), ()),
        ((mark(known_at=CONFIRMED + timedelta(seconds=1)),), ()),
        ((mark(price_date=CONFIRMED.date() - timedelta(days=1)),), ()),
        ((mark(),), ("pending_unknown_corporate_action",)),
    ],
)
def test_missing_data_never_publishes_formal_equity_or_metrics(marks, reasons):
    nav = value_account(
        account(lots=(lot(),)),
        CONFIRMED.date(),
        marks,
        rules(),
        created_at=CONFIRMED,
        blocking_reasons=reasons,
    )
    assert nav.quality == "incomplete"
    assert nav.equity is nav.cumulative_return is nav.max_drawdown is None
    assert nav.quality_reasons


def test_stale_suspension_price_explains_nav_but_no_formal_metrics():
    nav = value_account(
        account(lots=(lot(),)),
        CONFIRMED.date(),
        (mark(price_date=date(2026, 11, 6), stale_reason="suspension"),),
        rules(),
        created_at=CONFIRMED,
    )
    assert nav.quality == "stale" and nav.equity == Decimal(101100)
    assert nav.cumulative_return is nav.max_drawdown is None


def test_drawdown_uses_initial_anchor_and_historical_peak():
    assert maximum_drawdown(tuple(map(Decimal, (100, 120, 90, 110)))) == Decimal("0.25")
    assert cumulative_return(Decimal(110), Decimal(100)) == Decimal("0.1")
    initial = value_account(
        account(lots=(lot(),)), CONFIRMED.date(), (mark(price="12"),), rules(), created_at=CONFIRMED
    )
    next_date = CONFIRMED.date() + timedelta(days=1)
    nav = value_account(
        account(lots=(lot(),)),
        next_date,
        (mark(price="9", price_date=next_date, known_at=CONFIRMED + timedelta(days=1)),),
        rules(),
        created_at=CONFIRMED + timedelta(days=1),
        history=(initial,),
    )
    assert nav.max_drawdown == Decimal("0.002964426877470356")


def test_history_scope_versions_and_order_are_checked():
    n = value_account(account(), CONFIRMED.date(), (), rules(), created_at=CONFIRMED)
    with pytest.raises(SimulationError, match="another account"):
        value_account(
            account(),
            CONFIRMED.date() + timedelta(days=1),
            (),
            rules(),
            created_at=CONFIRMED,
            history=(validated_update(n, account_id="other"),),
        )
    with pytest.raises(SimulationError, match="increasing"):
        value_account(account(), CONFIRMED.date(), (), rules(), created_at=CONFIRMED, history=(n,))


def test_high_precision_cash_not_rounded_by_ambient_context():
    state = validated_update(
        account().state, cash=Decimal("1000000000000000000000.000000000000000001")
    )
    a = validated_update(account(), initial_cash=state.cash, state=state)
    with localcontext() as ctx:
        ctx.prec = 6
        nav = value_account(a, CONFIRMED.date(), (), rules(), created_at=CONFIRMED)
    assert nav.equity == state.cash
    assert nav.cumulative_return == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", 0.1])
def test_invalid_marks_rejected(value):
    with pytest.raises(ValidationError):
        mark(price=value)


def test_nav_cannot_use_future_dates_history_or_preclose_price_evidence():
    with pytest.raises(SimulationError, match="valuation-day close"):
        value_account(account(), CONFIRMED.date() + timedelta(days=1), (), rules(),
                      created_at=CONFIRMED)
    previous = value_account(account(), CONFIRMED.date(), (), rules(), created_at=CONFIRMED)
    future_history = validated_update(previous, created_at=CONFIRMED + timedelta(days=3))
    with pytest.raises(SimulationError, match="history was not available"):
        value_account(account(), CONFIRMED.date() + timedelta(days=1), (), rules(),
            created_at=CONFIRMED + timedelta(days=1), history=(future_history,))
    result = value_account(account(lots=(lot(),)), CONFIRMED.date(),
        (mark(known_at=CONFIRMED - timedelta(hours=12)),), rules(), created_at=CONFIRMED)
    assert result.quality == "incomplete" and result.equity is None
