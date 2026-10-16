"""Decimal threshold evaluation with explicit three-valued propagation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from investment_agent.supplemental_rules.contracts import FrozenSubject, MetricCondition, MetricLeaf
from investment_agent.supplemental_rules.metric_adapter import (
    AdaptedFactor,
    LeafStatus,
    adapt_factor,
)

ConditionStatus = Literal["triggered", "not_triggered", "unknown", "not_applicable"]


@dataclass(frozen=True)
class MetricEvaluation:
    status: ConditionStatus
    leaves: tuple[dict[str, Any], ...]
    unknowns: tuple[str, ...]


def _bound_value(value: Decimal, factor: AdaptedFactor) -> tuple[Decimal, Decimal]:
    return value - (factor.uncertainty or Decimal(0)), value + (factor.uncertainty or Decimal(0))


def _compare(factor: AdaptedFactor, operator: str, raw_threshold: Any) -> tuple[LeafStatus, str | None]:
    if factor.status != "true" or factor.value is None:
        return factor.status, factor.reasons[0] if factor.reasons else None
    values = raw_threshold if isinstance(raw_threshold, tuple | list) else (raw_threshold,)
    try:
        thresholds = tuple(Decimal(str(v)) for v in values)
    except Exception:
        return "unknown", "threshold_not_decimal"
    if any(not item.is_finite() for item in thresholds):
        return "unknown", "threshold_not_finite"
    if operator == "between":
        if len(thresholds) != 2 or thresholds[0] > thresholds[1]:
            return "unknown", "invalid_between_bounds"
        low, high = _bound_value(factor.value, factor)
        if low >= thresholds[0] and high <= thresholds[1]:
            return "true", None
        if high < thresholds[0] or low > thresholds[1]:
            return "false", None
        return "unknown", "float_uncertainty_overlaps_threshold"
    if operator == "in":
        if not thresholds:
            return "unknown", "empty_in_set"
        low, high = _bound_value(factor.value, factor)
        matches = [threshold for threshold in thresholds if low <= threshold <= high]
        exact_match = factor.value in thresholds and factor.uncertainty == 0
        if exact_match:
            return "true", None
        if matches:
            return "unknown", "float_uncertainty_overlaps_member"
        return "false", None
    if len(thresholds) != 1:
        return "unknown", "operator_requires_one_threshold"
    threshold = thresholds[0]
    low, high = _bound_value(factor.value, factor)
    op_map: dict[str, Callable[[], LeafStatus]] = {
        "<": lambda: ("true" if high < threshold else "false" if low >= threshold else "unknown"),
        "<=": lambda: ("true" if high <= threshold else "false" if low > threshold else "unknown"),
        ">": lambda: ("true" if low > threshold else "false" if high <= threshold else "unknown"),
        ">=": lambda: ("true" if low >= threshold else "false" if high < threshold else "unknown"),
        "==": lambda: ("true" if factor.value == threshold and factor.uncertainty == 0
                        else "false" if threshold < low or threshold > high else "unknown"),
        "!=": lambda: ("false" if factor.value == threshold and factor.uncertainty == 0
                        else "true" if threshold < low or threshold > high else "unknown"),
    }
    fn = op_map.get(operator)
    if fn is None:
        return "unknown", "unsupported_operator"
    result = fn()
    if result == "unknown":
        return "unknown", "float_uncertainty_overlaps_threshold"
    return result, None


def _aggregate(kind: str, statuses: list[LeafStatus]) -> ConditionStatus:
    if statuses and all(item == "not_applicable" for item in statuses):
        return "not_applicable"
    applicable = [item for item in statuses if item != "not_applicable"]
    if kind == "all":
        if "false" in applicable:
            return "not_triggered"
        if "unknown" in applicable:
            return "unknown"
        return "triggered" if applicable else "not_applicable"
    if "true" in applicable:
        return "triggered"
    if "unknown" in applicable:
        return "unknown"
    return "not_triggered" if applicable else "not_applicable"


def _leaf_status(status: ConditionStatus) -> LeafStatus:
    mapping: dict[ConditionStatus, LeafStatus] = {
        "triggered": "true",
        "not_triggered": "false",
        "unknown": "unknown",
        "not_applicable": "not_applicable",
    }
    return mapping[status]


def evaluate_metric(subject: FrozenSubject, condition: MetricCondition) -> MetricEvaluation:
    """Evaluate a condition tree without mutating or extending legacy rules."""
    rows: list[dict[str, Any]] = []

    def visit(node: MetricCondition | MetricLeaf) -> LeafStatus:
        if isinstance(node, MetricLeaf):
            adapted = adapt_factor(subject, node)
            status, reason = _compare(adapted, node.operator, node.value)
            row = {
                "factor_id": node.factor_id,
                "params": {key: str(value) if isinstance(value, Decimal) else value
                           for key, value in node.params.items()},
                "operator": node.operator,
                "threshold": [str(v) for v in node.value] if isinstance(node.value, tuple) else str(node.value),
                "unit": node.unit or adapted.unit,
                "factor_value": str(adapted.value) if adapted.value is not None else None,
                "factor_version": adapted.factor_version,
                "factor_quality": subject.factor_values.get(node.factor_id, {}).get("quality"),
                "flags": list(adapted.flags),
                "uncertainty": str(adapted.uncertainty) if adapted.uncertainty is not None else None,
                "inputs_ref": adapted.inputs_ref,
                "status": status,
                "reason": reason,
            }
            rows.append(row)
            return status
        statuses = [visit(child) for child in node.conditions]
        return _leaf_status(_aggregate(node.kind, statuses))

    output_status: dict[LeafStatus, ConditionStatus] = {
        "true": "triggered", "false": "not_triggered", "unknown": "unknown",
        "not_applicable": "not_applicable",
    }
    status: ConditionStatus = output_status[visit(condition)]
    unknowns = tuple(dict.fromkeys(
        str(row["reason"] or "factor_unknown") for row in rows if row["status"] == "unknown"
    ))
    return MetricEvaluation(status, tuple(rows), unknowns)
