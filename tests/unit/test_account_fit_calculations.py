"""P1 pure-calculation and resolver scenarios against explicit DTO facts."""

from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import PortfolioConstraints
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.contracts import Account
from investment_agent.portfolio.purposes import Purpose, PurposeVersion
from investment_agent.portfolio.suitability import AccountFitNumericResolver, AccountFitService
from investment_agent.portfolio.suitability_calculations import calculation_basis_key, evaluate_fit
from investment_agent.portfolio.suitability_contracts import (
    FitRequestItem,
    PurposeBinding,
    ValuationBinding,
)
from investment_agent.portfolio.suitability_inputs import FitContext, SuitabilityInputService
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import AS_OF, account_fit_sqlite, make_inputs


def _item(
    listing_id: int,
    *,
    action: Literal["open", "add", "hold"] = "open",
    fee: str | None = "10",
    execution: str | None = "1500",
) -> FitRequestItem:
    if action == "hold":
        return FitRequestItem(
            listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1), action="hold"
        )
    return FitRequestItem(
        listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
        action=action,
        trade_assumptions=assumptions(
            execution_price=execution, fixed_fee=fee, fee_rate="0" if fee is not None else None
        ),
    )


def _scenario(calculation: CalculationRecord) -> dict[str, Any]:
    return cast(dict[str, Any], calculation.details["scenarios"][1])


def test_f01_minimum_lot_uses_market_value_and_fee_only_reduces_nav() -> None:
    target = 600000
    account_id = make_inputs().account_id
    constraints = PortfolioConstraints(account_id=account_id, max_single_weight=Decimal("0.10"))
    small = make_inputs(cash="200000", constraints=constraints)
    large = make_inputs(cash="2000000", constraints=constraints)

    blocked, small_calc = evaluate_fit(small, _item(target), operation_id=uuid4())
    passed, large_calc = evaluate_fit(large, _item(target), operation_id=uuid4())

    assert small_calc is not None and large_calc is not None
    small_scenario = _scenario(small_calc)
    large_scenario = _scenario(large_calc)
    assert Decimal(small_scenario["holdings"][0]["market_value"]) == Decimal("150000")
    assert Decimal(small_scenario["nav"]) == Decimal("199990")
    with localcontext() as context:
        context.prec = 40
        assert Decimal(small_scenario["holdings"][0]["weight"]) == Decimal("150000") / Decimal(
            "199990"
        )
    assert blocked.financial_status == "blocked"
    assert Decimal(large_scenario["holdings"][0]["market_value"]) == Decimal("150000")
    assert Decimal(large_scenario["nav"]) == Decimal("1999990")
    with localcontext() as context:
        context.prec = 40
        assert Decimal(large_scenario["holdings"][0]["weight"]) == Decimal("150000") / Decimal(
            "1999990"
        )
    assert passed.financial_status == "pass"
    assert passed.ranking_bucket == "incomplete"


def test_f01_blocked_scenario_number_is_display_only_with_violations() -> None:
    target = 600000
    inputs = make_inputs(
        cash="200000",
        constraints=PortfolioConstraints(
            account_id=make_inputs().account_id, max_single_weight=Decimal("0.10")
        ),
    )
    _, calculation = evaluate_fit(inputs, _item(target), operation_id=uuid4())
    assert calculation is not None
    path = next(
        row["field_path"]
        for row in calculation.details["result_paths"]
        if row["scenario_index"] == 1
        and row["metric"] == "weight"
        and row.get("listing_ref", {}).get("id") == target
    )
    resolver = AccountFitNumericResolver(
        calculation, inputs.account_id, ObjectRef(kind="listing", id=target, revision=1)
    )

    resolved = resolver.resolve(
        ObjectRef(kind="calculation", id=calculation.id, revision=1, field_path=path),
        scenario=1,
        metric="weight",
    )

    assert resolved.scenario_status == "violated"
    assert "mandate_constraint_violation:max_single_weight" in resolved.violations
    assert resolved.authority == "display_only_not_plan_authorization"
    assert (
        resolver.render(
            ObjectRef(kind="calculation", id=calculation.id, revision=1, field_path=path),
            scenario=1,
            metric="weight",
            display="percent",
        )
        == "75.00%"
    )


