"""Deterministic, structured comparison of consecutive research scores."""

from dataclasses import dataclass
from typing import TypeGuard

CHANGE_SCHEMA_VERSION = "research-change.v1"

_SCORE_FIELDS: dict[str, float] = {
    "business_score": 1.0,
    "investment_score": 1.0,
    "research_confidence_score": 1.0,
}
_STRATEGY_FIELDS = (
    "company_profile.sector_type",
    "primary_strategy",
    "secondary_strategy",
    "strategy_type",
    "selected_model",
    "suggestion",
    "valuation_level",
    "risk_level",
    "needs_agent_review",
)
_QUALITY_FIELDS = (
    "missing_fields",
    "provider_conflicts",
    "quality_flags",
    "warnings",
)


@dataclass(frozen=True)
class ResearchDiff:
    schema_version: str
    baseline: bool
    previous_run_id: int | None
    current_run_id: int
    has_meaningful_changes: bool
    change_count: int
    meaningful_change_count: int
    category_counts: dict[str, int]
    changes: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "baseline": self.baseline,
            "previous_run_id": self.previous_run_id,
            "current_run_id": self.current_run_id,
            "has_meaningful_changes": self.has_meaningful_changes,
            "change_count": self.change_count,
            "meaningful_change_count": self.meaningful_change_count,
            "category_counts": self.category_counts,
            "changes": self.changes,
        }


def build_research_diff(
    *,
    current_run_id: int,
    current_score: dict[str, object],
    previous_run_id: int | None,
    previous_score: dict[str, object] | None,
) -> ResearchDiff:
    """Compare stable research fields while ignoring timestamps and display text."""

    if previous_score is None:
        return ResearchDiff(
            schema_version=CHANGE_SCHEMA_VERSION,
            baseline=True,
            previous_run_id=None,
            current_run_id=current_run_id,
            has_meaningful_changes=False,
            change_count=0,
            meaningful_change_count=0,
            category_counts={},
            changes=[],
        )

    changes: list[dict[str, object]] = []
    for field, threshold in _SCORE_FIELDS.items():
        _append_change(
            changes,
            category="score",
            field=field,
            before=_path_value(previous_score, field),
            after=_path_value(current_score, field),
            absolute_threshold=threshold,
        )

    factor_keys = _mapping_keys(previous_score, current_score, "factor_scores")
    for key in factor_keys:
        field = f"factor_scores.{key}"
        _append_change(
            changes,
            category="score",
            field=field,
            before=_path_value(previous_score, field),
            after=_path_value(current_score, field),
            absolute_threshold=1.0,
        )

    for field in _STRATEGY_FIELDS:
        _append_change(
            changes,
            category="strategy",
            field=field,
            before=_path_value(previous_score, field),
            after=_path_value(current_score, field),
        )
    for field in ("classification_confidence", "strategy_confidence"):
        _append_change(
            changes,
            category="strategy",
            field=field,
            before=_path_value(previous_score, field),
            after=_path_value(current_score, field),
            absolute_threshold=0.01,
        )

    metric_keys = _mapping_keys(previous_score, current_score, "key_metrics")
    for key in metric_keys:
        field = f"key_metrics.{key}"
        _append_change(
            changes,
            category="financial_metric",
            field=field,
            before=_path_value(previous_score, field),
            after=_path_value(current_score, field),
            relative_threshold_pct=1.0,
        )

    _append_change(
        changes,
        category="quality",
        field="data_quality_score",
        before=_path_value(previous_score, "data_quality_score"),
        after=_path_value(current_score, "data_quality_score"),
        absolute_threshold=0.01,
    )
    for field in _QUALITY_FIELDS:
        _append_change(
            changes,
            category="quality",
            field=field,
            before=_normalized_quality_value(field, previous_score.get(field)),
            after=_normalized_quality_value(field, current_score.get(field)),
        )
    freshness_keys = _mapping_keys(previous_score, current_score, "freshness")
    for key in freshness_keys:
        field = f"freshness.{key}.is_stale"
        _append_change(
            changes,
            category="quality",
            field=field,
            before=_path_value(previous_score, field),
            after=_path_value(current_score, field),
        )

    category_counts: dict[str, int] = {}
    for change in changes:
        category = str(change["category"])
        category_counts[category] = category_counts.get(category, 0) + 1
    meaningful_count = sum(change["meaningful"] is True for change in changes)
    return ResearchDiff(
        schema_version=CHANGE_SCHEMA_VERSION,
        baseline=False,
        previous_run_id=previous_run_id,
        current_run_id=current_run_id,
        has_meaningful_changes=meaningful_count > 0,
        change_count=len(changes),
        meaningful_change_count=meaningful_count,
        category_counts=category_counts,
        changes=changes,
    )


def _append_change(
    changes: list[dict[str, object]],
    *,
    category: str,
    field: str,
    before: object,
    after: object,
    absolute_threshold: float | None = None,
    relative_threshold_pct: float | None = None,
) -> None:
    if before == after:
        return
    delta: float | None = None
    relative_change_pct: float | None = None
    if _is_number(before) and _is_number(after):
        before_number = float(before)
        after_number = float(after)
        delta = round(after_number - before_number, 8)
        if before_number != 0:
            relative_change_pct = round(delta / abs(before_number) * 100, 6)

    meaningful = True
    if delta is not None and absolute_threshold is not None:
        meaningful = abs(delta) >= absolute_threshold
    elif delta is not None and relative_threshold_pct is not None:
        meaningful = (
            abs(relative_change_pct) >= relative_threshold_pct
            if relative_change_pct is not None
            else delta != 0
        )

    changes.append(
        {
            "category": category,
            "field": field,
            "before": before,
            "after": after,
            "delta": delta,
            "relative_change_pct": relative_change_pct,
            "meaningful": meaningful,
        }
    )


def _path_value(payload: dict[str, object], path: str) -> object:
    value: object = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _mapping_keys(
    previous: dict[str, object],
    current: dict[str, object],
    field: str,
) -> list[str]:
    keys: set[str] = set()
    for payload in (previous, current):
        value = payload.get(field)
        if isinstance(value, dict):
            keys.update(str(key) for key in value)
    return sorted(keys)


def _normalized_quality_value(field: str, value: object) -> object:
    if field in {"missing_fields", "warnings"} and isinstance(value, list):
        return sorted(str(item) for item in value)
    if field == "quality_flags" and isinstance(value, list):
        normalized: list[dict[str, object]] = []
        for item in value:
            if isinstance(item, dict):
                normalized.append(
                    {
                        key: item.get(key)
                        for key in ("field", "flag", "severity", "provider")
                    }
                )
        return sorted(
            normalized,
            key=lambda item: tuple(str(item[key]) for key in item),
        )
    return value


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)
