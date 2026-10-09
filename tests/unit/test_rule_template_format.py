"""Template format v3: authoring metadata and the ``{default, range}`` param form."""

from __future__ import annotations

import pytest

from investment_agent.rules.schema import RuleTemplate, canonical_definition, canonical_hash
from investment_agent.rules.templates import load_template_file, load_templates, template_files
from investment_agent.rules.validation import RuleValidationError, parse_template
from tests.unit._rules_support import CONTEXT, documented_template_data, template_data


def test_param_specs_split_into_the_flat_maps() -> None:
    template = parse_template(documented_template_data(), context=CONTEXT, require_docs=True)
    assert template.params["roe_min"] == 0.15
    assert template.param_ranges["roe_min"].min == 0.08
    assert template.param_ranges["roe_min"].max == 0.3
    assert template.param_meta is not None
    assert template.param_meta["roe_min"].label == "roe_min 标签"
    stored = canonical_definition(template, include_lifecycle=False)
    # The stored form keeps the flat maps, and reloads unchanged.
    assert stored["params"]["roe_min"] == 0.15
    assert canonical_hash(RuleTemplate.model_validate(stored)) == canonical_hash(template)


def test_legacy_template_hash_is_unchanged_by_the_new_optional_fields() -> None:
    legacy = parse_template(template_data(), context=CONTEXT)
    data = canonical_definition(legacy, include_lifecycle=False)
    for key in ("thesis", "fits", "param_meta", "examples", "label"):
        assert key not in str(sorted(data))
    assert "label" not in str(data["screen"])


def test_mixed_param_forms_are_rejected() -> None:
    data = documented_template_data()
    data["params"]["roe_min"] = 0.15
    with pytest.raises(RuleValidationError) as caught:
        parse_template(data, context=CONTEXT)
    assert "schema" in caught.value.codes


def test_ranges_beside_the_spec_form_are_rejected() -> None:
    data = documented_template_data(param_ranges={"roe_min": {"min": 0.1, "max": 0.2}})
    with pytest.raises(RuleValidationError) as caught:
        parse_template(data, context=CONTEXT)
    assert "schema" in caught.value.codes


def test_spec_default_outside_its_range() -> None:
    data = documented_template_data()
    data["params"]["roe_min"]["default"] = 0.5
    with pytest.raises(RuleValidationError) as caught:
        parse_template(data, context=CONTEXT)
    assert "param_out_of_range" in caught.value.codes


def test_authoring_lint_only_with_require_docs() -> None:
    parse_template(template_data(), context=CONTEXT)  # runtime: lenient
    with pytest.raises(RuleValidationError) as caught:
        parse_template(template_data(), context=CONTEXT, require_docs=True)
    paths = {issue.path for issue in caught.value.issues}
    assert {"thesis", "falsification", "params.roe_min", "screen.all[0]"} <= paths
    assert "screen.all[2].any[0]" in paths  # nested conditions too
    assert "holding_zones.exit_review.any[0]" in paths


def test_param_without_label_is_flagged() -> None:
    data = documented_template_data()
    del data["params"]["roe_min"]["label"]
    with pytest.raises(RuleValidationError) as caught:
        parse_template(data, context=CONTEXT, require_docs=True)
    assert [issue.path for issue in caught.value.issues] == ["params.roe_min"]


def test_example_ticker_must_be_canonical() -> None:
    data = documented_template_data(examples=[{"ticker": "600519"}])
    with pytest.raises(RuleValidationError) as caught:
        parse_template(data, context=CONTEXT)
    assert "invalid_symbol" in caught.value.codes


def test_config_templates_pass_the_authoring_lint() -> None:
    loaded = load_templates()
    assert len(loaded) == len(template_files())
    for path, _ in loaded:
        template = load_template_file(path, require_docs=True)
        assert template.thesis and template.falsification