def test_f02_uses_execution_price_for_cash_and_valuation_price_for_position() -> None:
    target, other = 600000, 600001
    inputs = make_inputs(
        cash="500000",
        holdings=((target, "50", "1500"), (other, "425", "1000")),
        prices={target: "1500", other: "1000"},
        rules=(target,),
        constraints=PortfolioConstraints(
            account_id=make_inputs().account_id,
            max_single_weight=Decimal("1"),
        ),
    )
    item = _item(target, action="add", fee="10", execution="1600")
    _, calculation = evaluate_fit(inputs, item, operation_id=uuid4())
    assert calculation is not None
    result = _scenario(calculation)
    target_holding = next(row for row in result["holdings"] if row["listing_ref"]["id"] == target)
    assert result["cash"] == "339990"
    assert result["nav"] == "989990"
    assert target_holding["market_value"] == "225000"
    with localcontext() as context:
        context.prec = 40
        assert Decimal(target_holding["weight"]) == Decimal("225000") / Decimal("989990")


def test_unknown_fee_preserves_known_quantity_and_only_bounds_cash() -> None:
    target = 600000
    inputs = make_inputs(cash="200000")
    item = _item(target, fee=None, execution="1500")
    assessment, calculation = evaluate_fit(inputs, item, operation_id=uuid4())
    assert calculation is not None
    action = _scenario(calculation)["actions"][0]
    assert action["quantity"] == "100"
    assert action["gross"] == "150000"
    assert action["fee"] is None
    assert _scenario(calculation)["cash"] is None
    assert _scenario(calculation)["cash_upper_bound"] == "50000"
    assert assessment.financial_status == "unknown"


def test_missing_rule_never_invents_a_buy_quantity_or_action() -> None:
    inputs = make_inputs(rules=())
    assessment, calculation = evaluate_fit(inputs, _item(600000), operation_id=uuid4())
    assert calculation is not None
    assert len(calculation.details["scenarios"]) == 1
    assert calculation.details["scenarios"][0]["actions"] == []
    assert assessment.financial_status == "unknown"
    assert any(check.code == "minimum_increment_unavailable" for check in assessment.checks)


def test_missing_valuation_never_publishes_a_complete_nav() -> None:
    inputs = make_inputs(with_valuation=False)
    assessment, calculation = evaluate_fit(inputs, _item(600000), operation_id=uuid4())
    assert calculation is not None
    assert all(row["nav"] is None for row in calculation.details["scenarios"])
    assert assessment.financial_status == "unknown"
    assert "valuation_missing" in assessment.checks[0].reason_codes or any(
        check.code == "valuation_missing" for check in assessment.checks
    )


def test_partial_valuation_does_not_derive_complete_nav_from_snapshot() -> None:
    complete = make_inputs(cash="200000")
    assert complete.valuation is not None
    partial_valuation = complete.valuation.model_copy(
        update={"coverage": "partial", "unknowns": ("holding_price_missing",)}
    )
    inputs = complete.model_copy(update={"valuation": partial_valuation})

    assessment, calculation = evaluate_fit(inputs, _item(600000), operation_id=uuid4())

    assert assessment.financial_status == "unknown"
    assert calculation is not None
    assert all(row["nav"] is None for row in calculation.details["scenarios"])
    assert calculation.details["snapshot"]["coverage"] == "partial"
    assert all(
        "valuation_incomplete" in row["unknowns"] for row in calculation.details["scenarios"]
    )


