"""ScreeningRule DSL: declarative Tier 1 algorithmic filtering."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from investment_agent.domain.strategy_types import ListingSnapshot

Op = Literal["<", "<=", ">", ">=", "==", "between"]


class Condition(BaseModel):
    metric: str
    # `op` is typed as `str` (not `Op`) so unknown operators surface as a
    # `ValueError("Unknown op: ...")` from `_eval_condition` at evaluation
    # time, rather than a Pydantic validation error at construction time.
    op: str
    value: float | tuple[float, float]


class ScreeningRule(BaseModel):
    required: list[Condition] = Field(default_factory=list)
    excluded: list[Condition] = Field(default_factory=list)
    preferred: list[Condition] = Field(default_factory=list)


class ScreeningResult(BaseModel):
    passed: bool
    failed_conditions: list[str] = Field(default_factory=list)
    preferred_misses: list[str] = Field(default_factory=list)
    preferred_score: float = 0.0


def _get_metric(snap: ListingSnapshot, metric: str) -> Decimal | None:
    value = getattr(snap, metric, None)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return None


def _eval_condition(
    value: Decimal | None,
    op: str,
    target: float | tuple[float, float],
) -> bool:
    """Returns True iff condition passes. Missing value → False."""
    if value is None:
        return False
    v = float(value)
    if op == "between":
        if not isinstance(target, tuple):
            raise ValueError(f"'between' requires tuple target, got: {target!r}")
        lo, hi = target
        return float(lo) <= v <= float(hi)
    if isinstance(target, tuple):
        raise ValueError(f"op {op!r} requires scalar target, got tuple: {target!r}")
    t = float(target)
    if op == "<":
        return v < t
    if op == "<=":
        return v <= t
    if op == ">":
        return v > t
    if op == ">=":
        return v >= t
    if op == "==":
        return v == t
    raise ValueError(f"Unknown op: {op}")


def evaluate_rule(rule: ScreeningRule, snap: ListingSnapshot) -> ScreeningResult:
    failed: list[str] = []
    misses: list[str] = []

    for cond in rule.required:
        v = _get_metric(snap, cond.metric)
        if not _eval_condition(v, cond.op, cond.value):
            failed.append(f"{cond.metric} {cond.op} {cond.value} (actual: {v})")

    for cond in rule.excluded:
        v = _get_metric(snap, cond.metric)
        if _eval_condition(v, cond.op, cond.value):
            failed.append(
                f"excluded:{cond.metric} {cond.op} {cond.value} (actual: {v})"
            )

    pref_passes = 0
    for cond in rule.preferred:
        v = _get_metric(snap, cond.metric)
        if _eval_condition(v, cond.op, cond.value):
            pref_passes += 1
        else:
            misses.append(f"{cond.metric} {cond.op} {cond.value}")

    pref_score = pref_passes / len(rule.preferred) if rule.preferred else 0.0

    return ScreeningResult(
        passed=len(failed) == 0,
        failed_conditions=failed,
        preferred_misses=misses,
        preferred_score=pref_score,
    )
