"""BT-012 pure synthetic reviewed observations; no persisted authority is created."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import uuid4

import pytest
from tests.next_iterations.test_bank_decomposition_proofs import decomposition_fact
from tests.next_iterations.test_bank_nii_proofs import make_fact

from investment_agent.bank_topics.contracts import AnnualPeriod, Decomposition, TopicFactRevision
from investment_agent.bank_topics.decomposition import (
    AMOUNT_HALF_UNIT,
    RATIO_HALF_UNIT,
    amount_identity_tolerance,
    calculate_decomposition,
    display_nii_growth_percent,
    net_interest_contributions,
)
from investment_agent.bank_topics.facts import normalize_fact_value, observation_from_topic_fact

PERIOD = AnnualPeriod.model_validate({"start": "2025-01-01", "end": "2025-12-31"})
BASELINE = AnnualPeriod.model_validate({"start": "2024-01-01", "end": "2024-12-31"})
IDS = ("nii_2025", "nii_2024", "nim_2025", "nim_2024", "nii_volume_effect", "nii_rate_effect", "nii_change_disclosed")


def calculation_facts() -> list[TopicFactRevision]:
    case_id = uuid4()
    facts = []
    for fact_id in IDS:
        fact = make_fact(fact_id) if fact_id in IDS[:4] else decomposition_fact(fact_id)
        facts.append(fact.model_copy(update={"case_id": case_id, "verification": "verified",
            "timing": fact.timing.model_copy(update={"checked_at": datetime(2026, 11, 7, tzinfo=UTC),
                                                     "review_actor": "synthetic-pure-calculation-test"})}))
    return facts


def calculate(facts: list[TopicFactRevision]) -> Decomposition:
    return calculate_decomposition([observation_from_topic_fact(fact, listing_id=18, proof_checked=True) for fact in facts],
                                   period=PERIOD, baseline_period=BASELINE)


def replace_amount(facts: list[TopicFactRevision], fact_id: str, raw: str) -> list[TopicFactRevision]:
    changed = []
    for fact in facts:
        if fact.stable_fact_id == fact_id:
            normalized = normalize_fact_value(fact.metric, raw, fact.raw_unit or "")
            fact = fact.model_copy(update={"raw_value": raw, "value": normalized.value})
        changed.append(fact)
    return changed


def test_golden_decomposition_is_decimal_precision40_and_not_a_nim_backsolve() -> None:
    facts = calculation_facts()
    with localcontext() as context:
        context.prec = 2
        result = calculate(facts)
        assert amount_identity_tolerance(99) == Decimal("49500000")
    assert result.coverage == "complete"
    assert result.delta_nii.value == Decimal("4316000000")
    assert result.delta_nim_bp.value == Decimal("-11")
    assert result.nii_yoy.value == Decimal("0.02042815829456116851337343866109420334442")
    assert display_nii_growth_percent(result) == "2.04"
    assert result.residual.value == 0 and result.rounding_consistent is True
    assert result.volume_effect.value == Decimal("16798000000")
    assert result.rate_effect.value == Decimal("-12482000000")
    assert result.tolerance.value == Decimal("2000000")


@pytest.mark.parametrize("missing", IDS)
def test_missing_fact_stays_null_and_never_balances_to_zero(missing: str) -> None:
    result = calculate([fact for fact in calculation_facts() if fact.stable_fact_id != missing])
    assert result.coverage == "partial" and result.rounding_consistent is None
    if missing in {"nii_volume_effect", "nii_rate_effect"}:
        assert result.residual.value is None and result.tolerance.value is None
        assert getattr(result, "volume_effect" if missing == "nii_volume_effect" else "rate_effect").value is None
    if missing == "nii_2024":
        assert result.nii_yoy.value is None and result.delta_nii.value is None
    if missing == "nim_2024":
        assert result.delta_nim_bp.value is None and result.delta_nii.value is not None


def test_no_disclosed_table_is_unavailable_but_annual_facts_remain_readable() -> None:
    result = calculate([fact for fact in calculation_facts() if fact.stable_fact_id in IDS[:4]])
    assert result.coverage == "unavailable"
    assert result.nii_current.value is not None and result.nim_current.value is not None
    assert result.volume_effect.value is None and result.rate_effect.value is None
    assert result.residual.value is None


@pytest.mark.parametrize(("raw", "reason"), [("0", "zero_denominator"), ("-1", "negative_denominator")])
def test_zero_and_negative_baseline_keep_absolute_change_but_no_growth_ratio(raw: str, reason: str) -> None:
    result = calculate(replace_amount(calculation_facts(), "nii_2024", raw))
    assert result.delta_nii.value is not None
    assert result.nii_yoy.value is None and result.nii_yoy.unknown_reason == reason
    assert display_nii_growth_percent(result) is None


def test_original_zero_is_not_missing_and_does_not_hide_a_nonzero_residual() -> None:
    result = calculate(replace_amount(calculation_facts(), "nii_volume_effect", "0"))
    assert result.volume_effect.value == 0 and result.volume_effect.unknown_reason is None
    assert result.coverage == "complete" and result.rounding_consistent is False
    assert result.residual.value != 0


@pytest.mark.parametrize(("raw", "consistent"), [("215,594", True), ("215,596", False)])
def test_rounding_interval_is_displayed_without_filling_a_balancing_contribution(raw: str, consistent: bool) -> None:
    result = calculate(replace_amount(calculation_facts(), "nii_2025", raw))
    assert result.rounding_consistent is consistent
    assert result.tolerance.value == AMOUNT_HALF_UNIT * 4
    assert result.volume_effect.value == Decimal("16798000000")
    assert result.disclosed_change.value == Decimal("4316000000")
    assert result.residual.value is not None
    assert Decimal("0.00005") == RATIO_HALF_UNIT


@pytest.mark.parametrize(("change", "reason"), [
    ({"verification": "candidate"}, "unverified_fact"),
    ({"verification": "scope_conflict"}, "scope_conflict"),
    ({"verification": "source_unavailable"}, "source_unavailable"),
    ({"reporting_scope": "standalone"}, "scope_conflict"),
    ({"raw_unit": "CNY"}, "unit_mismatch"),
    ({"value": Decimal("1e1000000")}, "invalid_decimal"),
])
def test_unusable_fact_cannot_enter_comparable_arithmetic(change: dict[str, Any], reason: str) -> None:
    facts = calculation_facts()
    facts[0] = facts[0].model_copy(update=change)
    result = calculate(facts)
    assert result.nii_current.value is None and result.nii_current.unknown_reason == reason
    assert result.delta_nii.value is None and result.nii_yoy.value is None
    assert result.delta_nim_bp.value is not None


def test_duplicate_and_cross_case_facts_do_not_select_the_latest() -> None:
    facts = calculation_facts()
    duplicate = calculate([*facts, facts[0].model_copy(update={"id": uuid4(), "revision": 2})])
    assert duplicate.nii_current.value is None and duplicate.nii_current.unknown_reason == "source_conflict"
    facts[0] = facts[0].model_copy(update={"case_id": uuid4()})
    crossed = calculate(facts)
    assert crossed.coverage == "unavailable" and crossed.nii_current.unknown_reason == "scope_conflict"


def test_expense_contributions_reverse_the_liability_effect_once() -> None:
    volume, rate = net_interest_contributions(asset_volume=Decimal("26238000000"), liability_volume=Decimal("9440000000"),
                                              asset_rate=Decimal("-49158000000"), liability_rate=Decimal("-36676000000"))
    assert volume == Decimal("16798000000") and rate == Decimal("-12482000000")
    with pytest.raises(ValueError):
        net_interest_contributions(asset_volume=True, liability_volume=Decimal(0),  # type: ignore[arg-type]
                                   asset_rate=Decimal(0), liability_rate=Decimal(0))


def test_existing_peer_evidence_and_topic_facts_combine_without_new_persistent_fact_rows() -> None:
    from dataclasses import replace

    from investment_agent.investor.common import ObjectRef

    observations = [observation_from_topic_fact(fact, listing_id=18, proof_checked=True) for fact in calculation_facts()]
    observations[2] = replace(observations[2], basis_kind="peer_field", source_refs=(
        ObjectRef(kind="peer_annual_snapshot", id=uuid4(), revision=1, field_path="fields.net_interest_margin"),))
    observations[3] = replace(observations[3], basis_kind="evidence", source_refs=(
        ObjectRef(kind="evidence", id=uuid4(), revision=1),))
    result = calculate_decomposition(observations, period=PERIOD, baseline_period=BASELINE)
    assert result.coverage == "complete" and result.delta_nim_bp.value == -11
    assert observations[2].basis_kind == "peer_field" and observations[2].source_refs[0].kind == "peer_annual_snapshot"
    assert observations[3].basis_kind == "evidence" and observations[3].source_refs[0].kind == "evidence"
    for changed in (replace(observations[2], unit="CNY"), replace(observations[2], proof_checked=False),
                    replace(observations[2], verification="candidate"), replace(observations[2], period=BASELINE)):
        result = calculate_decomposition([*observations[:2], changed, *observations[3:]], period=PERIOD, baseline_period=BASELINE)
        assert result.delta_nim_bp.value is None and result.coverage == "partial"


def test_rounding_and_traps_are_independent_of_ambient_decimal_context() -> None:
    from decimal import ROUND_DOWN, Inexact

    facts = calculation_facts()
    baseline = calculate(facts)
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.rounding = ROUND_DOWN
        ambient.traps[Inexact] = True
        stressed = calculate(facts)
        assert stressed == baseline
        assert display_nii_growth_percent(stressed) == "2.04"
        assert normalize_fact_value("nim", "1.87", "percent").value == Decimal("0.0187")
        assert amount_identity_tolerance(99) == Decimal("49500000")