def test_hold_has_no_trade_input_but_keeps_cash_and_position_checks() -> None:
    target = 600000
    inputs = make_inputs(
        cash="100000",
        holdings=((target, "100", "1500"),),
        prices={target: "1500"},
        rules=(),
    )
    assessment, calculation = evaluate_fit(
        inputs, _item(target, action="hold"), operation_id=uuid4()
    )
    assert calculation is not None
    assert all(not scenario["actions"] for scenario in calculation.details["scenarios"])
    assert assessment.request_item.trade_assumptions is None
    assert any(check.code == "cash_nonnegative" for check in assessment.checks)


def test_purpose_reserve_is_checked_separately_from_mandate_cash_floor() -> None:
    target = 600000
    inputs = make_inputs(
        cash="60000",
        holdings=((target, "10", "1500"),),
        prices={target: "1500"},
        rules=(),
    )
    purpose_id = uuid4()
    purpose_ref = ObjectRef(kind="portfolio_purposes", id=uuid4(), revision=1)
    purpose = Purpose(
        id=purpose_id,
        name="emergency reserve",
        amount=Decimal("60000"),
        available_budget=Decimal("40000"),
        required_cash=Decimal("20000"),
        horizon="short_term",
        required_on=date(2026, 12, 31),
        source="synthetic fixture",
        source_version="v1",
    )
    version = PurposeVersion(
        reference=purpose_ref,
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        confirmed_at=AS_OF,
        purposes=(purpose,),
        confirmed_cash=Decimal("60000"),
        unallocated=Decimal(0),
        semantic_hash="c" * 64,
    )
    selected = PurposeBinding(
        state="selected", purpose_ref=purpose_ref, purpose_id=purpose_id, confirmed_at=AS_OF
    )
    constraints = PortfolioConstraints(account_id=inputs.account_id, minimum_cash=Decimal("50000"))
    mandate = inputs.mandate.model_copy(update={"portfolio_constraints": constraints})
    inputs = inputs.model_copy(
        update={"purpose_binding": selected, "purpose_version": version, "mandate": mandate}
    )
    assessment, calculation = evaluate_fit(
        inputs, _item(target, action="hold"), operation_id=uuid4()
    )
    assert calculation is not None
    scenario = calculation.details["scenarios"][1]
    assert scenario["purpose_checks"]["required_cash"] == "20000"
    assert scenario["mandate_checks"]["minimum_cash"] == "pass"
    assert "purpose_required_cash_shortfall" not in scenario["violations"]
    assert assessment.financial_status == "pass"


def test_numeric_resolver_is_bound_to_account_listing_metric_and_scenario() -> None:
    inputs = make_inputs(cash="1200010")
    _, calculation = evaluate_fit(inputs, _item(600000), operation_id=uuid4())
    assert calculation is not None
    path = next(
        row["field_path"]
        for row in calculation.details["result_paths"]
        if row["scenario_index"] == 1
        and row["metric"] == "weight"
        and row.get("listing_ref", {}).get("id") == 600000
    )
    resolver = AccountFitNumericResolver(
        calculation, inputs.account_id, ObjectRef(kind="listing", id=600000, revision=1)
    )
    reference = ObjectRef(kind="calculation", id=calculation.id, revision=1, field_path=path)
    with localcontext() as context:
        context.prec = 40
        assert resolver.quantity(reference, scenario=1, metric="weight").value == Decimal("0.125")
    assert resolver.render(reference, scenario=1, metric="weight", display="percent") == "12.50%"
    with pytest.raises(InvestorError):
        resolver.quantity(reference, scenario=0, metric="weight")


