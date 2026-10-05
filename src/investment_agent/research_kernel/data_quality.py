from __future__ import annotations

from typing import Any

from investment_agent.research_kernel.models import is_missing, missing_fields

QUALITY_DIRECT = "direct"
QUALITY_DERIVED = "derived"
QUALITY_PROXY = "proxy"
QUALITY_MISSING = "missing"
QUALITY_STALE = "stale"
QUALITY_CONFLICT = "conflict"
QUALITY_MODEL_NOT_APPLICABLE = "model_not_applicable"

DEFAULT_CONFLICT_THRESHOLDS: dict[str, dict[str, float | str]] = {
    "pe": {"type": "relative", "value": 0.05},
    "pb": {"type": "relative", "value": 0.05},
    "roe": {"type": "relative", "value": 0.05},
    "market_cap": {"type": "relative", "value": 0.03},
    "revenue": {"type": "relative", "value": 0.03},
    "net_profit": {"type": "relative", "value": 0.03},
    "dividend_yield": {"type": "absolute", "value": 0.005},
}


def build_quality_flag(
    flag: str,
    *,
    field: str,
    message: str,
    provider: str | None = None,
    severity: str = "review",
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"field": field, "flag": flag, "message": message}
    if provider is not None:
        payload["provider"] = provider
    if severity is not None:
        payload["severity"] = severity
    if values is not None:
        payload["values"] = values
    return payload


def build_field_source(
    field: str,
    *,
    provider: str,
    source: str,
    quality: str,
    updated_at: str | None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "field": field,
        "provider": provider,
        "source": source,
        "quality": quality,
        "updated_at": updated_at,
        "note": note,
    }


def _quality_flag_key(flag: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (flag.get("field"), flag.get("flag"), flag.get("provider"))


def annotate_quality_fields(
    item: dict[str, Any],
    *,
    required_fields: list[str],
    provider: str,
    source: str,
    updated_at: str | None,
    field_quality: dict[str, str] | None = None,
    field_notes: dict[str, str] | None = None,
    non_applicable_fields: list[str] | None = None,
    stale_fields: list[str] | None = None,
) -> dict[str, Any]:
    result = dict(item)
    field_quality = field_quality or {}
    field_notes = field_notes or {}
    non_applicable = set(non_applicable_fields or [])
    stale_field_names = list(stale_fields or [])
    stale = set(stale_field_names)
    required_missing = missing_fields(result, required_fields)
    applicable_missing = [field for field in required_missing if field not in non_applicable]
    not_applicable = [field for field in required_missing if field in non_applicable]

    quality_flags_by_key: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for flag in result.get("quality_flags", []):
        quality_flags_by_key[_quality_flag_key(flag)] = flag
    for field in applicable_missing:
        flag = build_quality_flag(
            "missing_field",
            field=field,
            message=f"{field} is missing",
            provider=provider,
        )
        quality_flags_by_key[_quality_flag_key(flag)] = flag
    for field in not_applicable:
        flag = build_quality_flag(
            "model_not_applicable",
            field=field,
            message=f"{field} is not applicable for this model",
            provider=provider,
        )
        quality_flags_by_key[_quality_flag_key(flag)] = flag
    stale_source_fields = sorted(stale.difference(applicable_missing).difference(not_applicable))
    for field in stale_source_fields:
        flag = build_quality_flag(
            "stale_data",
            field=field,
            message=field_notes.get(field, f"{field} is stale"),
            provider=provider,
        )
        quality_flags_by_key[_quality_flag_key(flag)] = flag
    quality_flags = list(quality_flags_by_key.values())

    field_sources = dict(result.get("field_sources", {}))
    tracked_fields = list(
        dict.fromkeys(
            [*required_fields, *field_quality.keys(), *field_notes.keys(), *stale_field_names]
        ).keys()
    )
    for field in tracked_fields:
        if field in applicable_missing:
            quality = QUALITY_MISSING
        elif field in not_applicable:
            quality = QUALITY_MODEL_NOT_APPLICABLE
        elif field in stale:
            quality = QUALITY_STALE
        else:
            quality = field_quality.get(field, QUALITY_DIRECT)
        field_sources[field] = build_field_source(
            field,
            provider=provider,
            source=source,
            quality=quality,
            updated_at=updated_at,
            note=field_notes.get(field, ""),
        )

    result["missing_fields"] = applicable_missing
    result["quality_flags"] = quality_flags
    result["field_sources"] = field_sources
    result["needs_agent_review"] = bool(
        applicable_missing
        or not_applicable
        or stale_source_fields
        or result.get("needs_agent_review")
    )
    return result


def _as_number(value: Any) -> float | None:
    if is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_conflict(
    selected_value: float,
    peer_value: float,
    threshold: dict[str, float | str],
) -> bool:
    diff = abs(selected_value - peer_value)
    if threshold["type"] == "absolute":
        return diff > float(threshold["value"])
    denominator = max(abs(selected_value), 1e-12)
    return diff / denominator > float(threshold["value"])


def compare_provider_values(
    *,
    selected_provider: str,
    comparisons: dict[str, dict[str, Any]],
    thresholds: dict[str, dict[str, float | str]] | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or DEFAULT_CONFLICT_THRESHOLDS
    quality_flags: list[dict[str, Any]] = []
    warnings: list[str] = []
    provider_conflicts: dict[str, dict[str, Any]] = {}

    for field, values_by_provider in comparisons.items():
        threshold = thresholds.get(field)
        if threshold is None:
            continue

        selected_value = _as_number(values_by_provider.get(selected_provider))
        if selected_value is None:
            continue

        has_conflict = False
        for provider, value in values_by_provider.items():
            if provider == selected_provider:
                continue
            peer_value = _as_number(value)
            if peer_value is None:
                continue
            if _is_conflict(selected_value, peer_value, threshold):
                has_conflict = True
                break

        if not has_conflict:
            continue
        provider_conflicts[field] = {
            "field": field,
            "selected_provider": selected_provider,
            "values": dict(values_by_provider),
            "threshold": dict(threshold),
        }
        quality_flags.append(
            build_quality_flag(
                "provider_conflict",
                field=field,
                provider=selected_provider,
                message=f"provider conflict for {field}",
                values=dict(values_by_provider),
            )
        )
        warnings.append(f"provider conflict for {field}: {values_by_provider}")

    return {
        "quality_flags": quality_flags,
        "warnings": warnings,
        "provider_conflicts": provider_conflicts,
    }
