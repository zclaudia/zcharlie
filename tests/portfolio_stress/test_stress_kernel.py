"""PT-010/011 hand calculations and independent exact-rational edge oracle."""

from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Context, Decimal, Inexact, localcontext
from fractions import Fraction
from uuid import UUID

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.stress import calculate_path, display_amount
from investment_agent.portfolio.stress_contracts import (
    CalculationPath,
    FrozenHolding,
    FrozenIndustry,
    FrozenPath,
    StressShock,
)
from investment_agent.portfolio.stress_shocks import resolve_shock


def listing(identifier: int) -> ObjectRef:
    return ObjectRef(kind="listing", id=identifier, revision=1)


def holding(identifier: int, quantity: str | None, price: str | None) -> FrozenHolding:
    return FrozenHolding(listing_ref=listing(identifier), quantity=quantity, price=price,
                         quantity_unknown_reason="fixture_unknown" if quantity is None else None,
                         price_unknown_reason="fixture_unknown" if price is None and quantity != "0" else None,
                         price_ref=None if price is None else ObjectRef(kind="market_snapshot", id=identifier, revision=1))


def path(*holdings: FrozenHolding, cash: str | None = "100", coverage: str = "complete") -> FrozenPath:
    return FrozenPath(name="actual", source="actual", cash=cash,
                      cash_unknown_reason="fixture_missing_cash" if cash is None else None,
                      holdings=holdings, coverage=coverage)


def industry(identifier: int, code: str = "J66") -> FrozenIndustry:
    return FrozenIndustry(row_id=identifier, listing_ref=listing(identifier), code=code, source="fixture",
                          valid_from=date(2025, 1, 1), valid_to=None,
                          captured_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_most_specific_target_wins_without_combining_assumptions() -> None:
    shock = StressShock.model_validate({"name": "layered", "global": "-0.1",
        "industries": [{"code": "J66", "shock": "-0.3"}],
        "listings": [{"listing_ref": listing(1), "shock": "-0.5"}]})
    assert resolve_shock(shock, listing(1), industry(1)).value == Decimal("-0.5")
    assert resolve_shock(shock, listing(1), None).source == "listing"
    assert resolve_shock(shock, listing(2), industry(2)).value == Decimal("-0.3")
    assert resolve_shock(shock, listing(3), industry(3, "C10")).value == Decimal("-0.1")
    unknown = resolve_shock(shock, listing(4), None)
    assert unknown.value is None and unknown.source == "unknown_industry"
    with pytest.raises(ValueError, match="exact listing"):
        resolve_shock(shock, listing(4), industry(3))


def test_unmatched_zero_is_explicit_and_global_does_not_require_industry() -> None:
    empty = StressShock(name="none")
    assert resolve_shock(empty, listing(1), None).source == "unmatched_zero"
    assert resolve_shock(empty, listing(1), None).value == 0
    global_only = StressShock.model_validate({"name": "global", "global": "-0.2"})
    assert resolve_shock(global_only, listing(1), None).value == Decimal("-0.2")
    industry_only = StressShock(name="industry", industries=({"code": "J66", "shock": "-0.3"},))
    assert resolve_shock(industry_only, listing(1), industry(1, "C10")).source == "unmatched_zero"


def test_hand_calculation_preserves_cash_and_negative_loss() -> None:
    baseline = path(holding(1, "10", "20"), holding(2, "5", "40"))
    down = calculate_path(baseline, StressShock.model_validate({"name": "down", "global": "-0.2"}))
    assert (down.nav_before.value, down.nav_after.value, down.total_loss.value) == (500, 420, 80)
    assert down.loss_ratio.value == Decimal("0.16")
    assert [row.loss.value for row in down.holdings] == [40, 40]
    up = calculate_path(baseline, StressShock.model_validate({"name": "up", "global": "0.5"}))
    assert (up.nav_after.value, up.total_loss.value, up.loss_ratio.value) == (700, -200, Decimal("-0.4"))
    # No fee subtraction exists in the stress layer; FrozenPath cash already includes it.
    assert down.nav_before.value - down.known_securities_before.value == Decimal(100)


def test_full_loss_keeps_amounts_and_loss_ratio_but_weights_are_unknown() -> None:
    result = calculate_path(path(holding(1, "1", "100"), cash="0"), StressShock.model_validate({"name": "wipeout", "global": "-1"}))
    assert result.quality_state == "complete"
    assert result.nav_after.value == 0 and result.loss_ratio.value == 1
    assert result.holdings[0].weight_after.value is None
    assert result.holdings[0].weight_after.unknown_reason == "nonpositive_nav"


@pytest.mark.parametrize("cash", ["0", "-1"])
def test_nonpositive_initial_nav_has_no_fabricated_ratio(cash: str) -> None:
    result = calculate_path(path(cash=cash), StressShock(name="unchanged"))
    assert result.total_loss.value == 0 and result.quality_state == "complete"
    assert result.loss_ratio.value is None and result.loss_ratio.unknown_reason == "nonpositive_nav"


@pytest.mark.parametrize("missing", ["cash", "quantity", "price", "scope"])
def test_partial_values_keep_only_known_subtotals(missing: str) -> None:
    baseline = path(holding(1, "10", "20"), holding(2, None if missing == "quantity" else "5",
                    None if missing == "price" else "40"),
                    cash=None if missing == "cash" else "100", coverage="partial")
    result = calculate_path(baseline, StressShock.model_validate({"name": "down", "global": "-0.2"}))
    assert result.quality_state == "partial"
    assert result.total_loss.value is None and result.nav_before.value is None and result.nav_after.value is None
    assert result.loss_ratio.value is None and all(row.weight_after.value is None for row in result.holdings)
    assert result.known_securities_loss.value == (40 if missing in {"price", "quantity"} else 80)
    assert result.known_loss_count == (1 if missing in {"price", "quantity"} else 2)
    if missing == "cash":
        assert result.total_loss.unknown_reason == "cash_missing"


def test_industry_gap_is_local_to_affected_shock_and_zero_quantity_needs_no_price() -> None:
    baseline = path(holding(1, "10", "20"), holding(2, "0", None))
    global_result = calculate_path(baseline, StressShock.model_validate({"name": "global", "global": "-0.2"}))
    assert global_result.quality_state == "complete" and global_result.holdings[1].loss.value == 0
    layered = StressShock.model_validate({"name": "industry", "global": "-0.2", "industries": [{"code": "J66", "shock": "-0.3"}]})
    unknown = calculate_path(baseline, layered)
    assert unknown.nav_before.value == 300 and unknown.nav_after.value is None
    assert unknown.total_loss.value is None and unknown.known_securities_loss.value == 0
    assert unknown.holdings[1].loss.value == 0  # q=0 is known even without membership.
    known = calculate_path(baseline, layered, (industry(1),))
    assert known.quality_state == "complete" and known.total_loss.value == 60
    with pytest.raises(ValueError, match="unambiguous"):
        calculate_path(baseline, layered, (industry(1), industry(1)))


def test_display_uses_half_even_without_rounding_accumulated_exact_amounts() -> None:
    baseline = path(holding(1, "1", "0.015"), holding(2, "1", "0.015"), cash="0")
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)) as ambient:
        ambient.traps[Inexact] = True
        result = calculate_path(baseline, StressShock(name="zero"))
        assert result.nav_before.value == Decimal("0.030")
        assert display_amount(Decimal("0.015")).value == Decimal("0.02")
        assert display_amount(Decimal("0.015")).rounding_delta == Decimal("0.005")
        assert display_amount(Decimal("0.025")).value == Decimal("0.02")
        assert display_amount(result.nav_before.value).value == Decimal("0.03")


