"""No successful calculation is manufactured from a constant empty fee."""

from uuid import uuid4

from investment_agent.portfolio.suitability_calculations import evaluate_fit
from investment_agent.portfolio.suitability_contracts import FitRequestItem
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs


def test_fully_unavailable_input_has_no_ok_calculation() -> None:
    inputs = make_inputs(with_valuation=False, rules=(), prices={600000: None})
    snapshot = inputs.snapshot.model_copy(update={"known_cash": None, "cash_subtotal": None, "known_subtotal": None, "total_assets": None})
    inputs = inputs.model_copy(update={"snapshot": snapshot})
    item = FitRequestItem(listing_ref=inputs.listing_sources[0].listing_ref, action="open", trade_assumptions=assumptions(execution_price=None, fixed_fee=None, fee_rate=None))
    assessment, calculation = evaluate_fit(inputs, item, operation_id=uuid4())
    assert assessment.financial_status == "unknown"
    assert assessment.calculation_refs == ()
    assert calculation is None
