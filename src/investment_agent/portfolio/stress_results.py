"""S1 result assembly over frozen copies; no storage writes or live lookups."""

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Literal
from uuid import UUID

from investment_agent.evidence.contracts import CalculationAssumption, CalculationRecord
from investment_agent.investor.common import Contract, ObjectRef, Quantity, utc_timestamp
from investment_agent.investor.contracts import Mandate, PortfolioConstraints
from investment_agent.investor.enums import CalculationStatus
from investment_agent.portfolio.stress import PathStressResult, amount_precision, calculate_path
from investment_agent.portfolio.stress_canonical import canonical_hash
from investment_agent.portfolio.stress_contracts import (
    FrozenPath,
    FrozenStressInputs,
    StressShock,
    StressStudyRequest,
    StressThresholds,
)


class ConstraintResult(Contract):
    key: str
    source: Literal["mandate", "user_threshold"]
    status: Literal["pass", "fail", "unknown", "not_configured"]
    limit: Quantity | None
    observed: Quantity
    reason: str | None = None


class StressResultRow(Contract):
    path_index: int
    shock_index: int
    arithmetic: PathStressResult
    constraints: tuple[ConstraintResult, ...]
    # Pure valuation never promises liquidity, executable fills or probability.
    liquidity_assessed: Literal[False] = False


def _quantity(value: Decimal | None, unit: str, reason: str | None = None) -> Quantity:
    return Quantity(value=value, unit=unit, currency="CNY" if unit == "CNY" else None,
                    unknown_reason=reason or "incomplete_inputs" if value is None else None)


def _ratio(numerator: Decimal | None, nav: Quantity) -> Quantity:
    if numerator is None or nav.value is None:
        return _quantity(None, "ratio", nav.unknown_reason)
    if nav.value <= 0:
        return _quantity(None, "ratio", "nonpositive_nav")
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        return _quantity(numerator / nav.value, "ratio")


def _check(
    key: str, source: Literal["mandate", "user_threshold"], limit: Decimal | None,
    observed: Quantity, *, minimum: bool = False,
) -> ConstraintResult:
    if limit is None:
        return ConstraintResult(key=key, source=source, status="not_configured", limit=None,
                                observed=observed, reason="not_configured")
    status: Literal["pass", "fail", "unknown"]
    if observed.value is None:
        status, reason = "unknown", observed.unknown_reason
    else:
        passed = observed.value >= limit if minimum else observed.value <= limit
        status, reason = ("pass", None) if passed else ("fail", "limit_exceeded")
    return ConstraintResult(key=key, source=source, status=status,
                            limit=_quantity(limit, observed.unit), observed=observed, reason=reason)


def _sector_weight(
    path: FrozenPath, result: PathStressResult, inputs: FrozenStressInputs, shock: StressShock,
) -> Quantity:
    if result.nav_after.value is None:
        return _quantity(None, "ratio", result.nav_after.unknown_reason)
    if result.nav_after.value <= 0:
        return _quantity(None, "ratio", "nonpositive_nav")
    copies = path.industries if path.industries is not None else inputs.industries if path.source == "actual" else ()
    industries = {item.listing_ref: item.code for item in copies}
    groups: dict[str, Decimal] = {}
    with localcontext(Context(prec=amount_precision(path, shock), rounding=ROUND_HALF_EVEN)):
        for holding in result.holdings:
            if holding.value_after.value == 0:
                continue
            code = industries.get(holding.listing_ref)
            if code is None:
                return _quantity(None, "ratio", "industry_missing")
            if holding.value_after.value is None:
                return _quantity(None, "ratio", holding.value_after.unknown_reason)
            groups[code] = groups.get(code, Decimal(0)) + holding.value_after.value
    return _ratio(max(groups.values(), default=Decimal(0)), result.nav_after)


