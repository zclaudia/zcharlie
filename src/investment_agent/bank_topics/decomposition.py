"""Decimal-only disclosed NII decomposition, with explicit missing/comparability gaps."""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from investment_agent.bank_topics.contracts import (
    AmountQuantity,
    AnnualPeriod,
    BasisPointQuantity,
    Coverage,
    Decomposition,
    FactMetric,
    FractionQuantity,
    SourcedQuantity,
)
from investment_agent.bank_topics.facts import FactObservation, normalize_fact_value

PRECISION = 40
AMOUNT_HALF_UNIT = Decimal(500000)
RATIO_HALF_UNIT = Decimal("0.00005")


def _quantity[Q: SourcedQuantity](kind: type[Q], value: Decimal | None, reason: str | None, sources: Sequence[str]) -> Q:
    return kind.model_validate({"value": value, "unknown_reason": reason,
                                "source_fact_ids": tuple(dict.fromkeys(sources))})


def _usable(fact: FactObservation, period: AnnualPeriod, comparison: AnnualPeriod | None) -> str | None:
    if fact.period != period or comparison is not None and fact.comparison_period != comparison:
        return "period_mismatch"
    if (fact.reporting_scope, fact.accounting_standard, fact.currency) != ("consolidated", "CAS", "CNY"):
        return "scope_conflict"
    if fact.verification != "verified":
        return "unverified_fact" if fact.verification in {"candidate", "pending_review"} else fact.verification
    if fact.value is None:
        return fact.unknown_reason or "missing_value"
    if not isinstance(fact.value, Decimal) or not fact.value.is_finite():
        return "invalid_decimal"
    _, digits, exponent = fact.value.as_tuple()
    if not isinstance(exponent, int) or abs(exponent) > 40 or len(digits) > 40:
        return "invalid_decimal"
    if not fact.proof_checked:
        return "unverified_fact"
    if fact.basis_kind != "topic_fact":
        return None if fact.unit == ("fraction" if fact.metric == "nim" else "CNY") else "unit_mismatch"
    try:
        normalized = normalize_fact_value(fact.metric, fact.raw_value or "", fact.raw_unit or "")
    except ValueError:
        return "unit_mismatch"
    if fact.value != normalized.value or fact.unit != normalized.unit:
        return "unit_mismatch"
    return None


def amount_identity_tolerance(term_count: int) -> Decimal:
    if isinstance(term_count, bool) or not isinstance(term_count, int) or not 1 <= term_count <= 100:
        raise ValueError("bounded integer identity term count required")
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = PRECISION
        return AMOUNT_HALF_UNIT * term_count


def net_interest_contributions(
    *, asset_volume: Decimal, liability_volume: Decimal, asset_rate: Decimal, liability_rate: Decimal,
) -> tuple[Decimal, Decimal]:
    """Normalize expense-side effects by subtraction; do not discard printed signs."""
    values = (asset_volume, liability_volume, asset_rate, liability_rate)
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ValueError("contribution arithmetic requires finite Decimal inputs")
    if any(abs(value.adjusted()) > 40 or len(value.as_tuple().digits) > 40 for value in values):
        raise ValueError("contribution inputs exceed the bounded arithmetic profile")
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = PRECISION
        return asset_volume - liability_volume, asset_rate - liability_rate