def test_saved_path_classification_never_inherits_actual_account_copy() -> None:
    shock = StressShock.model_validate({"name": "industry", "global": "-0.1",
        "industries": [{"code": "J66", "shock": "-0.3"}]})
    common = (industry(1),)
    actual = path(holding(1, "10", "20"))
    assert calculate_path(actual, shock, common).total_loss.value == 60
    explicit_unknown = FrozenPath.model_validate(actual.model_dump() | {"industries": ()})
    assert calculate_path(explicit_unknown, shock, common).total_loss.value is None
    alternative = FrozenPath(
        name="saved", source="calculation", cash="100", coverage="complete",
        holdings=actual.holdings, industries=(),
        alternative=CalculationPath(calculation_ref=ObjectRef(kind="calculation", id=UUID(int=1), revision=1), scenario_index=0),
    )
    assert calculate_path(alternative, shock, common).total_loss.unknown_reason == "industry_missing"
    known_other_industry = FrozenPath.model_validate(alternative.model_dump() | {"industries": (industry(1, "C10"),)})
    assert calculate_path(known_other_industry, shock, common).total_loss.value == 20


@pytest.mark.parametrize("shock_value", ["-1", "10", "-0.123456789012345678"])
def test_extreme_mixed_spans_match_an_independent_rational_oracle(shock_value: str) -> None:
    # Products span 80 integer and 54 fractional places before adding 50 rows.
    large, tiny = "9" * 40, "0.000000000000000001"
    rows = tuple(holding(i, large if i % 2 else tiny, large if i % 3 else tiny) for i in range(1, 51))
    baseline = path(*rows, cash=tiny)
    shock = StressShock.model_validate({"name": "edge", "global": shock_value})
    before = sum((Fraction(row.quantity) * Fraction(row.price) for row in rows), Fraction(tiny))
    securities_after = sum((Fraction(row.quantity) * Fraction(row.price) * (1 + Fraction(shock_value)) for row in rows), Fraction(0))
    after = Fraction(tiny) + securities_after
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)):
        result = calculate_path(baseline, shock)
    assert Fraction(result.nav_before.value) == before
    assert Fraction(result.nav_after.value) == after
    assert Fraction(result.total_loss.value) == before - after
    ratio = (before - after) / before
    with localcontext(Context(prec=40)):
        expected_ratio = Decimal(ratio.numerator) / Decimal(ratio.denominator)
    assert result.loss_ratio.value == expected_ratio