def evaluate_constraints(
    path: FrozenPath, result: PathStressResult, inputs: FrozenStressInputs,
    shock: StressShock, *, constraints: PortfolioConstraints | None,
    thresholds: StressThresholds,
) -> tuple[ConstraintResult, ...]:
    """Each configured constraint retains its own missing-data outcome."""
    if constraints is not None and constraints.account_id != inputs.account_id:
        raise ValueError("Mandate constraints belong to another account")
    single = (_quantity(None, "ratio", result.nav_after.unknown_reason)
              if result.nav_after.value is None else
              _quantity(None, "ratio", "nonpositive_nav") if result.nav_after.value <= 0 else
              _quantity(max((item.weight_after.value for item in result.holdings
                             if item.weight_after.value is not None), default=Decimal(0)), "ratio"))
    cash = (_quantity(None, "CNY", "asset_scope_partial") if path.coverage != "complete"
            else _quantity(path.cash, "CNY", path.cash_unknown_reason))
    cash_weight = _ratio(cash.value, result.nav_after)
    configured = constraints.model_dump() if constraints is not None else {}
    return (
        _check("max_single_weight", "mandate", configured.get("max_single_weight"), single),
        _check("max_sector_weight", "mandate", configured.get("max_sector_weight"),
               _sector_weight(path, result, inputs, shock)),
        _check("minimum_cash", "mandate", configured.get("minimum_cash"), cash, minimum=True),
        _check("minimum_cash_weight", "mandate", configured.get("minimum_cash_weight"), cash_weight, minimum=True),
        _check("maximum_loss_cny", "user_threshold", thresholds.maximum_loss_cny, result.total_loss),
        _check("maximum_loss_ratio", "user_threshold", thresholds.maximum_loss_ratio, result.loss_ratio),
    )


def _validate_basis(request: StressStudyRequest, inputs: FrozenStressInputs, mandate: Mandate | None) -> None:
    if (request.account_id, request.snapshot_ref, request.valuation_ref) != (
        inputs.account_id, inputs.snapshot_ref, inputs.valuation_ref,
    ):
        raise ValueError("Frozen inputs differ from the requested account basis")
    if tuple(path.alternative for path in inputs.paths[1:]) != request.alternatives:
        raise ValueError("Frozen alternatives differ from the requested exact sources")
    if (mandate is None) != (inputs.mandate_ref is None):
        raise ValueError("Frozen mandate payload is required exactly when referenced")
    if mandate is not None:
        ref = ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision)
        if ref != inputs.mandate_ref or not any(
            version.ref == ref and version.payload_hash == canonical_hash(mandate)
            for version in inputs.versions
        ):
            raise ValueError("Mandate payload differs from its frozen version hash")
        if mandate.portfolio_constraints is not None and mandate.portfolio_constraints.account_id != inputs.account_id:
            raise ValueError("Mandate constraints belong to another account")


def stress_input_hash(
    request: StressStudyRequest, inputs: FrozenStressInputs, *, mandate: Mandate | None = None,
    industry_manifest: dict[str, Any] | None = None,
    input_diagnostics: tuple[dict[str, Any], ...] = (),
) -> str:
    _validate_basis(request, inputs, mandate)
    return canonical_hash({
        "request": request.model_dump(mode="python", exclude={"operation_id"}),
        "inputs": inputs, "mandate": mandate, "industry_manifest": industry_manifest,
        "input_diagnostics": input_diagnostics,
    })


def _assumptions(shocks: tuple[StressShock, ...]) -> tuple[CalculationAssumption, ...]:
    values: list[CalculationAssumption] = []
    for index, shock in enumerate(shocks):
        if shock.global_shock is not None:
            values.append(CalculationAssumption(name=f"shock_{index}_global", value=_quantity(shock.global_shock, "ratio"), provenance="user_scenario"))
        for item in shock.industries:
            values.append(CalculationAssumption(name=f"shock_{index}_csrc_{item.code}", value=_quantity(item.shock, "ratio"), provenance="user_scenario"))
        for target in shock.listings:
            values.append(CalculationAssumption(name=f"shock_{index}_listing_{target.listing_ref.id}", value=_quantity(target.shock, "ratio"), provenance="user_scenario", source_ref=target.listing_ref))
    return tuple(values)


