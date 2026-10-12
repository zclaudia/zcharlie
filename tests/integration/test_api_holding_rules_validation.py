"""P3d: holding-rule edit params are finite-checked at the API boundary (same
defense in depth as the admin watchlist rule schema)."""

import math

import pytest
from pydantic import ValidationError

from investment_agent.api.schemas.holding_rules import HoldingRuleEditRequest


def _body(params: dict[str, float]) -> dict[str, object]:
    return {"listing_id": 1, "rule_instance_id": 2, "params": params}


def test_finite_params_accepted_and_empty_default_kept():
    assert HoldingRuleEditRequest.model_validate(_body({"buy_drop": -0.1})).params == {
        "buy_drop": -0.1
    }
    assert HoldingRuleEditRequest.model_validate(
        {"listing_id": 1, "rule_instance_id": 2}
    ).params == {}


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_params_rejected_at_the_boundary(value: float):
    with pytest.raises(ValidationError, match="must be finite"):
        HoldingRuleEditRequest.model_validate(_body({"buy_drop": value}))