def calculate_decomposition(
    facts: Sequence[FactObservation], *, period: AnnualPeriod, baseline_period: AnnualPeriod,
) -> Decomposition:
    """Use reviewed observations only. This function does not verify originals or save facts."""
    if len(facts) > 100:
        raise ValueError("at most 100 bank facts per decomposition")
    identities = {(fact.owner_id, fact.case_id, fact.listing_id) for fact in facts}
    global_reason = "scope_conflict" if len(identities) > 1 else None
    if period.end.year != baseline_period.end.year + 1 or any(
        target.start != date(target.end.year, 1, 1) or target.end != date(target.end.year, 12, 31)
        or target.basis != "period_to_date/annual" for target in (period, baseline_period)
    ):
        global_reason = "period_mismatch"

    def pick[Q: SourcedQuantity](metric: FactMetric, target: AnnualPeriod, kind: type[Q], *, comparison: AnnualPeriod | None = None) -> Q:
        candidates = [fact for fact in facts if fact.metric == metric and fact.period == target]
        reason: str | None
        if len(candidates) != 1:
            reason = "source_conflict" if len(candidates) > 1 else "missing_fact"
            return _quantity(kind, None, reason, [fact.stable_fact_id for fact in candidates])
        fact = candidates[0]
        reason = global_reason or _usable(fact, target, comparison)
        return _quantity(kind, fact.value if reason is None else None, reason, (fact.stable_fact_id,))

    current = pick("nii", period, AmountQuantity)
    baseline = pick("nii", baseline_period, AmountQuantity)
    nim_current = pick("nim", period, FractionQuantity)
    nim_baseline = pick("nim", baseline_period, FractionQuantity)
    volume = pick("nii_volume_effect", period, AmountQuantity, comparison=baseline_period)
    rate = pick("nii_rate_effect", period, AmountQuantity, comparison=baseline_period)
    change = pick("nii_change_disclosed", period, AmountQuantity, comparison=baseline_period)

    def combine[Q: SourcedQuantity](kind: type[Q], inputs: Sequence[SourcedQuantity], value: Decimal | None = None,
                reason: str | None = None) -> Q:
        missing = next((item.unknown_reason for item in inputs if item.value is None), None)
        return _quantity(kind, None if missing else value, missing or reason,
                         [source for item in inputs for source in item.source_fact_ids])

    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = PRECISION
        delta = combine(AmountQuantity, (current, baseline),
                        current.value - baseline.value if current.value is not None and baseline.value is not None else None)
        if current.value is None or baseline.value is None:
            growth = combine(FractionQuantity, (current, baseline))
        elif baseline.value <= 0:
            growth = combine(FractionQuantity, (current, baseline), reason="zero_denominator" if baseline.value == 0 else "negative_denominator")
        else:
            assert delta.value is not None
            growth = combine(FractionQuantity, (current, baseline), delta.value / baseline.value)
        nim_delta = combine(BasisPointQuantity, (nim_current, nim_baseline),
                            (nim_current.value - nim_baseline.value) * Decimal(10000)
                            if nim_current.value is not None and nim_baseline.value is not None else None)
        inputs = (current, baseline, volume, rate)
        residual_value = delta.value - volume.value - rate.value if delta.value is not None and volume.value is not None and rate.value is not None else None
        residual = combine(AmountQuantity, inputs, residual_value)
        tolerance = combine(AmountQuantity, inputs, amount_identity_tolerance(4))
        all_inputs = (current, baseline, nim_current, nim_baseline, volume, rate, change)
        complete = all(item.value is not None for item in all_inputs)
        coverage: Coverage = "unavailable" if volume.value is None and rate.value is None else "complete" if complete else "partial"
        rounding: bool | None = None
        if complete:
            assert residual.value is not None and tolerance.value is not None
            assert volume.value is not None and rate.value is not None and change.value is not None and delta.value is not None
            rounding = (abs(residual.value) <= tolerance.value
                        and abs(change.value - volume.value - rate.value) <= amount_identity_tolerance(3)
                        and abs(delta.value - change.value) <= amount_identity_tolerance(3))
        return Decomposition(coverage=coverage, nii_current=current, nii_baseline=baseline,
                             nim_current=nim_current, nim_baseline=nim_baseline, volume_effect=volume,
                             rate_effect=rate, disclosed_change=change, delta_nii=delta, nii_yoy=growth,
                             delta_nim_bp=nim_delta, residual=residual, tolerance=tolerance,
                             rounding_consistent=rounding)


def display_nii_growth_percent(result: Decomposition) -> str | None:
    value = result.nii_yoy.value
    if value is None:
        return None
    with localcontext(Context(prec=max(PRECISION, value.adjusted() + 6), rounding=ROUND_HALF_EVEN)):
        return format((value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), "f")
