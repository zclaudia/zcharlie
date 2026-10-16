"""P2-only adaptation of immutable FactorValue results to exact decimals."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from investment_agent.factors.registry import FLAG_INDUSTRY_FALLBACK
from investment_agent.supplemental_rules.contracts import FrozenSubject, MetricLeaf

LeafStatus = Literal["true", "false", "unknown", "not_applicable"]


@dataclass(frozen=True)
class AdaptedFactor:
    status: LeafStatus
    value: Decimal | None
    uncertainty: Decimal | None
    unit: str | None
    factor_version: str | None
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    inputs_ref: dict[str, Any]


def _decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        result = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _contains_conflict(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("verification") == "conflict" or value.get("status") == "conflict":
            return True
        if value.get("conflicts"):
            return True
        return any(_contains_conflict(child) for child in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_conflict(child) for child in value)
    return False


def adapt_factor(subject: FrozenSubject, leaf: MetricLeaf) -> AdaptedFactor:
    raw = subject.factor_values.get(leaf.factor_id)
    metadata_rows = subject.selector_inputs.get("factor_metadata", [])
    metadata = {
        item.get("name"): item
        for item in metadata_rows
        if isinstance(item, dict)
    } if isinstance(metadata_rows, list) else {}
    raw_meta = metadata.get(leaf.factor_id)
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    if not isinstance(raw, dict):
        return AdaptedFactor("unknown", None, None, meta.get("unit"), None, (),
                             ("factor_value_missing",), {})
    flags = tuple(str(flag) for flag in raw.get("flags", ()))
    raw_refs = raw.get("inputs_ref")
    refs: dict[str, Any] = raw_refs if isinstance(raw_refs, dict) else {}
    quality = raw.get("quality")
    version = raw.get("version")
    unit = meta.get("unit")
    reasons: list[str] = []
    # P2's current factor registry exposes only fixed, versioned computations;
    # it has no parameter binding contract. Silently accepting leaf.params here
    # would evaluate (for example) two thresholds over the same default value
    # while presenting them as distinct parameterized factors.
    if leaf.params:
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("factor_parameters_unsupported",), refs)
    # These are P2 diagnostics. Do not extend or mutate the old FactorValue
    # quality enum, whose values and golden samples are a P1 contract.
    if "source_coverage_unknown" in flags:
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("factor_source_coverage_unknown",), refs)
    if ("conflicted" in flags or refs.get("conflicted") or refs.get("conflict")
            or _contains_conflict(refs)):
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("factor_source_conflicted",), refs)
    if ("stale" in flags or "stale_price" in flags or "dividends_stale" in flags
            or refs.get("stale") or refs.get("source_status") == "stale"):
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("factor_source_stale",), refs)
    if quality == "not_applicable":
        if FLAG_INDUSTRY_FALLBACK in flags:
            return AdaptedFactor("unknown", None, None, unit, version, flags,
                                 ("applicability_uses_industry_fallback",), refs)
        return AdaptedFactor("not_applicable", None, None, unit, version, flags,
                             ("factor_not_applicable",), refs)
    if quality == "missing":
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("factor_missing",), refs)
    if quality == "approx":
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("approx_factor_not_accepted_by_default",), refs)
    if quality != "ok":
        return AdaptedFactor("unknown", None, None, unit, version, flags,
                             ("factor_quality_unrecognized",), refs)
    if unit is None:
        reasons.append("factor_unit_unavailable")
    elif leaf.unit is not None and leaf.unit != unit:
        reasons.append("factor_unit_mismatch")
    value = _decimal(raw.get("value"))
    if value is None:
        reasons.append("factor_numeric_value_invalid")
    if reasons:
        return AdaptedFactor("unknown", value, None, unit, version, flags, tuple(reasons), refs)
    # FactorValue.value is a float by contract. Preserve its decimal rendering,
    # but carry the binary-to-decimal conversion error as an uncertainty band.
    source = raw.get("value")
    uncertainty = Decimal(0)
    if isinstance(source, float):
        uncertainty = abs(Decimal.from_float(source) - Decimal(str(source)))
    return AdaptedFactor("true", value, uncertainty, unit, version, flags, (), refs)