def assemble_stress_result(
    request: StressStudyRequest, inputs: FrozenStressInputs, *, result_id: UUID,
    created_at: datetime, mandate: Mandate | None = None,
    industry_manifest: dict[str, Any] | None = None,
    input_diagnostics: tuple[dict[str, Any], ...] = (),
    expected_input_hash: str | None = None,
) -> CalculationRecord:
    """Build an immutable result for the preallocated ID; the caller saves it."""
    digest = stress_input_hash(request, inputs, mandate=mandate, industry_manifest=industry_manifest,
                               input_diagnostics=input_diagnostics)
    if expected_input_hash is not None and expected_input_hash != digest:
        raise ValueError("Frozen stress input hash changed")
    timestamp = utc_timestamp(created_at)
    if timestamp < inputs.knowledge_cutoff:
        raise ValueError("Result cannot predate its frozen knowledge boundary")
    rows: list[StressResultRow] = []
    quality: list[str] = []
    any_usable = False
    results: dict[str, Quantity] = {}
    constraints = mandate.portfolio_constraints if mandate is not None else None
    for path_index, path in enumerate(inputs.paths):
        for shock_index, shock in enumerate(request.shocks):
            arithmetic = calculate_path(path, shock, inputs.industries)
            checks = evaluate_constraints(path, arithmetic, inputs, shock,
                                           constraints=constraints, thresholds=request.thresholds)
            rows.append(StressResultRow(path_index=path_index, shock_index=shock_index,
                                        arithmetic=arithmetic, constraints=checks))
            quality.extend(arithmetic.quality)
            quality.extend(f"{check.key}:{check.reason}" for check in checks if check.status == "unknown")
            any_usable |= any(row.value_before.value is not None for row in arithmetic.holdings) or path.cash is not None
            for metric in ("nav_before", "nav_after", "total_loss", "loss_ratio",
                           "known_securities_before", "known_securities_after", "known_securities_loss"):
                results[f"p{path_index}_s{shock_index}_{metric}"] = getattr(arithmetic, metric)
    # Do not serialize successful quantities in the non-ok status branch.
    status = CalculationStatus.OK if any_usable else CalculationStatus.MISSING_INPUTS
    details = {
        "schema_version": 1, "quality_state": "partial" if quality or not any_usable else "complete",
        "request": request.model_dump(mode="json"), "frozen_inputs": inputs.model_dump(mode="json"),
        "mandate": mandate.model_dump(mode="json") if mandate else None,
        "industry_manifest": industry_manifest, "input_diagnostics": input_diagnostics,
        "rows": [row.model_dump(mode="json") for row in rows],
        "liquidity_assessed": False,
        "price_diagnostics": [price.model_dump(mode="json") for price in inputs.prices],
        "limitations": ["static_price_scenario", "liquidity_not_assessed", "no_execution_or_probability"],
    }
    return CalculationRecord(
        id=result_id, revision=1, created_at=timestamp, actor="portfolio_stress_calculator",
        origin_ref=inputs.snapshot_ref, method="portfolio_stress", method_version=inputs.method_version,
        fact_refs=tuple(dict.fromkeys((*[version.ref for version in inputs.versions],
                                      *[price.price_ref for price in inputs.prices]))),
        assumption_refs=(), assumptions=_assumptions(request.shocks), input_hash=digest,
        as_of=inputs.knowledge_cutoff, applicable=any_usable, status=status,
        results=results if any_usable else {}, quality=tuple(dict.fromkeys(quality)),
        errors=() if any_usable else ("missing_usable_inputs",), details=details,
    )
