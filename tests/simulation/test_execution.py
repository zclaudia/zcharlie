from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.execution import ExecutionBar, match_daily_open
from investment_agent.simulation.risk import validated_update
from tests.simulation.test_risk import CONFIRMED, OPEN, execution_terms, locked, rules


def bar(**changes):
    return ExecutionBar.model_validate(
        dict(
            listing_id=1,
            trade_date=OPEN.date(),
            open="10",
            volume="10000",
            suspended=False,
            source_ref="bar1",
            confirmed_at=CONFIRMED,
            verified=True,
        )
        | changes
    )


def match(**changes):
    args = dict(
        order=locked().orders[0],
        bar=bar(),
        terms=execution_terms(),
        rules=rules(),
        confirmed_at=CONFIRMED,
    )
    return match_daily_open(**(args | changes))


def test_ordinary_full_fill_replay_and_provenance():
    result = match()
    assert result.status == "filled"
    assert result.fill.quantity == Decimal(100)
    assert result.fill.price == Decimal(10)
    assert result.fill.fee == Decimal(5)
    assert result.fill.market_data_ref == "bar1"
    assert result.fill.rule_version == "r1"
    assert result == match()


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"suspended": True}, "suspended_or_zero_volume"),
        ({"volume": "0"}, "suspended_or_zero_volume"),
        ({"open": "11"}, "adverse_limit_open"),
        ({"open": "10.01"}, "price_protection"),
    ],
)
def test_conservative_expiry(patch, reason):
    result = match(bar=bar(**patch))
    assert result.status == "expired" and result.fill is None and result.reason == reason


@pytest.mark.parametrize(
    "changes",
    [
        {"bar": None},
        {"terms": None},
        {"bar": bar(open=None)},
        {"bar": bar(volume=None)},
        {"bar": bar(suspended=None)},
        {"bar": bar(verified=False)},
        {"terms": execution_terms(verified=False)},
        {"confirmed_at": CONFIRMED - timedelta(seconds=1)},
    ],
)
def test_missing_or_unavailable_proof_waits(changes):
    assert match(**changes).status == "awaiting_data"


def test_unknown_corporate_action_and_invalid_price_block():
    assert match(terms=execution_terms(corporate_actions_complete=False)).status == "blocked"
    assert (
        match(terms=execution_terms(unsupported_corporate_actions=("merger",))).status == "blocked"
    )
    assert match(bar=bar(open="10.001")).status == "blocked"


def test_adverse_tick_rounding_buy_and_sell():
    o = validated_update(
        locked().orders[0], protection_price=Decimal("10.02"), reserved_cash=Decimal("1007")
    )
    result = match(order=o, rules=rules(slippage=dict(version="s", adverse_rate="0.0011")))
    assert result.fill.price == Decimal("10.02")
    sell = validated_update(
        o,
        side="sell",
        protection_price=Decimal("9.98"),
        reserved_cash=Decimal(0),
        reserved_quantity=Decimal(100),
    )
    result = match(order=sell, rules=rules(slippage=dict(version="s", adverse_rate="0.0011")))
    assert result.fill.price == Decimal("9.98")
    assert match(order=sell, bar=bar(open="9")).reason == "adverse_limit_open"


def test_wrong_scope_or_rule_never_matches():
    with pytest.raises(SimulationError, match="listing"):
        match(bar=bar(listing_id=2))
    with pytest.raises(SimulationError, match="rule"):
        match(terms=execution_terms(rule_version="other"))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", 0.1])
def test_nonfinite_or_invalid_bar_rejected(value):
    with pytest.raises(ValidationError):
        bar(open=value)


def test_snapshot_adapter_never_uses_adjusted_or_unarchived_prices():
    from types import SimpleNamespace

    from investment_agent.simulation.execution import bar_from_snapshot

    row = SimpleNamespace(
        adjust="none",
        id=1,
        listing_id=1,
        trade_date=OPEN.date(),
        open=Decimal(10),
        volume=Decimal(100),
        trade_status=1,
        captured_at=CONFIRMED,
    )
    assert bar_from_snapshot(row) == bar(volume="100", source_ref="market_snapshot:1")
    row.adjust = "forward"
    with pytest.raises(SimulationError, match="unadjusted"):
        bar_from_snapshot(row)


def test_match_result_rejects_inconsistent_metadata():
    from investment_agent.simulation.execution import MatchResult

    with pytest.raises(ValidationError):
        MatchResult(status="filled")
    with pytest.raises(ValidationError):
        MatchResult(status="blocked")


def test_bar_predating_execution_cannot_confirm_a_fill():
    assert match(bar=bar(confirmed_at=OPEN - timedelta(seconds=1))).status == "awaiting_data"


def test_daily_order_cannot_move_execution_date_or_keep_intraday_window():
    for patch in ({"execution_date": OPEN.date() + timedelta(days=1)},
                  {"valid_until": OPEN + timedelta(hours=1)}):
        with pytest.raises(SimulationError, match="execution window"):
            match(order=locked().orders[0].model_copy(update=patch))


def test_execution_terms_tick_validation_ignores_low_ambient_precision():
    from decimal import localcontext

    with localcontext() as ctx:
        ctx.prec = 6
        proof = execution_terms(lower_limit="1000000000000000000000.000000000000000001",
            upper_limit="1000000000000000000000.000000000000000003", tick_size="0.000000000000000001")
    assert proof.upper_limit > proof.lower_limit