@pytest.mark.parametrize(
    "updates",
    [
        {"applicable": False},
        {"origin_ref": ObjectRef(kind="portfolio_snapshot", id=uuid4(), revision=1)},
        {"fact_refs": ()},
    ],
)
def test_numeric_resolver_rejects_unbound_or_inapplicable_records(
    updates: dict[str, Any],
) -> None:
    inputs = make_inputs(cash="1200010")
    _, calculation = evaluate_fit(inputs, _item(600000), operation_id=uuid4())
    assert calculation is not None
    path = next(
        row["field_path"]
        for row in calculation.details["result_paths"]
        if row["scenario_index"] == 1 and row["metric"] == "weight"
    )
    resolver = AccountFitNumericResolver(
        calculation, inputs.account_id, ObjectRef(kind="listing", id=600000, revision=1)
    )
    invalid = calculation.model_copy(update=updates)
    invalid_resolver = AccountFitNumericResolver(
        invalid, inputs.account_id, ObjectRef(kind="listing", id=600000, revision=1)
    )
    reference = ObjectRef(kind="calculation", id=calculation.id, revision=1, field_path=path)
    assert resolver.quantity(reference, scenario=1, metric="weight").value is not None
    with pytest.raises(InvestorError):
        invalid_resolver.quantity(reference, scenario=1, metric="weight")


def test_numeric_resolver_rejects_publication_ready_or_inconsistent_diagnostics() -> None:
    inputs = make_inputs(cash="1200010")
    _, calculation = evaluate_fit(inputs, _item(600000), operation_id=uuid4())
    assert calculation is not None
    details = dict(calculation.details)
    paths = [dict(row) for row in details["result_paths"]]
    target_path = next(
        row for row in paths if row["scenario_index"] == 1 and row["metric"] == "weight"
    )
    target_path["publication_ready"] = True
    details["result_paths"] = paths
    resolver = AccountFitNumericResolver(
        calculation.model_copy(update={"details": details}),
        inputs.account_id,
        ObjectRef(kind="listing", id=600000, revision=1),
    )
    reference = ObjectRef(
        kind="calculation", id=calculation.id, revision=1, field_path=target_path["field_path"]
    )
    with pytest.raises(InvestorError):
        resolver.resolve(reference, scenario=1, metric="weight")


def test_partial_snapshot_does_not_infer_open_or_add_from_missing_target() -> None:
    inputs = make_inputs(snapshot_coverage="partial")
    with pytest.raises(InvestorError) as error:
        SuitabilityInputService.validate_action(inputs.snapshot, _item(600000))
    assert error.value.detail.code == "holding_presence_unknown"


@pytest.mark.asyncio
async def test_current_limits_mark_new_valuation_or_purpose_stale_and_basis_keys_differ(
    tmp_path: Path,
) -> None:
    inputs = make_inputs()
    item = _item(600000)
    assessment, _ = evaluate_fit(inputs, item, operation_id=uuid4())
    account = Account(
        id=inputs.account_id,
        name="synthetic",
        currency="CNY",
        revision=1,
        current_snapshot_id=inputs.snapshot.id,
    )
    context = FitContext(
        account=account,
        snapshot=inputs.snapshot,
        valuation=inputs.valuation,
        valuation_binding=inputs.valuation_binding,
        mandate=inputs.mandate,
        mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1),
        purpose_version=inputs.purpose_version,
        purpose_binding=inputs.purpose_binding,
        gaps=(),
    )
    async with account_fit_sqlite(tmp_path) as session:
        service = AccountFitService(session, calendar=lambda _day: True)
        changed_valuation = context.model_copy(
            update={
                "valuation_binding": ValuationBinding(
                    state="available",
                    valuation_ref=ObjectRef(kind="portfolio_valuation", id=uuid4(), revision=1),
                )
            }
        )
        valuation_limits = await service.limits(
            assessment,
            context=changed_valuation,
            current_sources=inputs.listing_sources,
            now=AS_OF,
        )
        assert valuation_limits.current_status == "stale"
        assert "valuation_changed" in valuation_limits.reasons

        new_purpose_ref = ObjectRef(kind="portfolio_purposes", id=uuid4(), revision=1)
        new_purpose = PurposeVersion(
            reference=new_purpose_ref,
            account_id=inputs.account_id,
            snapshot_ref=inputs.snapshot.reference,
            confirmed_at=AS_OF,
            purposes=(),
            confirmed_cash=Decimal("200000"),
            unallocated=Decimal("200000"),
            semantic_hash="d" * 64,
        )
        changed_purpose = context.model_copy(
            update={
                "purpose_version": new_purpose,
                "purpose_binding": PurposeBinding(
                    state="confirmed_empty", purpose_ref=new_purpose_ref, confirmed_at=AS_OF
                ),
            }
        )
        purpose_limits = await service.limits(
            assessment,
            context=changed_purpose,
            current_sources=inputs.listing_sources,
            now=AS_OF,
        )
        assert purpose_limits.current_status == "stale"
        assert "purposes_changed" in purpose_limits.reasons

    known_zero = _item(600000, fee="0", execution="1500")
    assert calculation_basis_key(item, inputs) != calculation_basis_key(known_zero, inputs)


