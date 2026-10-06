"""P1-C1: rule schema, validator error classes, param resolution, canonical form."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from investment_agent.rules.schema import (
    ConditionGroup,
    RuleInstance,
    RuleTemplate,
    canonical_hash,
    canonical_json,
    to_yaml,
)
from investment_agent.rules.validation import (
    RuleValidationError,
    default_context,
    effective_params,
    factors_in,
    parse_instance,
    parse_template,
    resolve_value,
)
from tests.unit._rules_support import CONTEXT, instance_data, template_data


def _codes(data: dict[str, Any]) -> set[str]:
    with pytest.raises(RuleValidationError) as info:
        parse_template(data, context=CONTEXT)
    return info.value.codes


def _template() -> RuleTemplate:
    return parse_template(template_data(), context=CONTEXT)


def _instance_codes(data: dict[str, Any]) -> set[str]:
    with pytest.raises(RuleValidationError) as info:
        parse_instance(data, _template(), context=CONTEXT)
    return info.value.codes


def test_valid_template_and_instance_parse() -> None:
    template = _template()
    assert template.ref == "quality_test@1"
    assert template.screen.mode == "all"
    instance = parse_instance(instance_data(), template, context=CONTEXT)
    assert instance.status == "draft"
    assert instance.template_id_version() == ("quality_test", 1)


def test_schema_errors_are_typed() -> None:
    assert _codes(template_data(extra_key=1)) == {"schema"}
    assert _codes(template_data(screen={"all": [], "any": []})) == {"schema"}
    assert _codes(template_data(calibration_status="final")) == {"schema"}


def test_unknown_factor() -> None:
    data = template_data()
    data["screen"]["all"][0]["factor"] = "dividend_yield"
    assert _codes(data) == {"unknown_factor"}


def test_pending_factor_only_in_holding_zones() -> None:
    data = template_data()
    data["screen"]["all"].append({"factor": "holding_days", "op": ">", "value": 30})
    data["candidate_zones"]["drop"]["any"].append(
        {"factor": "gain_vs_cost", "op": ">", "value": 0.5}
    )
    with pytest.raises(RuleValidationError) as info:
        parse_template(data, context=CONTEXT)
    assert info.value.codes == {"pending_factor"}
    assert sorted(issue.path for issue in info.value.issues) == [
        "candidate_zones.drop.any[1]",
        "screen.all[3]",
    ]


def test_invalid_op_and_op_value_mismatch() -> None:
    data = template_data()
    data["screen"]["all"][0]["op"] = "~="
    assert _codes(data) == {"invalid_op"}
    for condition in (
        {"factor": "roe", "op": ">=", "value": [0.1, 0.2]},
        {"factor": "roe", "op": ">=", "value": True},
        {"factor": "roe", "op": "between", "value": 0.1},
        {"factor": "roe", "op": "between", "value": [0.1]},
        {"factor": "roe", "op": "between", "value": [0.3, 0.1]},
        {"factor": "roe", "op": "in", "value": []},
    ):
        data = template_data()
        data["screen"]["all"][0] = condition
        assert _codes(data) == {"op_value_mismatch"}, condition
    data = template_data()
    data["screen"]["all"][0] = {"factor": "is_st", "op": "==", "value": False}
    parse_template(data, context=CONTEXT)


def test_free_text_values() -> None:
    data = template_data()
    data["screen"]["all"][0]["value"] = "high"
    assert _codes(data) == {"free_text_value"}
    data = template_data()
    data["screen"]["all"][0] = {"factor": "roe", "op": "in", "value": ["a", 0.1]}
    assert _codes(data) == {"free_text_value"}


def test_param_errors() -> None:
    data = template_data()
    data["screen"]["all"][0]["value"] = "$missing_param"
    assert _codes(data) == {"param_undefined"}
    data = template_data()
    data["params"]["roe_min"] = 0.5
    assert _codes(data) == {"param_out_of_range"}
    data = template_data()
    del data["param_ranges"]["roe_min"]
    assert _codes(data) == {"param_without_range"}
    data = template_data()
    data["param_ranges"]["roe_min"] = {"min": 0.3, "max": 0.1}
    assert _codes(data) == {"param_range_invalid"}
    data = template_data()
    data["param_ranges"]["ghost"] = {"min": 0, "max": 1}
    assert _codes(data) == {"param_undefined"}


def test_zone_errors() -> None:
    data = template_data()
    data["holding_zones"]["sell"] = {"any": [{"factor": "roe", "op": "<", "value": 0.05}]}
    with pytest.raises(RuleValidationError) as info:
        parse_template(data, context=CONTEXT)
    assert info.value.codes == {"zone_not_allowed"}
    assert "no sell zone" in str(info.value)
    data = template_data()
    data["candidate_zones"]["add"] = data["candidate_zones"].pop("buy_zone")
    assert _codes(data) == {"zone_not_allowed", "zone_missing"}


def test_empty_group_and_nesting() -> None:
    data = template_data()
    data["candidate_zones"]["drop"] = {"any": []}
    assert _codes(data) == {"empty_group"}
    data = template_data()
    data["screen"]["all"].append(
        {"any": [{"all": [{"factor": "roe", "op": ">", "value": 0.1}]}]}
    )
    assert _codes(data) == {"nesting_too_deep"}


def test_industry_group_errors() -> None:
    assert _codes(template_data(applicable_groups=["consumer", "fintech"])) == {
        "unknown_industry_group"
    }
    assert _codes(template_data(applicable_groups=[])) == {"empty_applicable_groups"}


def test_errors_are_aggregated() -> None:
    data = template_data(applicable_groups=["fintech"])
    data["screen"]["all"][0]["factor"] = "nope"
    data["screen"]["all"][1]["value"] = "cheap"
    data["holding_zones"]["sell"] = {"any": []}
    codes = _codes(data)
    assert {"unknown_industry_group", "unknown_factor", "free_text_value"} <= codes
    assert "zone_not_allowed" in codes


def test_instance_errors() -> None:
    assert _instance_codes(instance_data(template_ref="quality_test@2")) == {
        "template_ref_mismatch"
    }
    assert _instance_codes(instance_data(symbol="600519")) == {"invalid_symbol"}
    assert _instance_codes(instance_data(params={"pe_pct_max": 0.9})) == {"param_out_of_range"}
    assert _instance_codes(instance_data(params={"new_knob": 0.1})) == {"param_undefined"}
    assert _instance_codes(
        instance_data(zones={"trim": {"any": [{"factor": "roe", "op": "<", "value": 0.1}]}})
    ) == {"zone_not_allowed"}
    assert _instance_codes(
        instance_data(
            zones={"drop": {"any": [{"factor": "gain_vs_cost", "op": ">", "value": 0.1}]}}
        )
    ) == {"pending_factor"}
    assert _instance_codes(instance_data(scope="screen")) == {"schema"}
    holding = instance_data(
        scope="holding",
        params={},
        zones={"trim": {"any": [{"factor": "gain_vs_cost", "op": ">=", "value": 0.5}]}},
    )
    parse_instance(holding, _template(), context=CONTEXT)


def test_param_resolution() -> None:
    template = _template()
    instance = parse_instance(instance_data(), template, context=CONTEXT)
    params = effective_params(template, instance)
    assert params == {"roe_min": 0.15, "pe_pct_max": 0.3, "weight_max": 0.08}
    assert effective_params(template)["pe_pct_max"] == 0.4
    assert resolve_value("$pe_pct_max", params) == 0.3
    assert resolve_value(["$roe_min", 1], params) == [0.15, 1.0]
    assert resolve_value(True, params) is True
    with pytest.raises(KeyError):
        resolve_value("$ghost", params)
    with pytest.raises(ValueError):
        resolve_value("cheap", params)
    assert factors_in(template.screen) == {
        "roe",
        "pe_percentile_5y",
        "debt_ratio",
        "gross_margin",
    }


def test_yaml_and_json_round_trip_and_hash_stability() -> None:
    template = _template()
    again = RuleTemplate.model_validate(yaml.safe_load(to_yaml(template)))
    assert again == template
    assert to_yaml(again) == to_yaml(template)
    assert canonical_hash(again) == canonical_hash(template)
    from_json = RuleTemplate.model_validate(json.loads(canonical_json(template)))
    assert canonical_json(from_json) == canonical_json(template)
    # Integer vs float spelling and key order do not change the hash.
    data = template_data()
    data["candidate_zones"]["drop"]["any"][0]["value"] = 0.08
    data["param_ranges"]["roe_min"] = {"max": 0.3, "min": 0.08}
    reordered = dict(reversed(list(data.items())))
    assert canonical_hash(parse_template(reordered, context=CONTEXT)) == canonical_hash(template)
    data = template_data()
    data["holding_zones"]["exit_review"]["any"][0]["value"] = 0
    other = template_data()
    other["holding_zones"]["exit_review"]["any"][0]["value"] = 0.0
    assert canonical_hash(parse_template(data, context=CONTEXT)) == canonical_hash(
        parse_template(other, context=CONTEXT)
    )
    # Version is lifecycle metadata; content changes move the hash.
    assert canonical_hash(template.model_copy(update={"version": 7})) == canonical_hash(template)
    changed = template_data()
    changed["params"]["roe_min"] = 0.16
    assert canonical_hash(parse_template(changed, context=CONTEXT)) != canonical_hash(template)
    assert len(canonical_hash(template)) == 64


def test_instance_hash_ignores_lifecycle() -> None:
    instance = RuleInstance.model_validate(instance_data())
    approved = instance.model_copy(update={"status": "active", "approved_by": "user", "version": 3})
    assert canonical_hash(approved) == canonical_hash(instance)
    assert RuleInstance.model_validate(yaml.safe_load(to_yaml(approved))) == approved


def test_condition_group_children() -> None:
    group = ConditionGroup.model_validate({"any": [{"factor": "roe", "op": ">", "value": 1}]})
    assert group.mode == "any"
    assert len(group.children) == 1


def test_default_context_uses_registry_and_groups() -> None:
    ctx = default_context()
    assert "pe_percentile_5y" in ctx.factor_names
    assert "position_weight" not in ctx.factor_names
    assert {"banking", "utility", "other"} <= ctx.industry_groups
