"""Shared rule rendering for the review surfaces (watchlist, candidates, holdings).

A rule instance stores only its parameter overrides; zone conditions live in
the stored ``definition.zones`` and fall back to the template's zone set for
the instance's scope. This module is the one place that renders a rule record
the way every review page needs it - effective parameters (template defaults
merged with the overrides), the zones with resolved thresholds, the allowed
ranges, the factor units of the zone conditions and the calibration status - so
the watchlist, the candidate page and the holding rule panel cannot drift
apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.lifecycle import rule_params
from investment_agent.rules.store import RuleStore
from investment_agent.rules.validation import effective_params
from investment_agent.storage.models.rules import RuleInstanceRecord


async def rule_block(
    session: AsyncSession,
    record: RuleInstanceRecord,
    *,
    include_definition: bool,
    store: RuleStore | None = None,
) -> dict[str, Any]:
    """One rule as the review UI needs it.

    ``params`` are the effective values (template defaults merged with the
    instance's overrides), so zone conditions render with resolved thresholds;
    ``overrides`` keeps just the instance's own adjustments.
    """

    store = store or RuleStore(session)
    template = await store.get_template(record.template_id, record.template_version)
    overrides = rule_params(record)
    effective = dict(effective_params(template)) if template is not None else {}
    block: dict[str, Any] = {
        "rule_instance_id": record.id,
        "version": record.version,
        "template_id": record.template_id,
        "template_version": record.template_version,
        "template_ref": f"{record.template_id}@{record.template_version}",
        "scope": record.scope,
        "status": record.status,
        "params": {**effective, **overrides},
        "overrides": overrides,
        "calibration_status": template.calibration_status if template is not None else None,
        "drafted_by": record.drafted_by,
    }
    if include_definition:
        definition: dict[str, Any] = dict(record.definition or {})
        raw_zones = definition.get("zones")
        zones: dict[str, Any] = dict(raw_zones) if isinstance(raw_zones, dict) else {}
        if not zones and template is not None:
            zones = {
                name: group.model_dump(mode="json", by_alias=True, exclude_none=True)
                for name, group in template.zones_for(record.scope).items()
            }
        block["definition"] = definition
        block["zones"] = zones
        if template is not None:
            block["param_ranges"] = {
                name: {"min": item.min, "max": item.max}
                for name, item in template.param_ranges.items()
            }
        # Factor units of the zone conditions, so the page renders thresholds
        # with the same units the template page uses.
        block["factor_units"] = zone_factor_units(zones)
    return block


def zone_factor_units(zones: Mapping[str, Any]) -> dict[str, str]:
    from investment_agent.factors.catalog import DEFAULT_REGISTRY
    from investment_agent.rules.zone_detection import POSITION_FACTORS

    units: dict[str, str] = {}
    # Position factors are not in the registry (they come from the valuation):
    # weights and cost gains are fractions, holding days are natural days.
    position_units = {
        "position_weight": "fraction",
        "gain_vs_cost": "fraction",
        "holding_days": "days",
    }

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        factor = node.get("factor")
        if isinstance(factor, str):
            if factor in DEFAULT_REGISTRY:
                unit = DEFAULT_REGISTRY.get(factor).unit
                if unit:
                    units[factor] = unit
            elif factor in POSITION_FACTORS:
                units[factor] = position_units[factor]
            return
        for key in ("all", "any"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    walk(child)

    for zone in zones.values():
        walk(zone)
    return units


__all__ = ["rule_block", "zone_factor_units"]