@pytest.mark.asyncio
async def test_service_writes_and_replays_typed_result_in_file_backed_sqlite(
    tmp_path: Path,
) -> None:
    inputs = make_inputs(cash="2000000")
    operation_id = uuid4()
    item = _item(600000)
    async with account_fit_sqlite(tmp_path) as session:
        service = AccountFitService(session)
        first = await service.save(inputs, item, operation_id=operation_id)
        second = await service.save(inputs, item, operation_id=operation_id)

        assert first == second
        stored = await service.repository.get_exact(
            "local_owner", first.reference, expected_kind="account_fit"
        )
        assert stored == first


@pytest.mark.asyncio
async def test_current_limits_stale_when_selected_purpose_changes_within_same_version(
    tmp_path: Path,
) -> None:
    inputs = make_inputs()
    first_id, second_id = uuid4(), uuid4()
    purposes = tuple(
        Purpose(
            id=purpose_id,
            name=f"goal-{index}",
            amount=Decimal("10000"),
            available_budget=Decimal("8000"),
            required_cash=Decimal("2000"),
            horizon="short_term",
            required_on=date(2026, 12, 31),
            source="synthetic fixture",
            source_version="v1",
        )
        for index, purpose_id in enumerate((first_id, second_id))
    )
    purpose_ref = ObjectRef(kind="portfolio_purposes", id=uuid4(), revision=1)
    version = PurposeVersion(
        reference=purpose_ref,
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        confirmed_at=AS_OF,
        purposes=purposes,
        confirmed_cash=Decimal("200000"),
        unallocated=Decimal("180000"),
        semantic_hash="e" * 64,
    )
    first_binding = PurposeBinding(
        state="selected", purpose_ref=purpose_ref, purpose_id=first_id, confirmed_at=AS_OF
    )
    selected_inputs = inputs.model_copy(
        update={"purpose_version": version, "purpose_binding": first_binding}
    )
    assessment, _ = evaluate_fit(selected_inputs, _item(600000), operation_id=uuid4())
    account = Account(
        id=inputs.account_id,
        name="synthetic",
        currency="CNY",
        revision=1,
        current_snapshot_id=inputs.snapshot.id,
    )
    current_context = FitContext(
        account=account,
        snapshot=inputs.snapshot,
        valuation=inputs.valuation,
        valuation_binding=inputs.valuation_binding,
        mandate=inputs.mandate,
        mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1),
        purpose_version=version,
        purpose_binding=PurposeBinding(
            state="selected", purpose_ref=purpose_ref, purpose_id=second_id, confirmed_at=AS_OF
        ),
        gaps=(),
    )
    async with account_fit_sqlite(tmp_path) as session:
        service = AccountFitService(session, calendar=lambda _day: True)
        limits = await service.limits(
            assessment,
            context=current_context,
            current_sources=inputs.listing_sources,
            now=AS_OF,
        )
    assert limits.current_status == "stale"
    assert "purpose_binding_changed" in limits.reasons
